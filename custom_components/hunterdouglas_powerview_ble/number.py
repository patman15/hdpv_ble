"""Hunter Douglas PowerView velocity control."""

from homeassistant.components.bluetooth.passive_update_coordinator import (
    PassiveBluetoothCoordinatorEntity,
)
from homeassistant.components.number import NumberMode, RestoreNumber
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import format_mac
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import ConfigEntryType, async_setup_shade_platform
from .const import DOMAIN, LOGGER
from .coordinator import PVCoordinator


def _add_entities(
    coordinator: PVCoordinator, async_add_entities: AddEntitiesCallback
) -> None:
    """Create velocity number entity for a single shade coordinator."""
    async_add_entities([PowerViewVelocity(coordinator)])


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntryType,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the velocity number entity."""
    async_setup_shade_platform(hass, config_entry, async_add_entities, _add_entities)


class PowerViewVelocity(
    PassiveBluetoothCoordinatorEntity[PVCoordinator], RestoreNumber
):  # type: ignore[reportIncompatibleVariableOverride]
    """Number entity to control shade movement velocity."""

    _attr_has_entity_name = True
    _attr_name = "Velocity"
    _attr_icon = "mdi:speedometer"
    _attr_mode = NumberMode.SLIDER
    _attr_native_min_value = 0
    _attr_native_max_value = 100
    _attr_native_step = 1
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: PVCoordinator) -> None:
        """Initialize the velocity entity."""
        self._coord = coordinator
        self._attr_device_info = self._coord.device_info
        self._attr_unique_id = f"{DOMAIN}_{format_mac(self._coord.address)}_velocity"
        super().__init__(coordinator)

    @property
    def native_value(self) -> int:
        """Return the current velocity value."""
        return self._coord.velocity

    async def async_added_to_hass(self) -> None:
        """Restore last known velocity on startup."""
        await super().async_added_to_hass()
        last_data = await self.async_get_last_number_data()
        if last_data and last_data.native_value is not None:
            self._coord.velocity = int(last_data.native_value)
            LOGGER.debug(
                "%s: restored velocity to %s", self._coord.name, self._coord.velocity
            )

    async def async_set_native_value(self, value: float) -> None:
        """Set the velocity value."""
        self._coord.velocity = int(value)
        self.async_write_ha_state()
