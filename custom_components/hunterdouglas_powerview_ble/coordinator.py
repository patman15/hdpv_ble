"""Home Assistant coordinator for Hunter Douglas PowerView (BLE) integration."""

import asyncio
import time
from typing import Any, Final

from bleak.backends.device import BLEDevice
from bleak.exc import BleakError

from homeassistant.components import bluetooth
from homeassistant.components.bluetooth.const import DOMAIN as BLUETOOTH_DOMAIN
from homeassistant.components.bluetooth.passive_update_coordinator import (
    PassiveBluetoothDataUpdateCoordinator,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo

from .api import SHADE_TYPE, PowerViewBLE, ShadeCapability, get_shade_capabilities
from .const import ATTR_RSSI, CONF_HOME_KEY, DOMAIN, LOGGER


class PVCoordinator(PassiveBluetoothDataUpdateCoordinator):
    """Update coordinator for a battery management system."""

    # Firmware isn't in the advert — pull it via GATT. Retry every minute
    # while empty, then re-check daily so firmware upgrades eventually surface.
    _DEV_INFO_REFRESH_S: Final[float] = 24 * 3600
    _DEV_INFO_RETRY_S: Final[float] = 60

    # Position/battery come only from V2 adverts. Past this many seconds without
    # a fresh V2 record we stop reporting the retained sample as current, so an
    # out-of-range shade doesn't keep showing a stale position.
    _STALE_AFTER_S: Final[float] = 300.0

    def __init__(
        self,
        hass: HomeAssistant,
        ble_device: BLEDevice,
        data: dict[str, Any],
        friendly_name: str | None = None,
    ) -> None:
        """Initialize BMS data coordinator."""
        assert ble_device.name is not None
        self._friendly_name = friendly_name or ble_device.name
        home_key_hex: str = data.get(CONF_HOME_KEY, "")
        home_key: bytes = (
            bytes.fromhex(home_key_hex) if len(home_key_hex) == 32 else b""
        )
        self.api = PowerViewBLE(ble_device, home_key)
        self.data: dict[str, int | float | bool] = {}
        self._last_v2_at: float = 0.0
        self._manuf_dat = data.get("manufacturer_data")
        self.dev_details: dict[str, str] = {}
        self.velocity: int = 0
        self._last_dev_info_at: float = 0.0
        self._dev_info_task: asyncio.Task[None] | None = None

        LOGGER.debug(
            "Initializing coordinator for %s (%s)",
            self._friendly_name,
            ble_device.address,
        )
        super().__init__(
            hass,
            LOGGER,
            ble_device.address,
            bluetooth.BluetoothScanningMode.ACTIVE,
        )

    @property
    def type_id(self) -> int | None:
        """Return the shade type ID from manufacturer data or live BLE data."""
        if self._manuf_dat:
            return int(bytes.fromhex(self._manuf_dat)[2])
        live = self.data.get("type_id")
        return int(live) if live is not None else None

    @property
    def shade_capabilities(self) -> ShadeCapability:
        """Return the shade capabilities based on type ID."""
        return get_shade_capabilities(self.type_id)

    @property
    def data_available(self) -> bool:
        """Whether the last V2 advertisement is recent enough to trust.

        Position, tilt and battery are only carried in V2 adverts. An
        out-of-range shade stops sending them while HA may still consider the
        device present, so past ``_STALE_AFTER_S`` we no longer report the
        retained sample as current.
        """
        return (
            self._last_v2_at != 0.0
            and time.monotonic() - self._last_v2_at < self._STALE_AFTER_S
        )

    async def query_dev_info(self) -> None:
        """Fetch device info over GATT and push into the device registry.

        The HA device registry does not re-read DeviceInfo when the underlying
        data changes, so we update it explicitly when new details arrive.
        """
        LOGGER.debug("%s: querying device info", self.name)
        self._last_dev_info_at = time.monotonic()
        new = await self.api.query_dev_info()
        if new and new != self.dev_details:
            self.dev_details.update(new)
            self._push_device_registry_update()

    def _push_device_registry_update(self) -> None:
        reg = dr.async_get(self.hass)
        device = reg.async_get_device(identifiers={(DOMAIN, self.address)})
        if device is None:
            return
        reg.async_update_device(
            device.id,
            sw_version=self.dev_details.get("sw_rev"),
            hw_version=self.dev_details.get("hw_rev"),
            serial_number=self.dev_details.get("serial_nr"),
        )

    async def _refresh_dev_info_safe(self) -> None:
        try:
            await self.query_dev_info()
        except (BleakError, TimeoutError) as ex:
            LOGGER.debug("%s: dev_info refresh failed: %s", self.name, ex)

    def _maybe_refresh_dev_info(self) -> None:
        """Schedule a background dev_info refresh if stale."""
        if self._dev_info_task is not None and not self._dev_info_task.done():
            return
        interval = (
            self._DEV_INFO_REFRESH_S
            if self.dev_details.get("sw_rev")
            else self._DEV_INFO_RETRY_S
        )
        if time.monotonic() - self._last_dev_info_at < interval:
            return
        self._dev_info_task = self.hass.async_create_background_task(
            self._refresh_dev_info_safe(), name=f"pvble_dev_info_{self.address}"
        )

    @property
    def device_info(self) -> DeviceInfo:
        """Return detailed device information for GUI."""
        LOGGER.debug("%s: device_info, %s", self._friendly_name, self.dev_details)
        return DeviceInfo(
            identifiers={
                (DOMAIN, self.address),
                (BLUETOOTH_DOMAIN, self.address),
            },
            connections={(CONNECTION_BLUETOOTH, self.address)},
            name=self._friendly_name,
            configuration_url=None,
            manufacturer="Hunter Douglas",
            model=(
                str(SHADE_TYPE.get(self.type_id, "unknown"))
                if self.type_id is not None
                else None
            ),
            model_id=(str(self.type_id) if self.type_id is not None else None),
            serial_number=self.dev_details.get("serial_nr"),
            sw_version=self.dev_details.get("sw_rev"),
            hw_version=self.dev_details.get("hw_rev"),
        )

    @property
    def device_present(self) -> bool:
        """Check if a device is present."""
        return bluetooth.async_address_present(
            self.hass, self.address, connectable=True
        )

    def _async_stop(self) -> None:
        """Shutdown coordinator and any connection."""
        LOGGER.debug("%s: shutting down BMS device", self.name)
        if self._dev_info_task is not None and not self._dev_info_task.done():
            self._dev_info_task.cancel()
        self.hass.async_create_task(self.api.disconnect())
        super()._async_stop()

    @callback
    def _async_handle_bluetooth_event(
        self,
        service_info: bluetooth.BluetoothServiceInfoBleak,
        change: bluetooth.BluetoothChange,
    ) -> None:
        """Handle a Bluetooth event."""

        LOGGER.debug("BLE event %s: %s", change, service_info.manufacturer_data)
        self.api.set_ble_device(service_info.device)

        # Merge onto the retained sample rather than replacing it: an advert
        # without a valid V2 record (wrong length / legacy format / 2073
        # absent) decodes to {} and must not wipe the last-known position,
        # tilt and battery state.
        new_data: dict[str, int | float | bool] = dict(self.data)
        new_data[ATTR_RSSI] = service_info.rssi
        if change == bluetooth.BluetoothChange.ADVERTISEMENT:
            decoded = self.api.dec_manufacturer_data(
                bytearray(service_info.manufacturer_data.get(2073, b""))
            )
            if decoded:
                new_data.update(decoded)
                self.api.encrypted = bool(decoded.get("home_id"))
                self._last_v2_at = time.monotonic()
                self._maybe_refresh_dev_info()

        if new_data == self.data:
            return
        self.data = new_data
        LOGGER.debug("data sample %s", self.data)
        super()._async_handle_bluetooth_event(service_info, change)
