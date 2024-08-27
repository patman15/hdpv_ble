"""Hunter Douglas Powerview cover."""

from typing import Any

from homeassistant.components.bluetooth.passive_update_coordinator import (
    PassiveBluetoothCoordinatorEntity,
)
from homeassistant.components.cover import (
    ATTR_CURRENT_POSITION,
    ATTR_POSITION,
    CoverDeviceClass,
    CoverEntity,
    CoverEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo, format_mac
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import CLOSED_POSITION, OPEN_POSITION
from .const import DOMAIN, HOME_KEY, LOGGER
from .coordinator import PVCoordinator


async def async_setup_entry(
    _hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the demo cover platform."""

    coordinator: PVCoordinator = config_entry.runtime_data
    async_add_entities([PowerViewCover(coordinator)])


class PowerViewCover(PassiveBluetoothCoordinatorEntity[PVCoordinator], CoverEntity):  # type: ignore[reportIncompatibleVariableOverride]
    """Representation of a powerview shade."""

    _attr_has_entity_name = True
    _attr_device_class = CoverDeviceClass.SHADE
    _attr_supported_features = (
        CoverEntityFeature.OPEN
        | CoverEntityFeature.CLOSE
        | CoverEntityFeature.SET_POSITION
        | CoverEntityFeature.STOP
    )

    def __init__(
        self,
        coordinator: PVCoordinator,
    ) -> None:
        """Initialize the shade."""
        self._attr_name = CoverDeviceClass.SHADE
        self._coord = coordinator
        self._attr_device_info = self._coord.device_info
        self._target_position: int | None = None
        self._attr_unique_id = (
            f"{DOMAIN}_{format_mac(self._coord.address)}_{CoverDeviceClass.SHADE}"
        )
        super().__init__(coordinator)

    @property
    def device_info(self) -> DeviceInfo:  # type: ignore[reportIncompatibleVariableOverride]
        """Return the device_info of the device."""
        return self._coord.device_info

    @property
    def is_opening(self) -> bool | None:  # type: ignore[reportIncompatibleVariableOverride]
        """Return if the cover is opening or not."""
        return bool(self._coord.data.get("is_opening"))

    @property
    def is_closing(self) -> bool | None:  # type: ignore[reportIncompatibleVariableOverride]
        """Return if the cover is closing or not."""
        return bool(self._coord.data.get("is_closing"))

    @property
    def is_closed(self) -> bool:  # type: ignore[reportIncompatibleVariableOverride]
        """Return if the cover is closed."""
        return self.current_cover_position == CLOSED_POSITION

    @property
    def supported_features(self) -> CoverEntityFeature:  # type: ignore[reportIncompatibleVariableOverride]
        """Flag supported features, disable control if encryption is needed."""
        if (self._coord.data.get("home_id") and len(HOME_KEY) != 16) or self._coord.data.get("battery_charging"):
            return CoverEntityFeature(0)

        return super().supported_features

    @property
    def current_cover_position(self) -> int | None:  # type: ignore[reportIncompatibleVariableOverride]
        """Return current position of cover.

        None is unknown, 0 is closed, 100 is fully open.
        """
        if ATTR_CURRENT_POSITION in self._coord.data:
            pos = self._coord.data.get(ATTR_CURRENT_POSITION)
            return int(pos) if pos is not None else None
        return None

    async def async_set_cover_position(self, **kwargs: Any) -> None:
        """Move the cover to a specific position."""
        self._target_position = kwargs.get(ATTR_POSITION, None)
        if self._target_position is not None:
            LOGGER.debug("set cover to position %i", self._target_position)
            if not self._coord.device_present:
                LOGGER.debug("device not present")
                return
            try:
                await self._coord.api.set_position(self._target_position)
            except Exception as err:
                raise HomeAssistantError(
                    f"Failed to move cover '{self.name}' to {self._target_position}%: {err}"
                ) from err

    async def async_open_cover(self, **kwargs: Any) -> None:
        """Open the cover."""
        LOGGER.debug("open cover")
        try:
            await self._coord.api.set_position(OPEN_POSITION)
        except Exception as err:
            raise HomeAssistantError(
                f"Failed open cover '{self.name}': {err}"
            ) from err

    async def async_close_cover(self, **kwargs: Any) -> None:
        """Close the cover tilt."""
        LOGGER.debug("close cover")
        try:
            await self._coord.api.set_position(CLOSED_POSITION)
        except Exception as err:
            raise HomeAssistantError(
                f"Failed close cover '{self.name}': {err}"
            ) from err

    async def async_stop_cover(self, **kwargs: Any) -> None:
        """Stop the cover."""
        LOGGER.debug("stop cover")
        try:
            await self._coord.api.stop()
        except Exception as err:
            raise HomeAssistantError(
                f"Failed stop cover '{self.name}': {err}"
            ) from err
