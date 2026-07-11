"""The Hunter Douglas PowerView (BLE) integration.

@author: patman15
@license: Apache-2.0 license
"""

import base64
from collections.abc import Callable

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
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.dispatcher import (
    async_dispatcher_connect,
    async_dispatcher_send,
)
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import UUID_COV_SERVICE as UUID
from .const import (
    CONF_FRIENDLY_NAMES,
    CONF_HUB_URL,
    DOMAIN,
    LOGGER,
    MFCT_ID,
    SIGNAL_NEW_SHADE,
)
from .coordinator import PVCoordinator

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


async def _fetch_shade_names(hass: HomeAssistant, hub_url: str) -> dict[str, str]:
    """Fetch the shades' friendly names from the hub, keyed by BLE advert name.

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

    names: dict[str, str] = {}
    for shade in shades or []:
        ble_name = shade.get("bleName", "")
        if not ble_name:
            continue
        name_b64 = shade.get("name", "")
        try:
            name = base64.b64decode(name_b64).decode("utf-8") if name_b64 else ble_name
        except Exception:  # noqa: BLE001
            name = ble_name
        names[ble_name] = name
    return names


def _persist_cache_entry(
    hass: HomeAssistant,
    entry: ConfigEntryType,
    key: str,
    address: str,
    value: object,
) -> None:
    """Read-modify-write a per-address value into a cache stored on entry.data.

    HA replaces entry.data atomically, so concurrent setup tasks each pull
    the latest snapshot before merging — the no-op guard prevents writes
    that would just notify listeners with unchanged data.
    """
    cache = dict(entry.data.get(key, {}))
    if cache.get(address) == value:
        return
    cache[address] = value
    hass.config_entries.async_update_entry(entry, data={**entry.data, key: cache})


def _resolve_friendly_name(
    hass: HomeAssistant,
    entry: ConfigEntryType,
    service_info: BluetoothServiceInfoBleak,
    hub_name: str | None,
) -> str:
    """Resolve a shade's friendly name (Shelly-style) and refresh the cache.

    Hub data wins; otherwise fall back to the cached value from a prior
    successful resolution; otherwise fall back to the BLE advert name.
    """
    address = service_info.address
    cached_names: dict[str, str] = entry.data.get(CONF_FRIENDLY_NAMES, {})
    if hub_name is not None:
        friendly_name = hub_name
    elif address in cached_names:
        friendly_name = cached_names[address]
    else:
        friendly_name = service_info.name or address

    _persist_cache_entry(hass, entry, CONF_FRIENDLY_NAMES, address, friendly_name)
    return friendly_name


async def _async_setup_shade(
    hass: HomeAssistant,
    entry: ConfigEntryType,
    service_info: BluetoothServiceInfoBleak,
    shade_names: dict[str, str],
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

    friendly_name = _resolve_friendly_name(
        hass, entry, service_info, shade_names.get(service_info.name)
    )

    coordinator = PVCoordinator(hass, ble_device, entry.data.copy(), friendly_name)

    entry.runtime_data[address] = coordinator
    entry.async_on_unload(coordinator.async_start())

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

    # Resolve shade friendly names from hub if available
    hub_url = entry.data.get(CONF_HUB_URL, "")
    shade_names: dict[str, str] = {}
    if hub_url:
        shade_names = await _fetch_shade_names(hass, hub_url)

    # Forward platforms first so dispatched entities have their setup ready
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Kick off shade setup for already-discovered BLE devices (non-blocking)
    for service_info in async_discovered_service_info(hass, connectable=True):
        if (
            MFCT_ID in service_info.manufacturer_data
            and UUID in service_info.service_uuids
        ):
            hass.async_create_task(
                _async_setup_shade(hass, entry, service_info, shade_names)
            )

    # Register for future BLE discoveries
    def _async_discovered_device(
        service_info: BluetoothServiceInfoBleak,
        change: bluetooth.BluetoothChange,
    ) -> None:
        if service_info.address not in entry.runtime_data:
            hass.async_create_task(
                _async_setup_shade(hass, entry, service_info, shade_names)
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


async def async_remove_config_entry_device(
    hass: HomeAssistant,
    entry: ConfigEntryType,
    device_entry: dr.DeviceEntry,
) -> bool:
    """Allow user-driven device removal; purge cached state for that address."""
    addresses = {ident[1] for ident in device_entry.identifiers if ident[0] == DOMAIN}
    if not addresses:
        return True

    new_data = dict(entry.data)
    cache = dict(new_data.get(CONF_FRIENDLY_NAMES, {}))
    for addr in addresses:
        cache.pop(addr, None)
    new_data[CONF_FRIENDLY_NAMES] = cache
    hass.config_entries.async_update_entry(entry, data=new_data)

    for addr in addresses:
        coord = entry.runtime_data.pop(addr, None)
        if coord is not None:
            # _async_stop is a parent-class (DataUpdateCoordinator) convention;
            # entry.async_on_unload only fires on full entry unload, so we
            # invoke it directly here for per-device removal.
            coord._async_stop()  # noqa: SLF001

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntryType) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        entry.runtime_data.clear()

    LOGGER.debug("Unloaded config entry: %s, ok? %s!", entry.unique_id, str(unload_ok))
    return unload_ok
