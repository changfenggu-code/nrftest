from pathlib import Path

from tools.config import (
    ConfigError,
    require_local_config,
    resolve_settings,
)


def test_configuration_precedence_and_relative_paths(tmp_path: Path) -> None:
    local_document = {
        "toolchains": {
            "zephyr_root": "local/zephyr",
            "autopts_root": "local/auto-pts",
        },
        "tools": {"jlink_library": "local/JLinkARM.dll"},
        "device": {
            "serial": "local-application-serial",
            "bootloader_serial": "local-bootloader-serial",
            "debugger_serial": "123456789",
        },
    }
    environ = {
        "NRFTEST_ZEPHYR_ROOT": "environment/zephyr",
        "NRFTEST_DEVICE_SERIAL": "environment-application-serial",
        "NRFTEST_BOOTLOADER_SERIAL": "environment-bootloader-serial",
    }
    settings = resolve_settings(
        {"zephyr_root": "explicit/zephyr", "device_serial": None},
        environ=environ,
        local_document=local_document,
        project_root=tmp_path,
    )

    assert settings["zephyr_root"].value == str((tmp_path / "explicit/zephyr").resolve())
    assert settings["zephyr_root"].source == "cli"
    assert settings["autopts_root"].value == str((tmp_path / "local/auto-pts").resolve())
    assert settings["autopts_root"].source == "local:toolchains.autopts_root"
    assert settings["device_serial"].value == "environment-application-serial"
    assert settings["device_serial"].source == "env:NRFTEST_DEVICE_SERIAL"
    assert settings["bootloader_serial"].value == "environment-bootloader-serial"
    assert settings["bootloader_serial"].source == "env:NRFTEST_BOOTLOADER_SERIAL"
    assert settings["jlink_library"].value == str((tmp_path / "local/JLinkARM.dll").resolve())
    assert settings["debugger_serial"].value == "123456789"
    assert settings["build_dir"].value == str((tmp_path / ".work/build").resolve())
    assert settings["build_dir"].source == "default"
    assert "blehub_root" not in settings

    cli_override = resolve_settings(
        {"bootloader_serial": "cli-bootloader-serial"},
        environ=environ,
        local_document=local_document,
        project_root=tmp_path,
    )
    assert cli_override["bootloader_serial"].value == "cli-bootloader-serial"
    assert cli_override["bootloader_serial"].source == "cli"
    assert cli_override["device_serial"].value == "environment-application-serial"


def test_install_paths_remain_unset_without_machine_configuration(tmp_path: Path) -> None:
    settings = resolve_settings(
        {},
        environ={},
        local_document={},
        project_root=tmp_path,
    )

    for name in (
        "zephyr_root",
        "zephyr_sdk_root",
        "autopts_root",
        "host_tools_root",
        "jlink_library",
        "device_serial",
        "bootloader_serial",
        "debugger_serial",
    ):
        assert settings[name].value is None
        assert settings[name].source == "unset"
    assert settings["downloads_dir"].value == str((tmp_path / ".work/downloads").resolve())
    assert settings["downloads_dir"].source == "default"


def test_explicit_missing_local_config_is_rejected(tmp_path: Path) -> None:
    missing = tmp_path / "missing.toml"

    try:
        require_local_config(str(missing), {})
    except ConfigError as error:
        assert "local config does not exist" in str(error)
    else:
        raise AssertionError("an explicitly selected missing config must be rejected")
