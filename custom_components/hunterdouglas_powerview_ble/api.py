"""Hunter Douglas PowerView BLE API."""

import asyncio
from dataclasses import dataclass
from enum import Enum
import time
from typing import Final, NamedTuple

from bleak import BleakClient
from bleak.backends.device import BLEDevice
from bleak.exc import BleakError
from bleak.uuids import normalize_uuid_str
from bleak_retry_connector import establish_connection
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.ciphers.base import (
    AEADDecryptionContext,
    AEADEncryptionContext,
)

from homeassistant.components.cover import (
    ATTR_CURRENT_POSITION,
    ATTR_CURRENT_TILT_POSITION,
)

from .const import LOGGER, TIMEOUT, PowerType

UUID_COV_SERVICE: Final[str] = normalize_uuid_str("fdc1")
UUID_TX: Final[str] = "cafe1001-c0ff-ee01-8000-a110ca7ab1e0"
UUID_DEV_SERVICE: Final[str] = normalize_uuid_str("180a")
UUID_BAT_SERVICE: Final[str] = normalize_uuid_str("180f")

ATTR_ACTIVITY: Final[str] = "activity"


SHADE_TYPE: Final[dict[int, str]] = {
    # up down only
    1: "Designer Roller",
    4: "Roman",
    5: "Bottom Up",
    6: "Duette",
    10: "Duette and Applause SkyLift",
    19: "Provenance Woven Wood",
    26: "Vertical",
    31: "Vignette",
    32: "Vignette",
    42: "M25T Roller Blind",
    49: "AC Roller",
    52: "Banded Shades",
    53: "Sonnette",
    57: "Carole Roman Shades",
    84: "Vignette",
    # top down (single rail, inverted position)
    7: "Top Down",
    # top down bottom up (dual rail)
    8: "Duette, Top Down Bottom Up",
    9: "Duette DuoLite, Top Down Bottom Up",
    33: "Duette Architella, Top Down Bottom Up",
    47: "Pleated, Top Down Bottom Up",
    # tilt only (no position movement)
    39: "Parkland",
    40: "Everwood Alternative Wood Blinds",
    # tilt on closed
    18: "Bottom Up, Tilt on Closed 90°",
    44: "Twist",
    # tilt anywhere (position + tilt)
    51: "Venetian, Tilt Anywhere",
    54: "Vertical Slats, Left Stack",
    55: "Vertical Slats, Right Stack",
    56: "Vertical Slats, Split Stack",
    62: "Venetian, Tilt Anywhere",
    # duolite (dual overlapping fabrics)
    38: "Dual Overlapped, Tilt 90°",
    65: "Dual Overlapped",
    95: "Dual Overlapped Illuminated",
}


class ShadeCapability(NamedTuple):
    """Capability flags for a shade type."""

    has_tilt: bool = False
    tilt_only: bool = False
    is_tilt_on_closed: bool = False  # tilt only available when fully closed
    is_top_down: bool = False  # position logic is inverted (SkyLift style)
    is_tdbu: bool = False  # dual-rail Top Down Bottom Up (needs two entities)
    is_duolite: bool = False  # dual-fabric sheer+opaque (needs three entities)


SHADE_CAPABILITIES: Final[dict[int, ShadeCapability]] = {
    # tilt anywhere (position + tilt)
    51: ShadeCapability(has_tilt=True),
    54: ShadeCapability(has_tilt=True),
    55: ShadeCapability(has_tilt=True),
    56: ShadeCapability(has_tilt=True),
    62: ShadeCapability(has_tilt=True),
    # tilt only (no position movement)
    39: ShadeCapability(has_tilt=True, tilt_only=True),
    40: ShadeCapability(has_tilt=True, tilt_only=True),
    # tilt on closed (tilt only available at fully closed position)
    18: ShadeCapability(has_tilt=True, is_tilt_on_closed=True),
    44: ShadeCapability(has_tilt=True, is_tilt_on_closed=True),
    # top-down only (single rail, inverted position)
    7: ShadeCapability(is_top_down=True),
    10: ShadeCapability(is_top_down=True),
    # dual-rail top-down/bottom-up (two independent rails → two entities)
    8: ShadeCapability(is_tdbu=True),
    33: ShadeCapability(is_tdbu=True),
    47: ShadeCapability(is_tdbu=True),
    # duolite (dual overlapping fabrics → three entities)
    9: ShadeCapability(is_tdbu=True, is_duolite=True),
    38: ShadeCapability(is_duolite=True),
    65: ShadeCapability(is_duolite=True),
    95: ShadeCapability(is_duolite=True),
}

_DEFAULT_CAPABILITY: Final[ShadeCapability] = ShadeCapability()


def get_shade_capabilities(type_id: int | None) -> ShadeCapability:
    """Return shade capabilities for a given type_id."""
    if type_id is None:
        return _DEFAULT_CAPABILITY
    return SHADE_CAPABILITIES.get(type_id, _DEFAULT_CAPABILITY)


OPEN_POSITION: Final[int] = 100
CLOSED_POSITION: Final[int] = 0

POWER_LEVELS: Final[dict[int, int]] = {
    3: 100,  # 3 = 100% to 51% power remaining (also reported by hardwired)
    2: 50,  # 2 = 50% to 21% power remaining
    1: 20,  # 1 = 20% or less power remaining
    0: 0,  # 0 = No power remaining
}


class ShadeCmd(Enum):
    """The PowerView cover commands."""

    SET_POSITION = 0x01F7
    STOP = 0xB8F7
    ACTIVATE_SCENE = 0xBAF7
    IDENTIFY = 0x11F7
    POWER_STATUS = 0xDEFF


@dataclass
class PVDeviceInfo:
    """Dataclass holding available PowerView device information."""

    manufacturer: str = ""
    model: str = ""
    serial_nr: str = ""
    hw_rev: str = ""
    fw_rev: str = ""
    sw_rev: str = ""
    battery_level: int = 0


class PowerViewBLE:
    """Class to handle connection to PowerView remote device."""

    def __init__(self, ble_device: BLEDevice, home_key: bytes = b"") -> None:
        """Initialize device API via Bluetooth."""
        self._ble_device: BLEDevice = ble_device
        self.name: Final[str] = self._ble_device.name or "unknown"
        self._seqcnt: int = 1
        self._client: BleakClient = BleakClient(self._ble_device)
        self._data_event = asyncio.Event()
        self._data: bytes = b""
        self._info: PVDeviceInfo = PVDeviceInfo()
        self._is_encrypted: bool = False
        self._cmd_lock: Final = asyncio.Lock()
        self._cmd_next: tuple[ShadeCmd, bytes]
        self._cipher: Final[Cipher | None] = (
            Cipher(algorithms.AES(home_key), modes.CTR(bytes(16)))
            if len(home_key) == 16
            else None
        )

    async def _wait_event(self) -> None:
        await self._data_event.wait()
        self._data_event.clear()

    def set_ble_device(self, ble_device: BLEDevice) -> None:
        """Update the BLE device reference (e.g. when proxy details change)."""
        self._ble_device = ble_device

    @property
    def encrypted(self) -> bool:
        """Return whether communication with this shade is encrypted."""
        return self._is_encrypted

    @encrypted.setter
    def encrypted(self, value: bool) -> None:
        self._is_encrypted = value

    @property
    def has_key(self) -> bool:
        """Return True if a valid homekey was provided."""
        return self._cipher is not None

    @property
    def info(self) -> PVDeviceInfo:
        """Return device information, e.g. SW version."""
        return self._info

    @property
    def is_connected(self) -> bool:
        """Return whether remote device is connected."""
        return self._client.is_connected

    async def _transact(self, cmd: tuple[ShadeCmd, bytes]) -> int:
        # Assumes _cmd_lock is held and _connect() has run. Writes a framed
        # (optionally encrypted) request and waits for the device's reply;
        # caller inspects/validates self._data afterwards. Returns the seq
        # number used so callers can verify the echo.
        tx_data: bytes = bytes(
            int.to_bytes(cmd[0].value, 2, byteorder="little")
            + bytes([self._seqcnt, len(cmd[1])])
            + cmd[1]
        )
        LOGGER.debug("sending cmd: %s", tx_data.hex(" "))
        if self._cipher is not None and self._is_encrypted:
            enc: AEADEncryptionContext = self._cipher.encryptor()
            tx_data = enc.update(tx_data) + enc.finalize()
            LOGGER.debug("  encrypted: %s", tx_data.hex(" "))
        self._data_event.clear()
        await self._client.write_gatt_char(UUID_TX, tx_data, False)
        seq = self._seqcnt
        self._seqcnt += 1
        await asyncio.wait_for(self._wait_event(), timeout=TIMEOUT)
        return seq

    # general cmd: uint16_t cmd, uint8_t seqID, uint8_t data_len
    async def _cmd(self, cmd: tuple[ShadeCmd, bytes], disconnect: bool = True) -> None:
        self._cmd_next = cmd
        if self._cmd_lock.locked():
            LOGGER.debug("%s: device busy, queuing %s command", self.name, cmd[0])
            return

        async with self._cmd_lock:
            try:
                await self._connect()
                cmd_run: tuple[ShadeCmd, bytes] = self._cmd_next
                try:
                    seq = await self._transact(cmd_run)
                    self._verify_ack_reply(self._data, seq, cmd_run[0])
                except TimeoutError as ex:
                    raise TimeoutError("Device did not send confirmation.") from ex
                finally:
                    if disconnect:
                        await self._client.disconnect()  # device disconnects itself
            except Exception as ex:
                LOGGER.error("Error: %s - %s", type(ex).__name__, ex)
                raise

    async def _query(self, cmd: tuple[ShadeCmd, bytes]) -> bytes:
        """Send a read-type opcode and return its payload bytes."""
        async with self._cmd_lock:
            await self._connect()
            try:
                try:
                    seq = await self._transact(cmd)
                except TimeoutError as ex:
                    raise TimeoutError("Device did not send response.") from ex
                if not self._verify_header(self._data, seq, cmd[0]):
                    raise BleakError("Malformed query response header")
                length = int(self._data[3])
                return bytes(self._data[4 : 4 + length])
            finally:
                await self._client.disconnect()

    @staticmethod
    def dec_manufacturer_data(data: bytearray) -> dict[str, float | int | bool]:
        """Decode manufacturer data from BLE advertisement V2."""
        if len(data) != 9:
            LOGGER.debug("not a V2 record!")
            return {}
        # data[3] lower 2 bits are status flags; pos is in bits 2-7 of data[3]
        # and bits 0-3 of data[4].  Read flags before extracting position so
        # the masking below doesn't accidentally overwrite them.
        flags: Final[int] = data[3] & 0x3
        # Mask pos2 bits (upper nibble of data[4]) out before forming the
        # 10-bit position value, otherwise a non-zero top-rail position on
        # TDBU shades contaminates the bottom-rail reading.
        pos: Final[int] = ((data[4] & 0x0F) << 6) | ((data[3] >> 2) & 0x3F)
        pos2: Final[int] = (int(data[5]) << 4) + (int(data[4]) >> 4)
        return {
            ATTR_CURRENT_POSITION: pos / 10,
            "position2": pos2 >> 2,
            "position3": int(data[6]),
            ATTR_CURRENT_TILT_POSITION: int(data[7]),
            "home_id": int.from_bytes(data[0:2], byteorder="little"),
            "type_id": int(data[2]),
            "is_opening": bool(flags == 0x2),
            "is_closing": bool(flags == 0x1),
            "battery_charging": bool(flags == 0x3),  # observed
            "battery_level": POWER_LEVELS[(data[8] >> 6)],
            "resetMode": bool(data[8] & 0x1),
            "resetClock": bool(data[8] & 0x2),
        }

    # position cmd: uint16_t pos1, uint16_t pos2, uint16_t pos3, uint16_t tilt, uint8_t velocity
    async def set_position(
        self,
        pos1: int,
        pos2: int = 0x8000,
        pos3: int = 0x8000,
        tilt: int = 0x8000,
        velocity: int = 0x0,
        disconnect: bool = True,
    ) -> None:
        """Set position of device."""
        LOGGER.debug(
            "%s setting position to %i/%i/%i, tilt %i, velocity %s",
            self.name,
            pos1,
            pos2,
            pos3,
            tilt,
            velocity,
        )
        await self._cmd(
            (
                ShadeCmd.SET_POSITION,
                int.to_bytes(pos1 * 100, 2, byteorder="little")
                + int.to_bytes(pos2, 2, byteorder="little")
                + int.to_bytes(pos3, 2, byteorder="little")
                + int.to_bytes(tilt, 2, byteorder="little")
                + int.to_bytes(velocity, 1),
            ),
            disconnect,
        )

    async def open(self, velocity: int = 0x0) -> None:
        """Fully open cover."""
        LOGGER.debug("%s open", self.name)
        await self.set_position(OPEN_POSITION, velocity=velocity, disconnect=False)

    async def stop(self) -> None:
        """Stop device movement."""
        LOGGER.debug("%s stop", self.name)
        await self._cmd((ShadeCmd.STOP, b""))

    async def close(self, velocity: int = 0x0) -> None:
        """Fully close cover."""
        LOGGER.debug("%s close", self.name)
        await self.set_position(CLOSED_POSITION, velocity=velocity, disconnect=False)

    # uint8_t scene#, uint8_t unknown
    # open: scene 2
    # close: scene 3
    async def activate_scene(self, idx: int) -> None:
        """Activate stored scene."""
        LOGGER.debug("%s set scene #%i", self.name, idx)
        await self._cmd(
            (
                ShadeCmd.ACTIVATE_SCENE,
                int.to_bytes(idx, 1, byteorder="little") + bytes([0xA2]),
            ),
        )

    async def identify(self, beeps: int = 0x3) -> None:
        """Identify device."""
        LOGGER.debug("%s identify (%i)", self.name, beeps)
        await self._cmd((ShadeCmd.IDENTIFY, bytes([min(beeps, 0xFF)])))

    def _verify_header(self, data: bytes, seq_nr: int, cmd: ShadeCmd) -> bool:
        """Verify common header fields (length, echoed opcode, seq match)."""
        if len(data) < 4:
            LOGGER.error("Response message too short")
            return False
        if int.from_bytes(data[0:2], byteorder="little") != cmd.value & 0xFFEF:
            LOGGER.warning("Response to wrong command")
            return False
        if int(data[2]) != seq_nr:
            LOGGER.warning(
                "Response sequence id %i wrong, expected %d", int(data[2]), seq_nr
            )
            return False
        return True

    def _verify_ack_reply(self, data: bytes, seq_nr: int, cmd: ShadeCmd) -> bool:
        """Verify an ack-only reply (1-byte status payload, 0 == success)."""
        if not self._verify_header(data, seq_nr, cmd):
            return False
        if int(data[3]) != 1:
            LOGGER.error("Wrong response data length")
            return False
        if int(data[4] != 0):
            LOGGER.error("Command %X returned error #%d", cmd.value, int(data[4]))
            return False
        return True

    async def query_dev_info(self) -> dict[str, str]:
        """Return detailed device information."""
        data: dict[str, str] = {}
        uuids: Final[dict[str, str]] = {
            "manufacturer": "2a29",
            "model": "2a24",
            "serial_nr": "2a25",
            "hw_rev": "2a27",
            "fw_rev": "2a26",
            "sw_rev": "2a28",
        }

        async with self._cmd_lock:
            try:
                await self._connect()

                for key, uuid in uuids.items():
                    LOGGER.debug("querying %s(%s)", key, uuid)
                    data[key] = (
                        (await self._client.read_gatt_char(normalize_uuid_str(uuid)))
                        .copy()
                        .decode("UTF-8")
                    )
            except BleakError as ex:
                LOGGER.debug("%s: querying failed: %s", self.name, ex)
                raise
            finally:
                await self.disconnect()
        LOGGER.debug("%s device data: %s", self.name, data)
        return data.copy()

    async def query_power_type(self) -> PowerType | None:
        """Return the shade's power source, or None if unknown/unavailable."""
        if self._cipher is None:
            return None
        try:
            payload = await self._query((ShadeCmd.POWER_STATUS, b""))
        except (BleakError, TimeoutError) as ex:
            LOGGER.debug("%s: power_type query failed: %s", self.name, ex)
            return None

        # A 1-byte reply is an error code (e.g. 0x04 = invalid length); it
        # must NOT be returned as a power_type or it would be misread as
        # PowerType.value=4.
        if len(payload) == 1:
            LOGGER.debug(
                "%s: power_type query rejected (err=0x%02X)", self.name, payload[0]
            )
            return None
        if len(payload) == 8:
            try:
                return PowerType(payload[0])
            except ValueError:
                LOGGER.debug(
                    "%s: unknown power_type byte 0x%02X", self.name, payload[0]
                )
                return None
        LOGGER.debug(
            "%s: unexpected power_type payload length %d", self.name, len(payload)
        )
        return None

    def _on_disconnect(self, client: BleakClient) -> None:
        """Disconnect callback function."""

        LOGGER.debug("Disconnected from %s", client.address)

    def _notification_handler(self, _sender, data: bytearray) -> None:
        LOGGER.debug("%s received BLE data: %s", self.name, data.hex(" "))
        self._data = bytes(data)
        if self._cipher is not None and self._is_encrypted:
            dec: AEADDecryptionContext = self._cipher.decryptor()
            self._data = bytes(dec.update(bytes(data)) + dec.finalize())
            LOGGER.debug(
                "%s %s",
                "decoded data: ".rjust(19 + len(self.name)),
                self._data.hex(" "),
            )

        self._data_event.set()

    async def _connect(self) -> None:
        """Connect to the device and setup notification if not connected."""

        LOGGER.debug("Connecting %s", self.name)

        if self.is_connected:
            LOGGER.debug("%s already connected", self.name)
            return

        start: float = time.time()
        self._client = await establish_connection(
            BleakClient,
            self._ble_device,
            self.name,
            disconnected_callback=self._on_disconnect,
            ble_device_callback=lambda: self._ble_device,
            services=[
                UUID_COV_SERVICE,
                UUID_DEV_SERVICE,
                # self.UUID_BAT_SERVICE,
            ],
        )
        await self._client.start_notify(UUID_TX, self._notification_handler)

        LOGGER.debug("\tconnect took %is", time.time() - start)

        # await self._query_dev_info()

    async def disconnect(self) -> None:
        """Disconnect the device and stop notifications."""

        if self.is_connected:
            LOGGER.debug("Disconnecting device %s", self.name)
            try:
                self._data_event.clear()
                await self._client.disconnect()
            except BleakError:
                LOGGER.warning("Disconnect failed!")
