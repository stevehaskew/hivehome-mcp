"""Step 0 spike: prove the Hive login, SMS 2FA and device-login flow works.

Throwaway script. Run it interactively in a terminal:

    uv run python spike/login_spike.py            # full run (asks for SMS code)
    uv run python spike/login_spike.py --reuse    # skip phase A, reuse saved device
    uv run python spike/login_spike.py --forget   # deregister device + wipe Keychain

Phases:
  A. Password login + SMS code + device registration -> device keys
  B. Fresh Auth using the saved device keys -> tokens WITHOUT an SMS code?
  C. Start a session from saved refresh token only (simulates a server restart)
  D. The library's own re-login path (what happens when the refresh token dies)
  E. Read heating / hot water and dump an anonymised fixture

Secrets live in the macOS Keychain (service "hive-mcp-spike"), never on disk.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import logging
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import keyring

_saved_excepthook = sys.excepthook
from apyhiveapi import Auth, Hive  # noqa: E402
from apyhiveapi.helper import hive_exceptions as hx  # noqa: E402

# apyhiveapi.hive replaces sys.excepthook with one that logs frame locals
# (which can include the password). Put the default back.
sys.excepthook = _saved_excepthook

SERVICE = "hive-mcp-spike"
OUT_DIR = Path(__file__).parent / "out"
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
SENSITIVE_KEYS = {
    "email", "username", "firstName", "lastName", "phone", "mobile", "postcode",
    "address", "addressLine1", "addressLine2", "city", "latitude", "longitude",
    "macAddress", "mac", "serialNumber", "ipAddress", "ssid",
}

results: dict[str, str] = {}


def banner(text: str) -> None:
    print(f"\n=== {text} ===", flush=True)


def record(phase: str, outcome: str) -> None:
    results[phase] = outcome
    print(f"-> {phase}: {outcome}", flush=True)


def kc_get(key: str) -> Any:
    raw = keyring.get_password(SERVICE, key)
    return json.loads(raw) if raw else None


def kc_set(key: str, value: Any) -> None:
    keyring.set_password(SERVICE, key, json.dumps(value))


def kc_delete(key: str) -> None:
    try:
        keyring.delete_password(SERVICE, key)
    except keyring.errors.PasswordDeleteError:
        pass


def token_tail(tok: str | None) -> str:
    return f"len={len(tok)} …{tok[-4:]}" if tok else "none"


def describe(result: dict | None) -> str:
    if not result:
        return "empty result"
    if "AuthenticationResult" in result:
        ar = result["AuthenticationResult"]
        extra = " + NewDeviceMetadata" if "NewDeviceMetadata" in ar else ""
        return f"tokens (ExpiresIn={ar.get('ExpiresIn')}s, refresh={'RefreshToken' in ar}){extra}"
    return f"challenge {result.get('ChallengeName')}"


def session_token_dict(hive: Hive) -> dict[str, str]:
    td = hive.tokens.tokenData
    return {"token": td["token"], "refreshToken": td["refreshToken"], "accessToken": td["accessToken"]}


# --- Phase A -----------------------------------------------------------------

async def phase_a(username: str, password: str) -> None:
    banner("A. Password login + SMS 2FA + device registration")
    auth = Auth(username=username, password=password)
    result = await auth.login()
    print("login():", describe(result))

    if result and result.get("ChallengeName") == Auth.SMS_MFA_CHALLENGE:
        code = input("Enter the SMS code Hive just sent you: ").strip()
        result = await auth.sms_2fa(code, result)
        print("sms_2fa():", describe(result))
        record("A.sms", "SMS 2FA required and accepted")
    elif result and "AuthenticationResult" in result:
        record("A.sms", "no SMS needed (2FA not enabled on this account?)")
    else:
        record("A.sms", f"UNEXPECTED: {describe(result)}")
        raise SystemExit(1)

    if not auth.device_key:
        record("A.register", "FAILED: Cognito returned no NewDeviceMetadata, nothing to register")
        raise SystemExit(1)

    await auth.device_registration(f"hive-mcp spike ({os.uname().nodename})")
    group_key, device_key, device_password = await auth.get_device_data()
    registered = await auth.is_device_registered()
    record("A.register", f"device_key …{device_key[-6:]}, remembered={registered}")

    ar = result["AuthenticationResult"]
    kc_set("username", username)
    kc_set("password", password)
    kc_set("device_data", [group_key, device_key, device_password])
    kc_set("tokens", {"token": ar["IdToken"], "refreshToken": ar["RefreshToken"], "accessToken": ar["AccessToken"]})
    kc_set("saved_at", datetime.now().isoformat())
    print("Saved username, password, device_data and tokens to the Keychain.")


# --- Phase B -----------------------------------------------------------------

async def phase_b(username: str, password: str, device_data: list[str]) -> dict | None:
    banner("B. Fresh login using saved device keys (should need NO SMS)")
    auth = Auth(username, password, *device_data)
    result = await auth.login()
    print("login():", describe(result))

    if result and result.get("ChallengeName") == Auth.DEVICE_VERIFIER_CHALLENGE:
        result = await auth.device_login()
        print("device_login():", describe(result))
        if result and result.get("ChallengeName") == Auth.SMS_MFA_CHALLENGE:
            record("B.device_login", "FAILED: Cognito still asked for SMS, device not remembered")
            return None
        if result and "AuthenticationResult" in result:
            record("B.device_login", "OK: tokens obtained with no SMS")
            ar = result["AuthenticationResult"]
            kc_set("tokens", {"token": ar["IdToken"], "refreshToken": ar["RefreshToken"], "accessToken": ar["AccessToken"]})
            return result
    elif result and result.get("ChallengeName") == Auth.SMS_MFA_CHALLENGE:
        record("B.device_login", "FAILED: plain login asked for SMS despite device key")
        return None
    elif result and "AuthenticationResult" in result:
        record("B.device_login", "OK: tokens directly (no device challenge)")
        return result

    record("B.device_login", f"UNEXPECTED: {describe(result)}")
    return None


# --- Phase C -----------------------------------------------------------------

async def phase_c(username: str, password: str, device_data: list[str], tokens: dict) -> Hive | None:
    banner("C. Restart simulation: start session from saved tokens only")
    hive = Hive(username=username, password=password)
    before = tokens["accessToken"]
    try:
        await hive.startSession(
            {"username": username, "password": password, "device_data": device_data, "tokens": tokens}
        )
    except Exception as err:  # spike: report anything
        record("C.restart", f"FAILED: {type(err).__name__}: {err}")
        return None

    after = hive.tokens.tokenData.get("accessToken")
    refreshed = "refreshed on start" if after != before else "NOT refreshed (used as-is)"
    record("C.restart", f"OK: session started, access token {refreshed} ({token_tail(after)})")

    kc_set("tokens", session_token_dict(hive))
    print("refreshToken unchanged after refresh:", hive.tokens.tokenData["refreshToken"] == tokens["refreshToken"])
    return hive


# --- Phase D -----------------------------------------------------------------

async def phase_d(hive: Hive) -> None:
    banner("D. Library's own re-login path (used when refresh token has expired)")
    print("auth.access_token set on session Auth:", bool(hive.auth.access_token))
    try:
        result = await hive.login()
        record("D.relogin", f"OK: {describe(result)}")
    except hx.HiveInvalidDeviceAuthentication:
        record(
            "D.relogin",
            "FAILED: HiveInvalidDeviceAuthentication. is_device_registered() had no access token, "
            "so the library won't recover by itself; our wrapper must call device_login()",
        )
    except hx.HiveReauthRequired:
        record("D.relogin", "FAILED: HiveReauthRequired (SMS needed again)")
    except Exception as err:
        record("D.relogin", f"FAILED: {type(err).__name__}: {err}")


# --- Phase E -----------------------------------------------------------------

def anonymise(obj: Any, aliases: dict[str, str]) -> Any:
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            key = aliases.setdefault(k, f"id-{len(aliases) + 1}") if UUID_RE.match(k) else k
            out[key] = "<redacted>" if k in SENSITIVE_KEYS else anonymise(v, aliases)
        return out
    if isinstance(obj, list):
        return [anonymise(v, aliases) for v in obj]
    if isinstance(obj, str) and UUID_RE.match(obj):
        return aliases.setdefault(obj, f"id-{len(aliases) + 1}")
    return obj


async def phase_e(hive: Hive) -> None:
    banner("E. Read heating / hot water")
    for kind in ("climate", "water_heater"):
        for device in hive.deviceList.get(kind, []):
            print(f"\n[{kind}] {device.get('haName')}  hiveID={device.get('hiveID')}")
            try:
                if kind == "climate":
                    h = hive.heating
                    print("  current  :", await h.getCurrentTemperature(device))
                    print("  target   :", await h.getTargetTemperature(device))
                    print("  mode     :", await h.getMode(device))
                    print("  state    :", await h.getState(device))
                    print("  operation:", await h.getCurrentOperation(device))
                    print("  boost    :", await h.getBoostStatus(device), await h.getBoostTime(device))
                    print("  min/max  :", await h.getMinTemperature(device), await h.getMaxTemperature(device))
                    print("  schedule :", await h.getScheduleNowNextLater(device))
                else:
                    w = hive.hotwater
                    print("  mode     :", await w.getMode(device))
                    print("  state    :", await w.getState(device))
                    print("  boost    :", await w.getBoost(device), await w.getBoostTime(device))
                    print("  schedule :", await w.getScheduleNowNextLater(device))
            except Exception as err:
                print(f"  ERROR {type(err).__name__}: {err}")
    counts = {k: len(v) for k, v in hive.deviceList.items()}
    record("E.read", f"deviceList counts {counts}")

    OUT_DIR.mkdir(exist_ok=True)
    aliases: dict[str, str] = {}
    dump = {
        "products": anonymise(dict(hive.data.products), aliases),
        "devices": anonymise(dict(hive.data.devices), aliases),
        "deviceList": anonymise({k: list(v) for k, v in hive.deviceList.items()}, aliases),
    }
    path = OUT_DIR / "devices_anon.json"
    path.write_text(json.dumps(dump, indent=2, default=str))
    print(f"\nAnonymised dump written to {path}. Review it before using it as a fixture.")


# --- Main --------------------------------------------------------------------

async def main(args: argparse.Namespace) -> None:
    if args.forget:
        data, tokens = kc_get("device_data"), kc_get("tokens")
        if data and tokens:
            auth = Auth(kc_get("username"), kc_get("password"), *data)
            try:
                await auth.forget_device(tokens["accessToken"], data[1])
                print("Device forgotten in Cognito.")
            except Exception as err:
                print(f"forget_device failed ({type(err).__name__}); wiping Keychain anyway.")
        for key in ("username", "password", "device_data", "tokens", "saved_at"):
            kc_delete(key)
        print("Keychain entries removed.")
        return

    if args.reuse:
        username, password = kc_get("username"), kc_get("password")
        if not (username and password and kc_get("device_data")):
            raise SystemExit("Nothing saved yet. Run without --reuse first.")
        print(f"Reusing saved device from {kc_get('saved_at')}")
    else:
        username = os.environ.get("HIVE_USERNAME") or input("Hive email: ").strip()
        password = os.environ.get("HIVE_PASSWORD") or getpass.getpass("Hive password: ")
        await phase_a(username, password)

    device_data = kc_get("device_data")
    await phase_b(username, password, device_data)

    hive = await phase_c(username, password, device_data, kc_get("tokens"))
    if hive is not None:
        await phase_d(hive)
        await phase_e(hive)

    banner("Summary")
    for phase, outcome in results.items():
        print(f"{phase:16} {outcome}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--reuse", action="store_true", help="skip phase A and use the saved device")
    parser.add_argument("--forget", action="store_true", help="deregister the device and wipe the Keychain")
    parser.add_argument("--debug", action="store_true", help="library debug logs to stderr (may include username)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.WARNING, stream=sys.stderr)
    asyncio.run(main(args))
