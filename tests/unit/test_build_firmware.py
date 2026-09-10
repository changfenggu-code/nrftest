import os
from pathlib import Path

import pytest

from tools.build_firmware import (
    FirmwareBuildError,
    _build_environment,
    parse_dotconfig,
    patch_cache_root,
    validate_flash_segments,
    validate_required_config,
)
from tools.config import ResolvedValue
from tools.setup_host_tools import load_dtc_pin, managed_dtc_path


def test_patch_cache_follows_zephyr_drive_on_windows() -> None:
    configured = Path("D:/nrftest-cache")
    zephyr_root = Path("E:/nrftest-upstream/zephyrproject/zephyr")

    actual = patch_cache_root(configured, zephyr_root)

    if os.name == "nt":
        assert actual == Path("E:/nrftest-upstream/.nrftest-cache")
    else:
        assert actual == configured


@pytest.mark.skipif(os.name != "nt", reason="managed portable DTC is Windows-specific")
def test_build_uses_managed_windows_dtc_without_explicit_override(tmp_path: Path) -> None:
    settings = {
        "dtc": ResolvedValue(None, "unset"),
        "host_tools_root": ResolvedValue(str(tmp_path / "host-tools"), "test"),
        "downloads_dir": ResolvedValue(str(tmp_path / "downloads"), "test"),
    }
    dtc = managed_dtc_path(load_dtc_pin(), settings)
    dtc.parent.mkdir(parents=True)
    _ = dtc.write_bytes(b"test executable placeholder")

    environment = _build_environment(settings, tmp_path / "zephyr", tmp_path / "sdk")

    assert environment["PATH"].split(os.pathsep)[0] == str(dtc.parent)


def test_generated_config_and_flash_preserve_bootloader_boundaries(tmp_path: Path) -> None:
    config = tmp_path / ".config"
    _ = config.write_text(
        "\n".join(
            (
                "CONFIG_BOARD_HAS_NRF5_BOOTLOADER=y",
                "# CONFIG_BOOTLOADER_MCUBOOT is not set",
                "# CONFIG_USE_DT_CODE_PARTITION is not set",
                "CONFIG_FLASH_LOAD_OFFSET=0x1000",
                "CONFIG_UART_PIPE=y",
                "# CONFIG_UART_CONSOLE is not set",
                "CONFIG_HWINFO=y",
                "# CONFIG_BOOT_BANNER is not set",
                "# CONFIG_TEST_LOGGING_DEFAULTS is not set",
                "# CONFIG_LOG is not set",
                "CONFIG_BT=y",
                "CONFIG_BT_PERIPHERAL=y",
                "CONFIG_BT_GATT_DYNAMIC_DB=y",
                "CONFIG_BT_HCI=y",
                "CONFIG_BT_HCI_HOST=y",
                "CONFIG_BT_LL_SW_SPLIT=y",
                "CONFIG_HAS_BT_CTLR=y",
                "CONFIG_BT_CTLR_HCI=y",
            )
        )
        + "\n",
        encoding="utf-8",
    )

    values = parse_dotconfig(config)
    validate_required_config(values)
    assert validate_flash_segments([(0x1000, 0xDFFFF), (0x10001000, 0x10001010)]) == [
        (0x1000, 0xDFFFF)
    ]


@pytest.mark.parametrize("segments", [[(0x0, 0x1001)], [(0x1000, 0xE0001)], []])
def test_flash_validation_rejects_reserved_or_missing_application(
    segments: list[tuple[int, int]],
) -> None:
    with pytest.raises(FirmwareBuildError):
        _ = validate_flash_segments(segments)
