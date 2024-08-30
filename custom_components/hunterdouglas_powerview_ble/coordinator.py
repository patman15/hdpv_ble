"""Home Assistant coordinator for Hunter Douglas PowerView (BLE) integration."""

from typing import Any

from bleak.backends.device import BLEDevice

from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import DOMAIN as BLUETOOTH_DOMAIN
from homeassistant.components.bluetooth.passive_update_coordinator import (
    PassiveBluetoothDataUpdateCoordinator,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo

from .api import SHADE_TYPE, PowerViewBLE
from .const import ATTR_RSSI, DOMAIN, HOME_KEY, LOGGER


class PVCoordinator(PassiveBluetoothDataUpdateCoordinator):
    """Update coordinator for a battery management system."""

    def __init__(
        self, hass: HomeAssistant, ble_device: BLEDevice, data: dict[str, Any]
    ) -> None:
        """Initialize BMS data coordinator."""
        assert ble_device.name is not None
        self._mac = ble_device.address
        self.api = PowerViewBLE(ble_device, HOME_KEY)
        self.data: dict[str, int | float | bool] = {}
        self._manuf_dat = data.get("manufacturer_data")
        self.dev_details: dict[str, str] = {}

        LOGGER.debug(
            "Initializing coordinator for %s (%s)",
            ble_device.name,
            ble_device.address,
        )
        super().__init__(
            hass,
            LOGGER,
            ble_device.address,
            bluetooth.BluetoothScanningMode.ACTIVE,
        )

    async def query_dev_info(self) -> None:
        """Receive detailed information from device."""
        LOGGER.debug("%s: querying device info", self.name)
        self.dev_details.update(await self.api.query_dev_info())

    @property
    def device_info(self) -> DeviceInfo:
        """Return detailed device information for GUI."""
        LOGGER.debug("%s: device_info, %s", self.name, self.dev_details)
        return DeviceInfo(
            identifiers={
                (DOMAIN, self.name),
                (BLUETOOTH_DOMAIN, self.address),
            },
            connections={(CONNECTION_BLUETOOTH, self.address)},
            name=self.name,
            configuration_url=None,
            # properties used in GUI:
            manufacturer="Hunter Douglas",
            model=(
                str(SHADE_TYPE.get(int(bytes.fromhex(self._manuf_dat)[2]), "unknown"))
                if self._manuf_dat
                else None
            ),
            model_id=(
                str(bytes.fromhex(self._manuf_dat)[2]) if self._manuf_dat else None
            ),
            serial_number=self.dev_details.get("serial_nr"),
            sw_version=self.dev_details.get("sw_rev"),
            hw_version=self.dev_details.get("hw_rev"),
        )

    @property
    def device_present(self) -> bool:
        """Check if a device is present."""
        return bluetooth.async_address_present(self.hass, self._mac, connectable=True)

    @callback
    def _async_handle_bluetooth_event(
        self,
        service_info: bluetooth.BluetoothServiceInfoBleak,
        change: bluetooth.BluetoothChange,
    ) -> None:
        """Handle a Bluetooth event."""

        # if not self.dev_details:
        #     self.hass.async_create_task(self._get_device_info())

        LOGGER.debug("BLE event %s: %s", change, service_info.manufacturer_data)
        self.data = {ATTR_RSSI: service_info.rssi}
        if change == bluetooth.BluetoothChange.ADVERTISEMENT:
            self.data.update(
                self.api.dec_manufacturer_data(
                    bytearray(service_info.manufacturer_data.get(2073, b""))
                )
            )

        LOGGER.debug("data sample %s", self.data)
        super()._async_handle_bluetooth_event(service_info, change)
