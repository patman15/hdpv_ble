"""Hunter Douglas Powerview cover."""

from typing import Any

from homeassistant.components.cover import (
    ATTR_CURRENT_POSITION,
    ATTR_POSITION,
    CoverDeviceClass,
    CoverEntity,
    CoverEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_IDENTIFIERS, ATTR_NAME
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo, format_mac
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, LOGGER
from .coordinator import PVCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the demo cover platform."""

    coordinator: PVCoordinator = config_entry.runtime_data
    async_add_entities([PowerViewCover(coordinator)])


class PowerViewCover(CoverEntity):
    """Representation of a powerview shade."""

    _attr_has_entity_name = True
    _attr_device_class = CoverDeviceClass.SHADE
    _attr_supported_features = (
        CoverEntityFeature.OPEN
        | CoverEntityFeature.CLOSE
        | CoverEntityFeature.SET_POSITION
        #        | CoverEntityFeature.STOP
    )
    _attr_current_cover_position: int | None = None

    def __init__(
        self,
        coordinator: PVCoordinator,
    ) -> None:
        """Initialize the shade."""
        self._attr_name = CoverDeviceClass.SHADE
        self._coord = coordinator
        self._attr_device_info = self._coord.device_info
        self._attr_unique_id = (
            f"{DOMAIN}_{format_mac(self._coord.address)}_{CoverDeviceClass.SHADE}"
        )
        self._attr_current_cover_position: int | None = 0

    @property
    def is_closed(self) -> bool:  # type: ignore[reportIncompatibleVariableOverride]
        """Return if the cover is closed."""
        return self._attr_current_cover_position == 0

    async def async_set_cover_position(self, **kwargs: Any) -> None:
        """Move the cover to a specific position."""
        if pos := kwargs[ATTR_POSITION]:
            LOGGER.debug("set cover to position %i", pos)
            try:
                await self._coord.api.set_position(pos)
                self._attr_current_cover_position = pos
            except TimeoutError:
                pass

    async def async_open_cover(self, **kwargs: Any) -> None:
        """Open the cover."""
        LOGGER.debug("open cover")
        try:
            await self._coord.api.activate_scene(2)
            self._attr_current_cover_position = 100
        except TimeoutError:
            pass

    async def async_close_cover(self, **kwargs: Any) -> None:
        """Close the cover tilt."""
        LOGGER.debug("close cover")
        try:
            await self._coord.api.activate_scene(3)
            self._attr_current_cover_position = 0
        except TimeoutError:
            pass

    async def async_stop_cover(self, **kwargs: Any) -> None:
        """Stop the cover."""
        LOGGER.debug("stop cover")
