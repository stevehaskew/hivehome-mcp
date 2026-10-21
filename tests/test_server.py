"""The MCP tools end to end, through FastMCP's in-memory client and the fixture."""

from __future__ import annotations

import dataclasses
import logging
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client

from hive_mcp.auth import ReauthRequired
from hive_mcp.config import Settings
from hive_mcp.fake_client import FakeHiveClient
from hive_mcp.hive_client import HiveClient, HiveUnavailable
from hive_mcp.server import create_server
from hive_mcp.snapshot import LONDON, HiveSnapshot

FIXTURE = Path(__file__).parent / "fixtures/hive_sample.json"
NOW = datetime(2026, 10, 21, 22, 15, tzinfo=LONDON)


class FixedClient(FakeHiveClient):
    """Fixture data, pinned to the time the spike read it."""

    stale = False
    closed = False

    async def snapshot(self, *, refresh: bool = False) -> HiveSnapshot:
        snap = await super().snapshot()
        return dataclasses.replace(snap, fetched_at=NOW, stale=self.stale)

    async def close(self) -> None:
        FixedClient.closed = True


class FailingClient:
    def __init__(self, error: Exception) -> None:
        self.error = error

    async def snapshot(self, *, refresh: bool = False) -> HiveSnapshot:
        raise self.error

    async def write(
        self, product_type: str, product_id: str, kwargs: dict[str, Any]
    ) -> HiveSnapshot:
        raise self.error

    async def close(self) -> None:
        return None


def settings() -> Settings:
    return Settings(
        keychain_service="unused",
        log_dir=Path("/nonexistent"),
        log_level="INFO",
        fake=True,
        fake_fixture=FIXTURE,
        refresh_interval=timedelta(seconds=120),
        allow_writes=False,
    )


@pytest.fixture
async def client() -> AsyncIterator[Client[Any]]:
    FixedClient.stale = False
    server = create_server(settings(), lambda _s: FixedClient(FIXTURE))
    async with Client(server) as c:
        yield c


async def call(c: Client[Any], tool: str, **args: Any) -> dict[str, Any]:
    result = await c.call_tool(tool, args)
    assert not result.is_error
    data: dict[str, Any] = result.structured_content or {}
    return data


# --- tool catalogue ------------------------------------------------------------------


async def test_only_read_only_tools_are_exposed(client: Client[Any]) -> None:
    tools = {t.name: t for t in await client.list_tools()}
    assert set(tools) == {
        "list_devices",
        "get_heating_status",
        "get_hot_water_status",
        "get_schedule",
    }
    for tool in tools.values():
        assert tool.annotations is not None and tool.annotations.read_only_hint is True
        assert tool.description


async def test_client_closed_on_shutdown() -> None:
    FixedClient.closed = False
    async with Client(create_server(settings(), lambda _s: FixedClient(FIXTURE))) as c:
        await c.list_tools()
    assert FixedClient.closed


# --- list_devices ----------------------------------------------------------------


async def test_list_devices(client: Client[Any]) -> None:
    data = await call(client, "list_devices")
    assert data["as_of"] == "Wed 21/10/2026 22:15"
    assert data["stale"] is False
    names = [d["name"] for d in data["devices"]]
    assert names[0] == "Creche" and names[-1] == "Hot water"
    downstairs = next(d for d in data["devices"] if d["name"] == "Downstairs")
    assert downstairs["available"] is False
    assert "MALFORMED_NODE_DESCRIPTOR" in downstairs["problem"]
    creche = next(d for d in data["devices"] if d["name"] == "Creche")
    assert (creche["battery_pct"], creche["low_battery"]) == (20, True)
    controllers = {d["name"]: d["controller"] for d in data["devices"] if d["controller"]}
    assert controllers == {"Hot water": "Thermostat 3"}  # zones' own thermostats are left out


# --- get_heating_status ------------------------------------------------------------


async def test_all_zones_by_default(client: Client[Any]) -> None:
    data = await call(client, "get_heating_status")
    assert len(data["zones"]) == 8


async def test_one_zone_by_name(client: Client[Any]) -> None:
    data = await call(client, "get_heating_status", zone="creche")
    [zone] = data["zones"]
    assert zone["name"] == "Creche"
    assert (zone["current_temp_c"], zone["target_temp_c"], zone["mode"]) == (16.2, 12.0, "SCHEDULE")
    assert zone["heating_now"] is False
    assert [s["period"] for s in zone["schedule"]] == ["now", "next", "later"]
    assert zone["schedule"][0]["starts"] == "Wed 21/10/2026 13:00"
    assert zone["schedule"][0]["ends"] == "Thu 22/10/2026 02:00"
    assert zone["summary"] == (
        "Creche is 16.2°C, target 12°C, following its schedule, not calling for heat. "
        "Next: 10°C from tomorrow 02:00. Battery low (20%)."
    )
    assert zone["schedule"][2]["summary"] == "12°C from tomorrow 12:00 until Fri 02:00"
    assert "min_temp_c" not in zone


async def test_zone_off_summary(client: Client[Any]) -> None:
    [zone] = (await call(client, "get_heating_status", zone="Office A"))["zones"]
    assert zone["schedule"] == []
    assert zone["summary"] == (
        "Office A is 16.9°C, heating off (frost protection at 7°C), not calling for heat."
    )


async def test_unavailable_zone(client: Client[Any]) -> None:
    [zone] = (await call(client, "get_heating_status", zone="Downstairs"))["zones"]
    assert zone["available"] is False
    assert zone["current_temp_c"] is None
    assert zone["summary"].startswith("Downstairs is unavailable: boiler module and thermostat")


async def test_unknown_zone_lists_choices(
    client: Client[Any], caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG, logger="fastmcp"):
        result = await client.call_tool(
            "get_heating_status", {"zone": "Kitchen"}, raise_on_error=False
        )
    assert result.is_error
    text = result.content[0].text  # type: ignore[union-attr]
    assert "No heating zone called 'Kitchen'" in text and "Main Hall" in text
    # An expected mistake, not a server fault: kept off stderr (which shows ERROR).
    [record] = [r for r in caplog.records if r.getMessage().startswith("Error calling tool")]
    assert record.levelno == logging.INFO


# --- get_hot_water_status ------------------------------------------------------------


async def test_hot_water(client: Client[Any]) -> None:
    data = await call(client, "get_hot_water_status")
    assert (data["name"], data["mode"], data["is_on"]) == ("Hot water", "SCHEDULE", False)
    assert data["summary"] == (
        "Hot water is off, following its schedule. Next: hot water on from tomorrow 01:00."
    )
    assert data["as_of"] == "Wed 21/10/2026 22:15"


async def test_stale_flag_is_passed_through(client: Client[Any]) -> None:
    FixedClient.stale = True
    assert (await call(client, "list_devices"))["stale"] is True


# --- get_schedule ------------------------------------------------------------------


async def test_schedule_for_zone_switched_off(client: Client[Any]) -> None:
    data = await call(client, "get_schedule", zone="thermostat 3")
    assert (data["name"], data["mode"], data["in_force"]) == ("Thermostat 3", "OFF", False)
    assert data["summary"] == (
        "Weekly schedule for Thermostat 3. It isn't in use: Thermostat 3 is switched off."
    )
    assert [d["day"] for d in data["days"]][:2] == ["Monday", "Tuesday"]
    tuesday = data["days"][1]
    assert tuesday["summary"] == "00:00–10:00 7°C, 10:00–14:45 19°C, 14:45–24:00 7°C"
    assert tuesday["slots"][-1] == {
        "start": "14:45",
        "end": "24:00",
        "target_c": 7.0,
        "hot_water_on": None,
    }


async def test_schedule_for_hot_water(client: Client[Any]) -> None:
    data = await call(client, "get_schedule", zone="Hot water")
    assert (data["type"], data["in_force"]) == ("hot_water", True)
    assert data["days"][0]["summary"].startswith("00:00–01:00 off, 01:00–12:30 on")


async def test_schedule_all_day_and_unknown_zone(client: Client[Any]) -> None:
    data = await call(client, "get_schedule", zone="Main Hall")
    assert data["days"][0]["summary"] == "10°C all day"
    result = await client.call_tool("get_schedule", {"zone": "Kitchen"}, raise_on_error=False)
    assert result.is_error
    assert "Hot water" in result.content[0].text  # type: ignore[union-attr]


# --- failures ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (ReauthRequired("device no longer trusted"), "uv run hive-mcp login"),
        (HiveUnavailable("down"), "Couldn't reach Hive"),
        (RuntimeError("internal detail"), "Error calling tool"),
    ],
)
async def test_failures_become_readable_errors(error: Exception, expected: str) -> None:
    def factory(_s: Settings) -> HiveClient:
        return FailingClient(error)

    async with Client(create_server(settings(), factory)) as c:
        result = await c.call_tool("list_devices", {}, raise_on_error=False)
    assert result.is_error
    text = result.content[0].text
    assert expected in text
    assert "internal detail" not in text
