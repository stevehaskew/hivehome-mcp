"""The FastMCP server: read-only tools, plus write tools when HIVE_MCP_ALLOW_WRITES is set.

The write tools aren't registered at all unless writes are allowed and the audit
log can be opened. Limits and mode rules are in writes.py; every attempt is
recorded in the audit log (audit.py).

Runs over stdio, so nothing here may write to stdout: logging goes to stderr
and the log file (see logging_setup.py).
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any, TypeVar

from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError

from . import readings, writes
from .audit import AuditLog, Result
from .auth import ReauthRequired
from .config import Settings
from .credentials import CredentialsMissing
from .hive_client import HiveClient, HiveUnavailable, WriteFailed, make_client
from .models import HeatingMode, HotWaterMode
from .snapshot import HiveSnapshot
from .views import (
    DeviceList,
    HeatingReport,
    HotWaterView,
    WeeklyScheduleView,
    WriteResultView,
    device_summary,
    device_view,
    freshness,
    heating_view,
    hot_water_view,
    weekly_schedule_view,
)

_LOGGER = logging.getLogger(__name__)

_ABOUT = """\
Reads{and_controls} a Hive Active Heating system (UK). Temperatures are in °C; dates and
times are UK local time, written DD/MM/YYYY and 24-hour. There are two kinds of
heating zone: boiler zones (a thermostat switching the boiler) and TRV zones
(radiator valves). A zone that is OFF still holds a frost-protection
temperature. Some zones may be listed as unavailable with the reason; report
that rather than leaving them out. Readings can be up to a couple of minutes
old; `stale: true` means Hive couldn't be reached and the data is older.
"""

INSTRUCTIONS = _ABOUT.format(and_controls="") + "These tools are read-only.\n"

WRITE_INSTRUCTIONS = _ABOUT.format(and_controls=" and controls") + (
    "\nThis controls a real building's heating. Only change it when the user asks, and "
    "tell them exactly what changed. Temperatures must be "
    f"{writes.MIN_TEMP_C:g}–{writes.MAX_TEMP_C:g}°C in {writes.TEMP_STEP_C:g}°C steps and "
    f"boosts {writes.MIN_BOOST_MINUTES}–{writes.MAX_BOOST_MINUTES} minutes; values outside "
    "that are refused, not adjusted. MANUAL and OFF stay until someone changes them, so "
    "mention that.\n"
)

READ_ONLY = {"readOnlyHint": True, "idempotentHint": True, "openWorldHint": True}
SET_VALUE = {
    "readOnlyHint": False,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": True,
}
BOOST = {**SET_VALUE, "idempotentHint": False}

T = TypeVar("T")


def _tool_error(err: Exception) -> ToolError:
    """The message Claude sees for a known failure."""
    if isinstance(err, readings.UnknownDevice | writes.WriteRejected):
        return ToolError(str(err), log_level=logging.INFO)
    if isinstance(err, CredentialsMissing | ReauthRequired):
        _LOGGER.warning("Hive sign-in needed: %s", err)
        return ToolError(
            f"Hive needs signing in again ({err}). In a terminal, in the hive-mcp "
            "project folder, run: uv run hive-mcp login",
            log_level=logging.INFO,  # already logged above
        )
    if isinstance(err, WriteFailed):
        return ToolError(f"{err} Nothing was changed.", log_level=logging.WARNING)
    return ToolError("Couldn't reach Hive. Try again in a minute.", log_level=logging.WARNING)


_KNOWN_ERRORS = (
    readings.UnknownDevice,
    writes.WriteRejected,
    CredentialsMissing,
    ReauthRequired,
    HiveUnavailable,
    WriteFailed,
)


def _open_audit_log(settings: Settings) -> AuditLog | None:
    if not settings.allow_writes:
        return None
    try:
        return AuditLog(settings.log_dir / "audit.log")
    except OSError as err:
        _LOGGER.error("Write tools disabled: can't open the audit log: %s", err)
        return None


def create_server(
    settings: Settings, client_factory: Callable[[Settings], HiveClient] = make_client
) -> FastMCP:
    audit = _open_audit_log(settings)

    @asynccontextmanager
    async def lifespan(_server: FastMCP) -> AsyncIterator[dict[str, Any]]:
        client = client_factory(settings)
        _LOGGER.info(
            "Hive MCP server starting (%s, %s).",
            "fake data" if settings.fake else "live",
            "writes allowed" if audit else "read-only",
        )
        try:
            yield {"client": client}
        finally:
            await client.close()
            if audit:
                audit.close()
            _LOGGER.info("Hive MCP server stopped.")

    mcp = FastMCP(
        "hive-heating",
        instructions=WRITE_INSTRUCTIONS if audit else INSTRUCTIONS,
        lifespan=lifespan,
        mask_error_details=True,
    )

    async def with_snapshot(ctx: Context, use: Callable[[HiveSnapshot], T]) -> T:
        """Fetch a snapshot and turn known failures into messages Claude can relay."""
        client: HiveClient = ctx.lifespan_context["client"]
        try:
            snap = await client.snapshot()
            return use(snap)
        except _KNOWN_ERRORS as err:
            raise _tool_error(err) from err

    @mcp.tool(annotations=READ_ONLY)
    async def list_devices(ctx: Context) -> DeviceList:
        """List the heating zones and hot water, with availability and battery levels.

        Use the names (or ids) from here with the other tools.
        """

        def build(snap: HiveSnapshot) -> DeviceList:
            devices = [device_view(d) for d in readings.list_devices(snap)]
            return DeviceList(**freshness(snap).model_dump(), devices=devices)

        return await with_snapshot(ctx, build)

    @mcp.tool(annotations=READ_ONLY)
    async def get_heating_status(ctx: Context, zone: str | None = None) -> HeatingReport:
        """Current temperature, target, mode, boost and schedule for heating zones.

        Args:
            zone: Zone name (case-insensitive) or id. Leave empty for every zone.
        """

        def build(snap: HiveSnapshot) -> HeatingReport:
            if zone:
                ids = [readings.find_device(snap, zone, "heating")]
            else:
                ids = [d.id for d in readings.list_devices(snap) if d.type == "heating"]
            zones = [heating_view(readings.heating_status(snap, pid)) for pid in ids]
            return HeatingReport(**freshness(snap).model_dump(), zones=zones)

        return await with_snapshot(ctx, build)

    @mcp.tool(annotations=READ_ONLY)
    async def get_hot_water_status(ctx: Context, system: str | None = None) -> HotWaterView:
        """Whether the hot water is on, its mode, boost and schedule.

        Args:
            system: Only needed if there is more than one hot water system.
        """

        def build(snap: HiveSnapshot) -> HotWaterView:
            pid = readings.find_device(snap, system, "hot_water")
            return hot_water_view(readings.hot_water_status(snap, pid), snap)

        return await with_snapshot(ctx, build)

    @mcp.tool(annotations=READ_ONLY)
    async def get_schedule(ctx: Context, zone: str) -> WeeklyScheduleView:
        """The full weekly schedule (Monday to Sunday) for a heating zone or the hot water.

        Each day runs midnight to midnight, as a list of times and target temperatures
        (or hot water on/off). Use get_heating_status for just now, next and later.

        Args:
            zone: Heating zone name (case-insensitive) or id, or "Hot water".
        """

        def build(snap: HiveSnapshot) -> WeeklyScheduleView:
            pid = readings.find_device(snap, zone, None)
            return weekly_schedule_view(readings.weekly_schedule(snap, pid), snap)

        return await with_snapshot(ctx, build)

    if audit:
        _add_write_tools(mcp, audit)
    return mcp


Planner = Callable[[HiveSnapshot, str], writes.WritePlan | None]


def _add_write_tools(mcp: FastMCP, audit: AuditLog) -> None:
    async def perform(
        ctx: Context,
        tool: str,
        requested: dict[str, Any],
        zone: str | None,
        wanted: readings.DeviceType,
        plan: Planner,
    ) -> WriteResultView:
        """Read Hive fresh, check the request, send it, and audit every outcome."""
        client: HiveClient = ctx.lifespan_context["client"]
        name, pid, before = zone, None, None

        def record(result: Result, reason: str | None = None, **extra: Any) -> None:
            audit.record(
                tool=tool,
                zone=name,
                zone_id=pid,
                requested=requested,
                before=before,
                result=result,
                reason=reason,
                **extra,
            )

        try:
            snap = await client.snapshot(refresh=True)
            if snap.stale:
                raise HiveUnavailable("Couldn't reach Hive to check the current settings.")
            pid = readings.find_device(snap, zone, wanted)
            name, before = readings.device_name(snap, pid), writes.settings_of(snap, pid)
            todo = plan(snap, pid)
        except _KNOWN_ERRORS as err:
            rejected = isinstance(err, readings.UnknownDevice | writes.WriteRejected)
            record("rejected" if rejected else "failed", str(err))
            raise _tool_error(err) from err

        if todo is None:
            record("no_change", after=before)
            return WriteResultView(
                **freshness(snap).model_dump(),
                zone=name,
                id=pid,
                changed=False,
                confirmed=True,
                summary=f"No change needed: {name} is already set that way.",
                now=device_summary(snap, pid),
            )

        try:
            after_snap = await client.write(todo.product_type, todo.product_id, todo.kwargs)
        except _KNOWN_ERRORS as err:
            record("failed", str(err), sent=todo.kwargs)
            raise _tool_error(err) from err
        missing = writes.unconfirmed(todo, after_snap)
        after = writes.settings_of(after_snap, pid)
        record(
            "unconfirmed" if missing else "ok",
            "; ".join(missing) or None,
            sent=todo.kwargs,
            after=after,
        )
        _LOGGER.info("%s: %s (%s)", tool, todo.change, "unconfirmed" if missing else "ok")
        summary = todo.change + "."
        if todo.note:
            summary += " " + todo.note
        if missing:
            summary += (
                " Hive accepted the change, but its latest reading doesn't show it yet; "
                "check again in a minute or two."
            )
        return WriteResultView(
            **freshness(after_snap).model_dump(),
            zone=name,
            id=pid,
            changed=True,
            confirmed=not missing,
            summary=summary,
            now=device_summary(after_snap, pid),
        )

    # The limits in the docstrings below must match writes.py (a test checks).
    @mcp.tool(annotations=SET_VALUE)
    async def set_target_temperature(ctx: Context, zone: str, temp_c: float) -> WriteResultView:
        """Change a heating zone's target temperature.

        In SCHEDULE mode this overrides the schedule until its next change; in MANUAL
        it stays until changed. Refused for a zone that is OFF (use set_heating_mode)
        or boosting.

        Args:
            zone: Heating zone name (case-insensitive) or id.
            temp_c: Target in °C, 5–25°C in 0.5°C steps.
        """
        return await perform(
            ctx,
            "set_target_temperature",
            {"temp_c": temp_c},
            zone,
            "heating",
            lambda snap, pid: writes.plan_target(snap, pid, temp_c),
        )

    @mcp.tool(annotations=SET_VALUE)
    async def set_heating_mode(
        ctx: Context, zone: str, mode: HeatingMode, temp_c: float | None = None
    ) -> WriteResultView:
        """Set a heating zone to follow its schedule, hold a fixed temperature, or switch off.

        MANUAL and OFF stay until someone changes them. Setting a mode ends any boost.

        Args:
            zone: Heating zone name (case-insensitive) or id.
            mode: SCHEDULE, MANUAL or OFF.
            temp_c: Required for MANUAL (5–25°C in 0.5°C steps); leave out for SCHEDULE and OFF.
        """
        return await perform(
            ctx,
            "set_heating_mode",
            {"mode": mode, "temp_c": temp_c},
            zone,
            "heating",
            lambda snap, pid: writes.plan_heating_mode(snap, pid, mode, temp_c),
        )

    @mcp.tool(annotations=BOOST)
    async def boost_heating(
        ctx: Context, zone: str, minutes: int, temp_c: float
    ) -> WriteResultView:
        """Boost a heating zone to a temperature for a while, then go back to its mode.

        Refused if the zone is already boosting (cancel it first).

        Args:
            zone: Heating zone name (case-insensitive) or id.
            minutes: How long, 15–120 minutes.
            temp_c: Boost temperature in °C, 5–25°C in 0.5°C steps.
        """
        return await perform(
            ctx,
            "boost_heating",
            {"minutes": minutes, "temp_c": temp_c},
            zone,
            "heating",
            lambda snap, pid: writes.plan_heating_boost(snap, pid, minutes, temp_c),
        )

    @mcp.tool(annotations=SET_VALUE)
    async def cancel_heating_boost(ctx: Context, zone: str) -> WriteResultView:
        """End a heating zone's boost early; it goes back to the mode it was in before.

        Args:
            zone: Heating zone name (case-insensitive) or id.
        """
        return await perform(
            ctx, "cancel_heating_boost", {}, zone, "heating", writes.plan_cancel_heating_boost
        )

    @mcp.tool(annotations=SET_VALUE)
    async def set_hot_water_mode(
        ctx: Context, mode: HotWaterMode, system: str | None = None
    ) -> WriteResultView:
        """Set the hot water to follow its schedule, stay on, or stay off.

        ON and OFF stay until someone changes them. Setting a mode ends any boost.

        Args:
            mode: SCHEDULE, ON or OFF.
            system: Only needed if there is more than one hot water system.
        """
        return await perform(
            ctx,
            "set_hot_water_mode",
            {"mode": mode},
            system,
            "hot_water",
            lambda snap, pid: writes.plan_hot_water_mode(snap, pid, mode),
        )

    @mcp.tool(annotations=BOOST)
    async def boost_hot_water(
        ctx: Context, minutes: int, system: str | None = None
    ) -> WriteResultView:
        """Turn the hot water on for a while, then go back to its mode.

        Refused if it is already boosting (cancel it first).

        Args:
            minutes: How long, 15–120 minutes.
            system: Only needed if there is more than one hot water system.
        """
        return await perform(
            ctx,
            "boost_hot_water",
            {"minutes": minutes},
            system,
            "hot_water",
            lambda snap, pid: writes.plan_hot_water_boost(snap, pid, minutes),
        )

    @mcp.tool(annotations=SET_VALUE)
    async def cancel_hot_water_boost(ctx: Context, system: str | None = None) -> WriteResultView:
        """End a hot water boost early; it goes back to the mode it was in before.

        Args:
            system: Only needed if there is more than one hot water system.
        """
        return await perform(
            ctx,
            "cancel_hot_water_boost",
            {},
            system,
            "hot_water",
            writes.plan_cancel_hot_water_boost,
        )


def serve(settings: Settings) -> None:
    create_server(settings).run(transport="stdio", show_banner=False)
