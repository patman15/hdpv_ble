"""Diagnostics for the Hunter Douglas PowerView (BLE) integration.

Nothing in this module interprets what it collects, by design. The integration
used to classify a shade's power source and hide the battery entities of any
shade it judged hardwired; the classification was built on samples taken from
hardwired shades only, so it misread battery shades as hardwired and hid the
very sensors it was meant to protect. That code is gone.

This dump gathers the evidence needed to get the encoding right instead of
guessing at it: the hub's record verbatim, the raw advertisement, and the raw
0xFFDE reply, correlated per shade so a user can label each one and report back.
Three things are unknown and each is answered by a field below:

  * what the hub reports in `powerType` for a *known* battery shade
    (`shades[].hub_record`)
  * whether the 0xFFDE reply differs at all between battery and hardwired
    shades (`shades[].power_status_0xffde`)
  * whether 0xFFDE byte 2 is a real battery percentage — it reads 100 on
    hardwired shades — which would beat the advertisement's four-step level
    (`shades[].advertisement.power_level_code`)
"""

import asyncio
from typing import Any, Final

import aiohttp
from bleak.exc import BleakError

from homeassistant.components import bluetooth
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import DeviceEntry
from homeassistant.helpers.redact import async_redact_data

from . import ConfigEntryType
from .const import CONF_HOME_KEY, CONF_HUB_URL, DOMAIN, MFCT_ID
from .coordinator import PVCoordinator

# home_key is the AES key that drives the shades and home_id identifies the
# PowerView network; a serial number identifies the owner's hardware. None of
# them are needed to answer the power-source question, and this file is meant
# to be attached to a public issue.
TO_REDACT: Final[set[str]] = {
    CONF_HOME_KEY,
    "homeId",
    "home_id",
    "serial_nr",
    "serial_number",
}

# Reading 0xFFDE means connecting to the shade. Opening one connection per shade
# at once is the adapter contention that made the old startup-time power query
# flaky in the first place, so keep the fan-out bounded even though a user has
# to click a button to get here.
_MAX_PARALLEL_QUERIES: Final[int] = 3
_QUERY_TIMEOUT_S: Final[float] = 20.0
_HUB_TIMEOUT_S: Final[float] = 10.0

WHAT_WE_NEED: Final[str] = (
    "This integration does not detect a shade's power source, so every shade "
    "gets battery entities whether or not it has a battery. The values below "
    "are raw and uninterpreted on purpose. To help us encode this correctly, "
    "please attach this file to the GitHub issue and tell us, for each shade "
    "listed under 'shades', whether it runs on a battery wand, on a "
    "rechargeable battery, or is hardwired to mains power."
)


async def _async_hub_shades(
    hass: HomeAssistant, hub_url: str
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Return the hub's status and its raw /home/shades records, by BLE name.

    Records pass through verbatim. Establishing what the hub actually sends is
    the point, so nothing here is mapped, renamed or filtered.
    """
    if not hub_url:
        return {"configured": False}, {}

    session = async_get_clientsession(hass)
    timeout = aiohttp.ClientTimeout(total=_HUB_TIMEOUT_S)
    try:
        async with session.get(f"{hub_url}/home/shades", timeout=timeout) as resp:
            resp.raise_for_status()
            shades = await resp.json(content_type=None)
    except (TimeoutError, aiohttp.ClientError, ValueError) as ex:
        return {
            "configured": True,
            "reachable": False,
            "error": f"{type(ex).__name__}: {ex}",
        }, {}

    records: dict[str, dict[str, Any]] = {
        shade["bleName"]: shade for shade in shades or [] if shade.get("bleName")
    }
    status: dict[str, Any] = {
        "configured": True,
        "reachable": True,
        "shade_count": len(records),
        # Hub records are joined to shades by an exact name match, so a shade
        # whose advertised name differs from the hub's bleName silently loses
        # its hub data. List the names to make such a near-miss visible.
        "ble_names": sorted(records),
    }
    return status, records


def _advertisement(hass: HomeAssistant, coord: PVCoordinator) -> dict[str, Any]:
    """Return the shade's most recent advertisement, raw and decoded."""
    service_info = bluetooth.async_last_service_info(
        hass, coord.address, connectable=True
    )
    if service_info is None:
        return {"error": "no advertisement seen for this shade"}

    raw = bytes(service_info.manufacturer_data.get(MFCT_ID, b""))
    return {
        "raw": raw.hex(" "),
        "length": len(raw),
        # The top two bits of byte 8 are what the battery sensor reports, via a
        # 3/2/1/0 -> 100/50/20/0 % table. A hardwired shade is known to sit at
        # 3; report the code itself so what a battery shade puts here can be
        # compared against it.
        "power_level_code": raw[8] >> 6 if len(raw) == 9 else None,
        "rssi": service_info.rssi,
        "stale": not coord.data_available,
        "decoded": async_redact_data(dict(coord.data), TO_REDACT),
    }


async def _async_power_status(
    coord: PVCoordinator, sem: asyncio.Semaphore
) -> dict[str, Any]:
    """Read one shade's raw 0xFFDE reply over BLE.

    Failure is data too, not an error to raise: a shade that cannot be reached
    still belongs in the report, so the reason is recorded and the dump goes on.
    """
    if coord.api.encrypted and not coord.api.has_key:
        return {"error": "shade is encrypted and no home key is configured"}

    async with sem:
        try:
            payload = await asyncio.wait_for(
                coord.api.query_power_status(), timeout=_QUERY_TIMEOUT_S
            )
        except (BleakError, TimeoutError) as ex:
            return {"error": f"{type(ex).__name__}: {ex}"}

    return {
        "raw": payload.hex(" "),
        "length": len(payload),
        "bytes": {f"b{idx}": value for idx, value in enumerate(payload)},
    }


async def _async_shade(
    hass: HomeAssistant,
    coord: PVCoordinator,
    hub_records: dict[str, dict[str, Any]],
    sem: asyncio.Semaphore,
) -> dict[str, Any]:
    """Collect every raw power-related signal available for one shade."""
    ble_name = coord.api.name
    record = hub_records.get(ble_name)
    return {
        "name": coord.friendly_name,
        "ble_name": ble_name,
        "address": coord.address,
        "type_id": coord.type_id,
        "device": async_redact_data(dict(coord.dev_details), TO_REDACT),
        "advertisement": _advertisement(hass, coord),
        "power_status_0xffde": await _async_power_status(coord, sem),
        "hub_matched": record is not None,
        "hub_record": async_redact_data(record, TO_REDACT) if record else None,
    }


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntryType
) -> dict[str, Any]:
    """Return diagnostics for every shade in the config entry.

    Deliberately dumps the whole entry rather than one device: the encoding can
    only be settled by comparing a battery shade against a hardwired one, so a
    single file covering a mixed install is exactly what is wanted.
    """
    hub_status, hub_records = await _async_hub_shades(
        hass, entry.data.get(CONF_HUB_URL, "")
    )
    sem = asyncio.Semaphore(_MAX_PARALLEL_QUERIES)
    shades = await asyncio.gather(
        *(
            _async_shade(hass, coord, hub_records, sem)
            for coord in entry.runtime_data.values()
        )
    )
    return {
        "what_we_need_from_you": WHAT_WE_NEED,
        "config_entry": async_redact_data(dict(entry.data), TO_REDACT),
        "hub": hub_status,
        "shades": shades,
    }


async def async_get_device_diagnostics(
    hass: HomeAssistant, entry: ConfigEntryType, device: DeviceEntry
) -> dict[str, Any]:
    """Return diagnostics for a single shade."""
    addresses = {ident[1] for ident in device.identifiers if ident[0] == DOMAIN}
    coord = next(
        (coord for addr, coord in entry.runtime_data.items() if addr in addresses),
        None,
    )
    if coord is None:
        return {"error": "no coordinator is running for this device"}

    hub_status, hub_records = await _async_hub_shades(
        hass, entry.data.get(CONF_HUB_URL, "")
    )
    return {
        "what_we_need_from_you": WHAT_WE_NEED,
        "hub": hub_status,
        "shade": await _async_shade(hass, coord, hub_records, asyncio.Semaphore(1)),
    }
