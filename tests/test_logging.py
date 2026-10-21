from __future__ import annotations

import logging

from hive_mcp.logging_setup import DedupeFilter, ScrubFilter, scrub

JWT = "eyJhbGciOiJSUzI1NiJ9." + "a" * 40 + "." + "b" * 40


def _record(name: str, level: int, msg: str, *args: object) -> logging.LogRecord:
    return logging.LogRecord(name, level, __file__, 1, msg, args, None)


def test_scrub_removes_jwts_and_long_blobs() -> None:
    assert scrub(f"token={JWT} ok") == "token=<redacted> ok"
    assert scrub("x" * 200) == "<redacted>"
    assert scrub("Creche 16.2°C") == "Creche 16.2°C"


def test_scrub_filter_rewrites_formatted_message() -> None:
    record = _record("apyhiveapi.session", logging.DEBUG, "tokens: %s", JWT)
    assert ScrubFilter().filter(record)
    assert record.getMessage() == "tokens: <redacted>"


def test_dedupe_drops_repeated_library_warnings_only() -> None:
    f = DedupeFilter()
    warn = "KeyError accessing zone data for device %s"
    assert f.filter(_record("apyhiveapi.helper", logging.WARNING, warn, "a"))
    assert not f.filter(_record("apyhiveapi.helper", logging.WARNING, warn, "a"))
    assert f.filter(_record("apyhiveapi.helper", logging.WARNING, warn, "b"))
    assert f.filter(_record("apyhiveapi.helper", logging.ERROR, "boom"))
    assert f.filter(_record("apyhiveapi.helper", logging.ERROR, "boom"))
    assert f.filter(_record("hive_mcp.auth", logging.WARNING, "same"))
    assert f.filter(_record("hive_mcp.auth", logging.WARNING, "same"))
