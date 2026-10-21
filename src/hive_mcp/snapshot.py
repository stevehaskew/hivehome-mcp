"""The raw data every HiveClient returns."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

LONDON = ZoneInfo("Europe/London")

RawNodes = dict[str, dict[str, Any]]


@dataclass(frozen=True)
class HiveSnapshot:
    """Hive's products (zones, hot water) and devices (thermostats, TRVs, hub), by id."""

    products: RawNodes
    devices: RawNodes
    fetched_at: datetime
    stale: bool = False
