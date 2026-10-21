"""The audit log: one JSON line per write attempt."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from hive_mcp.audit import AuditLog
from hive_mcp.snapshot import LONDON


def test_one_json_line_per_record(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "logs/audit.log")  # creates the folder
    log.record(
        tool="set_target_temperature",
        zone="Thermostat 3",
        zone_id="id-16",
        requested={"temp_c": 12},
        result="ok",
        before={"mode": "MANUAL", "target_c": 7.0},
        sent={"target": 12},
        after={"mode": "MANUAL", "target_c": 12.0},
        now=datetime(2026, 10, 21, 14, 3, 12, tzinfo=LONDON),
    )
    log.record(
        tool="boost_heating",
        zone="Main Hall",
        zone_id="id-11",
        requested={"minutes": 300, "temp_c": 20},
        result="rejected",
        reason="A boost of 300 minutes is outside the allowed range of 15–120 minutes.",
    )
    log.close()

    lines = (tmp_path / "logs/audit.log").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    first, second = (json.loads(line) for line in lines)
    assert first == {
        "time": "21/10/2026 14:03:12",
        "tool": "set_target_temperature",
        "zone": "Thermostat 3",
        "id": "id-16",
        "before": {"mode": "MANUAL", "target_c": 7.0},
        "requested": {"temp_c": 12},
        "sent": {"target": 12},
        "result": "ok",
        "reason": None,
        "after": {"mode": "MANUAL", "target_c": 12.0},
    }
    assert second["result"] == "rejected"
    assert second["sent"] is None and second["after"] is None
    assert "15–120 minutes" in lines[1]  # written as UTF-8, not \u escapes


def test_time_is_uk_local(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "audit.log")
    summer_utc = datetime.fromisoformat("2026-07-01T12:00:00+00:00")
    log.record(tool="t", zone=None, zone_id=None, requested={}, result="failed", now=summer_utc)
    log.close()
    assert json.loads((tmp_path / "audit.log").read_text())["time"] == "01/07/2026 13:00:00"


def test_appends_across_restarts(tmp_path: Path) -> None:
    for _ in range(2):
        log = AuditLog(tmp_path / "audit.log")
        log.record(tool="t", zone=None, zone_id=None, requested={}, result="no_change")
        log.close()
    assert len((tmp_path / "audit.log").read_text().splitlines()) == 2
