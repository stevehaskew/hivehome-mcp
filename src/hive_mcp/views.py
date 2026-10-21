"""What the MCP tools return: readings plus UK-formatted text (DD/MM/YYYY, 24-hour, °C).

Numbers stay numeric so Claude can reason with them; times are given as UK
strings because that is how they will be repeated back to the user.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel, Field

from .models import (
    DeviceSummary,
    HeatingStatus,
    HotWaterStatus,
    ScheduleNowNextLater,
    ScheduleSlot,
    ZoneKind,
)
from .snapshot import LONDON, HiveSnapshot

LOW_BATTERY_PCT = 20

_MODE_TEXT = {
    "SCHEDULE": "following its schedule",
    "MANUAL": "manual (fixed target)",
    "ON": "on (manual)",
}


def uk_datetime(dt: datetime) -> str:
    """e.g. 'Wed 21/10/2026 22:00' in UK local time."""
    return dt.astimezone(LONDON).strftime("%a %d/%m/%Y %H:%M")


def uk_when(dt: datetime, ref: datetime) -> str:
    """`dt` for a sentence, relative to `ref`: '22:00', 'tomorrow 02:00', 'Fri 07:00'
    within the coming week, otherwise the full 'Wed 28/10/2026 06:00'."""
    local, today = dt.astimezone(LONDON), ref.astimezone(LONDON).date()
    days = (local.date() - today).days
    clock = local.strftime("%H:%M")
    if days == 0:
        return clock
    if days == 1:
        return f"tomorrow {clock}"
    if days == -1:
        return f"yesterday {clock}"
    if 1 < days < 7:
        return local.strftime("%a %H:%M")
    return uk_datetime(dt)


def celsius(value: float | None) -> str:
    return "unknown" if value is None else f"{value:g}°C"


class Freshness(BaseModel):
    as_of: str = Field(description="When Hive was last read, UK time (DD/MM/YYYY)")
    stale: bool = Field(description="True if Hive couldn't be reached and this is older data")


def freshness(snap: HiveSnapshot) -> Freshness:
    return Freshness(as_of=uk_datetime(snap.fetched_at), stale=snap.stale)


# --- devices ---------------------------------------------------------------------


class DeviceView(BaseModel):
    id: str
    name: str
    type: Literal["heating", "hot_water"]
    kind: ZoneKind | None = Field(None, description="boiler_zone (thermostat) or trv_zone")
    available: bool
    problem: str | None = None
    battery_pct: int | None = None
    low_battery: bool = False
    controller: str | None = Field(
        None, description="Thermostat that controls it, if not the zone's own"
    )


class DeviceList(Freshness):
    devices: list[DeviceView]


def device_view(d: DeviceSummary) -> DeviceView:
    low = d.battery_pct is not None and d.battery_pct <= LOW_BATTERY_PCT
    controller = d.controller if d.controller != d.name else None
    return DeviceView(
        **d.model_dump(exclude={"controller"}), controller=controller, low_battery=low
    )


# --- schedule ------------------------------------------------------------------------


class SlotView(BaseModel):
    period: Literal["now", "next", "later"]
    starts: str = Field(description="UK time, DD/MM/YYYY")
    ends: str = Field(description="UK time, DD/MM/YYYY")
    target_c: float | None = None
    hot_water_on: bool | None = None
    summary: str


def _slot_value(slot: ScheduleSlot) -> str:
    if slot.target_c is not None:
        return celsius(slot.target_c)
    return "hot water on" if slot.hot_water_on else "hot water off"


def schedule_view(s: ScheduleNowNextLater | None, as_of: datetime) -> list[SlotView]:
    if s is None:
        return []
    out = []
    for period, slot in (("now", s.now), ("next", s.next), ("later", s.later)):
        out.append(
            SlotView(
                period=period,  # type: ignore[arg-type]
                starts=uk_datetime(slot.starts),
                ends=uk_datetime(slot.ends),
                target_c=slot.target_c,
                hot_water_on=slot.hot_water_on,
                summary=f"{_slot_value(slot)} from {uk_when(slot.starts, as_of)} "
                f"until {uk_when(slot.ends, as_of)}",
            )
        )
    return out


# --- heating ---------------------------------------------------------------------


class BoostView(BaseModel):
    active: bool
    minutes_remaining: int | None = None
    ends: str | None = Field(None, description="Estimated end, UK time")


class HeatingZoneView(BaseModel):
    id: str
    name: str
    kind: ZoneKind
    available: bool
    problem: str | None = None
    current_temp_c: float | None = None
    target_temp_c: float | None = None
    mode: Literal["SCHEDULE", "MANUAL", "OFF"] | None = None
    frost_protection_c: float | None = None
    heating_now: bool | None = Field(None, description="Calling for heat at this moment")
    boost: BoostView
    battery_pct: int | None = None
    low_battery: bool = False
    schedule: list[SlotView] = Field(default_factory=list)
    summary: str


class HeatingReport(Freshness):
    zones: list[HeatingZoneView]


def _boost_view(active: bool, minutes: int | None, as_of: datetime) -> BoostView:
    ends = uk_datetime(as_of + timedelta(minutes=minutes)) if active and minutes else None
    return BoostView(active=active, minutes_remaining=minutes, ends=ends)


def heating_view(st: HeatingStatus) -> HeatingZoneView:
    low = st.battery_pct is not None and st.battery_pct <= LOW_BATTERY_PCT
    return HeatingZoneView(
        **st.model_dump(exclude={"boost", "schedule", "as_of", "heating_on"}),
        heating_now=st.heating_on,
        boost=_boost_view(st.boost.active, st.boost.minutes_remaining, st.as_of),
        low_battery=low,
        schedule=schedule_view(st.schedule, st.as_of),
        summary=heating_summary(st),
    )


def heating_summary(st: HeatingStatus) -> str:
    if not st.available:
        return f"{st.name} is unavailable: {st.problem or 'no reason given'}."
    parts = [f"{st.name} is {celsius(st.current_temp_c)}"]
    if st.boost.active:
        left = f", {st.boost.minutes_remaining} minutes left" if st.boost.minutes_remaining else ""
        parts.append(f"boosting to {celsius(st.target_temp_c)}{left}")
    elif st.mode == "OFF":
        parts.append(f"heating off (frost protection at {celsius(st.frost_protection_c)})")
    else:
        mode = _MODE_TEXT.get(st.mode or "", "in an unknown mode")
        parts.append(f"target {celsius(st.target_temp_c)}, {mode}")
    if st.heating_on is not None:
        parts.append("heating now" if st.heating_on else "not calling for heat")
    sentence = ", ".join(parts) + "."
    if st.schedule and st.mode == "SCHEDULE" and not st.boost.active:
        nxt = st.schedule.next
        sentence += f" Next: {celsius(nxt.target_c)} from {uk_when(nxt.starts, st.as_of)}."
    if st.battery_pct is not None and st.battery_pct <= LOW_BATTERY_PCT:
        sentence += f" Battery low ({st.battery_pct}%)."
    return sentence


# --- hot water ---------------------------------------------------------------------


class HotWaterView(Freshness):
    id: str
    name: str
    available: bool
    problem: str | None = None
    mode: Literal["SCHEDULE", "ON", "OFF"] | None = None
    is_on: bool | None = None
    boost: BoostView
    schedule: list[SlotView] = Field(default_factory=list)
    summary: str


def hot_water_view(st: HotWaterStatus, snap: HiveSnapshot) -> HotWaterView:
    return HotWaterView(
        **freshness(snap).model_dump(),
        **st.model_dump(exclude={"boost", "schedule", "as_of"}),
        boost=_boost_view(st.boost.active, st.boost.minutes_remaining, st.as_of),
        schedule=schedule_view(st.schedule, st.as_of),
        summary=hot_water_summary(st),
    )


def hot_water_summary(st: HotWaterStatus) -> str:
    if not st.available:
        return f"Hot water is unavailable: {st.problem or 'no reason given'}."
    state = "on" if st.is_on else "off" if st.is_on is False else "in an unknown state"
    if st.boost.active:
        left = f", {st.boost.minutes_remaining} minutes left" if st.boost.minutes_remaining else ""
        return f"Hot water is boosting{left}."
    mode = {"SCHEDULE": "following its schedule", "ON": "set to always on", "OFF": "switched off"}
    sentence = f"Hot water is {state}, {mode.get(st.mode or '', 'in an unknown mode')}."
    if st.schedule and st.mode == "SCHEDULE":
        nxt = st.schedule.next
        sentence += f" Next: {_slot_value(nxt)} from {uk_when(nxt.starts, st.as_of)}."
    return sentence
