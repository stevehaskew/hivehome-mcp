"""Readings against the scrubbed fixture.

Expected values are what the spike read from the live system through the
library's own getters at 22:15 on Wednesday 21/10/2026, so these tests also
check our interpretation agrees with the library's.
"""

from __future__ import annotations

import copy
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from hive_mcp import readings as r
from hive_mcp.snapshot import LONDON, HiveSnapshot

FIXTURE = Path(__file__).parent / "fixtures/hive_sample.json"
NOW = datetime(2026, 10, 21, 22, 15, tzinfo=LONDON)  # a Wednesday


@pytest.fixture
def snap() -> HiveSnapshot:
    data = json.loads(FIXTURE.read_text())
    return HiveSnapshot(data["products"], data["devices"], fetched_at=NOW)


def at(day: int, hh: int, mm: int = 0) -> datetime:
    return datetime(2026, 10, day, hh, mm, tzinfo=LONDON)


def slots(status: Any) -> list[tuple[datetime, datetime, Any]]:
    s = status.schedule
    assert s is not None
    return [
        (x.starts, x.ends, x.target_c if x.target_c is not None else x.hot_water_on)
        for x in (s.now, s.next, s.later)
    ]


# --- list_devices ------------------------------------------------------------------


def test_lists_heating_zones_then_hot_water(snap: HiveSnapshot) -> None:
    devices = r.list_devices(snap)
    assert [(d.name, d.type, d.kind) for d in devices] == [
        ("Creche", "heating", "trv_zone"),
        ("Downstairs", "heating", "boiler_zone"),
        ("Main Hall", "heating", "boiler_zone"),
        ("Oak Room", "heating", "trv_zone"),
        ("Office A", "heating", "trv_zone"),
        ("Office B", "heating", "trv_zone"),
        ("Thermostat 3", "heating", "boiler_zone"),
        ("Upstairs", "heating", "boiler_zone"),
        ("Hot water", "hot_water", None),
    ]


def test_offline_zone_is_listed_not_hidden(snap: HiveSnapshot) -> None:
    downstairs = next(d for d in r.list_devices(snap) if d.name == "Downstairs")
    assert not downstairs.available
    assert downstairs.problem == (
        "boiler module and thermostat offline; Hive reports error MALFORMED_NODE_DESCRIPTOR"
    )
    assert downstairs.battery_pct is None  # its thermostat's reading is from 2024


def test_batteries_and_controllers(snap: HiveSnapshot) -> None:
    by_name = {d.name: d for d in r.list_devices(snap)}
    assert by_name["Creche"].battery_pct == 20
    assert by_name["Main Hall"].battery_pct == 40
    assert by_name["Thermostat 3"].battery_pct == 20
    assert by_name["Hot water"].controller == "Thermostat 3"
    assert by_name["Creche"].controller is None


# --- find_device -----------------------------------------------------------------


def test_find_by_id_or_name(snap: HiveSnapshot) -> None:
    assert r.find_device(snap, "id-11", "heating") == "id-11"
    assert r.find_device(snap, "main hall", "heating") == "id-11"
    assert r.find_device(snap, None, "hot_water") == "id-18"


@pytest.mark.parametrize("key", [None, "Kitchen", "id-18"])
def test_find_rejects_ambiguous_or_unknown(snap: HiveSnapshot, key: str | None) -> None:
    with pytest.raises(r.UnknownDevice, match="Creche"):
        r.find_device(snap, key, "heating")


# --- heating_status ----------------------------------------------------------------


def test_trv_zone_on_schedule(snap: HiveSnapshot) -> None:
    st = r.heating_status(snap, "id-1")
    assert (st.name, st.kind, st.available) == ("Creche", "trv_zone", True)
    assert (st.current_temp_c, st.target_temp_c, st.mode) == (16.2, 12.0, "SCHEDULE")
    assert st.heating_on is False
    assert not st.boost.active
    assert slots(st) == [
        (at(21, 13), at(22, 2), 12.0),  # 13:00 and 22:00 slots were both 12°C
        (at(22, 2), at(22, 12), 10.0),
        (at(22, 12), at(23, 2), 12.0),
    ]
    assert st.as_of == NOW


def test_boiler_zone_on_schedule(snap: HiveSnapshot) -> None:
    st = r.heating_status(snap, "id-11")
    assert (st.current_temp_c, st.target_temp_c) == (18.5, 10.0)
    assert slots(st) == [
        (at(18, 10, 15), at(23, 9), 10.0),  # Hive's many 10°C slots, merged
        (at(23, 9), at(23, 12), 10.5),
        (at(23, 12), at(25, 3), 10.0),
    ]


def test_zone_off_shows_frost_protection_and_no_schedule(snap: HiveSnapshot) -> None:
    st = r.heating_status(snap, "id-5")
    assert (st.name, st.mode, st.target_temp_c) == ("Office A", "OFF", 7.0)
    assert st.frost_protection_c == 7.0
    assert st.schedule is None


def test_unavailable_zone_has_no_readings(snap: HiveSnapshot) -> None:
    st = r.heating_status(snap, "id-7")
    assert not st.available
    assert st.current_temp_c is None and st.mode is None
    assert "MALFORMED_NODE_DESCRIPTOR" in (st.problem or "")


def test_boost_reports_minutes_and_underlying_mode(snap: HiveSnapshot) -> None:
    product = snap.products["id-11"]
    product["state"].update(mode="BOOST", boost=25, target=22)
    product["props"]["working"] = True
    st = r.heating_status(snap, "id-11")
    assert st.boost.active and st.boost.minutes_remaining == 25
    assert (st.mode, st.target_temp_c, st.heating_on) == ("SCHEDULE", 22.0, True)
    assert st.schedule is None


# --- hot_water_status ----------------------------------------------------------------


def test_hot_water_on_schedule(snap: HiveSnapshot) -> None:
    st = r.hot_water_status(snap, "id-18")
    assert (st.name, st.mode, st.is_on) == ("Hot water", "SCHEDULE", False)
    assert slots(st) == [
        (at(21, 22), at(22, 1), False),
        (at(22, 1), at(22, 12), True),
        (at(22, 12), at(23, 1), False),
    ]


def test_hot_water_manual_is_reported_as_on(snap: HiveSnapshot) -> None:
    snap.products["id-18"]["state"].update(mode="MANUAL", status="ON")
    st = r.hot_water_status(snap, "id-18")
    assert (st.mode, st.is_on, st.schedule) == ("ON", True, None)


# --- now_next_later edge cases ------------------------------------------------------

WEEK = {
    "monday": [{"start": 420, "value": {"target": 20}}, {"start": 1320, "value": {"target": 15}}],
    "wednesday": [{"start": 360, "value": {"target": 18}}],
}


def test_schedule_wraps_back_to_last_week() -> None:
    # Monday 05:00 is before the week's first slot, so Sunday's carry-over applies:
    # the slot that started last Wednesday 06:00.
    nnl = r.now_next_later(WEEK, at(26, 5))
    assert nnl is not None
    assert (nnl.now.starts, nnl.now.ends, nnl.now.target_c) == (at(21, 6), at(26, 7), 18.0)
    assert (nnl.next.starts, nnl.later.starts) == (at(26, 7), at(26, 22))


def test_schedule_wraps_forward_into_next_week() -> None:
    nnl = r.now_next_later(WEEK, at(22, 9))  # Thursday
    assert nnl is not None
    assert nnl.now.starts == at(21, 6)
    assert (nnl.next.starts, nnl.later.starts, nnl.later.ends) == (
        at(26, 7),
        at(26, 22),
        at(28, 6),
    )


def test_schedule_across_clocks_going_back() -> None:
    # Clocks go back at 02:00 on Sunday 25/10/2026; slots stay on UK wall-clock times.
    week = {
        "saturday": [{"start": 1380, "value": {"target": 12}}],
        "sunday": [{"start": 420, "value": {"target": 19}}],
    }
    nnl = r.now_next_later(week, at(25, 1, 30))
    assert nnl is not None
    assert (nnl.now.starts, nnl.now.ends) == (at(24, 23), at(25, 7))
    assert nnl.now.ends.utcoffset() == at(25, 12).utcoffset()  # GMT after the change
    assert nnl.now.ends - nnl.now.starts == (at(25, 7) - at(24, 23))


@pytest.mark.parametrize("schedule", [None, {}, {"monday": []}, {"monday": [{"start": "x"}]}])
def test_schedule_missing_or_malformed(schedule: Any) -> None:
    assert r.now_next_later(schedule, NOW) is None


def test_single_slot_schedule() -> None:
    nnl = r.now_next_later({"friday": [{"start": 0, "value": {"target": 16}}]}, NOW)
    assert nnl is not None
    assert (nnl.now.starts, nnl.now.ends) == (at(16, 0), at(23, 0))
    assert (nnl.later.starts, nnl.later.target_c) == (at(30, 0), 16.0)


def test_repeated_values_are_merged_round_the_week() -> None:
    week = {
        "monday": [
            {"start": 360, "value": {"target": 18}},
            {"start": 600, "value": {"target": 18}},
        ],
        "tuesday": [{"start": 360, "value": {"target": 12}}],
        "sunday": [{"start": 1200, "value": {"target": 18.0}}],
    }
    nnl = r.now_next_later(week, at(19, 12))  # Monday
    assert nnl is not None
    # Sunday 20:00 runs on into Monday's 18°C slots, so the slot began last Sunday.
    assert (nnl.now.starts, nnl.now.ends, nnl.now.target_c) == (at(18, 20), at(20, 6), 18.0)
    assert (nnl.next.starts, nnl.next.ends, nnl.next.target_c) == (at(20, 6), at(25, 20), 12.0)


def test_schedule_with_one_value_throughout() -> None:
    week = {d: [{"start": 0, "value": {"status": "ON"}}] for d in ("monday", "thursday")}
    nnl = r.now_next_later(week, NOW)
    assert nnl is not None
    assert (nnl.now.starts, nnl.now.ends, nnl.now.hot_water_on) == (at(19, 0), at(26, 0), True)


def test_readings_do_not_mutate_snapshot(snap: HiveSnapshot) -> None:
    before = copy.deepcopy(snap.products)
    for d in r.list_devices(snap):
        fn = r.heating_status if d.type == "heating" else r.hot_water_status
        fn(snap, d.id)
    assert snap.products == before


# --- weekly schedule -----------------------------------------------------------------


def day_slots(days: list[Any], day: str) -> list[tuple[int, int, Any]]:
    [d] = [d for d in days if d.day == day]
    return [
        (s.start_minute, s.end_minute, s.target_c if s.target_c is not None else s.hot_water_on)
        for s in d.slots
    ]


def test_week_days_carry_over_midnight_and_fill_each_day() -> None:
    days = r.week_days(WEEK)
    assert [d.day for d in days] == list(r.DAYS)
    # Monday opens with Wednesday's 18°C, carried round the week.
    assert day_slots(days, "monday") == [(0, 420, 18.0), (420, 1320, 20.0), (1320, 1440, 15.0)]
    assert day_slots(days, "tuesday") == [(0, 1440, 15.0)]
    assert day_slots(days, "wednesday") == [(0, 360, 15.0), (360, 1440, 18.0)]
    assert day_slots(days, "sunday") == [(0, 1440, 18.0)]


def test_week_days_merge_repeats_and_ignore_bad_entries() -> None:
    week = {
        "monday": [{"start": 0, "value": {"target": 16}}, {"start": 600, "value": {"target": 16}}],
        "friday": [{"start": "x"}, {"start": 480, "value": {"target": 20}}],
    }
    days = r.week_days(week)
    assert day_slots(days, "monday") == [(0, 1440, 16.0)]
    assert day_slots(days, "friday") == [(0, 480, 16.0), (480, 1440, 20.0)]
    assert r.week_days(None) == []


def test_weekly_schedule_for_zone_switched_off(snap: HiveSnapshot) -> None:
    ws = r.weekly_schedule(snap, "id-16")
    assert (ws.name, ws.type, ws.mode, ws.boost_active) == ("Thermostat 3", "heating", "OFF", False)
    assert day_slots(ws.days, "tuesday") == [(0, 600, 7.0), (600, 885, 19.0), (885, 1440, 7.0)]


def test_weekly_schedule_for_hot_water(snap: HiveSnapshot) -> None:
    ws = r.weekly_schedule(snap, "id-18")
    assert (ws.type, ws.mode) == ("hot_water", "SCHEDULE")
    assert day_slots(ws.days, "monday")[:2] == [(0, 60, False), (60, 750, True)]


def test_weekly_schedule_for_unavailable_zone(snap: HiveSnapshot) -> None:
    ws = r.weekly_schedule(snap, "id-7")
    assert not ws.available and ws.days == []


def test_find_device_of_either_type(snap: HiveSnapshot) -> None:
    assert r.find_device(snap, "hot water", None) == "id-18"
    assert r.find_device(snap, "main hall", None) == "id-11"
    with pytest.raises(r.UnknownDevice, match="heating zone or hot water system.*Hot water"):
        r.find_device(snap, "Kitchen", None)
