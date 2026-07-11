"""Standalone debug report for PowerView BLE shade(s).

Talks directly to the shade over BLE (AES-CTR + GATT), with no coupling
to the Home Assistant integration code. Fetches the shade's homekey
from a G3 Gateway, scans for the BLE advertisement, then dumps:
  1. BLE advertisement (raw + decoded)
  2. Standard GATT device-info characteristics
  3. Decoded read-only PowerView queries:
       - 0xFFDD sel=4 product info        (firm update count, sw_rev)
       - 0xFFDD sel=5 extended product info (serial, fw/sw/hw_rev, type)
       - 0xFFDE power status              (hardwired vs battery, level)

Dependencies:  bleak, bleak-retry-connector, cryptography, requests  (no HA)

SAFETY: only the read-only opcodes above are ever sent. No set/move/
scene/rekey/factory-reset opcodes are implemented here.

Usage:
    python scripts/shade_report.py --ble-name PV-XXXXXX
    python scripts/shade_report.py --all
    python scripts/shade_report.py --all --hub http://hub.local -v
"""

from __future__ import annotations

from pathlib import Path
import sys

# Allow "python scripts/shade_report.py" (the documented invocation) to resolve
# the sibling package import below: we prepend the project root to sys.path so
# `scripts.extract_gateway3_homekey` is importable regardless of CWD.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse
import asyncio
import base64
import contextlib
import json
import logging
import struct
from typing import Any, Self

from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
from bleak.uuids import normalize_uuid_str
from bleak_retry_connector import establish_connection
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
import requests

from scripts.extract_gateway3_homekey import HUB, get_shade_key

# --- PowerView BLE protocol ---------------------------------------
MFCT_ID = 2073  # Hunter Douglas BLE manufacturer identifier
UUID_TX = "cafe1001-c0ff-ee01-8000-a110ca7ab1e0"
UUID_COV_SERVICE = normalize_uuid_str("fdc1")
UUID_DEV_SERVICE = normalize_uuid_str("180a")

# Standard GATT Device Information Service characteristics
GATT_DEV_INFO: list[tuple[str, str]] = [
    ("manufacturer", "2a29"),
    ("model", "2a24"),
    ("serial_nr", "2a25"),
    ("hw_rev", "2a27"),
    ("fw_rev", "2a26"),
    ("sw_rev", "2a28"),
]

# Read-only protocol queries ONLY. Never add opcodes that mutate state
# (e.g. 0x01F7 move, 0xFA5A set-scene, 0xFB02 rekey, 0xFFDF set-power-
# type, 0xFFEE factory-reset).
#
# Decoded queries (fw_rev=22, Duette type 6, hardwired):
#   0xFFDD → product-info opcode gated on a 1-byte selector.  sel=4
#            returns a 14-byte page (firm update count u32 LE at bytes
#            2-5, firm current version u16 LE at bytes 6-7 matching
#            GATT sw_rev).  sel=5 returns a 28-byte extended page with
#            every field confirmed against GATT Device Information:
#            serial (bytes 2-9, little-endian), fw_rev (10-11), sw_rev
#            (14-15), hw_rev (18-21 u32 LE), build/mfg id (22-25), and
#            type_id (26) matching the advertisement.  Decoded in
#            annotate_query.  0xF1DD returns the same sel=4 page and
#            a truncated 22-byte sel=5 (no trailing 6 bytes); not
#            queried separately since its data is a strict subset.
#            The emulator's ret_valFFDD (29 B, labelled "HW
#            diagnostics") is actually this extended product info with
#            dummy data, off-by-one vs real firmware.
#   0xFFDE → 8-byte payload.  Byte-by-byte findings on hardwired Duette
#            (type 6, fw_rev=22), verified across four shades and ~10
#            targeted motion/idle experiments on one unit, then corrected
#            against a six-shade dump cross-checked with the G3 hub:
#              b0 = 0x00       status, 0 = success.  NOT the power type.
#                              Every reply in this protocol leads with a
#                              status code — a rejected 0xFFDE returns a
#                              1-byte 0x04 (invalid length) in this same
#                              position, and _verify_ack_reply treats
#                              payload[0] == 0 as success.  It was read as
#                              a power-source enum once, which made every
#                              shade that answered look hardwired (0 being
#                              the hardwired code) and silently stripped
#                              the battery sensors off battery shades.  A
#                              constant cannot classify anything; the only
#                              reason it looked right is that every sample
#                              behind it came from a hardwired shade.
#              b1 = 0x01       power type, and it agrees with the hub: the
#                              G3 reports powerType=1 for these same six
#                              shades, all of them confirmed mains-powered.
#                              The battery and rechargeable codes are still
#                              unknown — no such sample exists yet.
#              b2 = 0x64 (100) strong hypothesis: battery/power-level
#                              %, pegged at 100 on wall power.  A
#                              battery shade should report its real %
#                              here.  NOT a duplicate of the advert's
#                              2-bit level field — this is the full
#                              byte-resolution value.
#              b3              live / noisy byte — read-to-read drift
#                              of 3-9 counts with no physical change.
#                              Too jittery to be temperature; best
#                              guess is radio or ADC telemetry.  Not
#                              practically useful without a burst-
#                              capture characterization.
#              b4 = 0x07       constant on all hardwired shades seen.
#                              Likely capability or power-config flags.
#              b5              persistent per-shade state.  Observed
#                              to change exactly once across ~10
#                              experiments on one unit (227 -> 205
#                              during the first commanded move of a
#                              session).  Each idle shade holds its
#                              own stable value (114/127/139/205 seen
#                              at position=100%).  Triggers RULED OUT:
#                              every move, cold-start after 15-min
#                              idle, short-period timer, distance-
#                              gated motion snapshot, top & bottom
#                              end-stop calibration.  Remaining
#                              candidates: boot-triggered state,
#                              long-period (hours+) timer, or hub-
#                              initiated refresh.
#              b6 type_id      matches advertisement byte 2 (confirmed).
#              b7 = 0x00       reserved / padding on all observations.
#            All b1-b7 findings are fw_rev=22 hardwired-only; re-verify
#            on battery / rechargeable hardware before relying on them.
#            0xFFDE remains the only reliable source for battery-vs-
#            hardwired detection (the advert's 2-bit level field caps
#            at 3 = "100% OR hardwired" and cannot disambiguate).
# (cmd, payload, label) — each entry is a query that returns real
# decoded data on fw_rev=22.  0xFFDD's two selectors are complementary:
# sel=4 carries firm update count + firm current version; sel=5 carries
# serial / fw_rev / sw_rev / hw_rev / build id / type_id.  0xF1DD is a
# strict subset of 0xFFDD (same sel=4 bytes; sel=5 drops the trailing 6)
# so it's not queried separately.
GET_QUERIES: list[tuple[int, bytes, str]] = [
    (0xFFDD, b"\x04", "product info (sel=4)"),
    (0xFFDD, b"\x05", "ext product info (sel=5)"),
    (0xFFDE, b"", "power status"),
]

# Advert level field is 2 bits, so only values 0-3 are observable on the wire.
POWER_LEVELS = {3: 100, 2: 50, 1: 20, 0: 0}

# PowerView BLE error-response shape (see api.py _verify_response): when a
# query's response has data_len=1, byte 4 is an error code rather than real
# payload.  Expand this dict as users report codes from different firmwares.
PV_ERROR_CODES: dict[int, str] = {
    0x04: "invalid length (hypothesis)",
}

# 0xFFDE byte 1 — power type (byte 0 is the reply's status code, not the power
# type).  1 = hardwired is confirmed: six shades at fw_rev=22 all report b1=0x01,
# the G3 hub independently reports powerType=1 for the same six, and all six are
# known to be mains-powered.  Every other code is deliberately absent: guessing
# at them is what produced the last bug, so an unseen value prints as unknown.
POWER_TYPE_LABELS: dict[int, str] = {
    1: "hardwired (confirmed)",
}

# Known-good response lengths per opcode.  Used only by annotate_query
# to decide whether a 1-byte reply should be interpreted as an error
# code (i.e. "we expected a longer payload and got a single byte — that
# byte is the error").  0xFFDD has two valid shapes because its two
# queried selectors return different lengths; 0xF1DD's 14- and 22-byte
# shapes are retained for completeness even though the script no longer
# queries that opcode directly.  Emulator's 0xFFDD dummy is 29 B but
# real fw_rev=22 returns 28 B at sel=5 — treat emulator as off-by-one.
EXPECTED_QUERY_LEN: dict[int, set[int]] = {
    0xF1DD: {14, 22},
    0xFFDD: {14, 28},
    0xFFDE: {8},
}

SCAN_TIMEOUT = 30.0
CMD_TIMEOUT = 30.0
GATEWAY_TIMEOUT = 30


# --- Gateway shade list --------------------------------------------
def fetch_shade_list(hub: str) -> list[dict[str, Any]]:
    """Return the list of shades registered with the G3 gateway."""
    resp = requests.get(f"{hub}/home/shades", timeout=GATEWAY_TIMEOUT)
    resp.raise_for_status()
    return json.loads(resp.content)


# --- BLE advertisement decode --------------------------------------
def decode_adv(manuf: bytes) -> dict[str, Any] | None:
    """Decode the 9-byte V2 advertisement payload."""
    if len(manuf) != 9:
        return None
    flags = manuf[3] & 0x3
    pos = ((manuf[4] & 0x0F) << 6) | ((manuf[3] >> 2) & 0x3F)
    pos2 = (manuf[5] << 4) + (manuf[4] >> 4)
    level_bits = (manuf[8] >> 6) & 0x3
    return {
        "home_id": int.from_bytes(manuf[0:2], "little"),
        "type_id": manuf[2],
        "position": pos / 10,
        "position2": pos2 >> 2,
        "position3": manuf[6],
        "tilt": manuf[7],
        "opening": flags == 0x2,
        "closing": flags == 0x1,
        "charging_flag": flags == 0x3,
        "level_bits": level_bits,
        "level_pct": POWER_LEVELS.get(level_bits, "?"),
    }


# --- BLE scanner helper --------------------------------------------
async def find_shades(
    names: set[str], timeout: float
) -> dict[str, tuple[BLEDevice, AdvertisementData]]:
    """Scan once, return the first advertisement seen for each named shade."""
    loop = asyncio.get_running_loop()
    results: dict[str, tuple[BLEDevice, AdvertisementData]] = {}
    done = asyncio.Event()

    def detected(dev: BLEDevice, adv: AdvertisementData) -> None:
        if dev.name in names and dev.name not in results:
            results[dev.name] = (dev, adv)
            if len(results) == len(names):
                loop.call_soon_threadsafe(done.set)

    scanner = BleakScanner(detection_callback=detected)
    await scanner.start()
    try:
        await asyncio.wait_for(done.wait(), timeout=timeout)
    except TimeoutError:
        pass
    finally:
        await scanner.stop()
    return results


# --- Minimal PowerView BLE client ----------------------------------
class PowerViewClient:
    """Talks the PowerView BLE protocol.

    Wire format (TX and RX, both encrypted with AES-128-CTR, IV = 16 zero
    bytes, key = the shade's homekey):

        byte 0..1  command        (little-endian uint16)
        byte 2     sequence       (TX: incremented; RX: echoed from TX)
        byte 3     payload length
        byte 4..   payload

    Both write and notify happen on the same UUID_TX characteristic.
    """

    def __init__(self, device: BLEDevice, home_key: bytes) -> None:
        """Store connection parameters; actual connect happens in __aenter__."""
        self.device = device
        self.client = BleakClient(device)
        self.home_key = home_key
        self.seq = 1
        self._rx: bytes = b""
        self._rx_event = asyncio.Event()

    async def __aenter__(self) -> Self:
        """Connect over BLE and subscribe to the TX characteristic."""
        # establish_connection (bleak_retry_connector) retries automatically on
        # transient WinRT errors and weak-signal cancellations, which plain
        # BleakClient.connect() surfaces as TimeoutError.  The services= filter
        # tells the stack which GATT services to enumerate, speeding discovery
        # on marginal links.  Mirrors the integration's connect path in api.py.
        self.client = await establish_connection(
            BleakClient,
            self.device,
            self.device.name or "shade",
            max_attempts=3,
            services=[UUID_COV_SERVICE, UUID_DEV_SERVICE],
        )
        await self.client.start_notify(UUID_TX, self._on_notify)
        return self

    async def __aexit__(self, *_: object) -> None:
        """Disconnect best-effort; errors here are not actionable."""
        with contextlib.suppress(Exception):
            await self.client.disconnect()

    def _aes(self) -> Cipher[modes.CTR]:
        return Cipher(algorithms.AES(self.home_key), modes.CTR(bytes(16)))

    def _on_notify(self, _sender: object, data: bytearray) -> None:
        dec = self._aes().decryptor()
        self._rx = dec.update(bytes(data)) + dec.finalize()
        self._rx_event.set()

    async def read_gatt(self, uuid: str) -> bytes:
        """Read a standard GATT characteristic by 16-bit UUID."""
        return bytes(await self.client.read_gatt_char(normalize_uuid_str(uuid)))

    async def query(self, cmd: int, data: bytes = b"") -> bytes:
        """Send a read-only command, return the response payload."""
        # cmd is packed BIG-endian so the wire bytes appear in the order the
        # shade expects: wire byte 0 = high byte of opcode, byte 1 = low byte.
        # GET_QUERIES uses the emulator's case-label naming (0xF1DD, 0xFFDD,
        # 0xFFDE), where byte 0 = 0xF1/0xFF and byte 1 = 0xDD/0xDE. The
        # integration enum (api.py) uses pre-byte-swapped values with
        # little-endian packing to reach the same wire bytes; here we keep
        # the emulator naming and byte-swap at pack time instead.
        tx = struct.pack(">HBB", cmd, self.seq, len(data)) + data
        enc = self._aes().encryptor()
        tx_enc = enc.update(tx) + enc.finalize()
        self.seq = (self.seq + 1) & 0xFF
        self._rx_event.clear()
        await self.client.write_gatt_char(UUID_TX, tx_enc, response=False)
        await asyncio.wait_for(self._rx_event.wait(), timeout=CMD_TIMEOUT)
        if len(self._rx) < 4:
            raise ValueError(f"Response too short: {self._rx.hex(' ')}")
        payload_len = self._rx[3]
        return self._rx[4 : 4 + payload_len]


# --- Report rendering ----------------------------------------------
def _section(title: str) -> None:
    print(f"\n  {title}")
    print(f"  {'-' * len(title)}")


def annotate_query(cmd: int, payload: bytes) -> str | None:
    """Return a one-line decode of a query response, or None if unknown.

    Printed beneath the raw hex so reporters see both.  Everything
    marked "hypothesis" still needs cross-shade confirmation — that's
    the whole point of this report.
    """
    expected = EXPECTED_QUERY_LEN.get(cmd, set())
    # Shade returned a 1-byte reply where we know a longer one is valid:
    # standard PowerView error-response shape — byte is an error code.
    if expected and len(payload) == 1 and 1 not in expected:
        err = payload[0]
        note = PV_ERROR_CODES.get(err, "unknown error code")
        good = ", ".join(str(n) for n in sorted(expected))
        return (
            f"likely error code 0x{err:02X} ({note}); "
            f"known good lengths: {good} B"
        )
    # Two-byte `8c XX` rejection: byte 0 is a fixed reject code, byte 1
    # echoes the bad input byte (observed for selector=0 and selector=3
    # on fw_rev=22, where the selector is the first byte of the query
    # payload).  Decoding this helps distinguish "bad selector" from
    # other short error shapes.
    if len(payload) == 2 and payload[0] == 0x8C:
        return (
            f"rejection (0x8C) — selector 0x{payload[1]:02X} "
            f"({payload[1]}) not supported by this opcode"
        )
    # 14-byte product-info page, returned by 0xF1DD and 0xFFDD when the
    # payload's first byte is 0x04.  The two opcodes return the same
    # bytes here (sel=5 is where they diverge — see below).  Confirmed
    # on fw_rev=22: byte 1 echoes the selector (0x04), bytes 2-5 are a
    # u32 counter (=1 on a freshly-imaged shade), and bytes 6-7 LE are
    # the firmware version (matches GATT sw_rev exactly — the emulator
    # also embeds SW_VERSION there in its ret_valF1DD dummy).  Bytes
    # 8-13 are zero on our samples; meaning unknown.
    if cmd in (0xF1DD, 0xFFDD) and len(payload) == 14 and payload[1] == 0x04:
        counter = int.from_bytes(payload[2:6], "little")
        firm_current = int.from_bytes(payload[6:8], "little")
        indent = "           "
        return (
            "14-byte product info (selector=0x04). Fields:\n"
            f"{indent}byte 1 = 0x{payload[1]:02X} → echoed selector\n"
            f"{indent}bytes 2-5 = {payload[2:6].hex(' ')} → "
            f"counter={counter} (u32 LE; hypothesis — firm update count?)\n"
            f"{indent}bytes 6-7 = {payload[6:8].hex(' ')} → firm current "
            f"version={firm_current} (confirmed; matches GATT sw_rev)\n"
            f"{indent}bytes 8-13 = {payload[8:14].hex(' ')} → unknown "
            f"(zero on all samples)"
        )
    # Extended product info (selector=0x05).  0xF1DD returns a 22-byte
    # subset; 0xFFDD returns 28 bytes (same first 22 plus 4-byte build/
    # mfg counter + type_id + model byte).  Every field below is
    # confirmed against GATT Device Information on fw_rev=22:
    #   bytes 2-9   = 8-byte serial LE (matches GATT 0x2a25 hex-reversed)
    #   bytes 10-11 = u16 LE fw_rev   (matches GATT 0x2a26)
    #   bytes 14-15 = u16 LE sw_rev   (matches GATT 0x2a28)
    #   bytes 18-21 = u32 LE hw_rev   (matches GATT 0x2a27)
    # The FFDD-only tail is still provisional — byte 26 matches the
    # advert's type_id; bytes 22-25 look like another 32-bit identifier
    # (1015780 on the "Study" shade), and byte 27 is a model/family byte.
    if (
        cmd in (0xF1DD, 0xFFDD)
        and len(payload) in (22, 28)
        and payload[1] == 0x05
    ):
        serial_hex = payload[2:10][::-1].hex().upper()
        fw_rev = int.from_bytes(payload[10:12], "little")
        sw_rev = int.from_bytes(payload[14:16], "little")
        hw_rev = int.from_bytes(payload[18:22], "little")
        indent = "           "
        lines = [
            f"{len(payload)}-byte extended product info (selector=0x05). "
            "Fields (all confirmed vs GATT):",
            f"{indent}byte 1 = 0x{payload[1]:02X} → echoed selector",
            f"{indent}bytes 2-9 = {payload[2:10].hex(' ')} → "
            f"serial={serial_hex} (LE; matches GATT 0x2a25)",
            f"{indent}bytes 10-11 = {payload[10:12].hex(' ')} → "
            f"fw_rev={fw_rev} (matches GATT 0x2a26)",
            f"{indent}bytes 12-13 = {payload[12:14].hex(' ')} → "
            "reserved (zero on samples)",
            f"{indent}bytes 14-15 = {payload[14:16].hex(' ')} → "
            f"sw_rev={sw_rev} (matches GATT 0x2a28)",
            f"{indent}bytes 16-17 = {payload[16:18].hex(' ')} → "
            "reserved (zero on samples)",
            f"{indent}bytes 18-21 = {payload[18:22].hex(' ')} → "
            f"hw_rev={hw_rev} (matches GATT 0x2a27)",
        ]
        if len(payload) == 28:
            build_id = int.from_bytes(payload[22:26], "little")
            lines.extend(
                [
                    f"{indent}bytes 22-25 = {payload[22:26].hex(' ')} → "
                    f"build/mfg id={build_id} (u32 LE; hypothesis)",
                    f"{indent}byte 26 = 0x{payload[26]:02X} → "
                    f"type_id={payload[26]} (matches advert)",
                    f"{indent}byte 27 = 0x{payload[27]:02X} → "
                    "model/family byte (hypothesis)",
                ]
            )
        return "\n".join(lines)
    if cmd == 0xFFDE and len(payload) == 8:
        power_type = payload[1]
        label = POWER_TYPE_LABELS.get(power_type, f"unknown ({power_type})")
        # Per-byte interpretations as of 2026-04-21, corrected once the hub
        # cross-check landed (see module-level comment).  b0, b1 and b6 are
        # confirmed; b2/b4/b7 are strong hypotheses based on hardwired shades
        # at fw_rev=22; b3 is a live noisy byte; b5 is persistent per-shade
        # state with an unknown refresh trigger.
        # 11-space indent on continuation lines aligns with the "→ "
        # prefix added by _print_queries (9 spaces + arrow + space).
        indent = "           "
        return (
            f"byte 0 = 0x{payload[0]:02X} → status (0 = success; NOT the power "
            f"type — reading it as one is what hid the battery sensors)\n"
            f"{indent}byte 1 = 0x{payload[1]:02X} → power_type={power_type} "
            f"({label}); same encoding as the hub's powerType\n"
            f"{indent}byte 2 = 0x{payload[2]:02X} ({payload[2]}) → "
            f"battery/power level % (hypothesis — pegged at 100 on hardwired)\n"
            f"{indent}byte 3 = 0x{payload[3]:02X} → live/noisy byte "
            f"(drifts between reads; radio or ADC telemetry)\n"
            f"{indent}byte 4 = 0x{payload[4]:02X} → capability/config flags "
            f"(const 0x07 on hardwired; hypothesis)\n"
            f"{indent}byte 5 = 0x{payload[5]:02X} → persistent per-shade state "
            f"(rarely updates; refresh trigger unknown)\n"
            f"{indent}byte 6 = 0x{payload[6]:02X} → type_id={payload[6]} "
            f"(cross-check with advert)\n"
            f"{indent}byte 7 = 0x{payload[7]:02X} → reserved/padding"
        )
    return None


def _print_advertisement(device: BLEDevice, adv: AdvertisementData) -> None:
    """Print section 1: the BLE advertisement (raw + decoded)."""
    print(f"  address:  {device.address}")
    print(f"  rssi:     {adv.rssi} dBm")
    manuf = adv.manufacturer_data.get(MFCT_ID, b"")
    print(f"  raw:      {manuf.hex(' ') if manuf else '(none)'}")
    decoded = decode_adv(manuf)
    if not decoded:
        return
    print(f"  home_id:  0x{decoded['home_id']:04x}")
    print(f"  type_id:  {decoded['type_id']}")
    print(
        f"  position: {decoded['position']}  "
        f"pos2={decoded['position2']}  pos3={decoded['position3']}  "
        f"tilt={decoded['tilt']}"
    )
    print(
        f"  flags:    opening={decoded['opening']}  "
        f"closing={decoded['closing']}  "
        f"charging_flag={decoded['charging_flag']}"
    )
    print(
        f"  level:    bits={decoded['level_bits']} (→ {decoded['level_pct']}%)"
    )


async def _print_gatt_info(api: PowerViewClient) -> None:
    """Print section 2: standard GATT device-info characteristics."""
    for key, uuid in GATT_DEV_INFO:
        try:
            value = (await api.read_gatt(uuid)).decode("utf-8", errors="replace")
            print(f"  {key:13} {value}")
        except Exception as ex:  # noqa: BLE001
            print(f"  {key:13} <error: {ex}>")


async def _print_queries(api: PowerViewClient) -> None:
    """Print section 3: read-only protocol queries."""
    for cmd, req, label in GET_QUERIES:
        try:
            resp = await api.query(cmd, req)
            print(
                f"  0x{cmd:04X} {label:25} "
                f"({len(resp):2} B): {resp.hex(' ')}"
            )
            note = annotate_query(cmd, resp)
            if note:
                print(f"         → {note}")
        except Exception as ex:  # noqa: BLE001
            print(f"  0x{cmd:04X} {label:25} FAILED — {ex}")
            break


async def report_shade(
    friendly_name: str,
    ble_name: str,
    home_key: bytes,
    device: BLEDevice | None,
    adv: AdvertisementData | None,
) -> None:
    """Print the full report for a single shade: advertisement + GATT + queries."""
    print(f"\n=== '{friendly_name}'  (BLE: {ble_name}) ===")

    _section("1. BLE advertisement")
    if device is None or adv is None:
        print("  not seen on air within scan window — skipping BLE queries")
        return
    _print_advertisement(device, adv)

    async with PowerViewClient(device, home_key) as api:
        _section("2. GATT device info")
        await _print_gatt_info(api)

        _section("3. Protocol queries (read-only)")
        await _print_queries(api)


# --- Main ----------------------------------------------------------
PER_SHADE_TIMEOUT = 45.0  # hard cap on total time spent per shade's report


async def run(
    hub: str,
    targets: list[tuple[str, str]] | None,
    all_shades: bool,
    scan_timeout: float,
    verbose: bool,
) -> int:
    """Fetch the shade list, homekey, then report on each target."""
    if verbose:
        logging.basicConfig(level=logging.DEBUG)

    # Always fetch the hub's full shade list: we need it regardless of
    # whether targets came from --all or --ble-name, because the homekey
    # fetch should use whichever shade the hub sees strongest, not
    # necessarily one the user happens to be targeting locally.
    print(f"Fetching shade list from {hub}...")
    try:
        shades = fetch_shade_list(hub)
    except Exception as ex:  # noqa: BLE001
        print(f"  failed: {ex}")
        return 1
    print(f"  found {len(shades)} shade(s)")

    if all_shades:
        targets = [
            (base64.b64decode(s["name"]).decode("utf-8"), s["bleName"])
            for s in shades
        ]
    assert targets is not None

    # Homekey is network-wide — fetch once from any shade the hub can
    # reach.  Try the hub's strongest-signal shades first to avoid
    # burning the 10s timeout on one that's currently unreachable.
    key_candidates = sorted(
        shades, key=lambda s: s.get("signalStrength", -100), reverse=True
    )
    print(f"\nFetching homekey from {hub} (one-time for the home)...")
    home_key: bytes | None = None
    for s in key_candidates:
        ble = s["bleName"]
        sig = s.get("signalStrength", "?")
        try:
            home_key = get_shade_key(hub, ble)
            print(f"  got it via {ble} (hub signal {sig}): {home_key.hex()}")
            break
        except Exception as ex:  # noqa: BLE001
            print(f"  {ble} (hub signal {sig}): {ex} — trying next shade...")
    if home_key is None:
        print("Could not fetch homekey from any shade. Aborting.")
        return 1

    name_set = {ble for _, ble in targets}
    print(
        f"\nScanning for {len(name_set)} shade(s) "
        f"(up to {scan_timeout:.0f}s; tap a hardwired shade if not seen)..."
    )
    seen = await find_shades(name_set, scan_timeout)
    print(f"  {len(seen)}/{len(name_set)} visible on air")

    for friendly, ble in targets:
        dev, adv = seen.get(ble, (None, None))
        # Per-shade timeout so one unreachable shade can't stall the whole run.
        try:
            await asyncio.wait_for(
                report_shade(friendly, ble, home_key, dev, adv),
                timeout=PER_SHADE_TIMEOUT,
            )
        except TimeoutError:
            print(f"  (shade {ble} exceeded {PER_SHADE_TIMEOUT:.0f}s — skipping)")
    return 0


def main() -> int:
    """Parse CLI args and dispatch to the async report runner."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hub", default=HUB, help=f"Gateway URL (default: {HUB})")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--ble-name",
        action="append",
        help="BLE name of a shade (repeatable), e.g. 'PV-XXXXXX'",
    )
    group.add_argument(
        "--all", action="store_true", help="Report on every shade known to the gateway"
    )
    parser.add_argument(
        "--scan-timeout",
        type=float,
        default=SCAN_TIMEOUT,
        help=f"BLE scan timeout in seconds (default: {SCAN_TIMEOUT})",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Enable debug logging"
    )
    args = parser.parse_args()

    targets: list[tuple[str, str]] | None = None
    if args.ble_name:
        targets = [(ble, ble) for ble in args.ble_name]
    return asyncio.run(
        run(
            args.hub,
            targets,
            args.all,
            args.scan_timeout,
            args.verbose,
        )
    )


if __name__ == "__main__":
    sys.exit(main())
