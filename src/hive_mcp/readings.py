"""Interpret raw Hive JSON into typed readings. Pure functions, no I/O.

Raw shapes (from the sign-in spike and live runs):
- products: "heating" (boiler zone; parent = boiler module, whose props.tui is the
  thermostat), "trvcontrol" (TRV zone; props.trvs lists its TRVs), "hotwater".
  A product Hive can't describe carries "error" and almost no state.
- state.mode is SCHEDULE / MANUAL / OFF / BOOST; during a boost the underlying
  mode is in props.previous.mode and state.boost holds minutes remaining.
- state.schedule maps monday..sunday to [{"start": minutes past midnight,
  "value": {"target": °C} | {"status": "ON"|"OFF"}}], in UK local time.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any, Literal

from .models import (
    Boost,
    DaySchedule,
    DeviceSummary,
    HeatingMode,
    HeatingStatus,
    HotWaterMode,
    HotWaterStatus,
    ScheduleNowNextLater,
    ScheduleSlot,
    WeeklySchedule,
    WeeklySlot,
    ZoneKind,
)
from .snapshot import LONDON, HiveSnapshot

HEATING_TYPES = {"heating": "boiler_zone", "trvcontrol": "trv_zone"}
HOT_WATER_TYPES = {"hotwater"}
DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
MINUTES_PER_WEEK = 7 * 24 * 60

DeviceType = Literal["heating", "hot_water"]


class UnknownDevice(LookupError):
    """No heating zone or hot water matches the id or name given."""


# --- lookup ----------------------------------------------------------------------


def _product_type(product: dict[str, Any]) -> DeviceType | None:
    if product.get("type") in HEATING_TYPES:
        return "heating"
    if product.get("type") in HOT_WATER_TYPES:
        return "hot_water"
    return None


def find_device(snap: HiveSnapshot, key: str | None, wanted: DeviceType | None) -> str:
    """Resolve an id or (case-insensitive) name to a product id.

    `wanted=None` accepts heating zones and hot water alike. With no key, succeeds
    only when there is exactly one device of that type.
    """
    candidates = {
        pid: device_name(snap, pid)
        for pid, product in snap.products.items()
        if (kind := _product_type(product)) is not None and wanted in (None, kind)
    }
    if key is None:
        if len(candidates) == 1:
            return next(iter(candidates))
        raise UnknownDevice(f"Several {_noun(wanted)}s; say which: {_choices(candidates)}.")
    if key in candidates:
        return key
    matches = [pid for pid, name in candidates.items() if name.casefold() == key.casefold()]
    if len(matches) == 1:
        return matches[0]
    raise UnknownDevice(f"No {_noun(wanted)} called {key!r}. Choose from: {_choices(candidates)}.")


def _noun(wanted: DeviceType | None) -> str:
    if wanted is None:
        return "heating zone or hot water system"
    return "heating zone" if wanted == "heating" else "hot water system"


def _choices(candidates: dict[str, str]) -> str:
    return ", ".join(sorted(candidates.values())) or "none found"


# --- names, health, batteries ------------------------------------------------------


def _device(snap: HiveSnapshot, device_id: Any) -> dict[str, Any] | None:
    return snap.devices.get(device_id) if isinstance(device_id, str) else None


def _thermostat(snap: HiveSnapshot, product: dict[str, Any]) -> dict[str, Any] | None:
    boiler = _device(snap, product.get("parent"))
    return _device(snap, boiler.get("props", {}).get("tui")) if boiler else None


def _related_devices(
    snap: HiveSnapshot, product: dict[str, Any]
) -> list[tuple[str, dict[str, Any]]]:
    """(label, device) pairs for the physical devices behind a product."""
    if product.get("type") == "trvcontrol":
        trvs = product.get("props", {}).get("trvs", [])
        return [("TRV", d) for d in (_device(snap, t) for t in trvs) if d]
    related = []
    if boiler := _device(snap, product.get("parent")):
        related.append(("boiler module", boiler))
    if (tui := _thermostat(snap, product)) and product.get("type") == "heating":
        related.append(("thermostat", tui))
    return related


def _name_of(node: dict[str, Any] | None) -> str | None:
    name = (node or {}).get("state", {}).get("name")
    return name if isinstance(name, str) and name else None


def controller_name(snap: HiveSnapshot, product_id: str) -> str | None:
    product = snap.products[product_id]
    if product.get("type") == "trvcontrol":
        return None
    return _name_of(_thermostat(snap, product))


def device_name(snap: HiveSnapshot, product_id: str) -> str:
    """Boiler zones take the thermostat's name (Hive names the zone itself "Zone 1")."""
    product = snap.products[product_id]
    if product.get("type") in HOT_WATER_TYPES:
        return "Hot water"
    if product.get("type") == "heating" and (name := controller_name(snap, product_id)):
        return name
    return _name_of(product) or product_id


def availability(snap: HiveSnapshot, product_id: str) -> tuple[bool, str | None]:
    product = snap.products[product_id]
    problems = []
    offline = [label for label, d in _related_devices(snap, product) if not _online(d)]
    if offline:
        problems.append(f"{_join(offline)} offline")
    elif not _online(product):
        problems.append("offline")
    if error := product.get("error"):
        problems.append(f"Hive reports error {error}")
    available = not product.get("error") and _online(product)
    return available, "; ".join(problems) or None


def _online(node: dict[str, Any]) -> bool:
    return node.get("props", {}).get("online") is not False


def _join(labels: list[str]) -> str:
    unique = list(dict.fromkeys(labels))
    if len(unique) == 1:
        count = labels.count(unique[0])
        return unique[0] if count == 1 else f"{count} {unique[0]}s"
    return ", ".join(unique[:-1]) + " and " + unique[-1]


def battery_pct(snap: HiveSnapshot, product_id: str) -> int | None:
    """Lowest battery among online battery devices (an offline one's reading is stale)."""
    levels = [
        d["props"]["battery"]
        for _, d in _related_devices(snap, snap.products[product_id])
        if _online(d)
        and d.get("props", {}).get("power") == "battery"
        and isinstance(d.get("props", {}).get("battery"), int | float)
    ]
    return int(min(levels)) if levels else None


# --- list ----------------------------------------------------------------------------


def list_devices(snap: HiveSnapshot) -> list[DeviceSummary]:
    out = []
    for pid, product in snap.products.items():
        kind = _product_type(product)
        if kind is None:
            continue
        available, problem = availability(snap, pid)
        out.append(
            DeviceSummary(
                id=pid,
                name=device_name(snap, pid),
                type=kind,
                kind=_zone_kind(product) if kind == "heating" else None,
                available=available,
                problem=problem,
                battery_pct=battery_pct(snap, pid) if kind == "heating" else None,
                controller=controller_name(snap, pid),
            )
        )
    return sorted(out, key=lambda d: (d.type != "heating", d.name.casefold()))


def _zone_kind(product: dict[str, Any]) -> ZoneKind:
    return "trv_zone" if product.get("type") == "trvcontrol" else "boiler_zone"


# --- heating ---------------------------------------------------------------------

_HEATING_MODES: dict[str, HeatingMode] = {"SCHEDULE": "SCHEDULE", "MANUAL": "MANUAL", "OFF": "OFF"}


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return None
    try:
        return round(float(value), 1)
    except ValueError:
        return None


def _modes(state: dict[str, Any], props: dict[str, Any]) -> tuple[str | None, Boost]:
    raw = state.get("mode")
    if raw == "BOOST":
        minutes = state.get("boost")
        boost = Boost(active=True, minutes_remaining=int(minutes) if minutes else None)
        return props.get("previous", {}).get("mode"), boost
    return raw, Boost(active=False)


def heating_status(
    snap: HiveSnapshot, product_id: str, now: datetime | None = None
) -> HeatingStatus:
    product = snap.products[product_id]
    available, problem = availability(snap, product_id)
    common: dict[str, Any] = {
        "id": product_id,
        "name": device_name(snap, product_id),
        "kind": _zone_kind(product),
        "available": available,
        "problem": problem,
        "battery_pct": battery_pct(snap, product_id),
        "as_of": snap.fetched_at,
    }
    if not available:
        return HeatingStatus(**common)

    state, props = product.get("state", {}), product.get("props", {})
    raw_mode, boost = _modes(state, props)
    mode = _HEATING_MODES.get(raw_mode or "")
    schedule = None
    if mode == "SCHEDULE" and not boost.active:
        schedule = now_next_later(state.get("schedule"), now or snap.fetched_at)
    return HeatingStatus(
        **common,
        current_temp_c=_number(props.get("temperature")),
        target_temp_c=_number(state.get("target")),
        mode=mode,
        frost_protection_c=_number(state.get("frostProtection")),
        heating_on=props.get("working") if isinstance(props.get("working"), bool) else None,
        boost=boost,
        schedule=schedule,
    )


# --- hot water ---------------------------------------------------------------------

_HOT_WATER_MODES: dict[str, HotWaterMode] = {"SCHEDULE": "SCHEDULE", "MANUAL": "ON", "OFF": "OFF"}


def hot_water_status(
    snap: HiveSnapshot, product_id: str, now: datetime | None = None
) -> HotWaterStatus:
    product = snap.products[product_id]
    available, problem = availability(snap, product_id)
    common: dict[str, Any] = {
        "id": product_id,
        "name": device_name(snap, product_id),
        "available": available,
        "problem": problem,
        "as_of": snap.fetched_at,
    }
    if not available:
        return HotWaterStatus(**common)

    state, props = product.get("state", {}), product.get("props", {})
    raw_mode, boost = _modes(state, props)
    mode = _HOT_WATER_MODES.get(raw_mode or "")
    status = state.get("status")
    schedule = None
    if mode == "SCHEDULE" and not boost.active:
        schedule = now_next_later(state.get("schedule"), now or snap.fetched_at)
    return HotWaterStatus(
        **common,
        mode=mode,
        is_on=status == "ON" if status in ("ON", "OFF") else None,
        boost=boost,
        schedule=schedule,
    )


# --- schedule --------------------------------------------------------------------


Entry = tuple[int, dict[str, Any]]  # (minutes after Monday 00:00, raw value)


def _week_entries(schedule: Any) -> list[Entry]:
    """Valid slot starts across the week, in order, with repeated values merged."""
    if not isinstance(schedule, dict):
        return []
    entries: list[Entry] = []
    for day_index, day in enumerate(DAYS):
        for raw in schedule.get(day) or []:
            start = raw.get("start")
            if isinstance(start, int) and 0 <= start < 24 * 60:
                entries.append((day_index * 24 * 60 + start, raw.get("value") or {}))
    return _merge_repeats(sorted(entries, key=lambda e: e[0])) if entries else []


def _slot_values(value: dict[str, Any]) -> dict[str, Any]:
    status = value.get("status")
    return {
        "target_c": _number(value.get("target")),
        "hot_water_on": status == "ON" if status in ("ON", "OFF") else None,
    }


def now_next_later(schedule: Any, now: datetime) -> ScheduleNowNextLater | None:
    """The slot in force at `now` and the two after it, wrapping round the week."""
    entries = _week_entries(schedule)
    if not entries:
        return None

    local = now.astimezone(LONDON)
    monday = local.date() - timedelta(days=local.weekday())
    now_minute = local.weekday() * 24 * 60 + local.hour * 60 + local.minute
    current = max((i for i, (m, _) in enumerate(entries) if m <= now_minute), default=-1)

    def at(position: int) -> datetime:
        week, index = divmod(position, len(entries))
        return _wall_clock(monday, entries[index][0] + week * MINUTES_PER_WEEK)

    def slot(position: int) -> ScheduleSlot:
        value = entries[position % len(entries)][1]
        return ScheduleSlot(starts=at(position), ends=at(position + 1), **_slot_values(value))

    return ScheduleNowNextLater(now=slot(current), next=slot(current + 1), later=slot(current + 2))


def week_days(schedule: Any) -> list[DaySchedule]:
    """Each day midnight to midnight; a day opens with the setting carried over from
    the day before, and back-to-back repeats are merged."""
    entries = _week_entries(schedule)
    if not entries:
        return []
    out = []
    for day_index, day in enumerate(DAYS):
        day_start, day_end = day_index * 24 * 60, (day_index + 1) * 24 * 60
        carried = [v for m, v in entries if m <= day_start] or [entries[-1][1]]
        changes = [(day_start, carried[-1])] + [e for e in entries if day_start < e[0] < day_end]
        ends = [m for m, _ in changes[1:]] + [day_end]
        slots = [
            WeeklySlot(start_minute=m - day_start, end_minute=end - day_start, **_slot_values(v))
            for (m, v), end in zip(changes, ends, strict=True)
        ]
        out.append(DaySchedule(day=day, slots=slots))  # type: ignore[arg-type]
    return out


def weekly_schedule(snap: HiveSnapshot, product_id: str) -> WeeklySchedule:
    product = snap.products[product_id]
    available, problem = availability(snap, product_id)
    kind = _product_type(product) or "heating"
    common: dict[str, Any] = {
        "id": product_id,
        "name": device_name(snap, product_id),
        "type": kind,
        "available": available,
        "problem": problem,
        "as_of": snap.fetched_at,
    }
    if not available:
        return WeeklySchedule(**common)
    state, props = product.get("state", {}), product.get("props", {})
    raw_mode, boost = _modes(state, props)
    modes: dict[str, Any] = _HEATING_MODES if kind == "heating" else _HOT_WATER_MODES
    return WeeklySchedule(
        **common,
        mode=modes.get(raw_mode or ""),
        boost_active=boost.active,
        days=week_days(state.get("schedule")),
    )


def _merge_repeats(entries: list[Entry]) -> list[Entry]:
    """Drop slots that only repeat the value before them, round the week, so each slot
    is a change. Hive often splits one setting over several slots (10°C, 10°C, 10°C)."""
    merged = [e for i, e in enumerate(entries) if e[1] != entries[i - 1][1]]
    return merged or entries[:1]


def _wall_clock(monday: date, minutes: int) -> datetime:
    """UK wall-clock time `minutes` after Monday 00:00 (DST-safe: no UTC arithmetic)."""
    days, minute_of_day = divmod(minutes, 24 * 60)
    return datetime.combine(
        monday + timedelta(days=days), time(minute_of_day // 60, minute_of_day % 60), LONDON
    )
