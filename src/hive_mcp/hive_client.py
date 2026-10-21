"""Where Hive data comes from. Clients only fetch raw JSON; readings.py interprets it.

LiveHiveClient is the only client that touches apyhiveapi (through ManagedHive).
FakeHiveClient (fake_client.py) serves the same shape from a fixture file.
"""

from __future__ import annotations

import asyncio
import copy
import logging
from datetime import datetime, timedelta
from typing import Any, Protocol

from ._hivelib import hive_exceptions
from .auth import ManagedHive, ReauthRequired
from .config import Settings
from .credentials import CredentialStore
from .snapshot import LONDON, HiveSnapshot

_LOGGER = logging.getLogger(__name__)


class HiveUnavailable(RuntimeError):
    """Hive couldn't be reached and there's no earlier data to fall back on."""


class WriteFailed(RuntimeError):
    """Hive didn't accept a write, or couldn't be reached to send it."""


class HiveClient(Protocol):
    async def snapshot(self, *, refresh: bool = False) -> HiveSnapshot:
        """The latest data; `refresh` skips the cache (used just before a write)."""
        ...

    async def write(
        self, product_type: str, product_id: str, kwargs: dict[str, Any]
    ) -> HiveSnapshot:
        """Send one setState, then return a freshly fetched snapshot."""
        ...

    async def close(self) -> None: ...


class LiveHiveClient:
    """Lazily signs in, then refreshes from Hive at most once per refresh_interval."""

    def __init__(self, store: CredentialStore, refresh_interval: timedelta) -> None:
        self._store = store
        self._refresh_interval = refresh_interval
        self._hive: ManagedHive | None = None
        self._lock = asyncio.Lock()
        self._last: HiveSnapshot | None = None

    async def _ensure_started(self) -> ManagedHive:
        if self._hive is None:
            hive = ManagedHive(self._store.require(), on_new_tokens=self._store.update_tokens)
            try:
                await hive.start()
            except BaseException:
                await hive.close()
                raise
            self._hive = hive
            self._last = self._capture(hive)
        return self._hive

    @staticmethod
    def _capture(hive: ManagedHive, *, stale: bool = False) -> HiveSnapshot:
        return HiveSnapshot(
            products=copy.deepcopy(dict(hive.data.products)),
            devices=copy.deepcopy(dict(hive.data.devices)),
            fetched_at=datetime.now(LONDON),
            stale=stale,
        )

    async def snapshot(self, *, refresh: bool = False) -> HiveSnapshot:
        async with self._lock:
            hive = await self._ensure_started()
            last = self._last
            fresh = (
                last is not None and datetime.now(LONDON) - last.fetched_at < self._refresh_interval
            )
            if last is not None and fresh and not refresh:
                return last
            return await self._refresh(hive)

    async def _refresh(self, hive: ManagedHive) -> HiveSnapshot:
        """Fetch from Hive (the caller holds the lock), falling back to the last reading."""
        last = self._last
        try:
            ok = await hive.getDevices("No_ID")
        except hive_exceptions.HiveReauthRequired as err:
            raise ReauthRequired(str(err) or "Hive needs you to sign in again.") from err
        if ok:
            self._last = self._capture(hive)
            return self._last
        # getDevices() logs and swallows network/API errors, keeping its old data.
        _LOGGER.warning("Hive refresh failed; serving the last good reading.")
        if last is None:
            raise HiveUnavailable("Couldn't reach Hive.")
        return HiveSnapshot(last.products, last.devices, last.fetched_at, stale=True)

    async def write(
        self, product_type: str, product_id: str, kwargs: dict[str, Any]
    ) -> HiveSnapshot:
        async with self._lock:
            hive = await self._ensure_started()
            try:
                await hive.hiveRefreshTokens()
                resp = await hive.api.setState(product_type, product_id, **kwargs)
            except (hive_exceptions.HiveReauthRequired, hive_exceptions.HiveAuthError) as err:
                raise ReauthRequired(str(err) or "Hive needs you to sign in again.") from err
            except Exception as err:  # the library raises bare HTTPError, KeyError and others
                _LOGGER.warning("Hive write failed: %s: %s", type(err).__name__, err)
                raise WriteFailed(f"Couldn't reach Hive ({type(err).__name__}).") from err
            status = (resp or {}).get("original")
            if status != 200:
                _LOGGER.warning("Hive rejected a write: HTTP %s", status)
                raise WriteFailed(f"Hive didn't accept the change (HTTP {status}).")
            _LOGGER.info("Hive accepted a write to %s/%s: %s", product_type, product_id, kwargs)
            return await self._refresh(hive)

    async def close(self) -> None:
        if self._hive is not None:
            await self._hive.close()
            self._hive = None


def make_client(settings: Settings) -> HiveClient:
    if settings.fake:
        from .fake_client import FakeHiveClient

        _LOGGER.info("Using fake Hive data from %s", settings.fake_fixture)
        return FakeHiveClient(settings.fake_fixture)
    return LiveHiveClient(CredentialStore(settings.keychain_service), settings.refresh_interval)
