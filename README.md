# HD PowerView Support via BLE for Home Assistant

[![GitHub Release][releases-shield]][releases]
[![License][license-shield]](LICENSE)

 A Home Assistant integration to support Hunter Douglas Powerview devices via Bluetooth

> [!WARNING]
> - This integration is under development!
> - Test coverage is low, malfunction might occur. 
> - The HOME_KEY is lost over updates!

## Features
- Zero configuration
- Supports [ESPHome Bluetooth proxy](https://esphome.io/components/bluetooth_proxy)

### Supported Devices

Type* | Description
-- | --
1  | Designer Roller
4  | Roman
5  | Bottom Up
6  | Duette
10 | Duette and Applause SkyLift
19 | Provenance Woven Wood
31, 32, 84 | Vignette
39 | Parkland
42 | M25T Roller Blind
49 | AC Roller
52 | Banded Shades
53 | Sonnette

\*) Type can be found in the PowerView app under *product info*, *type ID*

### Provided Information
The integration provides the following information about the battery

Platform | Description | Unit | Details
-- | -- | -- | --
`binary_sensor` | battery charging indicator | `bool` | true if battery is charging
`button` | identify shade | - | identify shade by LED and 3 beeps
`cover` | view/control position | `%` | percentage cover is open (100% is open)
`sensor` | SoC (state of charge) | `%` | range 100% (full), 50%, 20%, 0% (battery empty)

## Installation
> [!IMPORTANT]
> In case you added your shades to the app or a gateway, you need to [set the encryption key](#set-the-encryption-key) manually in the [`const.py`](https://github.com/patman15/hdpv_ble/blob/main/custom_components/hunterdouglas_powerview_ble/const.py) file after **each** update!

### Automatic
Installation can be done using [HACS](https://hacs.xyz/) by [adding a custom repository](https://hacs.xyz/docs/faq/custom_repositories/).

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=patman15&repository=hdpv_ble&category=Integration)

### Manual
1. Using the tool of choice open the directory (folder) for your HA configuration (where you find `configuration.yaml`).
1. If you do not have a `custom_components` directory (folder) there, you need to create it.
1. In the `custom_components` directory (folder) create a new folder called `hunterdouglas_powerview_ble`.
1. Download _all_ the files from the `custom_components/hunterdouglas_powerview_ble/` directory (folder) in this repository.
1. Place the files you downloaded in the new directory (folder) you created.
1. Restart Home Assistant
1. In the HA UI go to "Configuration" -> "Integrations" click "+" and search for "Hunter Douglas PowerView (BLE)"

## Set the Encryption Key
Currently, there are three methods to obtain the key:

1. Via adopting a BLE shade: There is a [shade emulator](/emu/PV_BLE_cover) that works with Arduino IDE and an ESP32 device (&ge; 2MiB flash, &ge; 128KiB required), e.g. [Adafruit QT Py ESP32-S3](https://www.adafruit.com/product/5426). Install and connect via serial port, then go to the PowerView app and add the shade `myPVcover` to your home. You will see a log message `set shade key: \xx\xx\xx\xx\xx\xx\xx\xx\xx\xx\xx\xx\xx\xx\xx\xx` . Copy this key. You can delete the shade from the app when done.
2. Extracting from gateway: This [script](scripts/extract_gateway3_homekey.py) is able to extract the key from a working PowerView gateway.
3. Grabbing from the app: Checkout this [post in the Home Assistant community forum](https://community.home-assistant.io/t/hunter-douglas-powerview-gen-3-integration/424836/228).

Finally, you need to manually copy the key to [`const.py`](https://github.com/patman15/hdpv_ble/blob/main/custom_components/hunterdouglas_powerview_ble/const.py).

> [!IMPORTANT]
> You need to update the file after **each** update!

## Known Issues
<details><summary>Shade inoperable after charging</summary>
It seems that the shades require some re-initialization after charging. The solution is currently unknown, but as a workaround you can operate the shade ones using the vendor app.
</details>

## Troubleshooting
In case you have severe troubles,

- please enable the debug protocol for the integration,
- reproduce the issue,
- disable the log (Home Assistant will prompt you to download the log), and finally
- [open an issue](https://github.com/patman15/hdpv_ble/issues/new?assignees=&labels=Bug&projects=&template=bug.yml) with a good description of what happened and attach the log.

# Thanks To
[@mannkind](https://github.com/mannkind)

[license-shield]: https://img.shields.io/github/license/patman15/hdpv_ble.svg?style=for-the-badge
[releases-shield]: https://img.shields.io/github/release/patman15/hdpv_ble.svg?style=for-the-badge
[releases]: https://github.com//patman15/hdpv_ble/releases

## Outlook
- Add tests!
- Allow parallel usage to PowerView app as "remote"
- Add support for tilt function
- Add support for further device types
