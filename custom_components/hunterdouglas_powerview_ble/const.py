"""Constants for the BLE Battery Management System integration."""

import logging
from typing import Final

# from bleak.uuids import normalize_uuid_str

# from homeassistant.const import (  # noqa: F401
#     ATTR_BATTERY_CHARGING,
#     ATTR_BATTERY_LEVEL,
#     ATTR_TEMPERATURE,
#     ATTR_VOLTAGE,
# )


DOMAIN: Final = "hunterdouglas_powerview_ble"
LOGGER: Final = logging.getLogger(__package__)
MFCT_ID: Final = 2073
TIMEOUT: Final = 5
HOME_KEY: Final = b""


# attributes (do not change)
ATTR_RSSI = "rssi"
