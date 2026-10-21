# Hive MCP server: plan and status

*Last updated 21/10/2026. Read-only prototype finished and working live.*

A local MCP server (Python, FastMCP, stdio) that lets Claude read, and later control, a Hive Active Heating system. It's built on the unofficial `pyhive-integration` library, which can break without notice. UK conventions throughout: °C, DD/MM/YYYY, 24-hour, British English.

## 0. Status at a glance

| Area | State |
|---|---|
| Login / 2FA / device trust | ✅ `hive-mcp login`, `logout`, `status [--simulate-expiry]`. Credentials in the macOS Keychain. Unattended re-login verified live. |
| Read-only MCP tools | ✅ `list_devices`, `get_heating_status`, `get_hot_water_status`, verified live against the real account. |
| Claude Code registration | ✅ Project scope (`.mcp.json`, gitignored; `.mcp.json.example` is committed), approved via `.claude/settings.local.json`. Used from a fresh session 21/10/2026. |
| Tests | ✅ 71 tests (`uv run pytest`), `ruff`, `mypy --strict`. |
| Claude Desktop registration | ✅ `mcpServers.hive` added to `claude_desktop_config.json` 21/10/2026 (backup alongside it). Needs a Desktop restart; not yet checked in the app. |
| Full weekly schedule tool | ⏳ Not done (the raw data is already in each snapshot). |
| Write tools | ⏳ Not started. Must stay behind `HIVE_MCP_ALLOW_WRITES` (§6). |
| Version control | ✅ git, `main`, remote `github.com/stevehaskew/hivehome-mcp` (public). |

The spike's test device and its Keychain entry (`hive-mcp-spike`) have been removed.

## 1. How it fits together

```
src/hive_mcp/
  __main__.py      CLI: serve | login | logout | status | show
  server.py        FastMCP app; read-only tools; turns known failures into ToolErrors
  views.py         Tool outputs: numbers + UK-formatted times + one-sentence summaries
  readings.py      Pure functions: raw Hive JSON -> typed readings (names, availability,
                   batteries, modes, boost, schedule now/next/later, DST-safe)
  models.py        Pydantic reading models (values, aware datetimes)
  snapshot.py      HiveSnapshot (raw products/devices + fetched_at + stale) and LONDON tz
  hive_client.py   HiveClient Protocol, LiveHiveClient (lazy sign-in, cache, stale fallback),
                   make_client(settings)
  fake_client.py   FakeHiveClient: serves tests/fixtures/hive_sample.json
  auth.py          interactive_login, device_login, forget_device, ManagedHive (library fixes)
  credentials.py   One Keychain item (service "hive-mcp", account "credentials"), env overrides
  config.py        Settings from env vars (see README)
  logging_setup.py stderr + rotating file (~/Library/Logs/hive-mcp/), token scrubbing,
                   dedupe of library warnings; never stdout
  _hivelib.py      The only module that imports apyhiveapi; restores sys.excepthook
scripts/make_fixture.py   Builds the scrubbed test fixture from the spike dump
spike/login_spike.py      Throwaway step-0 spike (kept for reference; output gitignored)
```

**The key design choice:** the library is used **only for sign-in and fetching raw data**. Both clients return a raw `HiveSnapshot`, and `readings.py` interprets it. The library's getters are thin wrappers with side effects, and `getState()` is misleading. The readings are checked against what the library's own getters reported live during the spike.

Tool outputs (`views.py`) always carry `as_of` (UK time) and `stale` (true when Hive couldn't be reached and the data is older). Each zone has a `summary` sentence. Batteries at 20% or below are flagged `low_battery`.

## 2. Library findings (`pyhive-integration`, checked 21/10/2026)

- **Pinned to `1.0.10`** (stable, 20/10/2026). `2.0.0b1`–`b4` is a beta rewrite: snake_case API with camelCase aliases, and the repo has moved to `Pyhass/Pyhive`. Stay on 1.0.10 until 2.0 is stable. Then only `_hivelib.py`/`auth.py`/`hive_client.py` need to change.
- **Auth** is AWS Cognito SRP via `boto3`, with the calls run in an executor. Possible results: tokens, an `SMS_MFA` challenge, or `DEVICE_SRP_AUTH` (for a device registered earlier).
- **The 1.0.10 wheel lacks the file-mode fixtures**, so `use@file.com` mode is broken. That's why we have our own `FakeHiveClient`.
- **Two library bugs**, both worked around in `ManagedHive` and worth reporting upstream (check whether 2.0 fixes them):
  1. `Session.login()` checks whether the device is "remembered" using an access token that a fresh process doesn't have. It then raises `HiveInvalidDeviceAuthentication`, so the library can never log back in by itself. *Fix:* we override `login()` with a plain device-SRP flow.
  2. `hiveRefreshTokens()` holds the non-reentrant `_refreshLock` while it calls `_retryLogin()`. That function calls `hiveRefreshTokens(force_refresh=True)` and **deadlocks**, so an expired refresh token hangs the session. This was reproduced against the unpatched library. *Fix:* we override `_retryLogin()` so it re-logs in with the device keys (retrying network errors after 0/5/10 seconds) without refreshing again.
- Importing `apyhiveapi.hive` replaces `sys.excepthook` with one that logs local variables, which could include the password. `_hivelib.py` puts the original back.
- `is_device_registered()` returns `False` even for a working, registered device. Don't rely on it.
- The library logs about 24 `KeyError 'zone'` warnings per TRV per poll. They're harmless; our logging keeps them out of stderr and removes duplicates in the file.
- **All the library's setters** (`setTargetTemperature`, `setMode`, `setBoostOn`, …) end up calling `session.api.setState(product_type, product_id, **kwargs)` followed by `getDevices()`. For example `target=21`, `mode="SCHEDULE"`, or `mode="BOOST", boost=mins, target=temp`. The write tools can call `setState` directly with product ids, as the readings do, rather than building the library's `deviceList` dicts. Before using them, check `setBoostOff` and the hot-water setters (`hotwater.py`) for their exact kwargs.

## 3. The live system (from the spike and live runs)

- **8 heating zones and 1 hot water.**
  - 4 **boiler zones** (`heating` products): Main Hall, Upstairs, Thermostat 3, Downstairs. Each zone has a boiler module plus a thermostat, and the zone takes the thermostat's name; Hive calls the "Thermostat 3" zone "Zone 1".
  - 4 **TRV zones** (`trvcontrol` products): Creche, Oak Room and two offices named after people (the fixture calls them Office A and Office B).
- **Downstairs is offline.** Its boiler module and thermostat are both offline, and Hive returns `MALFORMED_NODE_DESCRIPTOR` for it; the library silently drops it. We list it as unavailable, with the reason.
- **Thermostat 3** controls the hot water too.
- **A zone that's OFF** shows target 7°C, which is its frost protection.
- **Low batteries** at 21/10/2026: Creche TRV 20%, Oak Room TRV 20%, Thermostat 3 at 20%. Main Hall and Upstairs at 40%.
- **No SMS 2FA** on this account, so the SMS branch of `hive-mcp login` is **untested**. If 2FA is ever turned on, re-run the spike or test the login again.
- **Personal data stays out of the repo.** Zone names that are people's names, hardware addresses (EUI-64), the hub hardware ID, account email and holiday dates must not appear in committed files. `scripts/make_fixture.py --rename "Real=Generic"` handles the fixture. The spike output (`spike/out/`) and `.claude/settings.local.json` are gitignored.

## 4. Decisions

- **Async library** (`apyhiveapi`), because FastMCP tools are async.
- **stdio transport only.** It's for a single user on one Mac.
- **2FA and tokens:** sign-in happens in a terminal (`hive-mcp login`), because a stdio server can't ask for an SMS code. The login registers this Mac as a trusted Cognito device, so later logins need no SMS. The Keychain holds username, password, device keys and tokens. Tokens are written back only when the refresh token changes, i.e. after a re-login. `HIVE_USERNAME`/`HIVE_PASSWORD` override the stored username and password.
- **Refreshing:** the server signs in on the first tool call, fetches at most every `HIVE_MCP_REFRESH_SECONDS` (default 120, minimum 60), and serves the last good snapshot, marked `stale`, if Hive can't be reached.
- **Errors Claude sees:**
  - "sign in again: run `uv run hive-mcp login`"
  - "Couldn't reach Hive"
  - an unknown zone, with the list of valid names.
  Anything else is masked.
- **Registration:** `.mcp.json` uses absolute paths (`/opt/homebrew/bin/uv --directory /path/to/hivehome-mcp run hive-mcp serve`), so the server starts even when the app is launched from the Dock with a minimal `PATH`.

## 5. Next steps (in suggested order)

1. ✅ **Use it from a fresh Claude Code session** (21/10/2026). The tools load and the answers are correct. Wording to fix (see step 4).
2. ✅ **Version control.** First commit pushed to `github.com/stevehaskew/hivehome-mcp` (public), so keep checking for personal data before every push.
3. ✅ **Claude Desktop registration.** Added the same command to `~/Library/Application Support/Claude/claude_desktop_config.json` under `mcpServers.hive`. Still to do: restart Claude Desktop and check the tools appear.
4. **Small polish:**
   - FastMCP logs expected `ToolError`s (such as an unknown zone) at ERROR on stderr. Quieten that.
   - Schedule: merge back-to-back slots with the same target. Main Hall shows "now 10°C, next 10°C, later 10°C", and the summary's "Next: 10°C from 01:00" reads like a change when nothing changes. Hot water likewise for same on/off state.
   - Summaries repeat the full date twice ("from Wed 21/10/2026 22:00 until Thu 22/10/2026 02:00"). Use times alone for today and "tomorrow 02:00" or "Thu 02:00" within a week; keep full dates for later.
   - The per-zone `min_temp_c`/`max_temp_c` (5/32) and the boiler zones' `controller` (always the zone's own name) are noise in every response. Drop them, or keep `controller` only when it differs (it's useful for hot water).
   - Optionally add a `get_schedule(zone)` tool that returns the full weekly schedule. The raw schedule is already in the snapshot (`state.schedule`, minutes past midnight per day).
5. **Write tools (phase 2).** See §6.
6. **Upstream:** report the two library bugs (§2) to `Pyhass/Pyhive` after checking 2.0.0b4. Ask the user before posting anything publicly.

## 6. Phase 2: write tools (design notes, not built)

From the original brief: clamp temperatures, cap boost durations, log every write, and keep writes behind a config flag.

- **Flag:** `HIVE_MCP_ALLOW_WRITES` (already in `Settings.allow_writes`, default off). While it's off, the write tools **aren't registered at all**.
- **Proposed tools:**
  - `set_target_temperature(zone, temp_c)`
  - `set_heating_mode(zone, SCHEDULE|MANUAL|OFF)`
  - `boost_heating(zone, minutes, temp_c)` / `cancel_heating_boost(zone)`
  - `set_hot_water_mode(SCHEDULE|ON|OFF)`
  - `boost_hot_water(minutes)` / `cancel_hot_water_boost()`
- **Safety limits** (constants, enforced server-side):
  - Target and boost temperatures within **5–25°C**. Hive allows 5–32°C.
  - Boost at most **120 minutes**, minimum 15.
  - Reject values out of range rather than silently clamping them, and say what the limits are.
  - Refuse to write to an unavailable zone.
- **Audit log:** a separate file `~/Library/Logs/hive-mcp/audit.log`. One line per write attempt with the time (UK), tool, zone name and id, the values before, the requested values, the result, and the values after the refresh.
- **Implementation:** a `write` method on `LiveHiveClient` that calls `hive.api.setState(product_type, product_id, **kwargs)` under the client lock, checks for HTTP 200, then forces a refresh and drops the cached snapshot. `FakeHiveClient` records writes and updates its in-memory products, so the tools can be tested end to end.
- **Tool annotations:** `readOnlyHint: false`, `destructiveHint: false`, `idempotentHint: true` for the set-value tools. Each response says what changed, e.g. "Main Hall target changed from 10°C to 18°C".

## 7. Risks

- **Unofficial API:** Hive may change its Cognito set-up or endpoints. The library pin and narrow adapter limit the damage, but they can't prevent breakage.
- **The 2.0 beta is moving quickly:** 1.0.x may stop being maintained.
- **Device trust:** if Cognito forgets the device, re-login needs `hive-mcp login` again. With SMS 2FA enabled, that would also need a phone.
