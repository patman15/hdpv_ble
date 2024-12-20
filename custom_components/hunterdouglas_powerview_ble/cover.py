"""Hunter Douglas Powerview cover."""

from typing import Any, Final

from bleak.exc import BleakError
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
        self._target_position: int | None = round(
            self._coord.data.get(ATTR_CURRENT_POSITION, OPEN_POSITION)
        )
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
        return bool(self._coord.data.get("is_opening")) or (
            isinstance(self._target_position, int)
            and isinstance(self.current_cover_position, int)
            and self._target_position > self.current_cover_position
            and self._coord.api.is_connected
        )

    @property
    def is_closing(self) -> bool | None:  # type: ignore[reportIncompatibleVariableOverride]
        """Return if the cover is closing or not."""
        return bool(self._coord.data.get("is_closing")) or (
            isinstance(self._target_position, int)
            and isinstance(self.current_cover_position, int)
            and self._target_position < self.current_cover_position
            and self._coord.api.is_connected
        )

    @property
    def is_closed(self) -> bool:  # type: ignore[reportIncompatibleVariableOverride]
        """Return if the cover is closed."""
        return self.current_cover_position == CLOSED_POSITION

    @property
    def supported_features(self) -> CoverEntityFeature:  # type: ignore[reportIncompatibleVariableOverride]
        """Flag supported features, disable control if encryption is needed."""
        if (
            self._coord.data.get("home_id") and len(HOME_KEY) != 16
        ) or self._coord.data.get("battery_charging"):
            return CoverEntityFeature(0)

        return super().supported_features

    @property
    def current_cover_position(self) -> int | None:  # type: ignore[reportIncompatibleVariableOverride]
        """Return current position of cover.

        None is unknown, 0 is closed, 100 is fully open.
        """
        pos: Final = self._coord.data.get(ATTR_CURRENT_POSITION)
        return round(pos) if pos is not None else None

    async def async_set_cover_position(self, **kwargs: Any) -> None:
        """Move the cover to a specific position."""
        target_position: Final = kwargs.get(ATTR_POSITION)
        if target_position is not None:
            LOGGER.debug("set cover to position %f", target_position)
            if self.current_cover_position == round(target_position) and not (
                self.is_closing or self.is_opening
            ):
                return
            self._target_position = round(target_position)
            try:
                await self._coord.api.set_position(round(target_position))
                self.async_write_ha_state()
            except BleakError as err:
                LOGGER.error(
                    "Failed to move cover '%s' to %f%%: %s",
                    self.name,
                    target_position,
                    err,
                )

    def _reset_target_position(self) -> None:
        self._target_position = None

    async def async_open_cover(self, **kwargs: Any) -> None:
        """Open the cover."""
        LOGGER.debug("open cover")
        if self.current_cover_position == OPEN_POSITION:
            return
        try:
            self._target_position = OPEN_POSITION
            await self._coord.api.open()
            self.async_write_ha_state()
        except BleakError as err:
            LOGGER.error("Failed to open cover '%s': %s", self.name, err)
            self._reset_target_position()

    async def async_close_cover(self, **kwargs: Any) -> None:
        """Close the cover tilt."""
        LOGGER.debug("close cover")
        if self.current_cover_position == CLOSED_POSITION:
            return
        try:
            self._target_position = CLOSED_POSITION
            await self._coord.api.close()
            self.async_write_ha_state()
        except BleakError as err:
            LOGGER.error("Failed to close cover '%s': %s", self.name, err)
            self._reset_target_position()

    async def async_stop_cover(self, **kwargs: Any) -> None:
        """Stop the cover."""
        LOGGER.debug("stop cover")
        try:
            await self._coord.api.stop()
            self._reset_target_position()
            self.async_write_ha_state()
        except BleakError as err:
            LOGGER.error("Failed to stop cover '%s': %s", self.name, err)
