from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

import pytest

from hive_mcp._hivelib import hive_exceptions
from hive_mcp.auth import (
    LoginFailed,
    ManagedHive,
    ReauthRequired,
    device_login,
    interactive_login,
)
from hive_mcp.credentials import Credentials, DeviceData, Tokens

from .conftest import FakeAuth, auth_result

DEVICE = DeviceData("group", "dev-key-123456", "device-secret")
STORED = Tokens(token="id-stored", refreshToken="refresh-stored", accessToken="access-stored")


def creds(tokens: Tokens | None = STORED) -> Credentials:
    return Credentials("me@example.com", "pw", device_data=DEVICE, tokens=tokens)


# --- device_login --------------------------------------------------------------


async def test_device_login_via_device_srp(fake_auth: type[FakeAuth]) -> None:
    result = await device_login("me@example.com", "pw", DEVICE)
    assert result == auth_result("relogin")
    assert fake_auth.instances[0].args == ("me@example.com", "pw", *DEVICE)


async def test_device_login_direct_tokens(fake_auth: type[FakeAuth]) -> None:
    fake_auth.login_result = auth_result("direct")
    assert await device_login("me@example.com", "pw", DEVICE) == auth_result("direct")


@pytest.mark.parametrize(
    ("login", "device"),
    [
        ({"ChallengeName": "SMS_MFA"}, None),
        ({"ChallengeName": "DEVICE_SRP_AUTH"}, {"ChallengeName": "SMS_MFA"}),
        ({"ChallengeName": "DEVICE_SRP_AUTH"}, None),
    ],
)
async def test_device_login_never_prompts(
    fake_auth: type[FakeAuth], login: dict[str, Any], device: dict[str, Any] | None
) -> None:
    fake_auth.login_result = login
    fake_auth.device_login_result = device
    with pytest.raises(ReauthRequired):
        await device_login("me@example.com", "pw", DEVICE)


# --- interactive_login -----------------------------------------------------------


async def test_interactive_login_without_sms(fake_auth: type[FakeAuth]) -> None:
    fake_auth.login_result = auth_result("first")
    outcome = await interactive_login("me@example.com", "pw", "my mac", lambda: "unused")
    assert not outcome.sms_used
    assert outcome.tokens.refreshToken == "refresh-first"
    assert outcome.device_data == DEVICE
    assert fake_auth.instances[0].registered_as == "my mac"


async def test_interactive_login_with_sms(fake_auth: type[FakeAuth]) -> None:
    fake_auth.login_result = {"ChallengeName": "SMS_MFA", "Session": "s"}
    fake_auth.sms_result = auth_result("sms")
    outcome = await interactive_login("me@example.com", "pw", "my mac", lambda: " 123456 ")
    assert outcome.sms_used
    assert fake_auth.instances[0].sms_code == "123456"
    assert outcome.tokens.accessToken == "access-sms"


async def test_interactive_login_without_device_keys(
    fake_auth: type[FakeAuth], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_auth.login_result = auth_result("first")

    class NoDeviceAuth(FakeAuth):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self.device_key = None

    monkeypatch.setattr("hive_mcp.auth.Auth", NoDeviceAuth)
    with pytest.raises(LoginFailed, match="device keys"):
        await interactive_login("me@example.com", "pw", "my mac", lambda: "")


# --- ManagedHive -------------------------------------------------------------------


async def test_login_override_updates_and_persists_tokens(fake_auth: type[FakeAuth]) -> None:
    saved: list[Tokens] = []
    hive = ManagedHive(creds(), on_new_tokens=saved.append)
    result = await hive.login()
    assert result == auth_result("relogin")
    assert hive.tokens.tokenData["refreshToken"] == "refresh-relogin"
    assert hive.relogin_count == 1
    assert [t.refreshToken for t in saved] == ["refresh-relogin"]


async def test_loading_stored_tokens_does_not_persist(fake_auth: type[FakeAuth]) -> None:
    saved: list[Tokens] = []
    hive = ManagedHive(creds(), on_new_tokens=saved.append)
    await hive.updateTokens(STORED.as_session_config(), False)
    assert saved == []


async def test_access_token_refresh_does_not_persist(fake_auth: type[FakeAuth]) -> None:
    saved: list[Tokens] = []
    hive = ManagedHive(creds(), on_new_tokens=saved.append)
    await hive.updateTokens(STORED.as_session_config(), False)
    await hive.updateTokens(auth_result("hourly", refresh=False))
    assert hive.tokens.tokenData["accessToken"] == "access-hourly"
    assert saved == []


async def test_login_override_maps_reauth(fake_auth: type[FakeAuth]) -> None:
    fake_auth.login_result = {"ChallengeName": "SMS_MFA"}
    hive = ManagedHive(creds())
    with pytest.raises(hive_exceptions.HiveReauthRequired):
        await hive.login()


class _ExpiringRefreshAuth:
    """Replaces the library's session Auth: first refresh fails, later ones work."""

    SMS_MFA_CHALLENGE = "SMS_MFA"

    def __init__(self) -> None:
        self.refresh_calls: list[str] = []

    async def refresh_token(self, token: str) -> dict[str, Any]:
        self.refresh_calls.append(token)
        if token == "refresh-stored":
            raise hive_exceptions.HiveRefreshTokenExpired
        return auth_result("after-refresh", refresh=False)


async def test_expired_refresh_token_recovers_through_library_fallback(
    fake_auth: type[FakeAuth],
) -> None:
    """Library refresh -> _retryLogin -> our login(), without the lock deadlock."""
    saved: list[Tokens] = []
    hive = ManagedHive(creds(), on_new_tokens=saved.append)
    await hive.updateTokens(STORED.as_session_config(), False)
    hive.tokens.tokenCreated = datetime.min
    session_auth = _ExpiringRefreshAuth()
    hive.auth = session_auth

    # The unpatched library deadlocks here, so fail fast rather than hang.
    await asyncio.wait_for(hive.hiveRefreshTokens(), timeout=5)

    assert session_auth.refresh_calls == ["refresh-stored"]
    assert hive.relogin_count == 1
    assert hive.tokens.tokenData["accessToken"] == "access-relogin"
    assert [t.refreshToken for t in saved] == ["refresh-relogin"]
    await hive.close()


async def test_retry_login_retries_network_errors_then_gives_up(
    fake_auth: type[FakeAuth], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0

    async def flaky_login() -> dict[str, Any]:
        nonlocal calls
        calls += 1
        raise hive_exceptions.HiveApiError

    async def no_sleep(_s: float) -> None:
        return None

    hive = ManagedHive(creds())
    monkeypatch.setattr(hive, "login", flaky_login)
    monkeypatch.setattr("hive_mcp.auth.asyncio.sleep", no_sleep)
    with pytest.raises(hive_exceptions.HiveReauthRequired):
        await hive._retryLogin()
    assert calls == 3
    await hive.close()


def test_managed_hive_needs_device_data() -> None:
    with pytest.raises(ValueError):
        ManagedHive(Credentials("me@example.com", "pw"))
