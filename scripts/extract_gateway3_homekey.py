"""Extract PowerView homekey from a G3 PowerView Gateway."""

import base64
import json
import struct
from typing import Any, Final

import requests

HUB: Final[str] = "http://powerview-g3.local"
TIMEOUT: Final[int] = 10


def create_request(sid: int, cid: int, sequence_id: int, data: bytes) -> bytes:
    """Assemble a request frame for the PowerView protocol."""
    return struct.pack("<BBBB", sid, cid, sequence_id, len(data)) + data


def decode_response(packet: bytes) -> dict[str, Any]:
    """Decode a response frame from the PowerView protocol."""
    if len(packet) < 4:
        raise ValueError("Packet size too small")
    sid, cid, sequence_id, length = struct.unpack("<BBBB", packet[0:4])
    if len(packet) != 4 + length:
        raise ValueError("Not all data present")
    if length < 1:
        raise ValueError("No errorCode present")
    (error_code,) = struct.unpack("<B", packet[4:5])
    data: Final[bytes] = packet[5:]
    return {
        "cid": cid,
        "sid": sid,
        "sequenceId": sequence_id,
        "errorCode": error_code,
        "data": data,
    }


def create_get_shade_key_request(sequence_id) -> bytes:
    """Create a GetShadeKey request frame."""
    return create_request(251, 18, sequence_id, b"")


def get_shade_key(hub: str, ble_name) -> bytes:
    """Get the homekey for a shade."""
    try:
        shades_exec_resp: requests.Response = requests.post(
            hub + "/home/shades/exec?shades=" + ble_name,
            json={"hex": create_get_shade_key_request(1).hex()},
            timeout=TIMEOUT,
        )
        shades_exec_resp.raise_for_status()
    except requests.exceptions.RequestException as ex:
        print(f"Unable to send GetShadeKey {ex!s}")
        raise

    result: dict = json.loads(shades_exec_resp.content)
    if result.get("err") != 0 or len(result.get("responses", [])) != 1:
        raise OSError("Error when attempting GetShadeKey")
    response: Final[bytes] = bytes.fromhex(result["responses"][0]["hex"])
    dec_resp: Final[dict[str, Any]] = decode_response(response)
    if dec_resp["errorCode"] != 0:
        raise ValueError("BLE errorCode is not 0")
    if len(dec_resp["data"]) != 16:
        raise ValueError("Expected 16 byte homekey")
    return dec_resp["data"]


def main(hub: str) -> None:
    """Extract the homekeys from all shades."""
    try:
        shades_resp: requests.Response = requests.get(
            hub + "/home/shades", timeout=TIMEOUT
        )
        shades_resp.raise_for_status()
    except requests.exceptions.RequestException as ex:
        print(f"Unable to get list of shades:\n\t{ex!s}")
        return

    shades = json.loads(shades_resp.content)
    print(f"Found {len(shades)} shades, interrogating")
    for shade in shades:
        name: str = base64.b64decode(shade["name"]).decode("utf-8")
        key: bytes = get_shade_key(hub, shade["bleName"])

        print(f"Shade '{name}':")
        print(f"\tBLE name: '{shade['bleName']}'")
        print(f"\tHomeKey: {key.hex()}")


if __name__ == "__main__":
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        description="Extract PowerView homekey from a G3 PowerView Gateway"
    )
    parser.add_argument("hub", nargs="?", help="URL to HUB", default=HUB)
    args = parser.parse_args()
    sys.exit(main(**vars(args)))
