# hive-mcp

Local MCP server for Hive Active Heating (read and write tools), used from Claude Code and Claude Desktop. **Read [README.md](README.md) first**, especially "How it works" and the library notes. Open work is in the [GitHub issues](https://github.com/stevehaskew/hivehome-mcp/issues) (`gh issue list`).

## The live system

- 8 heating zones and 1 hot water. 4 **boiler zones** (`heating`): Main Hall, Upstairs, Thermostat 3, Downstairs. Each takes its thermostat's name (Hive itself calls the Thermostat 3 zone "Zone 1"). 4 **TRV zones** (`trvcontrol`): Creche, Oak Room and two offices named after people (the fixture calls them Office A and Office B).
- **Downstairs is offline.** Hive returns `MALFORMED_NODE_DESCRIPTOR` for it, and the tools list it as unavailable.
- **Thermostat 3** controls the hot water too. Its heating zone is normally OFF and nobody uses it, so it's the zone for live write checks.
- An OFF zone shows target 7°C (frost protection). Straight after switching a zone OFF, Hive briefly still reports the old target.
- The write tools are turned on in `.mcp.json` and Claude Desktop (`"env": {"HIVE_MCP_ALLOW_WRITES": "1"}`). Every attempt is recorded in `~/Library/Logs/hive-mcp/audit.log`.

## Rules

- **Never write to stdout** in server code paths. stdout carries the MCP protocol; use `logging` (it goes to stderr and `~/Library/Logs/hive-mcp/`).
- **Only `_hivelib.py` imports `apyhiveapi`.** Interpretation of Hive data belongs in `readings.py` (pure functions over raw JSON), not in library getters.
- **Keep personal data out of committed files:** people's names in zone names, hardware IDs (EUI-64), the account email, real Hive UUIDs. Regenerate the fixture with `scripts/make_fixture.py --rename "Real=Generic"`.
- **UK conventions** in anything user-facing: °C, DD/MM/YYYY, 24-hour clock, British English.
- **Writes to the heating** must stay behind `HIVE_MCP_ALLOW_WRITES`, with the server-side limits in `writes.py` (5–25°C, 15–120 minute boosts, rejected rather than clamped) and the audit log.
- **Live writes need the user's OK each time.** It's a real building's heating, so use a zone nobody is using and put it back afterwards.
- **Live checks need the user.** `hive-mcp login` needs their password, so ask them to run it and paste the output. Read-only live calls (`hive-mcp show`, the MCP tools) are fine to run.

## Commands

```bash
uv run pytest
uv run ruff check . && uv run ruff format --check .
uv run mypy
uv run hive-mcp show                  # live readings
HIVE_MCP_FAKE=1 uv run hive-mcp show  # fixture readings
```
