"""Hunter Douglas Powerview cover."""

from typing import Final

from homeassistant.components.bluetooth.passive_update_coordinator import (
    PassiveBluetoothCoordinatorEntity,
)
from homeassistant.components.button import (
    ButtonDeviceClass,
    ButtonEntity,
    ButtonEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import format_mac
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import ConfigEntryType, async_setup_shade_platform
from .const import DOMAIN, LOGGER
from .coordinator import PVCoordinator

BUTTONS_SHADE: Final = [
    ButtonEntityDescription(
        key="identify",
        device_class=ButtonDeviceClass.IDENTIFY,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
]


def _add_entities(
    coordinator: PVCoordinator, async_add_entities: AddEntitiesCallback
) -> None:
    """Create button entities for a single shade coordinator."""
    async_add_entities([PowerViewButton(coordinator, descr) for descr in BUTTONS_SHADE])


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntryType,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the button platform."""
    async_setup_shade_platform(hass, config_entry, async_add_entities, _add_entities)


class PowerViewButton(PassiveBluetoothCoordinatorEntity[PVCoordinator], ButtonEntity):  # type: ignore[reportIncompatibleVariableOverride]
    """Representation of a powerview shade."""

    _attr_has_entity_name = True

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

    async def async_press(self) -> None:
        """Handle the button press."""
        LOGGER.debug("identify cover")
        await self._coord.api.identify()
