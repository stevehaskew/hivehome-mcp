"""Plan writes to Hive: safety limits, mode rules and the setState kwargs. Pure, no I/O.

Each plan_* function checks a request against the current snapshot and returns a
WritePlan (what to send and what to expect afterwards), None when nothing needs
sending, or raises WriteRejected with a reason Claude can relay.

setState kwargs, from apyhiveapi 1.0.10 (heating.py, hotwater.py; TRV zones take
the same as boiler zones, under their own product type):
- target:            target=°C
- heating mode:      mode=SCHEDULE|MANUAL|OFF (we send MANUAL with target=°C)
- heating boost:     mode=BOOST, boost=minutes, target=°C
- hot water mode:    mode=SCHEDULE|MANUAL|OFF (Hive stores "on" as MANUAL)
- hot water boost:   mode=BOOST, boost=minutes
- cancelling boost:  mode=props.previous.mode, plus target=previous.target when
                     going back to MANUAL or OFF
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from . import readings
from .models import HeatingMode, HotWaterMode
from .snapshot import HiveSnapshot
from .views import uk_when

# Hive allows 5–32°C; we stop at 25°C. Boosts are capped at two hours.
MIN_TEMP_C = 5.0
MAX_TEMP_C = 25.0
TEMP_STEP_C = 0.5
MIN_BOOST_MINUTES = 15
MAX_BOOST_MINUTES = 120


class WriteRejected(ValueError):
    """The request breaks a safety limit or doesn't make sense for the zone's state."""


@dataclass(frozen=True)
class WritePlan:
    product_type: str
    product_id: str
    kwargs: dict[str, Any]
    change: str  # e.g. "Thermostat 3 target changed from 7°C to 12°C"
    expect: dict[str, Any] = field(default_factory=dict)  # Settings to see after the refresh
    note: str | None = None  # Anything Claude should pass on (e.g. "until 22:00")


# --- current settings -------------------------------------------------------------


def settings_of(snap: HiveSnapshot, product_id: str) -> dict[str, Any]:
    """The values a write can change, for the audit log and for checking a write took."""
    product = snap.products[product_id]
    state = product.get("state", {})
    raw_mode = state.get("mode")
    boosting = _boosting(product)
    out: dict[str, Any] = {
        "mode": (product.get("props", {}).get("previous") or {}).get("mode")
        if boosting
        else raw_mode,
        "boost_minutes": _int(state.get("boost")) if boosting else None,
    }
    if product.get("type") in readings.HOT_WATER_TYPES:
        out["hot_water_on"] = state.get("status") == "ON" if state.get("status") else None
    else:
        out["target_c"] = _temp(state.get("target"))
    return out


def _boosting(product: dict[str, Any]) -> bool:
    return bool(product.get("state", {}).get("mode") == "BOOST")


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _temp(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _c(value: float | None) -> str:
    return "unknown" if value is None else f"{value:g}°C"


def _hive_number(value: float) -> int | float:
    """21.0 -> 21: setState sends str(value), and Hive's own app sends whole numbers."""
    return int(value) if value.is_integer() else value


# --- checks -----------------------------------------------------------------------


def check_temperature(temp_c: float) -> float:
    if not math.isfinite(temp_c) or not MIN_TEMP_C <= temp_c <= MAX_TEMP_C:
        raise WriteRejected(
            f"{temp_c:g}°C is outside the allowed range of {MIN_TEMP_C:g}–{MAX_TEMP_C:g}°C."
        )
    if not (temp_c / TEMP_STEP_C).is_integer():
        raise WriteRejected(
            f"Temperatures go in steps of {TEMP_STEP_C:g}°C; {temp_c:g}°C isn't one."
        )
    return float(temp_c)


def check_minutes(minutes: int) -> int:
    if not MIN_BOOST_MINUTES <= minutes <= MAX_BOOST_MINUTES:
        raise WriteRejected(
            f"A boost of {minutes} minutes is outside the allowed range of "
            f"{MIN_BOOST_MINUTES}–{MAX_BOOST_MINUTES} minutes."
        )
    return minutes


def _require_available(snap: HiveSnapshot, product_id: str) -> tuple[str, dict[str, Any]]:
    name = readings.device_name(snap, product_id)
    available, problem = readings.availability(snap, product_id)
    if not available:
        raise WriteRejected(
            f"{name} is unavailable ({problem or 'no reason given'}), so it can't be changed."
        )
    return name, snap.products[product_id]


# --- heating ----------------------------------------------------------------------


def plan_target(snap: HiveSnapshot, product_id: str, temp_c: float) -> WritePlan | None:
    temp_c = check_temperature(temp_c)
    name, product = _require_available(snap, product_id)
    now = settings_of(snap, product_id)
    if _boosting(product):
        raise WriteRejected(
            f"{name} is boosting. Cancel the boost first (cancel_heating_boost), "
            "or start a new one after it."
        )
    if now["mode"] == "OFF":
        raise WriteRejected(
            f"{name} is switched off. To heat it, use set_heating_mode with mode MANUAL "
            "and a temperature (or SCHEDULE)."
        )
    if now["target_c"] == temp_c:
        return None
    note = None
    if now["mode"] == "SCHEDULE":
        note = "This overrides the schedule until its next change"
        status = readings.heating_status(snap, product_id)
        if status.schedule is not None:
            note += f" at {uk_when(status.schedule.next.starts, snap.fetched_at)}"
        note += "."
    return WritePlan(
        product["type"],
        product_id,
        {"target": _hive_number(temp_c)},
        f"{name} target changed from {_c(now['target_c'])} to {_c(temp_c)}",
        expect={"target_c": temp_c},
        note=note,
    )


def plan_heating_mode(
    snap: HiveSnapshot, product_id: str, mode: HeatingMode, temp_c: float | None
) -> WritePlan | None:
    if mode == "MANUAL":
        if temp_c is None:
            raise WriteRejected("MANUAL needs a temperature (temp_c).")
        temp_c = check_temperature(temp_c)
    elif temp_c is not None:
        raise WriteRejected(f"Don't give a temperature for {mode}; it only applies to MANUAL.")
    name, product = _require_available(snap, product_id)
    now = settings_of(snap, product_id)
    boosting = _boosting(product)
    # Straight after a plain mode=OFF, Hive still reports the old target; it settles to
    # the frost setting a minute or so later (seen live 21/10/2026). So OFF also sends the
    # frost-protection temperature, as the library's boost-off does.
    target = temp_c
    if mode == "OFF":
        target = _temp(product.get("state", {}).get("frostProtection"))
    if not boosting and now["mode"] == mode and (target is None or now["target_c"] == target):
        return None

    kwargs: dict[str, Any] = {"mode": mode}
    expect: dict[str, Any] = {"mode": mode, "boost_minutes": None}
    if target is not None:
        kwargs["target"] = _hive_number(target)
        expect["target_c"] = target
    before = _heating_mode_text(now["mode"], now["target_c"])
    after = f"MANUAL at {_c(temp_c)}" if mode == "MANUAL" else mode
    change = f"{name} changed from {before} to {after}"
    if now["mode"] == mode == "OFF" and not boosting:
        change = (
            f"{name} stays OFF; its stored target goes from {_c(now['target_c'])} "
            f"back to the frost-protection {_c(target)}"
        )
    if boosting:
        change += " (the boost was cancelled)"
    note = {
        "MANUAL": "It stays at this temperature until someone changes it.",
        "OFF": "It stays off (frost protection only) until someone changes it.",
    }.get(mode)
    return WritePlan(product["type"], product_id, kwargs, change, expect=expect, note=note)


def _heating_mode_text(mode: str | None, target: float | None) -> str:
    if mode == "MANUAL":
        return f"MANUAL at {_c(target)}"
    return mode or "an unknown mode"


def plan_heating_boost(
    snap: HiveSnapshot, product_id: str, minutes: int, temp_c: float
) -> WritePlan:
    minutes, temp_c = check_minutes(minutes), check_temperature(temp_c)
    name, product = _require_available(snap, product_id)
    now = settings_of(snap, product_id)
    if _boosting(product):
        left = f" ({now['boost_minutes']} minutes left)" if now["boost_minutes"] else ""
        raise WriteRejected(
            f"{name} is already boosting{left}. Cancel it first to start a new one."
        )
    return WritePlan(
        product["type"],
        product_id,
        {"mode": "BOOST", "boost": minutes, "target": _hive_number(temp_c)},
        f"{name} boosting to {_c(temp_c)} for {minutes} minutes",
        expect={"target_c": temp_c, "boosting": True},
        note=f"Afterwards it goes back to {_heating_mode_text(now['mode'], now['target_c'])}.",
    )


def plan_cancel_heating_boost(snap: HiveSnapshot, product_id: str) -> WritePlan | None:
    name, product = _require_available(snap, product_id)
    if not _boosting(product):
        return None
    previous = product.get("props", {}).get("previous") or {}
    mode, target = previous.get("mode"), _temp(previous.get("target"))
    note = None
    if mode == "MANUAL" and target is None:
        mode, note = (
            "SCHEDULE",
            "Hive didn't say what the manual temperature was, so it's back on its schedule.",
        )
    elif mode not in ("SCHEDULE", "MANUAL", "OFF"):
        mode, note = (
            "SCHEDULE",
            "Hive didn't say what mode it was in before, so it's back on its schedule.",
        )
    kwargs: dict[str, Any] = {"mode": mode}
    expect: dict[str, Any] = {"mode": mode, "boost_minutes": None, "boosting": False}
    if mode in ("MANUAL", "OFF") and target is not None:
        kwargs["target"] = _hive_number(target)
        expect["target_c"] = target
    after = _heating_mode_text(mode, target)
    return WritePlan(
        product["type"],
        product_id,
        kwargs,
        f"{name} boost cancelled, back to {after}",
        expect,
        note,
    )


# --- hot water --------------------------------------------------------------------

_HIVE_HOT_WATER_MODE = {"SCHEDULE": "SCHEDULE", "ON": "MANUAL", "OFF": "OFF"}
_OUR_HOT_WATER_MODE = {v: k for k, v in _HIVE_HOT_WATER_MODE.items()}


def plan_hot_water_mode(
    snap: HiveSnapshot, product_id: str, mode: HotWaterMode
) -> WritePlan | None:
    name, product = _require_available(snap, product_id)
    now = settings_of(snap, product_id)
    hive_mode = _HIVE_HOT_WATER_MODE[mode]
    boosting = _boosting(product)
    if not boosting and now["mode"] == hive_mode:
        return None
    before = _OUR_HOT_WATER_MODE.get(now["mode"] or "", now["mode"] or "an unknown mode")
    change = f"{name} changed from {before} to {mode}"
    if boosting:
        change += " (the boost was cancelled)"
    note = {
        "ON": "It stays on until someone changes it.",
        "OFF": "It stays off until someone changes it.",
    }.get(mode)
    return WritePlan(
        product["type"],
        product_id,
        {"mode": hive_mode},
        change,
        expect={"mode": hive_mode, "boost_minutes": None},
        note=note,
    )


def plan_hot_water_boost(snap: HiveSnapshot, product_id: str, minutes: int) -> WritePlan:
    minutes = check_minutes(minutes)
    name, product = _require_available(snap, product_id)
    now = settings_of(snap, product_id)
    if _boosting(product):
        left = f" ({now['boost_minutes']} minutes left)" if now["boost_minutes"] else ""
        raise WriteRejected(
            f"{name} is already boosting{left}. Cancel it first to start a new one."
        )
    before = _OUR_HOT_WATER_MODE.get(now["mode"] or "", now["mode"] or "an unknown mode")
    return WritePlan(
        product["type"],
        product_id,
        {"mode": "BOOST", "boost": minutes},
        f"{name} boosting for {minutes} minutes",
        expect={"boosting": True},
        note=f"Afterwards it goes back to {before}.",
    )


def plan_cancel_hot_water_boost(snap: HiveSnapshot, product_id: str) -> WritePlan | None:
    name, product = _require_available(snap, product_id)
    if not _boosting(product):
        return None
    mode = (product.get("props", {}).get("previous") or {}).get("mode")
    note = None
    if mode not in _OUR_HOT_WATER_MODE:
        mode, note = (
            "SCHEDULE",
            "Hive didn't say what mode it was in before, so it's back on its schedule.",
        )
    return WritePlan(
        product["type"],
        product_id,
        {"mode": mode},
        f"{name} boost cancelled, back to {_OUR_HOT_WATER_MODE[mode]}",
        expect={"mode": mode, "boost_minutes": None, "boosting": False},
        note=note,
    )


# --- after the write ----------------------------------------------------------------


def unconfirmed(plan: WritePlan, snap: HiveSnapshot) -> list[str]:
    """Expected settings the refreshed snapshot doesn't show (empty if the write took)."""
    after = settings_of(snap, plan.product_id)
    boosting = _boosting(snap.products[plan.product_id])
    missing = []
    for key, want in plan.expect.items():
        have = boosting if key == "boosting" else after.get(key)
        if have != want:
            missing.append(f"{key} is {have!r}, expected {want!r}")
    return missing
