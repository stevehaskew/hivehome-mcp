# hive-mcp

Local MCP server for Hive Active Heating. **Read [PLAN.md](PLAN.md) first.** It has the status, design, library bugs and next steps.

## Rules

- **Never write to stdout** in server code paths. stdout carries the MCP protocol; use `logging` (it goes to stderr and `~/Library/Logs/hive-mcp/`).
- **Only `_hivelib.py` imports `apyhiveapi`.** Interpretation of Hive data belongs in `readings.py` (pure functions over raw JSON), not in library getters.
- **Keep personal data out of committed files:** people's names in zone names, hardware IDs (EUI-64), the account email, real Hive UUIDs. Regenerate the fixture with `scripts/make_fixture.py --rename "Real=Generic"`.
- **UK conventions** in anything user-facing: °C, DD/MM/YYYY, 24-hour clock, British English.
- **Writes to the heating** must stay behind `HIVE_MCP_ALLOW_WRITES`, with server-side limits and an audit log (PLAN.md §6).
- **Live checks need the user.** `hive-mcp login` needs their password, so ask them to run it and paste the output. Read-only live calls (`hive-mcp show`, the MCP tools) are fine to run.

## Commands

```bash
uv run pytest
uv run ruff check . && uv run ruff format --check .
uv run mypy
uv run hive-mcp show                  # live readings
HIVE_MCP_FAKE=1 uv run hive-mcp show  # fixture readings
```
