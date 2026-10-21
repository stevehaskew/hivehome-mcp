"""The write tools end to end: FastMCP client -> server -> FakeHiveClient -> audit log."""

from __future__ import annotations

import dataclasses
import json
import re
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client

from hive_mcp import writes
from hive_mcp.auth import ReauthRequired
from hive_mcp.config import Settings
from hive_mcp.server import create_server

from .test_server import FIXTURE, FailingClient, FixedClient, settings

WRITE_TOOLS = {
    "set_target_temperature",
    "set_heating_mode",
    "boost_heating",
    "cancel_heating_boost",
    "set_hot_water_mode",
    "boost_hot_water",
    "cancel_hot_water_boost",
}


def write_settings(log_dir: Path) -> Settings:
    return dataclasses.replace(settings(), allow_writes=True, log_dir=log_dir)


class Harness:
    def __init__(self, c: Client[Any], fake: FixedClient, log_dir: Path) -> None:
        self.c, self.fake, self.log_dir = c, fake, log_dir

    async def call(self, tool: str, **args: Any) -> dict[str, Any]:
        result = await self.c.call_tool(tool, args)
        assert not result.is_error
        return result.structured_content or {}

    async def fail(self, tool: str, **args: Any) -> str:
        result = await self.c.call_tool(tool, args, raise_on_error=False)
        assert result.is_error
        text: str = result.content[0].text  # type: ignore[union-attr]
        return text

    def audit(self) -> list[dict[str, Any]]:
        path = self.log_dir / "audit.log"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


@pytest.fixture
async def hive(tmp_path: Path) -> AsyncIterator[Harness]:
    FixedClient.stale = False
    fake = FixedClient(FIXTURE)
    async with Client(create_server(write_settings(tmp_path), lambda _s: fake)) as c:
        yield Harness(c, fake, tmp_path)


# --- registration ----------------------------------------------------------------------


async def test_write_tools_absent_when_writes_are_off(tmp_path: Path) -> None:
    off = dataclasses.replace(settings(), log_dir=tmp_path)
    server = create_server(off, lambda _s: FixedClient(FIXTURE))
    assert "read-only" in (server.instructions or "")
    async with Client(server) as c:
        names = {t.name for t in await c.list_tools()}
        assert names.isdisjoint(WRITE_TOOLS)
    assert not (tmp_path / "audit.log").exists()


async def test_write_tools_present_when_writes_are_on(hive: Harness) -> None:
    tools = {t.name: t for t in await hive.c.list_tools()}
    assert set(tools) >= WRITE_TOOLS
    for name in WRITE_TOOLS:
        ann = tools[name].annotations
        assert ann is not None and ann.read_only_hint is False and ann.destructive_hint is False
    assert tools["set_target_temperature"].annotations.idempotent_hint is True  # type: ignore[union-attr]
    assert tools["boost_heating"].annotations.idempotent_hint is False  # type: ignore[union-attr]


def test_instructions_mention_the_limits(tmp_path: Path) -> None:
    server = create_server(write_settings(tmp_path), lambda _s: FixedClient(FIXTURE))
    assert "real building" in (server.instructions or "")
    assert "5–25°C in 0.5°C steps" in (server.instructions or "")


async def test_no_write_tools_without_an_audit_log(tmp_path: Path) -> None:
    blocker = tmp_path / "not-a-folder"
    blocker.write_text("")
    s = write_settings(blocker)  # log_dir is a file, so the audit log can't be opened
    async with Client(create_server(s, lambda _s: FixedClient(FIXTURE))) as c:
        assert {t.name for t in await c.list_tools()}.isdisjoint(WRITE_TOOLS)


async def test_documented_limits_match_the_constants(hive: Harness) -> None:
    temps = f"{writes.MIN_TEMP_C:g}–{writes.MAX_TEMP_C:g}°C in {writes.TEMP_STEP_C:g}°C steps"
    mins = f"{writes.MIN_BOOST_MINUTES}–{writes.MAX_BOOST_MINUTES} minutes"
    tools = {
        t.name: (t.description or "") + json.dumps(t.input_schema, ensure_ascii=False)
        for t in await hive.c.list_tools()
    }
    for name in ("set_target_temperature", "set_heating_mode", "boost_heating"):
        assert temps in tools[name]
    for name in ("boost_heating", "boost_hot_water"):
        assert mins in tools[name]
    # No other ranges have crept in.
    for desc in tools.values():
        for found in re.findall(r"\d+(?:\.\d+)?–\d+(?:\.\d+)?", desc):
            assert found in (temps + mins)


# --- successful writes -------------------------------------------------------------------


async def test_set_heating_mode_manual_end_to_end(hive: Harness) -> None:
    out = await hive.call("set_heating_mode", zone="thermostat 3", mode="MANUAL", temp_c=12)
    assert hive.fake.writes == [("heating", "id-16", {"mode": "MANUAL", "target": 12})]
    assert out["changed"] is True and out["confirmed"] is True
    assert out["zone"] == "Thermostat 3"
    assert out["summary"] == (
        "Thermostat 3 changed from OFF to MANUAL at 12°C. "
        "It stays at this temperature until someone changes it."
    )
    assert "target 12°C, manual" in out["now"]

    [entry] = hive.audit()
    assert entry["tool"] == "set_heating_mode"
    assert (entry["zone"], entry["id"], entry["result"]) == ("Thermostat 3", "id-16", "ok")
    assert entry["before"] == {"mode": "OFF", "boost_minutes": None, "target_c": 7.0}
    assert entry["requested"] == {"mode": "MANUAL", "temp_c": 12}
    assert entry["sent"] == {"mode": "MANUAL", "target": 12}
    assert entry["after"] == {"mode": "MANUAL", "boost_minutes": None, "target_c": 12.0}
    assert re.fullmatch(r"\d\d/\d\d/\d{4} \d\d:\d\d:\d\d", entry["time"])


async def test_target_then_boost_then_cancel(hive: Harness) -> None:
    out = await hive.call("set_target_temperature", zone="Main Hall", temp_c=18)
    assert out["summary"].startswith("Main Hall target changed from 10°C to 18°C. This overrides")

    out = await hive.call("boost_heating", zone="Main Hall", minutes=30, temp_c=21)
    assert out["summary"] == (
        "Main Hall boosting to 21°C for 30 minutes. Afterwards it goes back to SCHEDULE."
    )
    assert "boosting to 21°C, 30 minutes left" in out["now"]

    out = await hive.call("cancel_heating_boost", zone="Main Hall")
    assert out["summary"] == "Main Hall boost cancelled, back to SCHEDULE."
    assert [w[2] for w in hive.fake.writes] == [
        {"target": 18},
        {"mode": "BOOST", "boost": 30, "target": 21},
        {"mode": "SCHEDULE"},
    ]
    assert [e["result"] for e in hive.audit()] == ["ok", "ok", "ok"]


async def test_hot_water_on_and_boost(hive: Harness) -> None:
    out = await hive.call("set_hot_water_mode", mode="ON")
    assert hive.fake.writes[-1] == ("hotwater", "id-18", {"mode": "MANUAL"})
    assert out["confirmed"] is True
    assert "set to always on" in out["now"]

    await hive.call("boost_hot_water", minutes=45)
    out = await hive.call("cancel_hot_water_boost")
    assert out["summary"] == "Hot water boost cancelled, back to ON."
    assert hive.fake.writes[-1][2] == {"mode": "MANUAL"}


async def test_no_change_sends_nothing_but_is_audited(hive: Harness) -> None:
    out = await hive.call("set_heating_mode", zone="Thermostat 3", mode="OFF")
    assert out["changed"] is False
    assert out["summary"] == "No change needed: Thermostat 3 is already set that way."
    out = await hive.call("cancel_heating_boost", zone="Main Hall")
    assert out["changed"] is False
    assert hive.fake.writes == []
    assert [e["result"] for e in hive.audit()] == ["no_change", "no_change"]


async def test_unconfirmed_when_the_refresh_does_not_show_the_change(hive: Harness) -> None:
    async def swallowed(product_type: str, product_id: str, kwargs: dict[str, Any]) -> Any:
        hive.fake.writes.append((product_type, product_id, kwargs))
        return await hive.fake.snapshot()  # Hive said 200 but nothing changed

    hive.fake.write = swallowed  # type: ignore[method-assign]
    out = await hive.call("set_target_temperature", zone="Main Hall", temp_c=18)
    assert out["confirmed"] is False
    assert "doesn't show it yet" in out["summary"]
    [entry] = hive.audit()
    assert entry["result"] == "unconfirmed"
    assert entry["reason"] == "target_c is 10.0, expected 18.0"


async def test_manual_then_off_restores_the_frost_target(hive: Harness) -> None:
    await hive.call("set_heating_mode", zone="Thermostat 3", mode="MANUAL", temp_c=12)
    out = await hive.call("set_heating_mode", zone="Thermostat 3", mode="OFF")
    assert hive.fake.writes[-1] == ("heating", "id-16", {"mode": "OFF", "target": 7})
    assert out["confirmed"] is True
    assert hive.audit()[-1]["after"] == {"mode": "OFF", "boost_minutes": None, "target_c": 7.0}


# --- refusals ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tool", "args", "message"),
    [
        ("set_target_temperature", {"zone": "Main Hall", "temp_c": 26}, "5–25°C"),
        ("set_target_temperature", {"zone": "Main Hall", "temp_c": 4}, "5–25°C"),
        ("set_target_temperature", {"zone": "Main Hall", "temp_c": 18.3}, "steps of 0.5°C"),
        ("set_heating_mode", {"zone": "Main Hall", "mode": "MANUAL", "temp_c": 30}, "5–25°C"),
        ("set_heating_mode", {"zone": "Main Hall", "mode": "MANUAL"}, "needs a temperature"),
        ("boost_heating", {"zone": "Main Hall", "minutes": 240, "temp_c": 20}, "15–120"),
        ("boost_heating", {"zone": "Main Hall", "minutes": 5, "temp_c": 20}, "15–120"),
        ("boost_heating", {"zone": "Main Hall", "minutes": 30, "temp_c": 28}, "5–25°C"),
        ("boost_hot_water", {"minutes": 180}, "15–120"),
        ("set_target_temperature", {"zone": "Thermostat 3", "temp_c": 18}, "switched off"),
        ("set_target_temperature", {"zone": "Downstairs", "temp_c": 18}, "unavailable"),
        ("boost_heating", {"zone": "Downstairs", "minutes": 30, "temp_c": 20}, "unavailable"),
        ("set_target_temperature", {"zone": "Attic", "temp_c": 18}, "No heating zone called"),
    ],
)
async def test_refused_writes_send_nothing_and_are_audited(
    hive: Harness, tool: str, args: dict[str, Any], message: str
) -> None:
    text = await hive.fail(tool, **args)
    assert message in text
    assert hive.fake.writes == []
    [entry] = hive.audit()
    assert entry["tool"] == tool and entry["result"] == "rejected"
    assert message in entry["reason"]
    assert entry["sent"] is None


async def test_already_boosting_is_refused(hive: Harness) -> None:
    await hive.call("boost_heating", zone="Upstairs", minutes=30, temp_c=20)
    text = await hive.fail("boost_heating", zone="Upstairs", minutes=60, temp_c=22)
    assert "already boosting (30 minutes left)" in text
    assert len(hive.fake.writes) == 1


async def test_stale_data_refuses_writes(hive: Harness) -> None:
    FixedClient.stale = True
    text = await hive.fail("set_target_temperature", zone="Main Hall", temp_c=18)
    assert "Couldn't reach Hive" in text
    assert hive.fake.writes == []
    assert hive.audit()[0]["result"] == "failed"


async def test_failed_write_is_audited(hive: Harness) -> None:
    hive.fake.fail_writes = "Hive didn't accept the change (HTTP 500)."
    text = await hive.fail("set_target_temperature", zone="Main Hall", temp_c=18)
    assert "HTTP 500" in text and "Nothing was changed" in text
    [entry] = hive.audit()
    assert entry["result"] == "failed"
    assert entry["sent"] == {"target": 18}
    assert entry["after"] is None


async def test_sign_in_needed_during_write(tmp_path: Path) -> None:
    factory = lambda _s: FailingClient(ReauthRequired("device forgotten"))  # noqa: E731
    async with Client(create_server(write_settings(tmp_path), factory)) as c:
        result = await c.call_tool("set_hot_water_mode", {"mode": "OFF"}, raise_on_error=False)
    assert result.is_error
    assert "uv run hive-mcp login" in result.content[0].text
    entry = json.loads((tmp_path / "audit.log").read_text())
    assert entry["result"] == "failed" and "device forgotten" in entry["reason"]
