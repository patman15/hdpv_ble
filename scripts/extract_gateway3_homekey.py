import requests
import json
import base64
import struct

HUB = "http://192.168.0.184"

def create_request(sid, cid, sequenceId, data):
    data = struct.pack("<BBBB", sid, cid, sequenceId, len(data)) + data
    return data

def decode_response(packet):
    if len(packet) < 4:
        raise Exception('Packet size too small')
    sid, cid, sequenceId, length = struct.unpack("<BBBB", packet[0:4])
    if len(packet) != 4 + length:
        raise Exception('Not all data present')
    if length < 1:
        raise Exception('No errorCode present')
    errorCode, = struct.unpack("<B", packet[4:5])
    data = packet[5:]
    return {
            'cid': cid,
            'sid': sid,
            'sequenceId': sequenceId,
            'errorCode': errorCode,
            'data': data
            }

def create_get_shade_key_request(sequenceId):
    return create_request(251, 18, sequenceId, b"")

def get_shade_key(hub, bleName):
    shades_exec_resp = requests.post(hub + "/home/shades/exec?shades=" + bleName, json={"hex":create_get_shade_key_request(1).hex()})
    if shades_exec_resp.status_code != 200:
        raise Exception('Unable to send GetShadeKey')
    result = json.loads(shades_exec_resp.content)
    if not 'err' in result or result['err'] != 0 or not 'responses' in result or len(result['responses']) != 1:
        raise Exception('Error when attempting GetShadeKey')
    result = result['responses'][0]
    result = bytes.fromhex(result['hex'])
    result = decode_response(result)
    if result['errorCode'] != 0:
        raise Exception('BLE errorCode is not 0')
    if len(result['data']) != 16:
        raise Exception('Expected 16 byte homekey')
    return result['data']


def main(hub):
    shades_resp = requests.get(hub + "/home/shades")
    if shades_resp.status_code != 200:
        raise Exception('Unable to get list of shades')
    shades = json.loads(shades_resp.content)
    print(f"Found {len(shades)} shades, interrogating")
    for shade in shades:
        name = base64.b64decode(shade['name']).decode('utf-8')
        print(f"Shade '{name}':")
        print(f"\tBLE name: '{shade['bleName']}'")

        key = get_shade_key(hub, shade['bleName'])
        print(f"\tHomeKey: {key.hex()}")


if __name__ == '__main__':
    import argparse
    import sys
    parser = argparse.ArgumentParser(description="Extract PowerView homekey from a G3 PowerView Gateway")
    parser.add_argument("hub", help="URL to HUB", default="http://powerview-g3.local")
    args = parser.parse_args()
    sys.exit(main(**vars(args)))
