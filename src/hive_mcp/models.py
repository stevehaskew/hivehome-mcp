"""Typed readings returned by the tools. Temperatures in °C; datetimes are Europe/London.

These carry values, not display strings; UK formatting happens at the tool layer.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

ZoneKind = Literal["boiler_zone", "trv_zone"]
HeatingMode = Literal["SCHEDULE", "MANUAL", "OFF"]
HotWaterMode = Literal["SCHEDULE", "ON", "OFF"]


class DeviceSummary(BaseModel):
    id: str
    name: str
    type: Literal["heating", "hot_water"]
    kind: ZoneKind | None = Field(None, description="Heating only: boiler zone or TRV zone")
    available: bool
    problem: str | None = Field(None, description="Why the device is unavailable, if it is")
    battery_pct: int | None = Field(None, description="Lowest battery among its devices")
    controller: str | None = Field(None, description="Thermostat name, where there is one")


class Boost(BaseModel):
    active: bool
    minutes_remaining: int | None = None


class ScheduleSlot(BaseModel):
    starts: datetime
    ends: datetime
    target_c: float | None = None
    hot_water_on: bool | None = None


class ScheduleNowNextLater(BaseModel):
    now: ScheduleSlot
    next: ScheduleSlot
    later: ScheduleSlot


class HeatingStatus(BaseModel):
    id: str
    name: str
    kind: ZoneKind
    available: bool
    problem: str | None = None
    current_temp_c: float | None = None
    target_temp_c: float | None = None
    mode: HeatingMode | None = Field(None, description="Underlying mode (kept during a boost)")
    frost_protection_c: float | None = None
    heating_on: bool | None = Field(None, description="Calling for heat right now")
    boost: Boost = Field(default_factory=lambda: Boost(active=False))
    min_temp_c: float = 5.0
    max_temp_c: float = 32.0
    battery_pct: int | None = None
    schedule: ScheduleNowNextLater | None = Field(None, description="Only in SCHEDULE mode")
    as_of: datetime


class HotWaterStatus(BaseModel):
    id: str
    name: str
    available: bool
    problem: str | None = None
    mode: HotWaterMode | None = None
    is_on: bool | None = Field(None, description="Hot water is heating right now")
    boost: Boost = Field(default_factory=lambda: Boost(active=False))
    schedule: ScheduleNowNextLater | None = None
    as_of: datetime
