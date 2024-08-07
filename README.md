# HD PowerView Support via BLE for Home Assistant

[![GitHub Release][releases-shield]][releases]
[![License][license-shield]](LICENSE)

 A Home Assistant integration to support Hunter Douglas Powerview devices via Bluetooth

## :warning: Limitations
- This integration is under development!
- Test coverage is low, malfunction might occur. 
- Only devices that are **not** added to the app are controlable. It is possible to add them to the app if you just want to monitor the status (position, battery) in Home Assistant.
- Currently only position change is supported (e.g., no tilt)

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
10 | Duette and Applause SkyLift",
19 | Provenance Woven Wood
31, 32, 84 | Vignette
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
`cover` | view/control position | `%` | percentage cover is open (100% is open)
`sensor` | SoC (state of charge) | `%` | range 100% (full), 50%, 20%, 0% (battery empty)

## Installation
### Automatic
Installation can be done using [HACS](https://hacs.xyz/) by [adding a custom repository](https://hacs.xyz/docs/faq/custom_repositories/).

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=patman15&repository=hdpv_ble&category=Integration)

### Manual
1. Using the tool of choice open the directory (folder) for your HA configuration (where you find `configuration.yaml`).
1. If you do not have a `custom_components` directory (folder) there, you need to create it.
1. In the `custom_components` directory (folder) create a new folder called `bms_ble`.
1. Download _all_ the files from the `custom_components/bms_ble/` directory (folder) in this repository.
1. Place the files you downloaded in the new directory (folder) you created.
1. Restart Home Assistant
1. In the HA UI go to "Configuration" -> "Integrations" click "+" and search for "BLE Battery Management"


## Outlook
- Add support for encryption
- Allow parallel usage to PowerView app as "remote"
- Add support for tilt function
- Add support for further device types

## Troubleshooting
In case you have severe troubles,

- please enable the debug protocol for the integration,
- reproduce the issue,
- disable the log (Home Assistant will prompt you to download the log), and finally
- [open an issue]([https://github.com/patman15/BMS_BLE-HA/issues](https://github.com/patman15/hdpv_ble/issues/new?assignees=&labels=Bug&projects=&template=bug.yml)) with a good description of what happened and attach the log.

[license-shield]: https://img.shields.io/github/license/patman15/hdpv_ble.svg?style=for-the-badge
[releases-shield]: https://img.shields.io/github/release/patman15/hdpv_ble.svg?style=for-the-badge
[releases]: https://github.com//patman15/hdpv_ble/releases