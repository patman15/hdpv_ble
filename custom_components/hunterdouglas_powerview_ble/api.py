"""Hunter Douglas PowerView BLE API."""

import asyncio
from enum import Enum

from bleak import BleakClient
from bleak.backends.device import BLEDevice
from bleak.exc import BleakError

from .const import LOGGER


class shade_cmd(Enum):
    """The PowerView cover commands."""

    set_position = 0x01F7
    stop = 0xB8F7
    activate_scene = 0xBAF7


class PowerViewBLE:
    """Class to handle connection to PowerView remote device."""

    UUID_SERVICE = "0000fdc1-0000-1000-8000-00805f9b34fb"
    UUID_TX = "cafe1001-c0ff-ee01-8000-A110CA7AB1E0"

    def __init__(self, ble_device: BLEDevice) -> None:
        """Initialize device API via Bluetooth."""
        self._ble_device: BLEDevice = ble_device
        self.name = self._ble_device.name
        self.seqcnt: int = 0
        self._client: BleakClient | None = None
        self._data_event = asyncio.Event()

    async def _wait_event(self) -> None:
        await self._data_event.wait()
        self._data_event.clear()

    @property
    def is_connected(self) -> bool:
        """Return whether remote device is connected."""
        return self._client is not None and self._client.is_connected

    # general cmd: uint16_t cmd, uint8_t seqID, uint8_t data_len
    async def _cmd(self, cmd: shade_cmd, data: bytearray) -> None:
        try:
            await self._connect()
            assert self._client is not None, "missing BT client"
            tx_data = (
                bytearray(
                    int.to_bytes(cmd.value, 2, byteorder="little")
                    + bytes([self.seqcnt, len(data)])
                )
                + data
            )
            self.seqcnt += 1
            await self._client.write_gatt_char(self.UUID_TX, tx_data, False)
        finally:
            await self._disconnect()

    # position cmd: uint16_t pos1, uint16_t pos2, uint16_t pos3, uint16_t tilt, uint8_t velocity
    async def set_position(self, value: int) -> None:
        """Set position of device."""
        LOGGER.debug("%s setting position to %i", self.name, value)
        await self._cmd(
            shade_cmd.set_position,
            bytearray(
                int.to_bytes(value * 100, 2, byteorder="little")
                + bytes([0x00, 0x80, 0x00, 0x80, 0x00, 0x80, 0x0])
            ),
        )

    # TODO: currently HA / Bleak communication is too slow to make that function reasonable
    # def stop(self) -> None:
    #     """Stop device movement."""
    #     LOGGER.debug("%s stop")

    # uint8_t scene#, uint8_t unknown
    # open: scene 2
    # close: scene 3
    async def activate_scene(self, idx: int) -> None:
        """Stop device movement."""
        LOGGER.debug("%s set scene #%i", self.name, idx)
        await self._cmd(
            shade_cmd.activate_scene,
            bytearray(
                int.to_bytes(idx, 1, byteorder="little")
                + bytes([0xA2])
            ),
        )

    def _on_disconnect(self, client: BleakClient) -> None:
        """Disconnect callback function."""

        LOGGER.debug("Disconnected from %s", client.address)

    def _notification_handler(self, sender, data: bytearray) -> None:
        LOGGER.debug("%s received BLE data: %s", self.name, data)

        self._data_event.set()

    async def _connect(self) -> None:
        """Connect to the device and setup notification if not connected."""

        if not self.is_connected:
            LOGGER.debug("Connecting %s", self.name)
            self._client = BleakClient(
                self._ble_device,
                disconnected_callback=self._on_disconnect,
                services=[self.UUID_SERVICE],
            )
            await self._client.connect()
            await self._client.start_notify(self.UUID_TX, self._notification_handler)
        else:
            LOGGER.debug("%s already connected", self.name)

    async def _disconnect(self) -> None:
        """Disconnect the device and stop notifications."""

        if self._client and self.is_connected:
            LOGGER.debug("Disconnecting device %s", self.name)
            try:
                self._data_event.clear()
                await self._client.disconnect()
            except BleakError:
                LOGGER.warning("Disconnect failed!")

        self._client = None
