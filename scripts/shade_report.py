"""Standalone debug report for PowerView BLE shade(s).

Talks directly to the shade over BLE (AES-CTR + GATT), with no coupling
to the Home Assistant integration code. Fetches the shade's homekey
from a G3 Gateway, scans for the BLE advertisement, then dumps:
  1. BLE advertisement (raw + decoded)
  2. Standard GATT device-info characteristics
  3. Read-only PowerView queries:
       - 0xF1DD product info
       - 0xFFDD HW diagnostics (contains serial / firmware / type / model)
       - 0xFFDE power status (battery vs. hardwired)

Dependencies:  bleak, bleak-retry-connector, cryptography, requests  (no HA)

SAFETY: only the three read-only opcodes above are ever sent. No
set/move/scene/rekey/factory-reset opcodes are implemented here.

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
# Observed (fw_rev=22, Duette type 6, hardwired):
#   0xF1DD → 1-byte `04` payload on len=0 input; 2-byte `8c 00` on len=1;
#            `04` again for len=2/4/8.  The `8c 00` response is IDENTICAL
#            to what 0xFFDD returns at len=1, so it's a shared reject
#            path, not real data.  Conclusion: F1DD is either disabled or
#            gated on specific non-zero parameter bytes we haven't found.
#            Standard GATT chars (2a24-2a29) already give us the product
#            info this opcode would have returned.
#   0xFFDD → same signature as F1DD: `04` at len∈{0,2,4,8}, `8c 00` at
#            len=1.  Same conclusion — superseded by GATT 180A service
#            (manufacturer/model/serial/hw_rev/fw_rev/sw_rev).
#   0xFFDE → 8-byte payload.  Byte-by-byte findings on hardwired Duette
#            (type 6, fw_rev=22), verified across four shades and ~10
#            targeted motion/idle experiments on one unit:
#              b0 power_type   0=hardwired (confirmed); 1=battery,
#                              2=rechargeable (hypothesis — no non-
#                              hardwired sample yet).
#              b1 = 0x01       constant on all hardwired shades seen.
#                              Likely a power-type subvariant or flags
#                              byte; pending a non-hardwired sample.
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
GET_QUERIES: list[tuple[int, str]] = [
    (0xF1DD, "product info"),
    (0xFFDD, "HW diagnostics"),
    (0xFFDE, "power status"),
]

# Advert level field is 2 bits, so only values 0-3 are observable on the wire.
POWER_LEVELS = {3: 100, 2: 50, 1: 20, 0: 0}

# PowerView BLE error-response shape (see api.py _verify_response): when a
# query's response has data_len=1, byte 4 is an error code rather than real
# payload.  Expand this dict as users report codes from different firmwares.
PV_ERROR_CODES: dict[int, str] = {
    0x04: "invalid length (hypothesis)",
}

# Observed two-byte error signatures.  fw_rev=22 returns `8c 00` for both
# 0xF1DD and 0xFFDD with a 1-byte input — identical bytes across two
# semantically different opcodes, so this is a shared reject path rather
# than opcode-specific data.  Suspected meaning: "unsupported / bad
# parameter", but not confirmed.
PV_ERROR_SIGNATURES_2B: dict[bytes, str] = {
    b"\x8c\x00": "generic reject (hypothesis; fw_rev=22, F1DD+FFDD)",
}

# 0xFFDE byte 0 — power type.  0 is confirmed hardwired on fw_rev=22.
# 1/2 are hypotheses pending sample data from battery/rechargeable shades.
POWER_TYPE_LABELS: dict[int, str] = {
    0: "hardwired",
    1: "battery (hypothesis)",
    2: "rechargeable (hypothesis)",
}

# Emulator-confirmed success-response lengths (see emu/PV_BLE_cover.ino).
# Used to flag a real shade's 1-byte reply as "way shorter than expected".
EXPECTED_QUERY_LEN: dict[int, int] = {
    0xF1DD: 14,
    0xFFDD: 29,
    0xFFDE: 8,
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
    expected = EXPECTED_QUERY_LEN.get(cmd)
    # Shade returned a 1-byte reply where we know a longer one is valid:
    # standard PowerView error-response shape — byte is an error code.
    if expected and len(payload) == 1 and expected > 1:
        err = payload[0]
        note = PV_ERROR_CODES.get(err, "unknown error code")
        return (
            f"likely error code 0x{err:02X} ({note}); "
            f"emulator returns {expected} B on success"
        )
    if cmd == 0xFFDE and len(payload) == 8:
        pt = payload[0]
        label = POWER_TYPE_LABELS.get(pt, f"unknown ({pt})")
        # Per-byte interpretations as of 2026-04-21 investigation (see
        # module-level comment for the experiment log).  b0 and b6 are
        # confirmed; b1/b2/b4/b7 are strong hypotheses based on four
        # hardwired shades at fw_rev=22; b3 is a live noisy byte; b5 is
        # persistent per-shade state with an unknown refresh trigger.
        # 11-space indent on continuation lines aligns with the "→ "
        # prefix added by _print_queries (9 spaces + arrow + space).
        indent = "           "
        return (
            f"byte 0 = 0x{payload[0]:02X} → power_type={pt} ({label})\n"
            f"{indent}byte 1 = 0x{payload[1]:02X} → power-subtype/flags "
            f"(const 0x01 on hardwired; hypothesis)\n"
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


# Payload lengths to try when --probe is set.  Small, zero-filled inputs
# only — we are varying length to see which values the firmware accepts,
# not guessing parameter content.  Kept short because longer sweeps take
# longer to run and each probe is an encrypted BLE round-trip.
PROBE_LENGTHS: list[int] = [0, 1, 2, 4, 8]

# Opcodes we'll probe.  MUST only contain documented read-only GETs;
# never add a SET/move/scene/rekey/reset opcode here (see SAFETY note in
# the module docstring).
PROBE_OPCODES: list[tuple[int, str]] = [
    (0xF1DD, "product info"),
    (0xFFDD, "HW diagnostics"),
]


async def probe_opcode(api: PowerViewClient, cmd: int, label: str) -> None:
    """Sweep zero-filled input payloads across PROBE_LENGTHS.

    Tag policy: `HIT` only when the response length matches what the
    emulator returns on success (EXPECTED_QUERY_LEN).  Anything shorter
    is flagged `short?`; shorter-than-3 is almost certainly an error
    shape — fw_rev=22 has been observed returning both 1-byte (`04`)
    and 2-byte (`8c 00`) rejections, the latter being the *same* for
    different opcodes, so a short payload is not proof of a real hit.
    """
    expected = EXPECTED_QUERY_LEN.get(cmd, 0)
    print(f"  0x{cmd:04X} {label} (expect {expected} B on success):")
    for n in PROBE_LENGTHS:
        data = bytes(n)
        try:
            payload = await api.query(cmd, data)
            if expected and len(payload) == expected:
                tag = "HIT   "
            elif len(payload) < 3:
                tag = "error?"
            else:
                tag = "short?"
            hex_display = payload.hex(" ") if payload else "(empty)"
            print(
                f"    len={n:2} → "
                f"{tag} ({len(payload):2} B): {hex_display}"
            )
        except Exception as ex:  # noqa: BLE001
            print(f"    len={n:2} → FAILED — {ex}")


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
    for cmd, label in GET_QUERIES:
        try:
            payload = await api.query(cmd)
            print(
                f"  0x{cmd:04X} {label:16} "
                f"({len(payload):2} B): {payload.hex(' ')}"
            )
            note = annotate_query(cmd, payload)
            if note:
                print(f"         → {note}")
        except Exception as ex:  # noqa: BLE001
            print(f"  0x{cmd:04X} {label:16} FAILED — {ex}")
            break


async def _print_probe(api: PowerViewClient) -> None:
    """Print section 4: payload-length sweep over read-only probe opcodes."""
    for cmd, label in PROBE_OPCODES:
        try:
            await probe_opcode(api, cmd, label)
        except Exception as ex:  # noqa: BLE001
            print(f"  0x{cmd:04X} probe aborted — {ex}")
            break


async def report_shade(
    friendly_name: str,
    ble_name: str,
    home_key: bytes,
    device: BLEDevice | None,
    adv: AdvertisementData | None,
    probe: bool = False,
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

        if probe:
            _section("4. Probe (payload-length sweep, read-only opcodes)")
            await _print_probe(api)


# --- Main ----------------------------------------------------------
PER_SHADE_TIMEOUT = 45.0  # hard cap on total time spent per shade's report


async def run(
    hub: str,
    targets: list[tuple[str, str]] | None,
    all_shades: bool,
    scan_timeout: float,
    verbose: bool,
    probe: bool = False,
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
                report_shade(friendly, ble, home_key, dev, adv, probe=probe),
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
    parser.add_argument(
        "--probe",
        action="store_true",
        help=(
            "Sweep payload lengths for 0xF1DD/0xFFDD to find what the "
            "firmware accepts.  Read-only; slow (one BLE round-trip per "
            "length).  Share the output to help decode these opcodes."
        ),
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
            probe=args.probe,
        )
    )


if __name__ == "__main__":
    sys.exit(main())
