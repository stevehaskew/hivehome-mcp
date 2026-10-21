# hive-mcp

A local MCP server for reading (and later controlling) Hive Active Heating. It's built on the unofficial `pyhive-integration` library. See [PLAN.md](PLAN.md) for the design and the spike findings.

## Sign in

```bash
uv run hive-mcp login     # email, password, SMS code if Hive asks; registers this Mac
uv run hive-mcp status    # shows what's stored and tests the connection
uv run hive-mcp logout    # deregisters the device and wipes the keychain entry
```

To see what the server will report:

```bash
uv run hive-mcp show                  # every zone and the hot water
uv run hive-mcp show "Main Hall" --json
HIVE_MCP_FAKE=1 uv run hive-mcp show  # from tests/fixtures/hive_sample.json, no Hive login
```

Credentials, device keys and tokens are stored in one macOS Keychain item (service `hive-mcp`). Nothing is written to the repo.

To test unattended recovery after the refresh token expires, without waiting for it to actually expire:

```bash
uv run hive-mcp status --simulate-expiry
```

## The MCP server

```bash
uv run hive-mcp serve                  # stdio; normally started by Claude, not by hand
HIVE_MCP_FAKE=1 uv run hive-mcp serve  # same, serving the fixture
```

Read-only tools: `list_devices`, `get_heating_status` (one zone by name or id, or every zone if none is given) and `get_hot_water_status`.

It's registered for Claude Code at project scope in `.mcp.json`, which is gitignored because it holds absolute paths. Copy [.mcp.json.example](.mcp.json.example) to `.mcp.json` and replace `/path/to/hivehome-mcp` with this folder's path. The paths are absolute (uv at `/opt/homebrew/bin/uv`), so it still works when the app isn't launched from a shell. Approve it the first time Claude Code asks. Or create the entry with:

```bash
claude mcp add hive --scope project -- /opt/homebrew/bin/uv --directory /path/to/hivehome-mcp run hive-mcp serve
```

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `HIVE_USERNAME`, `HIVE_PASSWORD` | – | Override the stored username/password |
| `HIVE_MCP_KEYCHAIN_SERVICE` | `hive-mcp` | Keychain service name |
| `HIVE_MCP_LOG_DIR` | `~/Library/Logs/hive-mcp` | Rotating log file location |
| `HIVE_MCP_LOG_LEVEL` | `INFO` | File log level |
| `HIVE_MCP_FAKE` | off | Serve fixture data instead of calling Hive |
| `HIVE_MCP_FAKE_FIXTURE` | `tests/fixtures/hive_sample.json` | Fixture used in fake mode |
| `HIVE_MCP_REFRESH_SECONDS` | `120` (min 60) | How often to fetch fresh data from Hive |
| `HIVE_MCP_ALLOW_WRITES` | off | Register the write tools (later) |

## Development

```bash
uv run pytest
uv run ruff check . && uv run ruff format --check .
uv run mypy
```
