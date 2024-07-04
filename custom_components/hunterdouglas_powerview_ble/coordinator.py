"""Home Assistant coordinator for Hunter Douglas PowerView (BLE) integration."""

from datetime import timedelta
from typing import Any

from bleak.backends.device import BLEDevice
from bleak.exc import BleakError

from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import DOMAIN as BLUETOOTH_DOMAIN
from homeassistant.const import ATTR_NAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import PowerViewBLE
from .const import ATTR_RSSI, DOMAIN, LOGGER, UPDATE_INTERVAL


class PVCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Update coordinator for a battery management system."""

    def __init__(
        self,
        hass: HomeAssistant,
        ble_device: BLEDevice,
    ) -> None:
        """Initialize BMS data coordinator."""
        assert ble_device.name is not None
        super().__init__(
            hass=hass,
            logger=LOGGER,
            name=ble_device.name,
            update_interval=timedelta(seconds=UPDATE_INTERVAL),
            always_update=False,  # only update when sensor value has changed
        )

        self._mac = ble_device.address
        self.api = PowerViewBLE(ble_device)
        LOGGER.debug(
            "Initializing coordinator for %s (%s)",
            ble_device.name,
            ble_device.address,
        )
        if service_info := bluetooth.async_last_service_info(
            self.hass, address=self._mac, connectable=True
        ):
            LOGGER.debug("device data: %s", service_info.as_dict())

        self.device_info = DeviceInfo(
            identifiers={
                (DOMAIN, ble_device.name),
                (BLUETOOTH_DOMAIN, ble_device.address),
            },
            connections={(CONNECTION_BLUETOOTH, ble_device.address)},
            name=ble_device.name,
            configuration_url=None,
            # properties used in GUI:
            manufacturer="Hunter Douglas",
            model="shade",
        )

    @property
    def address(self) -> str:
        """Return MAC address of remote device."""
        return self._mac

    async def _async_update_data(self) -> dict[str, Any]:
        """Return the latest data from the device."""
        LOGGER.debug("%s data update", self.device_info.get(ATTR_NAME))

        try:
            info = {}  # await self._device.async_update()
        except TimeoutError:
            LOGGER.debug("Device communication timeout")
            raise
        except BleakError as err:
            raise UpdateFailed(
                f"device communicating failed: {err!s} ({type(err).__name__})"
            ) from err

        if (
            service_info := bluetooth.async_last_service_info(
                self.hass, address=self._mac, connectable=True
            )
        ) is not None:
            info.update({ATTR_RSSI: service_info.rssi})

        LOGGER.debug("data sample %s", info)
        return info
