"""Runtime settings, read from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

_TRUTHY = {"1", "true", "yes", "on"}
_REPO_FIXTURE = Path(__file__).resolve().parents[2] / "tests/fixtures/hive_sample.json"


def _flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _TRUTHY


@dataclass(frozen=True)
class Settings:
    keychain_service: str
    log_dir: Path
    log_level: str
    fake: bool
    fake_fixture: Path
    refresh_interval: timedelta
    allow_writes: bool

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            keychain_service=os.environ.get("HIVE_MCP_KEYCHAIN_SERVICE", "hive-mcp"),
            log_dir=Path(
                os.environ.get("HIVE_MCP_LOG_DIR", Path.home() / "Library/Logs/hive-mcp")
            ).expanduser(),
            log_level=os.environ.get("HIVE_MCP_LOG_LEVEL", "INFO").upper(),
            fake=_flag("HIVE_MCP_FAKE"),
            fake_fixture=Path(os.environ.get("HIVE_MCP_FAKE_FIXTURE", _REPO_FIXTURE)).expanduser(),
            # Hive's own app polls about every 2 minutes; never hit it more than once a minute.
            refresh_interval=timedelta(
                seconds=max(60, int(os.environ.get("HIVE_MCP_REFRESH_SECONDS", "120")))
            ),
            allow_writes=_flag("HIVE_MCP_ALLOW_WRITES"),
        )
