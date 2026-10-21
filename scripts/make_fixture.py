"""Turn the spike's device dump into a test fixture with personal data removed.

    uv run python scripts/make_fixture.py --rename "Real Name=Generic Name" ...

The spike already swapped Hive UUIDs for id-N aliases and redacted MAC/IP
addresses. This removes what it missed: names given on the command line (kept
out of the repo on purpose), Zigbee EUI-64 addresses, the hub's hardware ID and
holiday-mode dates. The output is checked for leftovers before it's written.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "spike/out/devices_anon.json"
TARGET = ROOT / "tests/fixtures/hive_sample.json"

UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
EUI64_RE = re.compile(r"^[0-9A-F]{16}$")


class Scrubber:
    def __init__(self, renames: dict[str, str]) -> None:
        # Longest first, so "Jo Smith's Office" is replaced before any "Jo Smith".
        self.renames = sorted(renames.items(), key=lambda kv: -len(kv[0]))
        self.eui64: dict[str, str] = {}

    def text(self, value: str) -> str:
        for old, new in self.renames:
            value = value.replace(old, new)
        return value

    def fake_eui64(self, value: str) -> str:
        return self.eui64.setdefault(value, f"{len(self.eui64) + 1:016X}")

    def walk(self, obj: Any, key: str | None = None) -> Any:
        if isinstance(obj, dict):
            out = {self.text(k): self.walk(v, k) for k, v in obj.items()}
            if key == "holidayMode":
                out.update({k: None for k in ("start", "end") if out.get(k) is not None})
            return out
        if isinstance(obj, list):
            return [self.walk(v, key) for v in obj]
        if isinstance(obj, str):
            if key == "eui64" or EUI64_RE.match(obj):
                return self.fake_eui64(obj)
            if key == "hardwareIdentifier":
                return "HW-0000"
            return self.text(obj)
        return obj


def leftovers(serialised: str, renames: dict[str, str], originals: list[str]) -> list[str]:
    problems = [f"name {old!r}" for old in renames if old in serialised]
    problems += [f"value {v!r}" for v in originals if v in serialised]
    if UUID_RE.search(serialised):
        problems.append("a raw UUID")
    if "@" in serialised:
        problems.append("an '@' (email address?)")
    return problems


def originals_to_check(data: Any) -> list[str]:
    found: list[str] = []

    def walk(obj: Any, key: str | None = None) -> None:
        if isinstance(obj, dict):
            for k, v in obj.items():
                walk(v, k)
        elif isinstance(obj, list):
            for v in obj:
                walk(v, key)
        elif isinstance(obj, str) and key in ("eui64", "hardwareIdentifier"):
            found.append(obj)

    walk(data)
    return found


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--rename", action="append", default=[], metavar="OLD=NEW")
    parser.add_argument("--source", type=Path, default=SOURCE)
    parser.add_argument("--target", type=Path, default=TARGET)
    args = parser.parse_args()

    renames = dict(item.split("=", 1) for item in args.rename)
    data = json.loads(args.source.read_text())
    scrubbed = Scrubber(renames).walk(data)
    serialised = json.dumps(scrubbed, indent=2, ensure_ascii=False) + "\n"

    problems = leftovers(serialised, renames, originals_to_check(data))
    if problems:
        print("Refusing to write; still found: " + ", ".join(problems), file=sys.stderr)
        return 1
    args.target.parent.mkdir(parents=True, exist_ok=True)
    args.target.write_text(serialised)
    print(f"Wrote {args.target.relative_to(ROOT)} ({len(serialised):,} bytes).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
