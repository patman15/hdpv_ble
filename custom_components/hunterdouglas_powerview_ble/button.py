"""Hunter Douglas Powerview cover."""

from typing import Any, Final

from bleak.exc import BleakError
from homeassistant.components.bluetooth.passive_update_coordinator import (
    PassiveBluetoothCoordinatorEntity,
)
from homeassistant.components.button import (
    ButtonDeviceClass,
    ButtonEntity,
    ButtonEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo, format_mac
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import CLOSED_POSITION, OPEN_POSITION
from .const import DOMAIN, HOME_KEY, LOGGER
from .coordinator import PVCoordinator

BUTTONS_SHADE: Final = [
    ButtonEntityDescription(
        key="identify",
        device_class=ButtonDeviceClass.IDENTIFY,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
]


async def async_setup_entry(
    _hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the demo cover platform."""

    coordinator: PVCoordinator = config_entry.runtime_data
    for descr in BUTTONS_SHADE:
        async_add_entities([PowerViewButton(coordinator, descr)])


class PowerViewButton(PassiveBluetoothCoordinatorEntity[PVCoordinator], ButtonEntity):  # type: ignore[reportIncompatibleVariableOverride]
    """Representation of a powerview shade."""

    _attr_has_entity_name = True
    _attr_device_class = ButtonDeviceClass.IDENTIFY

    def __init__(
        self,
        coordinator: PVCoordinator,
        description: ButtonEntityDescription,
    ) -> None:
        """Initialize the shade."""
        self.entity_description = description
        self._coord: PVCoordinator = coordinator
        self._attr_device_info = self._coord.device_info
        self._attr_unique_id = (
            f"{DOMAIN}_{format_mac(self._coord.address)}_{ButtonDeviceClass.IDENTIFY}"
        )
        super().__init__(coordinator)

    @property
    def device_info(self) -> DeviceInfo:  # type: ignore[reportIncompatibleVariableOverride]
        """Return the device_info of the device."""
        return self._coord.device_info

    async def async_press(self) -> None:
        """Handle the button press."""
        await self._coord.api.identify()
