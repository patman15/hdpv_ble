"""Support for Hunter Douglas PowerView binary sensors."""

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.components.bluetooth.passive_update_coordinator import (
    PassiveBluetoothCoordinatorEntity,
)
from homeassistant.const import ATTR_BATTERY_CHARGING
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import format_mac
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import ConfigEntryType, async_setup_shade_platform
from .const import DOMAIN
from .coordinator import PVCoordinator

BINARY_SENSOR_TYPES: list[BinarySensorEntityDescription] = [
    BinarySensorEntityDescription(
        key=ATTR_BATTERY_CHARGING,
        translation_key=ATTR_BATTERY_CHARGING,
        device_class=BinarySensorDeviceClass.BATTERY_CHARGING,
    )
]


def _add_entities(
    coordinator: PVCoordinator, async_add_entities: AddEntitiesCallback
) -> None:
    """Create binary sensor entities for a single shade coordinator."""
    include_battery = coordinator.show_battery_entities
    async_add_entities(
        [
            PVBinarySensor(coordinator, descr, format_mac(coordinator.address))
            for descr in BINARY_SENSOR_TYPES
            if include_battery or descr.key != ATTR_BATTERY_CHARGING
        ]
    )


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntryType,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add sensors for passed config_entry in Home Assistant."""
    async_setup_shade_platform(hass, config_entry, async_add_entities, _add_entities)


class PVBinarySensor(
    PassiveBluetoothCoordinatorEntity[PVCoordinator], BinarySensorEntity
):  # type: ignore[reportIncompatibleMethodOverride]
    """The generic PV binary sensor implementation."""

    def __init__(
        self,
        coord: PVCoordinator,
        descr: BinarySensorEntityDescription,
        unique_id: str,
    ) -> None:
        """Initialize PV binary sensor."""
        self._attr_unique_id = f"{DOMAIN}-{unique_id}-{descr.key}"
        self._attr_device_info = coord.device_info
        self._attr_has_entity_name = True
        self.entity_description = descr
        super().__init__(coord)

    @property
    def is_on(self) -> bool | None:  # type: ignore[reportIncompatibleVariableOverride]
        """Handle updated data from the coordinator."""
        return bool(self.coordinator.data.get(self.entity_description.key))
