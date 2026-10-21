from __future__ import annotations

import json

import pytest

from hive_mcp.credentials import (
    Credentials,
    CredentialsMissing,
    CredentialStore,
    DeviceData,
    Tokens,
)

from .conftest import MemoryKeyring

TOKENS = Tokens(token="id", refreshToken="refresh", accessToken="access")
CREDS = Credentials(
    username="me@example.com",
    password="pw",
    device_data=DeviceData("g", "k", "p"),
    tokens=TOKENS,
    device_name="test",
)


def test_round_trip(memory_keyring: MemoryKeyring) -> None:
    store = CredentialStore("svc")
    saved = store.save(CREDS)
    loaded = store.load()
    assert loaded == saved
    assert loaded is not None and loaded.saved_at is not None
    assert list(memory_keyring.items) == [("svc", "credentials")]


def test_nothing_stored(memory_keyring: MemoryKeyring) -> None:
    store = CredentialStore("svc")
    assert store.load() is None
    with pytest.raises(CredentialsMissing):
        store.require()
    assert store.clear() is False


def test_env_overrides_username_and_password(
    memory_keyring: MemoryKeyring, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = CredentialStore("svc")
    store.save(CREDS)
    monkeypatch.setenv("HIVE_USERNAME", "other@example.com")
    monkeypatch.setenv("HIVE_PASSWORD", "env-pw")
    loaded = store.require()
    assert (loaded.username, loaded.password) == ("other@example.com", "env-pw")
    assert loaded.device_data == CREDS.device_data


def test_update_tokens_keeps_stored_values_not_env(
    memory_keyring: MemoryKeyring, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = CredentialStore("svc")
    store.save(CREDS)
    monkeypatch.setenv("HIVE_PASSWORD", "env-pw")
    store.update_tokens(Tokens(token="id2", refreshToken="refresh2", accessToken="access2"))
    raw = json.loads(memory_keyring.items[("svc", "credentials")])
    assert raw["password"] == "pw"
    assert raw["tokens"]["refreshToken"] == "refresh2"


def test_clear(memory_keyring: MemoryKeyring) -> None:
    store = CredentialStore("svc")
    store.save(CREDS)
    assert store.clear() is True
    assert store.load() is None
