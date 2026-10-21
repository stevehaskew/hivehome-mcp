"""Shared fixtures: an in-memory keyring and a scriptable fake Cognito Auth."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import keyring
import pytest
from keyring.backend import KeyringBackend

from hive_mcp import auth as auth_module


class MemoryKeyring(KeyringBackend):
    priority = 1

    def __init__(self) -> None:
        super().__init__()  # type: ignore[no-untyped-call]
        self.items: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.items.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.items[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        if (service, username) not in self.items:
            raise keyring.errors.PasswordDeleteError(username)
        del self.items[(service, username)]


@pytest.fixture
def memory_keyring(monkeypatch: pytest.MonkeyPatch) -> Iterator[MemoryKeyring]:
    backend = MemoryKeyring()
    previous = keyring.get_keyring()
    keyring.set_keyring(backend)
    monkeypatch.delenv("HIVE_USERNAME", raising=False)
    monkeypatch.delenv("HIVE_PASSWORD", raising=False)
    yield backend
    keyring.set_keyring(previous)


def auth_result(suffix: str, *, refresh: bool = True) -> dict[str, Any]:
    ar: dict[str, Any] = {
        "IdToken": f"id-{suffix}",
        "AccessToken": f"access-{suffix}",
        "ExpiresIn": 3600,
    }
    if refresh:
        ar["RefreshToken"] = f"refresh-{suffix}"
    return {"AuthenticationResult": ar}


class FakeAuth:
    """Stands in for apyhiveapi's HiveAuthAsync inside hive_mcp.auth."""

    SMS_MFA_CHALLENGE = "SMS_MFA"
    DEVICE_VERIFIER_CHALLENGE = "DEVICE_SRP_AUTH"

    login_result: dict[str, Any] | None = None
    device_login_result: dict[str, Any] | None = None
    sms_result: dict[str, Any] | None = None
    instances: list[FakeAuth] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.args = args
        self.device_key: str | None = "dev-key-123456"
        self.registered_as: str | None = None
        self.sms_code: str | None = None
        FakeAuth.instances.append(self)

    async def login(self) -> dict[str, Any] | None:
        return FakeAuth.login_result

    async def device_login(self) -> dict[str, Any] | None:
        return FakeAuth.device_login_result

    async def sms_2fa(self, code: str, _challenge: dict[str, Any]) -> dict[str, Any] | None:
        self.sms_code = code
        return FakeAuth.sms_result

    async def device_registration(self, name: str) -> None:
        self.registered_as = name

    async def get_device_data(self) -> tuple[str, str, str]:
        return ("group", self.device_key or "", "device-secret")


@pytest.fixture
def fake_auth(monkeypatch: pytest.MonkeyPatch) -> type[FakeAuth]:
    FakeAuth.login_result = {"ChallengeName": "DEVICE_SRP_AUTH"}
    FakeAuth.device_login_result = auth_result("relogin")
    FakeAuth.sms_result = None
    FakeAuth.instances = []
    monkeypatch.setattr(auth_module, "Auth", FakeAuth)
    return FakeAuth
