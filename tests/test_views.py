"""UK wording of times in summaries."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from hive_mcp.snapshot import LONDON
from hive_mcp.views import uk_when

REF = datetime(2026, 10, 21, 22, 15, tzinfo=LONDON)  # a Wednesday


@pytest.mark.parametrize(
    ("when", "expected"),
    [
        (datetime(2026, 10, 21, 23, 0, tzinfo=LONDON), "23:00"),
        (datetime(2026, 10, 22, 2, 0, tzinfo=LONDON), "tomorrow 02:00"),
        (datetime(2026, 10, 20, 21, 30, tzinfo=LONDON), "yesterday 21:30"),
        (datetime(2026, 10, 27, 7, 0, tzinfo=LONDON), "Tue 07:00"),
        (datetime(2026, 10, 28, 6, 0, tzinfo=LONDON), "Wed 28/10/2026 06:00"),
        (datetime(2026, 10, 18, 10, 15, tzinfo=LONDON), "Sun 18/10/2026 10:15"),
    ],
)
def test_uk_when(when: datetime, expected: str) -> None:
    assert uk_when(when, REF) == expected


def test_uk_when_uses_uk_dates_not_utc() -> None:
    # 23:30 UTC on Wednesday is 00:30 BST on Thursday.
    when = datetime(2026, 10, 21, 23, 30, tzinfo=UTC)
    assert uk_when(when, REF) == "tomorrow 00:30"
