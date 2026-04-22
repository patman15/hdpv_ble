"""The Hunter Douglas PowerView (BLE) integration.

@author: patman15
@license: Apache-2.0 license
"""

import asyncio
import base64
from collections.abc import Callable
from dataclasses import dataclass

import aiohttp
from bleak.backends.device import BLEDevice
from bleak.exc import BleakError

from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import (
    BluetoothCallbackMatcher,
    BluetoothScanningMode,
    BluetoothServiceInfoBleak,
    async_ble_device_from_address,
    async_discovered_service_info,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.dispatcher import (
    async_dispatcher_connect,
    async_dispatcher_send,
)
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import UUID_COV_SERVICE as UUID
from .const import (
    CONF_HUB_URL,
    CONF_POWER_TYPES,
    LOGGER,
    MFCT_ID,
    SIGNAL_NEW_SHADE,
    PowerType,
)
from .coordinator import PVCoordinator

# Hub /home/shades powerType (aiopvapi Gen3: 1=hardwired, 2=battery,
# 3=rechargeable) → our internal PowerType (matches BLE 0xFFDE byte 0).
_HUB_POWERTYPE_TO_INTERNAL: dict[int, PowerType] = {
    1: PowerType.HARDWIRED,
    2: PowerType.BATTERY,
    3: PowerType.RECHARGEABLE,
}


@dataclass(frozen=True)
class ShadeMetadata:
    """Per-shade metadata sourced from the G3 hub's /home/shades response."""

    friendly_name: str
    power_type: PowerType | None = None


PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.COVER,
    Platform.NUMBER,
    Platform.SENSOR,
]

type HubRuntimeData = dict[str, PVCoordinator]
type ConfigEntryType = ConfigEntry[HubRuntimeData]

type AddEntitiesFn = Callable[[PVCoordinator, AddEntitiesCallback], None]


def async_setup_shade_platform(
    hass: HomeAssistant,
    config_entry: ConfigEntryType,
    async_add_entities: AddEntitiesCallback,
    add_fn: AddEntitiesFn,
) -> None:
    """Set up a platform for all current and future shades."""
    for coordinator in config_entry.runtime_data.values():
        add_fn(coordinator, async_add_entities)

    @callback
    def _async_new_shade(coordinator: PVCoordinator) -> None:
        add_fn(coordinator, async_add_entities)

    config_entry.async_on_unload(
        async_dispatcher_connect(
            hass,
            SIGNAL_NEW_SHADE.format(entry_id=config_entry.entry_id),
            _async_new_shade,
        )
    )


async def _fetch_shade_metadata(
    hass: HomeAssistant, hub_url: str
) -> dict[str, ShadeMetadata]:
    """Fetch per-shade metadata (friendly name, power_type) from the hub.

    Returns empty dict on failure.
    """
    session = async_get_clientsession(hass)
    timeout = aiohttp.ClientTimeout(total=10)
    try:
        async with session.get(f"{hub_url}/home/shades", timeout=timeout) as resp:
            resp.raise_for_status()
            shades = await resp.json(content_type=None)
    except (TimeoutError, aiohttp.ClientError, ValueError):
        return {}

    metadata: dict[str, ShadeMetadata] = {}
    for shade in shades or []:
        ble_name = shade.get("bleName", "")
        if not ble_name:
            continue
        name_b64 = shade.get("name", "")
        try:
            name = base64.b64decode(name_b64).decode("utf-8") if name_b64 else ble_name
        except Exception:  # noqa: BLE001
            name = ble_name
        raw_pt = shade.get("powerType")
        power_type = (
            _HUB_POWERTYPE_TO_INTERNAL.get(raw_pt) if isinstance(raw_pt, int) else None
        )
        metadata[ble_name] = ShadeMetadata(friendly_name=name, power_type=power_type)
    return metadata


async def _async_setup_shade(
    hass: HomeAssistant,
    entry: ConfigEntryType,
    service_info: BluetoothServiceInfoBleak,
    shade_metadata: dict[str, ShadeMetadata],
) -> None:
    """Create a coordinator for a newly discovered shade."""
    address = service_info.address

    if address in entry.runtime_data:
        return

    ble_device: BLEDevice | None = async_ble_device_from_address(
        hass=hass, address=address, connectable=True
    )
    if not ble_device:
        LOGGER.debug("BLE device %s not connectable, skipping", address)
        return

    meta = shade_metadata.get(service_info.name)
    friendly_name = meta.friendly_name if meta else service_info.name
    cached_map: dict[str, int] = dict(entry.data.get(CONF_POWER_TYPES, {}))
    power_type: PowerType | None = meta.power_type if meta else None
    if power_type is None:
        cached_value = cached_map.get(address)
        if isinstance(cached_value, int):
            try:
                power_type = PowerType(cached_value)
            except ValueError:
                power_type = None

    coordinator = PVCoordinator(
        hass, ble_device, entry.data.copy(), friendly_name, power_type
    )

    entry.runtime_data[address] = coordinator
    entry.async_on_unload(coordinator.async_start())

    # BLE fallback for hub-less (manual-key) setups: one-time query so the
    # entity platforms (dispatched below) see the resolved value.
    if coordinator.power_type is None and coordinator.api.has_key:
        try:
            await asyncio.wait_for(coordinator.query_power_type(), timeout=10)
        except (BleakError, TimeoutError):
            LOGGER.debug("power_type BLE fallback failed for %s", address)

    # Persist any newly-resolved power_type so hub-down restarts keep classification.
    if coordinator.power_type is not None and cached_map.get(address) != int(
        coordinator.power_type
    ):
        cached_map[address] = int(coordinator.power_type)
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_POWER_TYPES: cached_map}
        )

    # Populate dev_details before entity dispatch so the device registers with
    # firmware/serial on first creation — the HA registry doesn't re-read
    # DeviceInfo later. Failures are retried from the advertisement handler.
    try:
        await coordinator.query_dev_info()
    except (BleakError, TimeoutError):
        LOGGER.debug(
            "Initial device info query failed for %s (%s); will retry via adverts",
            friendly_name,
            address,
        )

    async_dispatcher_send(
        hass,
        SIGNAL_NEW_SHADE.format(entry_id=entry.entry_id),
        coordinator,
    )


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntryType) -> bool:
    """Set up PowerView Home from a config entry."""
    LOGGER.debug("Setup of %s", repr(entry))

    entry.runtime_data = {}

    # Resolve shade friendly names and power_type from hub if available
    hub_url = entry.data.get(CONF_HUB_URL, "")
    shade_metadata: dict[str, ShadeMetadata] = {}
    if hub_url:
        shade_metadata = await _fetch_shade_metadata(hass, hub_url)

    # Forward platforms first so dispatched entities have their setup ready
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Kick off shade setup for already-discovered BLE devices (non-blocking)
    for service_info in async_discovered_service_info(hass, connectable=True):
        if (
            MFCT_ID in service_info.manufacturer_data
            and UUID in service_info.service_uuids
        ):
            hass.async_create_task(
                _async_setup_shade(hass, entry, service_info, shade_metadata)
            )

    # Register for future BLE discoveries
    def _async_discovered_device(
        service_info: BluetoothServiceInfoBleak,
        change: bluetooth.BluetoothChange,
    ) -> None:
        if service_info.address not in entry.runtime_data:
            hass.async_create_task(
                _async_setup_shade(hass, entry, service_info, shade_metadata)
            )

    entry.async_on_unload(
        bluetooth.async_register_callback(
            hass,
            _async_discovered_device,
            BluetoothCallbackMatcher(
                service_uuid=UUID,
                manufacturer_id=MFCT_ID,
            ),
            BluetoothScanningMode.ACTIVE,
        )
    )

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntryType) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        entry.runtime_data.clear()

    LOGGER.debug("Unloaded config entry: %s, ok? %s!", entry.unique_id, str(unload_ok))
    return unload_ok
