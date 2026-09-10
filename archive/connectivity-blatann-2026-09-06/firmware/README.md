# nRF52840 Connectivity Firmware

This directory contains the firmware assets for the PC-controlled BLE test
Peripheral used to exercise BleHub Central. The target is the Nordic nRF52840
Dongle, PCA10059, connected through USB CDC and controlled by
`pc-ble-driver-py`/Blatann.

## Pinned compatibility set

| Component | Version |
| --- | --- |
| Blatann | 0.6.0 |
| pc-ble-driver-py | 0.17.0 |
| pc-ble-driver native ABI | 4.1.4 |
| Connectivity image | 4.1.4 |
| SoftDevice/API | S132, SoftDevice serialization API v5.1.0 |
| Hardware | nRF52840 Dongle, PCA10059 |

## Verified assets

- `verified/connectivity_4.1.4_usb_with_s132_5.1.0.hex`
- `verified/connectivity_4.1.4_usb_with_s132_5.1.0_dfu_pkg.zip`
- `vendor/pc_ble_driver_py-0.17.0-cp310-cp310-win_amd64.whl`

The DFU ZIP manifest contains an application image (`nrf52840_xxaa.bin`) and
S132 SoftDevice 5.1.0 (`s132_nrf52_5.1.0_softdevice.bin`). It does not contain
the Dongle bootloader, so the existing bootloader is not part of this update.

The SHA-256 values are recorded in `SHA256SUMS.txt`.

## Provenance

The wheel was downloaded from the official PyPI release metadata for
`pc-ble-driver-py==0.17.0`:

<https://files.pythonhosted.org/packages/e5/e5/4403a43308eeb72e486952843af2654ee41dc88061b7e775447c923a6429/pc_ble_driver_py-0.17.0-cp310-cp310-win_amd64.whl>

The upstream sources are archived by their owners:

- <https://github.com/NordicSemiconductor/pc-ble-driver-py>
- <https://github.com/NordicSemiconductor/pc-ble-driver/releases/tag/v4.1.4>
- <https://github.com/ThomasGerstenberg/blatann>

## Current device state

The connected device was identified as:

- Serial: `F3CDDDE125C5`
- Board: `PCA10059`
- Port: `COM11`
- Traits: `devkit,nordicDfu,nordicUsb,serialPorts,usb`
- MCU state: `Application`
- Product: `nRF52 Connectivity`

The Nordic device driver is installed and `nrfutil device list` reports the
Connectivity application. The USB CDC path has also been validated by the
Blatann scanner. The next unverified boundary is Peripheral advertising and
BleHub Central RF interaction.

## Programming caution

For this device and current nRF Util version, use the DFU ZIP rather than the
HEX. `nrfutil device program` treats a HEX as a J-Link/MCUboot input depending
on device traits, while the DFU ZIP is parsed as Nordic secure DFU. Before any
write operation, verify the selected serial number and close programs that may
hold `COM11`.

The following command only parses the package and generates an operation; it
does not program the device:

```text
nrfutil device program --firmware "firmware/verified/connectivity_4.1.4_usb_with_s132_5.1.0_dfu_pkg.zip" --serial-number F3CDDDE125C5 --generate
```

Actual programming remains a separate, explicit step.
