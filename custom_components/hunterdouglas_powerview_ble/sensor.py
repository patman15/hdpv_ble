"""Platform for sensor integration."""

from homeassistant.components.bluetooth.passive_update_coordinator import (
    PassiveBluetoothCoordinatorEntity,
)
from homeassistant.components.sensor import SensorEntity, SensorEntityDescription
from homeassistant.components.sensor.const import SensorDeviceClass, SensorStateClass
from homeassistant.const import (
    ATTR_BATTERY_LEVEL,
    PERCENTAGE,
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    EntityCategory,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import format_mac
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import ConfigEntryType, async_setup_shade_platform
from .const import ATTR_RSSI, DOMAIN
from .coordinator import PVCoordinator

SENSOR_TYPES: list[SensorEntityDescription] = [
    SensorEntityDescription(
        key=ATTR_BATTERY_LEVEL,
        translation_key=ATTR_BATTERY_LEVEL,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        device_class=SensorDeviceClass.BATTERY,
    ),
    SensorEntityDescription(
        key=ATTR_RSSI,
        translation_key=ATTR_RSSI,
        native_unit_of_measurement=SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
        state_class=SensorStateClass.MEASUREMENT,
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
]


def _add_entities(
    coordinator: PVCoordinator, async_add_entities: AddEntitiesCallback
) -> None:
    """Create sensor entities for a single shade coordinator."""
    async_add_entities(
        [
            PVSensor(coordinator, descr, format_mac(coordinator.address))
            for descr in SENSOR_TYPES
        ]
    )


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntryType,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add sensors for passed config_entry in Home Assistant."""
    async_setup_shade_platform(hass, config_entry, async_add_entities, _add_entities)


class PVSensor(PassiveBluetoothCoordinatorEntity[PVCoordinator], SensorEntity):  # type: ignore[reportIncompatibleMethodOverride]
    """The generic BMS sensor implementation."""

    _attr_has_entity_name = True

    def __init__(
        self, pv_dev: PVCoordinator, descr: SensorEntityDescription, unique_id: str
    ) -> None:
        """Initialize the BMS sensor."""
        self._attr_unique_id = f"{DOMAIN}-{unique_id}-{descr.key}"
        self._attr_device_info = pv_dev.device_info
        self.entity_description = descr
        super().__init__(pv_dev)

    @property
    def native_value(self) -> int | float | None:  # type: ignore[reportIncompatibleVariableOverride]
        """Return the sensor value.

        RSSI refreshes on every advert, so it is always reported. V2-derived
        values (e.g. battery) read as unknown once the advert data is stale.
        """
        if (
            self.entity_description.key != ATTR_RSSI
            and not self.coordinator.data_available
        ):
            return None
        return self.coordinator.data.get(self.entity_description.key)
