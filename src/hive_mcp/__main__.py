"""Command line: `hive-mcp serve | login | logout | status | show`.

`serve` runs the MCP server over stdio (Claude starts it); the rest are for a terminal.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import logging
import os
import socket
import sys
from dataclasses import replace
from datetime import datetime

from .auth import (
    LoginFailed,
    ManagedHive,
    ReauthRequired,
    device_login,
    forget_device,
    interactive_login,
)
from .config import Settings
from .credentials import Credentials, CredentialsMissing, CredentialStore, Tokens
from .hive_client import HiveUnavailable, make_client
from .logging_setup import configure_logging
from .models import HeatingStatus, HotWaterStatus, ScheduleSlot
from .readings import UnknownDevice, find_device, heating_status, hot_water_status, list_devices

_LOGGER = logging.getLogger("hive_mcp.cli")


def _say(text: str = "") -> None:
    print(text, flush=True)


def _uk_time(iso: str | None) -> str:
    if not iso:
        return "unknown"
    return datetime.fromisoformat(iso).strftime("%d/%m/%Y %H:%M")


# --- login -------------------------------------------------------------------


async def cmd_login(store: CredentialStore, args: argparse.Namespace) -> int:
    existing = store.load()
    default_user = existing.username if existing else ""
    prompt = f"Hive email [{default_user}]: " if default_user else "Hive email: "
    username = input(prompt).strip() or default_user  # noqa: ASYNC250 - nothing else running yet
    if not username:
        _say("An email address is needed.")
        return 2
    password = getpass.getpass("Hive password: ")
    device_name = args.device_name or f"hive-mcp ({socket.gethostname()})"

    _say("Signing in to Hive…")
    try:
        outcome = await interactive_login(
            username, password, device_name, lambda: input("Enter the SMS code Hive sent you: ")
        )
    except LoginFailed as err:
        _say(f"Login failed: {err}")
        return 1

    if existing and existing.device_data and existing.device_data.key != outcome.device_data.key:
        _say("Removing the previously registered device…")
        await forget_device(existing)

    creds = store.save(
        Credentials(
            username=username,
            password=password,
            device_data=outcome.device_data,
            tokens=outcome.tokens,
            device_name=device_name,
        )
    )
    _say(f"Signed in{' with SMS code' if outcome.sms_used else ''}.")
    _say(f"Registered this Mac as {device_name!r} (device …{outcome.device_data.key[-6:]}).")
    _say(f"Saved to the keychain under service {store.service!r}.")

    if args.no_verify:
        return 0
    _say("Checking that Hive accepts the device keys without a password prompt or SMS…")
    try:
        result = await device_login(creds.username, creds.password, outcome.device_data)
    except ReauthRequired as err:
        _say(f"Warning: unattended login does not work yet: {err}")
        return 1
    store.update_tokens(Tokens.from_auth_result(result))
    _say("OK: unattended re-login works.")
    return 0


# --- logout ------------------------------------------------------------------


async def cmd_logout(store: CredentialStore, args: argparse.Namespace) -> int:
    creds = store.load()
    if creds is None or creds.device_data is None:
        removed = store.clear()
        _say("Nothing to sign out of." if not removed else "Keychain entry removed.")
        return 0
    if not args.local_only:
        _say("Deregistering this device with Hive…")
        if await forget_device(creds):
            _say("Device deregistered.")
        else:
            _say("Couldn't deregister the device (see log); removing local credentials anyway.")
    store.clear()
    _say("Keychain entry removed.")
    return 0


# --- status ------------------------------------------------------------------


async def cmd_status(store: CredentialStore, args: argparse.Namespace) -> int:
    creds = store.load()
    if creds is None:
        _say("Not signed in. Run `hive-mcp login`.")
        return 1
    _say(f"Account     : {creds.username}")
    _say(
        f"Device      : {creds.device_name or 'unnamed'} "
        f"({'…' + creds.device_data.key[-6:] if creds.device_data else 'not registered'})"
    )
    _say(f"Tokens      : {'stored' if creds.tokens else 'none'}")
    _say(f"Last saved  : {_uk_time(creds.saved_at)}")
    if creds.device_data is None:
        _say("No device keys stored. Run `hive-mcp login`.")
        return 1
    if args.offline:
        return 0

    if args.simulate_expiry:
        if creds.tokens is None:
            _say("No stored tokens to expire; a normal check already forces a device login.")
        else:
            _say("Simulating an expired refresh token (the stored one is left alone)…")
            creds = replace(creds, tokens=replace(creds.tokens, refreshToken="expired-for-test"))

    _say("Connecting to Hive…")
    hive = ManagedHive(creds, on_new_tokens=store.update_tokens)
    try:
        devices = await asyncio.wait_for(hive.start(), timeout=60)
    except ReauthRequired as err:
        _say(f"Hive needs you to sign in again: {err}")
        _say("Run `hive-mcp login`.")
        return 1
    except TimeoutError:
        _say("Hive didn't respond within 60 seconds.")
        return 1
    finally:
        await hive.close()

    climate = devices.get("climate", [])
    water = devices.get("water_heater", [])
    _say(f"Connected   : {len(climate)} heating zone(s), {len(water)} hot water")
    for device in climate:
        _say(f"  - {device.get('haName')}")
    if hive.relogin_count:
        _say(f"Re-login    : {hive.relogin_count} device login(s) without SMS; new tokens saved")
    if args.simulate_expiry and not hive.relogin_count:
        _say("Warning: expected a device re-login but none happened.")
        return 1
    return 0


# --- show --------------------------------------------------------------------


def _slot(slot: ScheduleSlot) -> str:
    value = (
        f"{slot.target_c:g}°C"
        if slot.target_c is not None
        else ("on" if slot.hot_water_on else "off")
    )
    return f"{value} {slot.starts:%a %H:%M}–{slot.ends:%a %H:%M}"


def _describe(st: HeatingStatus | HotWaterStatus) -> str:
    if not st.available:
        return f"{st.name}: unavailable ({st.problem})"
    if isinstance(st, HeatingStatus):
        mode: str = st.mode or "?"
        if st.mode == "OFF":
            mode = f"OFF (frost protection {st.frost_protection_c:g}°C)"
        line = (
            f"{st.name}: {st.current_temp_c}°C, target {st.target_temp_c:g}°C, {mode}, "
            f"{'heating' if st.heating_on else 'idle'}"
        )
        if st.battery_pct is not None:
            line += f", battery {st.battery_pct}%"
    else:
        line = f"{st.name}: {'on' if st.is_on else 'off'}, {st.mode}"
    if st.boost.active:
        line += f", BOOST {st.boost.minutes_remaining} min left"
    if st.schedule:
        s = st.schedule
        line += f"\n    now {_slot(s.now)} · next {_slot(s.next)} · later {_slot(s.later)}"
    return line


async def cmd_show(store: CredentialStore, args: argparse.Namespace) -> int:
    settings = Settings.from_env()
    client = make_client(settings)
    try:
        snap = await client.snapshot()
        statuses: list[HeatingStatus | HotWaterStatus] = []
        if args.device and args.type == "hot_water":
            statuses.append(hot_water_status(snap, find_device(snap, args.device, "hot_water")))
        elif args.device:
            statuses.append(heating_status(snap, find_device(snap, args.device, "heating")))
        else:
            for d in list_devices(snap):
                getter = heating_status if d.type == "heating" else hot_water_status
                statuses.append(getter(snap, d.id))
    except (CredentialsMissing, ReauthRequired) as err:
        _say(f"{err}")
        return 1
    except (UnknownDevice, HiveUnavailable) as err:
        _say(str(err))
        return 1
    finally:
        await client.close()

    if args.json:
        import json

        _say(json.dumps([st.model_dump(mode="json") for st in statuses], indent=2))
        return 0
    source = "fake data" if settings.fake else "Hive"
    stale = " (STALE: Hive unreachable)" if snap.stale else ""
    _say(f"From {source} at {snap.fetched_at:%d/%m/%Y %H:%M}{stale}")
    for st in statuses:
        _say(_describe(st))
    return 0


# --- serve -------------------------------------------------------------------


def run_serve(settings: Settings) -> int:
    from .server import serve

    serve(settings)
    return 0


# --- entry point -------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hive-mcp", description="Hive heating MCP server")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logs to stderr")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("serve", help="run the MCP server over stdio (Claude starts this)")

    login = sub.add_parser("login", help="sign in and register this Mac as a trusted device")
    login.add_argument("--device-name", help="name shown in Hive/Cognito for this device")
    login.add_argument("--no-verify", action="store_true", help="skip the unattended login check")

    logout = sub.add_parser("logout", help="deregister this device and wipe stored credentials")
    logout.add_argument("--local-only", action="store_true", help="don't contact Hive")

    status = sub.add_parser("status", help="show stored credentials and test the connection")
    status.add_argument("--offline", action="store_true", help="don't contact Hive")
    status.add_argument(
        "--simulate-expiry",
        action="store_true",
        help="pretend the refresh token has expired, to test unattended re-login",
    )

    show = sub.add_parser("show", help="print heating and hot water readings")
    show.add_argument("device", nargs="?", help="zone name or id (default: everything)")
    show.add_argument("--type", choices=("heating", "hot_water"), default="heating")
    show.add_argument("--json", action="store_true", help="print the full typed readings")
    return parser


COMMANDS = {"login": cmd_login, "logout": cmd_logout, "status": cmd_status, "show": cmd_show}


def main(argv: list[str] | None = None) -> int:
    # FastMCP otherwise installs its own (rich) stderr handler at import; let its
    # records flow through ours so they reach the log file too.
    os.environ.setdefault("FASTMCP_LOG_ENABLED", "false")
    os.environ.setdefault("FASTMCP_SHOW_SERVER_BANNER", "false")
    args = build_parser().parse_args(argv)
    settings = Settings.from_env()
    configure_logging(settings, verbose=args.verbose)
    if args.command == "serve":
        return run_serve(settings)
    store = CredentialStore(settings.keychain_service)
    try:
        return asyncio.run(COMMANDS[args.command](store, args))
    except KeyboardInterrupt:
        _say("\nCancelled.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
