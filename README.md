# hive-mcp

A local MCP server for reading and controlling Hive Active Heating from Claude (Claude Code and Claude Desktop). It's built on the unofficial `pyhive-integration` library, which can break without notice. See [How it works](#how-it-works) for the design and the library notes. Open work is tracked in [GitHub issues](https://github.com/stevehaskew/hivehome-mcp/issues).

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

Read-only tools: `list_devices`, `get_heating_status` (one zone by name or id, or every zone if none is given), `get_hot_water_status` and `get_schedule` (the full Monday–Sunday schedule for one heating zone or the hot water).

Write tools, only registered when `HIVE_MCP_ALLOW_WRITES=1` (otherwise the server doesn't offer them at all):

- `set_target_temperature(zone, temp_c)`: in SCHEDULE mode this overrides the schedule until its next change. Refused for a zone that is OFF or boosting.
- `set_heating_mode(zone, SCHEDULE|MANUAL|OFF, temp_c)`: `temp_c` is required for MANUAL and not allowed otherwise.
- `boost_heating(zone, minutes, temp_c)` / `cancel_heating_boost(zone)`
- `set_hot_water_mode(SCHEDULE|ON|OFF)`
- `boost_hot_water(minutes)` / `cancel_hot_water_boost()`

Limits are enforced by the server: temperatures 5–25°C in 0.5°C steps, boosts 15–120 minutes. A value outside them is refused, not adjusted. Unavailable zones are refused, and so is starting a boost on a zone that's already boosting. Each call reads Hive fresh first and again after the write, and reports whether Hive's reading shows the change.

Every write attempt, including refused ones, is logged to `audit.log` in the log folder as one JSON line: `time` (UK), `tool`, `zone`, `id`, `before`, `requested`, `sent` (the kwargs sent to Hive), `result` (`ok`, `unconfirmed`, `no_change`, `rejected` or `failed`), `reason` and `after`. If the audit log can't be opened, the write tools aren't offered.

To turn writes on in Claude, add `"env": {"HIVE_MCP_ALLOW_WRITES": "1"}` to the server's entry in `.mcp.json` or `claude_desktop_config.json`.

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
| `HIVE_MCP_ALLOW_WRITES` | off | Register the write tools |

## How it works

```
src/hive_mcp/
  __main__.py      CLI: serve | login | logout | status | show
  server.py        FastMCP app: read tools, write tools (only if allowed), known failures -> ToolErrors
  writes.py        Write planning (no I/O): limits, mode rules, setState kwargs, "did it take?"
  audit.py         audit.log: one JSON line per write attempt, in its own rotating file
  views.py         Tool outputs: numbers + UK-formatted times + one-sentence summaries
  readings.py      Pure functions: raw Hive JSON -> typed readings (names, availability,
                   batteries, modes, boost, schedules; repeats merged, DST-safe)
  models.py        Pydantic reading models (values, aware datetimes)
  snapshot.py      HiveSnapshot (raw products/devices + fetched_at + stale) and the London time zone
  hive_client.py   HiveClient Protocol; LiveHiveClient (lazy sign-in, cache, stale fallback,
                   write = setState + forced refresh); make_client(settings)
  fake_client.py   FakeHiveClient: serves the fixture, records and applies writes
  auth.py          interactive_login, device_login, forget_device, ManagedHive (library fixes)
  credentials.py   One Keychain item (service "hive-mcp", account "credentials"), env overrides
  config.py        Settings from environment variables
  logging_setup.py stderr + rotating file, token scrubbing, de-duplicated library warnings; never stdout
  _hivelib.py      The only module that imports apyhiveapi; restores sys.excepthook
scripts/make_fixture.py   Builds the scrubbed test fixture from the spike dump
spike/login_spike.py      The original sign-in spike, kept for reference (output gitignored)
```

**The library is used only to sign in, fetch raw data and send `setState`.** Both clients return a raw `HiveSnapshot`, and `readings.py` interprets it. The library's getters are thin wrappers with side effects, and `getState()` is misleading. The readings were checked against what the library's own getters reported live.

**Other decisions:**

- **stdio only**, for a single user on one Mac. The library is async because FastMCP tools are.
- **Sign-in happens in a terminal** (`hive-mcp login`), because a stdio server can't ask for an SMS code. It registers this Mac as a trusted Cognito device, so later logins (including the server's unattended re-login) need no SMS. Tokens are written back to the Keychain only when the refresh token changes.
- **Refreshing:** the server signs in on the first tool call, fetches at most every `HIVE_MCP_REFRESH_SECONDS`, and serves the last good snapshot, marked `stale`, if Hive can't be reached. Write tools always read fresh first and refuse to act on stale data.
- **Errors Claude sees** are limited to: sign in again (`uv run hive-mcp login`), couldn't reach Hive, an unknown zone (with the valid names), and a refused or failed write with the reason. Anything else is masked.
- **Registration uses absolute paths** (`/opt/homebrew/bin/uv --directory …`), so the server starts even when the app is launched from the Dock with a minimal `PATH`.

### Library notes (`pyhive-integration` 1.0.10)

- **Pinned to 1.0.10.** 2.0 (in beta, now at [Pyhass/Pyhive](https://github.com/Pyhass/Pyhive)) is a rewrite with a snake_case API. When moving to it, only `_hivelib.py`, `auth.py` and `hive_client.py` should need to change.
- **Two bugs are worked around in `ManagedHive`:** `Session.login()` can't re-login a fresh process, and `hiveRefreshTokens()` deadlocks once the refresh token expires. Details are in `auth.py` and [issue #4](https://github.com/stevehaskew/hivehome-mcp/issues/4), which is about reporting them upstream.
- **Other quirks:**
  - Importing `apyhiveapi.hive` replaces `sys.excepthook` with one that logs local variables, which could include the password; `_hivelib.py` puts the original back.
  - `is_device_registered()` returns `False` even for a working device.
  - The 1.0.10 wheel lacks the file-mode fixtures, which is why there's a `FakeHiveClient`.
  - About 24 harmless `KeyError 'zone'` warnings per TRV per poll; they're kept off stderr and de-duplicated in the log file.
- **Writes** call `api.setState(product_type, product_id, **kwargs)` directly: POST `/nodes/{type}/{id}`, with every value sent as a string. The kwargs, taken from the library's own setters, are listed at the top of `writes.py`. TRV zones (`trvcontrol`) take the same kwargs as boiler zones. `setState` raises bare exceptions or returns `{}` on some errors, so `LiveHiveClient.write` treats anything other than HTTP 200 as a failure.

## Development

```bash
uv run pytest
uv run ruff check . && uv run ruff format --check .
uv run mypy
```
