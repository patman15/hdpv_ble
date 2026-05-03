"""Constants for the BLE Battery Management System integration."""

from enum import IntEnum
import logging
from typing import Final

DOMAIN: Final[str] = "hunterdouglas_powerview_ble"
LOGGER: Final = logging.getLogger(__package__)
MFCT_ID: Final[int] = 2073
TIMEOUT: Final[int] = 5

CONF_HOME_KEY: Final[str] = "home_key"
CONF_HUB_URL: Final[str] = "hub_url"
CONF_POWER_TYPES: Final[str] = "power_types"
CONF_FRIENDLY_NAMES: Final[str] = "friendly_names"


class PowerType(IntEnum):
    """Shade power source. Values match the BLE 0xFFDE reply's byte 0."""

    HARDWIRED = 0
    BATTERY = 1
    RECHARGEABLE = 2


# dispatcher signal for newly discovered shades (format with entry_id)
SIGNAL_NEW_SHADE: Final[str] = f"{DOMAIN}_new_shade_{{entry_id}}"

# attributes (do not change)
ATTR_RSSI: Final[str] = "rssi"
