"""The FastMCP server. Read-only for now; write tools will sit behind HIVE_MCP_ALLOW_WRITES.

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

from . import readings
from .auth import ReauthRequired
from .config import Settings
from .credentials import CredentialsMissing
from .hive_client import HiveClient, HiveUnavailable, make_client
from .snapshot import HiveSnapshot
from .views import (
    DeviceList,
    HeatingReport,
    HotWaterView,
    device_view,
    freshness,
    heating_view,
    hot_water_view,
)

_LOGGER = logging.getLogger(__name__)

INSTRUCTIONS = """\
Reads a Hive Active Heating system (UK). Temperatures are in °C; dates and times
are UK local time, written DD/MM/YYYY and 24-hour. There are two kinds of
heating zone: boiler zones (a thermostat switching the boiler) and TRV zones
(radiator valves). A zone that is OFF still holds a frost-protection
temperature. Some zones may be listed as unavailable with the reason; report
that rather than leaving them out. Readings can be up to a couple of minutes
old; `stale: true` means Hive couldn't be reached and the data is older.
These tools are read-only.
"""

READ_ONLY = {"readOnlyHint": True, "idempotentHint": True, "openWorldHint": True}

T = TypeVar("T")


def create_server(
    settings: Settings, client_factory: Callable[[Settings], HiveClient] = make_client
) -> FastMCP:
    @asynccontextmanager
    async def lifespan(_server: FastMCP) -> AsyncIterator[dict[str, Any]]:
        client = client_factory(settings)
        _LOGGER.info("Hive MCP server starting (%s).", "fake data" if settings.fake else "live")
        try:
            yield {"client": client}
        finally:
            await client.close()
            _LOGGER.info("Hive MCP server stopped.")

    mcp = FastMCP(
        "hive-heating",
        instructions=INSTRUCTIONS,
        lifespan=lifespan,
        mask_error_details=True,
    )

    async def with_snapshot(ctx: Context, use: Callable[[HiveSnapshot], T]) -> T:
        """Fetch a snapshot and turn known failures into messages Claude can relay."""
        client: HiveClient = ctx.lifespan_context["client"]
        try:
            snap = await client.snapshot()
            return use(snap)
        except readings.UnknownDevice as err:
            raise ToolError(str(err)) from err
        except (CredentialsMissing, ReauthRequired) as err:
            _LOGGER.warning("Hive sign-in needed: %s", err)
            raise ToolError(
                f"Hive needs signing in again ({err}). In a terminal, in the hive-mcp "
                "project folder, run: uv run hive-mcp login"
            ) from err
        except HiveUnavailable as err:
            raise ToolError("Couldn't reach Hive. Try again in a minute.") from err

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

    return mcp


def serve(settings: Settings) -> None:
    create_server(settings).run(transport="stdio", show_banner=False)
