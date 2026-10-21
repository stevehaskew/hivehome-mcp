# Hive MCP server: plan and status

*Last updated 21/10/2026. Read-only server finished and in use from Claude Code and Claude Desktop. Write tools built behind `HIVE_MCP_ALLOW_WRITES`, tested against the fake client, and checked live on Thermostat 3. They aren't turned on in any app config yet.*

A local MCP server (Python, FastMCP, stdio) that lets Claude read, and later control, a Hive Active Heating system. It's built on the unofficial `pyhive-integration` library, which can break without notice. UK conventions throughout: °C, DD/MM/YYYY, 24-hour, British English.

## 0. Status at a glance

| Area | State |
|---|---|
| Login / 2FA / device trust | ✅ `hive-mcp login`, `logout`, `status [--simulate-expiry]`. Credentials in the macOS Keychain. Unattended re-login verified live. |
| Read-only MCP tools | ✅ `list_devices`, `get_heating_status`, `get_hot_water_status`, `get_schedule`, verified live against the real account. |
| Claude Code registration | ✅ Project scope (`.mcp.json`, gitignored; `.mcp.json.example` is committed), approved via `.claude/settings.local.json`. Used from a fresh session 21/10/2026. |
| Tests | ✅ 184 tests (`uv run pytest`), `ruff`, `mypy --strict`. |
| Claude Desktop registration | ✅ `mcpServers.hive` added to `claude_desktop_config.json` 21/10/2026 (backup alongside it). Checked in the app after a restart. |
| Write tools | ✅ Built behind `HIVE_MCP_ALLOW_WRITES` (§6), tested end to end against `FakeHiveClient`, boiler-zone heating checked live 21/10/2026. Not turned on in any app config yet. |
| Version control | ✅ git, `main`, remote `github.com/stevehaskew/hivehome-mcp` (public). |

The spike's test device and its Keychain entry (`hive-mcp-spike`) have been removed.

## 1. How it fits together

```
src/hive_mcp/
  __main__.py      CLI: serve | login | logout | status | show
  server.py        FastMCP app; read tools, write tools (only if allowed); known failures -> ToolErrors
  writes.py        Pure write planning: limits, mode rules, setState kwargs, "did it take?"
  audit.py         audit.log: one JSON line per write attempt, own rotating file
  views.py         Tool outputs: numbers + UK-formatted times + one-sentence summaries
                   ("tomorrow 02:00" in sentences, full DD/MM/YYYY in fields)
  readings.py      Pure functions: raw Hive JSON -> typed readings (names, availability,
                   batteries, modes, boost, schedule now/next/later and full
                   week, repeats merged, DST-safe)
  models.py        Pydantic reading models (values, aware datetimes)
  snapshot.py      HiveSnapshot (raw products/devices + fetched_at + stale) and LONDON tz
  hive_client.py   HiveClient Protocol, LiveHiveClient (lazy sign-in, cache, stale fallback,
                   write = setState + forced refresh), make_client(settings)
  fake_client.py   FakeHiveClient: serves tests/fixtures/hive_sample.json; records and applies writes
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
- **Setters** (checked 21/10/2026 in `heating.py`, `hotwater.py`): all call `hiveRefreshTokens()`, then `api.setState(product_type, product_id, **kwargs)` (POST `/nodes/{type}/{id}`, **every value sent as a string**), then `getDevices()`. We call `setState` directly with product ids.
  - Heating: `target=t`; `mode=SCHEDULE|MANUAL|OFF`; boost `mode=BOOST, boost=mins, target=t`. Boost off sends `mode=props.previous.mode`, plus `target=previous.target` (default 7) when that mode is MANUAL or OFF.
  - Hot water: `mode=…` passed straight through; boost `mode=BOOST, boost=mins`; boost off `mode=previous.mode`. Hive stores "on" as `MANUAL`, so we send `MANUAL` for ON (**to confirm live**).
  - **TRV zones** (`trvcontrol`) go through the same class with the same kwargs, under their own type. Their `props.previous` is `{}` when not boosting, so cancelling a TRV boost falls back to SCHEDULE if Hive gives no previous mode. TRV writes are untested live.
  - `setState` error handling is poor: 401/403 raise `HiveAuthError`, network errors a bare `HTTPError`, and some failures return `{}` with no `"original"`. `LiveHiveClient.write` catches all of these and treats anything other than 200 as failed.

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
3. ✅ **Claude Desktop registration.** Added the same command to `~/Library/Application Support/Claude/claude_desktop_config.json` under `mcpServers.hive`. Checked in the app after a restart.
4. ✅ **Small polish** (all done 21/10/2026):
   - ✅ (21/10/2026) Expected `ToolError`s are logged below ERROR (unknown zone and sign-in at INFO, Hive unreachable at WARNING), so they stay off stderr.
   - ✅ (21/10/2026) `readings.now_next_later` merges back-to-back slots with the same value, round the week, so now/next/later are real changes. Main Hall's "now" slot can therefore start days ago.
   - ✅ (21/10/2026) Summaries use `views.uk_when`: "22:00", "tomorrow 02:00", "yesterday 21:30", "Fri 09:00" within the coming week, otherwise the full date. The structured `starts`/`ends`/`as_of` fields keep full DD/MM/YYYY dates.
   - ✅ (21/10/2026) `min_temp_c`/`max_temp_c` are gone (they were constants, not read from Hive; the write tools will have their own limits, §6). `controller` is only given when it isn't the zone's own thermostat, i.e. for hot water.
   - ✅ (21/10/2026) `get_schedule(zone)` returns the full weekly schedule for a heating zone or the hot water (`readings.week_days`). Each day runs midnight to midnight, opening with the setting carried over from the day before, with repeats merged. It also says whether the schedule is in use (`in_force`); OFF zones still have a stored schedule.
5. **Write tools (phase 2).** See §6. Go-ahead given 21/10/2026; setters read (§2) and design agreed with the user.
   - ✅ Built and tested against `FakeHiveClient` (21/10/2026).
   - ✅ Live check 21/10/2026 (user approved), on Thermostat 3 at 12°C, below the room's 18.6°C so the boiler never fired: MANUAL 12°C → 15-minute boost at 12°C → cancel (back to MANUAL 12°C) → OFF. Every step confirmed by the fresh reading and audited. **Straight after `mode=OFF`, Hive still reported target 12°C**, and it settled back to 7°C about a minute later. So OFF now also sends `target=frostProtection`, like the library's boost-off. Thermostat 3 ended exactly as found (OFF, 7°C).
   - Still unconfirmed live: hot water ON=MANUAL (the user chose to skip it), TRV zone writes, and the OFF+target form (covered by the tests; it's the same kwargs the library's boost-off sends).
   - Then the user decides whether to turn writes on in `.mcp.json` / Claude Desktop (`"env": {"HIVE_MCP_ALLOW_WRITES": "1"}`).
6. **Upstream:** report the two library bugs (§2) to `Pyhass/Pyhive` after checking 2.0.0b4. Ask the user before posting anything publicly.

## 6. Phase 2: write tools (built 21/10/2026)

From the original brief: limit temperatures, cap boost durations, log every write, and keep writes behind a config flag. Agreed with the user 21/10/2026.

- **Flag:** `HIVE_MCP_ALLOW_WRITES` (`Settings.allow_writes`, default off). While it's off, the write tools **aren't registered at all**. They also aren't registered if the audit log can't be opened.
- **Tools:**
  - `set_target_temperature(zone, temp_c)`
  - `set_heating_mode(zone, SCHEDULE|MANUAL|OFF)`
  - `boost_heating(zone, minutes, temp_c)` / `cancel_heating_boost(zone)`
  - `set_hot_water_mode(SCHEDULE|ON|OFF)`
  - `boost_hot_water(minutes)` / `cancel_hot_water_boost()`
- **Safety limits** (constants, enforced server-side):
  - Target and boost temperatures within **5–25°C**. Hive allows 5–32°C.
  - Boost at most **120 minutes**, minimum 15.
  - Temperatures in 0.5°C steps.
  - Values out of range are rejected, not clamped, and the message gives the limits.
  - Unavailable zones are refused, as is a new boost while one is running ("cancel it first").
- **Mode rules** (agreed):
  - `set_target_temperature` in SCHEDULE is Hive's temporary override until the next slot (the reply says when). It's refused for OFF ("use set_heating_mode") and while boosting.
  - `set_heating_mode` MANUAL **requires** `temp_c` (sent as mode+target in one call); SCHEDULE/OFF take none. Setting a mode ends a boost.
  - No extra confirmation step for OFF/MANUAL: the client's approval prompt is enough. The reply says it stays until someone changes it.
  - Already set that way, or no boost to cancel: nothing is sent (`no_change`).
- **Flow per call** (`server._add_write_tools.perform`): fresh read (`snapshot(refresh=True)`; refused if stale) → `writes.plan_*` → `client.write` (setState under the lock, HTTP 200 check, forced refresh) → `writes.unconfirmed` compares the fresh reading with what was expected → reply with `changed`, `confirmed`, `summary` ("Thermostat 3 changed from OFF to MANUAL at 12°C. …") and `now`.
- **Audit log:** `~/Library/Logs/hive-mcp/audit.log`, JSON lines (1 MB × 10 files), kept out of the main log. Fields: `time` (DD/MM/YYYY HH:MM:SS UK), `tool`, `zone`, `id`, `before`, `requested`, `sent`, `result` (`ok`/`unconfirmed`/`no_change`/`rejected`/`failed`), `reason`, `after`. Refused attempts are logged too.
- **Tool annotations:** `readOnlyHint: false`, `destructiveHint: false`. `idempotentHint` is true for the set-value and cancel tools and false for the boost tools.

## 7. Risks

- **Unofficial API:** Hive may change its Cognito set-up or endpoints. The library pin and narrow adapter limit the damage, but they can't prevent breakage.
- **The 2.0 beta is moving quickly:** 1.0.x may stop being maintained.
- **Device trust:** if Cognito forgets the device, re-login needs `hive-mcp login` again. With SMS 2FA enabled, that would also need a phone.
