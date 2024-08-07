"""Constants for the BLE Battery Management System integration."""

import logging

from bleak.uuids import normalize_uuid_str

# from homeassistant.const import (  # noqa: F401
#     ATTR_BATTERY_CHARGING,
#     ATTR_BATTERY_LEVEL,
#     ATTR_TEMPERATURE,
#     ATTR_VOLTAGE,
# )


DOMAIN = "hunterdouglas_powerview_ble"
LOGGER = logging.getLogger(__package__)
UUID = normalize_uuid_str("fdc1")
MFCT_ID = 2073
TIMEOUT = 15

# attributes (do not change)
ATTR_RSSI = "rssi"
