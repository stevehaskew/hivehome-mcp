"""The audit log: one JSON line per write attempt, in its own rotating file.

Kept apart from the main log (and from the root logger), so it holds only the
record of what was asked of the heating and what happened.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Literal

from .snapshot import LONDON

Result = Literal["ok", "unconfirmed", "no_change", "rejected", "failed"]


class AuditLog:
    def __init__(self, path: Path) -> None:
        """Opens the file now, so a log that can't be written stops writes being offered."""
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._handler = RotatingFileHandler(
            path, maxBytes=1_000_000, backupCount=10, encoding="utf-8"
        )
        self._handler.setFormatter(logging.Formatter("%(message)s"))

    def record(
        self,
        *,
        tool: str,
        zone: str | None,
        zone_id: str | None,
        requested: dict[str, Any],
        result: Result,
        reason: str | None = None,
        before: dict[str, Any] | None = None,
        sent: dict[str, Any] | None = None,
        after: dict[str, Any] | None = None,
        now: datetime | None = None,
    ) -> None:
        entry = {
            "time": (now or datetime.now(LONDON)).astimezone(LONDON).strftime("%d/%m/%Y %H:%M:%S"),
            "tool": tool,
            "zone": zone,
            "id": zone_id,
            "before": before,
            "requested": requested,
            "sent": sent,
            "result": result,
            "reason": reason,
            "after": after,
        }
        line = json.dumps(entry, ensure_ascii=False)
        self._handler.handle(logging.makeLogRecord({"msg": line, "args": None}))

    def close(self) -> None:
        self._handler.close()
