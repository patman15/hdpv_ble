"""Constants for the BLE Battery Management System integration."""

import logging

# from homeassistant.const import (  # noqa: F401
#     ATTR_BATTERY_CHARGING,
#     ATTR_BATTERY_LEVEL,
#     ATTR_TEMPERATURE,
#     ATTR_VOLTAGE,
# )


DOMAIN = "hunterdouglas_powerview_ble"
LOGGER = logging.getLogger(__package__)
UPDATE_INTERVAL = 30  # in seconds
UUID = "0000fdc1-0000-1000-8000-00805f9b34fb"
MFCT_ID = 2073

# attributes (do not change)
ATTR_RSSI = "rssi"

