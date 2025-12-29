"""Constants for the BLE Battery Management System integration."""

import logging
from typing import Final

DOMAIN: Final[str] = "hunterdouglas_powerview_ble"
LOGGER: Final = logging.getLogger(__package__)
MFCT_ID: Final[int] = 2073
TIMEOUT: Final[int] = 5

# put the key here, needs to be 16 bytes long, e.g.
# HOME_KEY: Final[bytes] = b"\x00\x01\x02\x03\x04\x05\x06\x07\x08\x09\x0a\x0b\x0c\x0d\x0e\x0f"
HOME_KEY: Final[bytes] = b""


# attributes (do not change)
ATTR_RSSI: Final[str] = "rssi"
