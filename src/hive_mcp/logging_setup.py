"""Logging: stderr for humans, a rotating file for the record. Never stdout.

stdout carries the MCP protocol once the server runs, so nothing may log there.
The Hive library is noisy (a KeyError warning per TRV on every poll), so it is
kept to ERROR on stderr and de-duplicated in the file.
"""

from __future__ import annotations

import logging
import re
import sys
from logging.handlers import RotatingFileHandler

from .config import Settings

LIBRARY_LOGGERS = ("apyhiveapi", "botocore", "boto3", "urllib3", "fastmcp", "mcp", "docket")

# JWTs (Cognito id/access tokens) and long opaque blobs (refresh tokens, device secrets).
_SECRET_PATTERNS = (
    re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    re.compile(r"[A-Za-z0-9+/=_-]{120,}"),
)


def scrub(text: str) -> str:
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("<redacted>", text)
    return text


class ScrubFilter(logging.Filter):
    """Redact anything token-shaped from the formatted message."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        cleaned = scrub(message)
        if cleaned != message:
            record.msg, record.args = cleaned, None
        return True


class DedupeFilter(logging.Filter):
    """Drop library warnings already seen in this process (keeps errors)."""

    def __init__(self) -> None:
        super().__init__()
        self._seen: set[tuple[str, str]] = set()

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno >= logging.ERROR or not record.name.startswith(LIBRARY_LOGGERS):
            return True
        key = (record.name, record.getMessage())
        if key in self._seen:
            return False
        self._seen.add(key)
        return True


class _LibraryLevelFilter(logging.Filter):
    """Raise the threshold for library loggers on one handler only."""

    def __init__(self, level: int) -> None:
        super().__init__()
        self._level = level

    def filter(self, record: logging.LogRecord) -> bool:
        return not record.name.startswith(LIBRARY_LOGGERS) or record.levelno >= self._level


def configure_logging(settings: Settings, *, verbose: bool = False) -> None:
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.DEBUG)

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%d/%m/%Y %H:%M:%S"
    )
    scrubber = ScrubFilter()

    stderr = logging.StreamHandler(sys.stderr)
    stderr.setLevel(logging.DEBUG if verbose else logging.WARNING)
    stderr.addFilter(_LibraryLevelFilter(logging.DEBUG if verbose else logging.ERROR))
    stderr.addFilter(scrubber)
    stderr.setFormatter(fmt)
    root.addHandler(stderr)

    try:
        settings.log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            settings.log_dir / "hive-mcp.log", maxBytes=1_000_000, backupCount=5, encoding="utf-8"
        )
    except OSError as err:
        logging.getLogger(__name__).warning("File logging disabled: %s", err)
        return
    file_handler.setLevel(settings.log_level)
    file_handler.addFilter(_LibraryLevelFilter(logging.WARNING))
    file_handler.addFilter(DedupeFilter())
    file_handler.addFilter(scrubber)
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)
