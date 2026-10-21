from __future__ import annotations

import re
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from hive_mcp import hive_client
from hive_mcp._hivelib import hive_exceptions
from hive_mcp.auth import ReauthRequired
from hive_mcp.config import Settings
from hive_mcp.credentials import Credentials, CredentialsMissing, CredentialStore, DeviceData
from hive_mcp.fake_client import FakeHiveClient
from hive_mcp.hive_client import HiveUnavailable, LiveHiveClient, WriteFailed, make_client

from .conftest import MemoryKeyring

FIXTURE = Path(__file__).parent / "fixtures/hive_sample.json"


async def test_fake_client_serves_fixture_copies() -> None:
    client = FakeHiveClient(FIXTURE)
    first = await client.snapshot()
    first.products["id-11"]["state"]["target"] = 99
    second = await client.snapshot()
    assert second.products["id-11"]["state"]["target"] == 10
    assert "id-22" in second.devices
    assert second.fetched_at.tzinfo is not None


def test_make_client_honours_fake_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HIVE_MCP_FAKE", "1")
    monkeypatch.setenv("HIVE_MCP_FAKE_FIXTURE", str(FIXTURE))
    assert isinstance(make_client(Settings.from_env()), FakeHiveClient)
    monkeypatch.delenv("HIVE_MCP_FAKE")
    assert isinstance(make_client(Settings.from_env()), LiveHiveClient)


def test_default_fixture_path_exists(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HIVE_MCP_FAKE_FIXTURE", raising=False)
    assert Settings.from_env().fake_fixture == FIXTURE.resolve()


def test_refresh_interval_has_a_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HIVE_MCP_REFRESH_SECONDS", "5")
    assert Settings.from_env().refresh_interval == timedelta(seconds=60)


# --- LiveHiveClient with a stand-in ManagedHive ---------------------------------------


class _Data:
    def __init__(self) -> None:
        self.products: dict[str, Any] = {"p": {"state": {"target": 10}}}
        self.devices: dict[str, Any] = {"d": {}}


class _Api:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.result: dict[str, Any] | BaseException = {"original": 200, "parsed": {}}

    async def setState(self, n_type: str, n_id: str, **kwargs: Any) -> dict[str, Any]:  # noqa: N802
        self.calls.append((n_type, n_id, kwargs))
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


class StubHive:
    """Mimics the parts of ManagedHive that LiveHiveClient uses."""

    instances: list[StubHive] = []
    start_error: BaseException | None = None

    def __init__(self, creds: Credentials, on_new_tokens: Any = None) -> None:
        self.creds = creds
        self.data = _Data()
        self.refreshes = 0
        self.refresh_result: bool | BaseException = True
        self.closed = False
        self.api = _Api()
        self.token_checks = 0
        StubHive.instances.append(self)

    async def hiveRefreshTokens(self) -> None:  # noqa: N802
        self.token_checks += 1

    async def start(self) -> dict[str, Any]:
        if StubHive.start_error:
            raise StubHive.start_error
        return {}

    async def getDevices(self, _n_id: str) -> bool:  # noqa: N802
        self.refreshes += 1
        if isinstance(self.refresh_result, BaseException):
            raise self.refresh_result
        self.data.products["p"]["state"]["target"] += 1
        return self.refresh_result

    async def close(self) -> None:
        self.closed = True


@pytest.fixture
def live(memory_keyring: MemoryKeyring, monkeypatch: pytest.MonkeyPatch) -> LiveHiveClient:
    StubHive.instances, StubHive.start_error = [], None
    monkeypatch.setattr(hive_client, "ManagedHive", StubHive)
    store = CredentialStore("svc")
    store.save(Credentials("me@example.com", "pw", device_data=DeviceData("g", "k", "p")))
    return LiveHiveClient(store, timedelta(seconds=120))


def _age(client: LiveHiveClient, seconds: int) -> None:
    last = client._last
    assert last is not None
    client._last = type(last)(
        last.products, last.devices, last.fetched_at - timedelta(seconds=seconds)
    )


async def test_live_starts_lazily_and_caches(live: LiveHiveClient) -> None:
    assert StubHive.instances == []
    first = await live.snapshot()
    second = await live.snapshot()
    hive = StubHive.instances[0]
    assert hive.refreshes == 0
    assert first is second
    assert first.products["p"]["state"]["target"] == 10


async def test_live_refreshes_after_interval(live: LiveHiveClient) -> None:
    await live.snapshot()
    _age(live, 121)
    snap = await live.snapshot()
    assert StubHive.instances[0].refreshes == 1
    assert snap.products["p"]["state"]["target"] == 11
    assert not snap.stale


async def test_live_serves_stale_data_when_refresh_fails(live: LiveHiveClient) -> None:
    await live.snapshot()
    StubHive.instances[0].refresh_result = False
    _age(live, 121)
    snap = await live.snapshot()
    assert snap.stale
    assert snap.products["p"]["state"]["target"] == 10


async def test_live_maps_reauth(live: LiveHiveClient) -> None:
    await live.snapshot()
    StubHive.instances[0].refresh_result = hive_exceptions.HiveReauthRequired()
    _age(live, 121)
    with pytest.raises(ReauthRequired):
        await live.snapshot()


async def test_live_start_failure_closes_session(live: LiveHiveClient) -> None:
    StubHive.start_error = ReauthRequired("sign in again")
    with pytest.raises(ReauthRequired):
        await live.snapshot()
    assert StubHive.instances[0].closed
    StubHive.start_error = None
    await live.snapshot()  # retries with a fresh session next time
    assert len(StubHive.instances) == 2


async def test_live_without_credentials(memory_keyring: MemoryKeyring) -> None:
    client = LiveHiveClient(CredentialStore("empty"), timedelta(seconds=120))
    with pytest.raises(CredentialsMissing):
        await client.snapshot()


async def test_live_close(live: LiveHiveClient) -> None:
    await live.snapshot()
    await live.close()
    assert StubHive.instances[0].closed


def test_unavailable_is_an_error() -> None:
    assert issubclass(HiveUnavailable, RuntimeError)


async def test_live_refresh_skips_the_cache(live: LiveHiveClient) -> None:
    await live.snapshot()
    snap = await live.snapshot(refresh=True)
    assert StubHive.instances[0].refreshes == 1
    assert snap.products["p"]["state"]["target"] == 11


async def test_live_write_sends_set_state_then_refreshes(live: LiveHiveClient) -> None:
    await live.snapshot()
    snap = await live.write("heating", "p", {"target": 12})
    hive = StubHive.instances[0]
    assert hive.api.calls == [("heating", "p", {"target": 12})]
    assert hive.token_checks == 1
    assert hive.refreshes == 1  # forced, although the cache was fresh
    assert snap.products["p"]["state"]["target"] == 11


@pytest.mark.parametrize(
    ("result", "message"),
    [
        ({"original": 500, "parsed": {}}, "HTTP 500"),
        ({}, "HTTP None"),  # the library returns {} after some errors
        (OSError("boom"), "Couldn't reach Hive (OSError)"),
        (KeyError("original"), "Couldn't reach Hive (KeyError)"),
    ],
)
async def test_live_write_failures(live: LiveHiveClient, result: Any, message: str) -> None:
    await live.snapshot()
    StubHive.instances[0].api.result = result
    with pytest.raises(WriteFailed, match=re.escape(message)):
        await live.write("heating", "p", {"target": 12})
    assert StubHive.instances[0].refreshes == 0


async def test_live_write_auth_error_needs_sign_in(live: LiveHiveClient) -> None:
    await live.snapshot()
    StubHive.instances[0].api.result = hive_exceptions.HiveAuthError("401")
    with pytest.raises(ReauthRequired):
        await live.write("heating", "p", {"target": 12})


async def test_fake_client_records_and_applies_writes() -> None:
    client = FakeHiveClient(FIXTURE)
    snap = await client.write("heating", "id-16", {"mode": "BOOST", "boost": 30, "target": 20})
    state = snap.products["id-16"]["state"]
    assert (state["mode"], state["boost"], state["target"]) == ("BOOST", 30, 20)
    assert snap.products["id-16"]["props"]["previous"] == {"mode": "OFF", "target": 7}
    snap = await client.write("heating", "id-16", {"mode": "OFF", "target": 7})
    assert "boost" not in snap.products["id-16"]["state"]
    assert client.writes[0] == ("heating", "id-16", {"mode": "BOOST", "boost": 30, "target": 20})
    client.fail_writes = "nope"
    with pytest.raises(WriteFailed, match="nope"):
        await client.write("heating", "id-16", {"target": 12})
