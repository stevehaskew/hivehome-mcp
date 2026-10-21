"""Write planning: limits, mode rules and the kwargs sent to Hive (no I/O)."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from hive_mcp import writes
from hive_mcp.snapshot import LONDON, HiveSnapshot
from hive_mcp.writes import WriteRejected

FIXTURE = Path(__file__).parent / "fixtures/hive_sample.json"
NOW = datetime(2026, 10, 21, 22, 15, tzinfo=LONDON)

MAIN_HALL, DOWNSTAIRS, THERMOSTAT_3, CRECHE, OFFICE_A, HOT_WATER = (
    "id-11",
    "id-7",
    "id-16",
    "id-1",
    "id-5",
    "id-18",
)


def snap(**states: dict[str, Any]) -> HiveSnapshot:
    """The fixture, with product state/props patched: snap(**{"id-16": {"state": {...}}})."""
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    for pid, patch in states.items():
        for section, values in patch.items():
            data["products"][pid].setdefault(section, {}).update(values)
    return HiveSnapshot(data["products"], data["devices"], NOW)


BOOSTING = {"state": {"mode": "BOOST", "boost": 25, "target": 21}}


# --- limits -------------------------------------------------------------------------


@pytest.mark.parametrize("temp", [5, 5.5, 18, 24.5, 25])
def test_temperatures_in_range_are_accepted(temp: float) -> None:
    assert writes.check_temperature(temp) == temp


@pytest.mark.parametrize("temp", [4.5, 25.5, 32, -1, float("nan"), float("inf")])
def test_temperatures_out_of_range_are_rejected_not_clamped(temp: float) -> None:
    with pytest.raises(WriteRejected, match="5–25°C"):
        writes.check_temperature(temp)


@pytest.mark.parametrize("temp", [18.2, 20.25])
def test_temperatures_must_be_half_degrees(temp: float) -> None:
    with pytest.raises(WriteRejected, match="steps of 0.5°C"):
        writes.check_temperature(temp)


@pytest.mark.parametrize("minutes", [15, 60, 120])
def test_boost_minutes_in_range(minutes: int) -> None:
    assert writes.check_minutes(minutes) == minutes


@pytest.mark.parametrize("minutes", [0, 14, 121, 600, -30])
def test_boost_minutes_out_of_range(minutes: int) -> None:
    with pytest.raises(WriteRejected, match="15–120 minutes"):
        writes.check_minutes(minutes)


def test_limits_are_checked_before_anything_else() -> None:
    # Even for an unavailable zone, the limit is the reason given.
    with pytest.raises(WriteRejected, match="5–25°C"):
        writes.plan_target(snap(), DOWNSTAIRS, 30)


# --- unavailable zones ------------------------------------------------------------------


@pytest.mark.parametrize(
    "plan",
    [
        lambda s: writes.plan_target(s, DOWNSTAIRS, 18),
        lambda s: writes.plan_heating_mode(s, DOWNSTAIRS, "OFF", None),
        lambda s: writes.plan_heating_boost(s, DOWNSTAIRS, 30, 20),
        lambda s: writes.plan_cancel_heating_boost(s, DOWNSTAIRS),
    ],
)
def test_unavailable_zone_is_refused(plan: Any) -> None:
    with pytest.raises(WriteRejected, match="Downstairs is unavailable .*offline"):
        plan(snap())


# --- target -----------------------------------------------------------------------------


def test_target_in_manual() -> None:
    s = snap(**{THERMOSTAT_3: {"state": {"mode": "MANUAL", "target": 15}}})
    plan = writes.plan_target(s, THERMOSTAT_3, 18.5)
    assert plan is not None
    assert (plan.product_type, plan.product_id) == ("heating", THERMOSTAT_3)
    assert plan.kwargs == {"target": 18.5}
    assert plan.change == "Thermostat 3 target changed from 15°C to 18.5°C"
    assert plan.note is None


def test_whole_degrees_are_sent_as_integers() -> None:
    s = snap(**{THERMOSTAT_3: {"state": {"mode": "MANUAL", "target": 15}}})
    plan = writes.plan_target(s, THERMOSTAT_3, 18.0)
    assert plan is not None and plan.kwargs == {"target": 18}
    assert isinstance(plan.kwargs["target"], int)


def test_target_in_schedule_is_an_override_until_the_next_change() -> None:
    plan = writes.plan_target(snap(), MAIN_HALL, 18)
    assert plan is not None
    assert plan.change == "Main Hall target changed from 10°C to 18°C"
    assert plan.note is not None and plan.note.startswith(
        "This overrides the schedule until its next change at "
    )


def test_target_for_trv_zone_uses_its_own_product_type() -> None:
    plan = writes.plan_target(snap(), CRECHE, 16)
    assert plan is not None and plan.product_type == "trvcontrol"


def test_target_refused_when_off() -> None:
    with pytest.raises(WriteRejected, match="switched off.*set_heating_mode"):
        writes.plan_target(snap(), THERMOSTAT_3, 18)


def test_target_refused_while_boosting() -> None:
    with pytest.raises(WriteRejected, match="boosting"):
        writes.plan_target(snap(**{MAIN_HALL: BOOSTING}), MAIN_HALL, 18)


def test_target_already_set_is_no_change() -> None:
    assert writes.plan_target(snap(), MAIN_HALL, 10) is None


# --- mode -------------------------------------------------------------------------------


def test_manual_needs_a_temperature() -> None:
    with pytest.raises(WriteRejected, match="MANUAL needs a temperature"):
        writes.plan_heating_mode(snap(), THERMOSTAT_3, "MANUAL", None)


@pytest.mark.parametrize("mode", ["SCHEDULE", "OFF"])
def test_other_modes_take_no_temperature(mode: Any) -> None:
    with pytest.raises(WriteRejected, match="only applies to MANUAL"):
        writes.plan_heating_mode(snap(), THERMOSTAT_3, mode, 18)


def test_manual_sends_mode_and_target_together() -> None:
    plan = writes.plan_heating_mode(snap(), THERMOSTAT_3, "MANUAL", 12)
    assert plan is not None
    assert plan.kwargs == {"mode": "MANUAL", "target": 12}
    assert plan.change == "Thermostat 3 changed from OFF to MANUAL at 12°C"
    assert plan.note == "It stays at this temperature until someone changes it."


def test_off_sends_the_frost_protection_target_and_warns_it_stays_off() -> None:
    plan = writes.plan_heating_mode(snap(), MAIN_HALL, "OFF", None)
    assert plan is not None and plan.kwargs == {"mode": "OFF", "target": 7}
    assert plan.expect["target_c"] == 7
    assert plan.change == "Main Hall changed from SCHEDULE to OFF"
    assert plan.note is not None and "stays off" in plan.note


def test_off_with_a_leftover_target_puts_the_frost_target_back() -> None:
    # Live, Hive briefly reported target 12°C after MANUAL 12°C -> OFF.
    s = snap(**{THERMOSTAT_3: {"state": {"mode": "OFF", "target": 12}}})
    plan = writes.plan_heating_mode(s, THERMOSTAT_3, "OFF", None)
    assert plan is not None and plan.kwargs == {"mode": "OFF", "target": 7}
    assert plan.change == (
        "Thermostat 3 stays OFF; its stored target goes from 12°C back to the frost-protection 7°C"
    )


def test_off_without_a_frost_setting_sends_just_the_mode() -> None:
    s = snap(**{MAIN_HALL: {"state": {"frostProtection": None}}})
    plan = writes.plan_heating_mode(s, MAIN_HALL, "OFF", None)
    assert plan is not None and plan.kwargs == {"mode": "OFF"}


def test_same_mode_is_no_change() -> None:
    assert writes.plan_heating_mode(snap(), THERMOSTAT_3, "OFF", None) is None
    s = snap(**{THERMOSTAT_3: {"state": {"mode": "MANUAL", "target": 15}}})
    assert writes.plan_heating_mode(s, THERMOSTAT_3, "MANUAL", 15) is None
    assert writes.plan_heating_mode(s, THERMOSTAT_3, "MANUAL", 16) is not None


def test_mode_during_boost_cancels_it() -> None:
    s = snap(**{MAIN_HALL: {**BOOSTING, "props": {"previous": {"mode": "SCHEDULE"}}}})
    plan = writes.plan_heating_mode(s, MAIN_HALL, "SCHEDULE", None)
    assert plan is not None and plan.kwargs == {"mode": "SCHEDULE"}
    assert "boost was cancelled" in plan.change


# --- heating boost ----------------------------------------------------------------------


def test_boost_kwargs() -> None:
    plan = writes.plan_heating_boost(snap(), THERMOSTAT_3, 30, 20)
    assert plan.kwargs == {"mode": "BOOST", "boost": 30, "target": 20}
    assert plan.change == "Thermostat 3 boosting to 20°C for 30 minutes"
    assert plan.note == "Afterwards it goes back to OFF."


def test_boost_refused_when_already_boosting() -> None:
    with pytest.raises(WriteRejected, match=r"already boosting \(25 minutes left\)"):
        writes.plan_heating_boost(snap(**{MAIN_HALL: BOOSTING}), MAIN_HALL, 30, 20)


def test_cancel_without_boost_is_no_change() -> None:
    assert writes.plan_cancel_heating_boost(snap(), MAIN_HALL) is None


def test_cancel_back_to_schedule() -> None:
    s = snap(**{MAIN_HALL: {**BOOSTING, "props": {"previous": {"mode": "SCHEDULE"}}}})
    plan = writes.plan_cancel_heating_boost(s, MAIN_HALL)
    assert plan is not None and plan.kwargs == {"mode": "SCHEDULE"}
    assert plan.change == "Main Hall boost cancelled, back to SCHEDULE"
    assert plan.note is None


@pytest.mark.parametrize(
    ("previous", "kwargs", "after"),
    [
        ({"mode": "OFF", "target": 7}, {"mode": "OFF", "target": 7}, "OFF"),
        ({"mode": "MANUAL", "target": 16}, {"mode": "MANUAL", "target": 16}, "MANUAL at 16°C"),
    ],
)
def test_cancel_back_to_manual_or_off_restores_target(
    previous: dict[str, Any], kwargs: dict[str, Any], after: str
) -> None:
    s = snap(**{THERMOSTAT_3: {**BOOSTING, "props": {"previous": previous}}})
    plan = writes.plan_cancel_heating_boost(s, THERMOSTAT_3)
    assert plan is not None and plan.kwargs == kwargs
    assert plan.change.endswith(f"back to {after}")


def test_cancel_trv_boost_with_no_previous_mode_falls_back_to_schedule() -> None:
    # TRV zones in the fixture have props.previous == {}.
    plan = writes.plan_cancel_heating_boost(snap(**{CRECHE: BOOSTING}), CRECHE)
    assert plan is not None and plan.kwargs == {"mode": "SCHEDULE"}
    assert plan.product_type == "trvcontrol"
    assert plan.note is not None and "back on its schedule" in plan.note


def test_cancel_manual_without_previous_target_falls_back_to_schedule() -> None:
    s = snap(**{THERMOSTAT_3: {**BOOSTING, "props": {"previous": {"mode": "MANUAL"}}}})
    plan = writes.plan_cancel_heating_boost(s, THERMOSTAT_3)
    assert plan is not None and plan.kwargs == {"mode": "SCHEDULE"}


# --- hot water -------------------------------------------------------------------------


def test_hot_water_on_is_sent_as_manual() -> None:
    plan = writes.plan_hot_water_mode(snap(), HOT_WATER, "ON")
    assert plan is not None
    assert (plan.product_type, plan.kwargs) == ("hotwater", {"mode": "MANUAL"})
    assert plan.change == "Hot water changed from SCHEDULE to ON"
    assert plan.note == "It stays on until someone changes it."


def test_hot_water_same_mode_is_no_change() -> None:
    assert writes.plan_hot_water_mode(snap(), HOT_WATER, "SCHEDULE") is None


def test_hot_water_boost() -> None:
    plan = writes.plan_hot_water_boost(snap(), HOT_WATER, 60)
    assert plan.kwargs == {"mode": "BOOST", "boost": 60}
    assert plan.note == "Afterwards it goes back to SCHEDULE."
    with pytest.raises(WriteRejected, match="15–120"):
        writes.plan_hot_water_boost(snap(), HOT_WATER, 180)


def test_hot_water_cancel_boost() -> None:
    assert writes.plan_cancel_hot_water_boost(snap(), HOT_WATER) is None
    s = snap(**{HOT_WATER: {"state": {"mode": "BOOST", "boost": 40}}})
    plan = writes.plan_cancel_hot_water_boost(s, HOT_WATER)
    assert plan is not None and plan.kwargs == {"mode": "SCHEDULE"}
    assert plan.change == "Hot water boost cancelled, back to SCHEDULE"


# --- settings and confirmation -----------------------------------------------------------


def test_settings_of() -> None:
    s = snap()
    assert writes.settings_of(s, THERMOSTAT_3) == {
        "mode": "OFF",
        "boost_minutes": None,
        "target_c": 7.0,
    }
    assert writes.settings_of(s, HOT_WATER) == {
        "mode": "SCHEDULE",
        "boost_minutes": None,
        "hot_water_on": False,
    }
    boosting = snap(**{MAIN_HALL: {**BOOSTING, "props": {"previous": {"mode": "SCHEDULE"}}}})
    assert writes.settings_of(boosting, MAIN_HALL)["boost_minutes"] == 25


def test_unconfirmed_lists_what_did_not_take() -> None:
    plan = writes.plan_heating_mode(snap(), THERMOSTAT_3, "MANUAL", 12)
    assert plan is not None
    assert writes.unconfirmed(plan, snap()) == [
        "mode is 'OFF', expected 'MANUAL'",
        "target_c is 7.0, expected 12.0",
    ]
    after = snap(**{THERMOSTAT_3: {"state": {"mode": "MANUAL", "target": 12}}})
    assert writes.unconfirmed(plan, after) == []
