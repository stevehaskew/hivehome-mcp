"""A HiveClient that serves a fixture file instead of calling Hive.

Writes are recorded in `writes` and applied to the in-memory data roughly as Hive
would, so the write tools can be tested end to end.
"""

from __future__ import annotations

import copy
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from .hive_client import WriteFailed
from .snapshot import LONDON, HiveSnapshot, RawNodes


class FakeHiveClient:
    def __init__(self, fixture: Path) -> None:
        data = json.loads(fixture.read_text(encoding="utf-8"))
        self.products: RawNodes = data["products"]
        self.devices: RawNodes = data["devices"]
        self.writes: list[tuple[str, str, dict[str, Any]]] = []
        self.fail_writes: str | None = None  # set to a reason to make writes fail

    async def snapshot(self, *, refresh: bool = False) -> HiveSnapshot:
        return HiveSnapshot(
            products=copy.deepcopy(self.products),
            devices=copy.deepcopy(self.devices),
            fetched_at=datetime.now(LONDON),
        )

    async def close(self) -> None:
        return None

    async def write(
        self, product_type: str, product_id: str, kwargs: dict[str, Any]
    ) -> HiveSnapshot:
        if self.fail_writes:
            raise WriteFailed(self.fail_writes)
        self.writes.append((product_type, product_id, dict(kwargs)))
        product = self.products[product_id]
        state, props = product.setdefault("state", {}), product.setdefault("props", {})
        mode = kwargs.get("mode")
        if mode == "BOOST":
            props["previous"] = {"mode": state.get("mode"), "target": state.get("target")}
            state["boost"] = int(kwargs["boost"])
        elif mode is not None:
            state.pop("boost", None)
        if mode is not None:
            state["mode"] = mode
        if "target" in kwargs:
            state["target"] = kwargs["target"]
        if product_type == "hotwater" and mode is not None:
            state["status"] = "ON" if mode in ("BOOST", "MANUAL") else "OFF"
        return await self.snapshot(refresh=True)
