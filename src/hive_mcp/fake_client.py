"""A HiveClient that serves a fixture file instead of calling Hive."""

from __future__ import annotations

import copy
import json
from datetime import datetime
from pathlib import Path

from .snapshot import LONDON, HiveSnapshot, RawNodes


class FakeHiveClient:
    def __init__(self, fixture: Path) -> None:
        data = json.loads(fixture.read_text(encoding="utf-8"))
        self.products: RawNodes = data["products"]
        self.devices: RawNodes = data["devices"]

    async def snapshot(self) -> HiveSnapshot:
        return HiveSnapshot(
            products=copy.deepcopy(self.products),
            devices=copy.deepcopy(self.devices),
            fetched_at=datetime.now(LONDON),
        )

    async def close(self) -> None:
        return None
