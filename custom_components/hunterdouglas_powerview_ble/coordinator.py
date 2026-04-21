"""Home Assistant coordinator for Hunter Douglas PowerView (BLE) integration."""

from typing import Any

from bleak.backends.device import BLEDevice

from homeassistant.components import bluetooth
from homeassistant.components.bluetooth.const import DOMAIN as BLUETOOTH_DOMAIN
from homeassistant.components.bluetooth.passive_update_coordinator import (
    PassiveBluetoothDataUpdateCoordinator,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo

from .api import SHADE_TYPE, PowerViewBLE, ShadeCapability, get_shade_capabilities
from .const import ATTR_RSSI, CONF_HOME_KEY, DOMAIN, LOGGER, PowerType


class PVCoordinator(PassiveBluetoothDataUpdateCoordinator):
    """Update coordinator for a battery management system."""

    def __init__(
        self,
        hass: HomeAssistant,
        ble_device: BLEDevice,
        data: dict[str, Any],
        friendly_name: str | None = None,
        power_type: PowerType | None = None,
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
        self._manuf_dat = data.get("manufacturer_data")
        self.dev_details: dict[str, str] = {}
        self.velocity: int = 0
        self._power_type: PowerType | None = power_type

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

    async def query_dev_info(self) -> None:
        """Receive detailed information from device."""
        LOGGER.debug("%s: querying device info", self.name)
        self.dev_details.update(await self.api.query_dev_info())

    @property
    def power_type(self) -> PowerType | None:
        """Return the shade's power source, or None if unknown."""
        return self._power_type

    @property
    def show_battery_entities(self) -> bool:
        """Whether battery-related entities should be created for this shade.

        Unknown power_type is treated as battery-ish so real battery data is
        never silently hidden on unclassified shades.
        """
        return self._power_type != PowerType.HARDWIRED

    async def query_power_type(self) -> None:
        """Query the shade's power_type over BLE and cache the result."""
        LOGGER.debug("%s: querying power_type", self.name)
        self._power_type = await self.api.query_power_type()

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
        new_data: dict[str, int | float | bool] = {ATTR_RSSI: service_info.rssi}
        if change == bluetooth.BluetoothChange.ADVERTISEMENT:
            new_data.update(
                self.api.dec_manufacturer_data(
                    bytearray(service_info.manufacturer_data.get(2073, b""))
                )
            )
            self.api.encrypted = bool(new_data.get("home_id"))

        if new_data == self.data:
            return
        self.data = new_data
        LOGGER.debug("data sample %s", self.data)
        super()._async_handle_bluetooth_event(service_info, change)
