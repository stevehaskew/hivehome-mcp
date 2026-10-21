"""Hive credentials in the OS keychain (one JSON item), with env var overrides.

Stored item (service = Settings.keychain_service, account = "credentials"):
    {"username", "password", "device_data": [group_key, key, password] | null,
     "tokens": {"token", "refreshToken", "accessToken"} | null,
     "device_name", "saved_at"}

HIVE_USERNAME / HIVE_PASSWORD override the stored username/password, for
headless use. Device data and tokens only ever come from the keychain.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from typing import Any, NamedTuple

import keyring
import keyring.errors

_LOGGER = logging.getLogger(__name__)
_ACCOUNT = "credentials"


class DeviceData(NamedTuple):
    group_key: str
    key: str
    password: str


@dataclass(frozen=True)
class Tokens:
    token: str
    refreshToken: str  # noqa: N815 - matches the library's key names
    accessToken: str  # noqa: N815

    @classmethod
    def from_auth_result(cls, result: dict[str, Any]) -> Tokens:
        ar = result["AuthenticationResult"]
        return cls(
            token=ar["IdToken"], refreshToken=ar["RefreshToken"], accessToken=ar["AccessToken"]
        )

    def as_session_config(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class Credentials:
    username: str
    password: str
    device_data: DeviceData | None = None
    tokens: Tokens | None = None
    device_name: str | None = None
    saved_at: str | None = None


class CredentialsMissing(RuntimeError):
    """No usable credentials; the user needs to run `hive-mcp login`."""


class CredentialStore:
    def __init__(self, service: str) -> None:
        self.service = service

    def _read_raw(self) -> dict[str, Any] | None:
        raw = keyring.get_password(self.service, _ACCOUNT)
        if not raw:
            return None
        try:
            data: dict[str, Any] = json.loads(raw)
        except json.JSONDecodeError:
            _LOGGER.error("Keychain item %s is not valid JSON; ignoring it.", self.service)
            return None
        return data

    def load(self) -> Credentials | None:
        """Return stored credentials with env overrides applied, or None."""
        data = self._read_raw() or {}
        username = os.environ.get("HIVE_USERNAME") or data.get("username")
        password = os.environ.get("HIVE_PASSWORD") or data.get("password")
        if not username or not password:
            return None
        device = data.get("device_data")
        tokens = data.get("tokens")
        return Credentials(
            username=username,
            password=password,
            device_data=DeviceData(*device) if device else None,
            tokens=Tokens(**tokens) if tokens else None,
            device_name=data.get("device_name"),
            saved_at=data.get("saved_at"),
        )

    def require(self) -> Credentials:
        creds = self.load()
        if creds is None or creds.device_data is None:
            raise CredentialsMissing("No Hive credentials stored. Run `hive-mcp login`.")
        return creds

    def save(self, creds: Credentials) -> Credentials:
        stamped = replace(creds, saved_at=datetime.now().isoformat(timespec="seconds"))
        payload = {
            "username": stamped.username,
            "password": stamped.password,
            "device_data": list(stamped.device_data) if stamped.device_data else None,
            "tokens": asdict(stamped.tokens) if stamped.tokens else None,
            "device_name": stamped.device_name,
            "saved_at": stamped.saved_at,
        }
        keyring.set_password(self.service, _ACCOUNT, json.dumps(payload))
        return stamped

    def update_tokens(self, tokens: Tokens) -> None:
        """Persist new tokens, keeping everything else as stored (not env overrides)."""
        data = self._read_raw()
        if data is None:
            _LOGGER.warning("Token update skipped: nothing stored in the keychain.")
            return
        data["tokens"] = asdict(tokens)
        data["saved_at"] = datetime.now().isoformat(timespec="seconds")
        keyring.set_password(self.service, _ACCOUNT, json.dumps(data))
        _LOGGER.info("Saved refreshed Hive tokens to the keychain.")

    def clear(self) -> bool:
        try:
            keyring.delete_password(self.service, _ACCOUNT)
        except keyring.errors.PasswordDeleteError:
            return False
        return True
