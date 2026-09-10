import hashlib
import subprocess
import zipfile
from pathlib import Path

import pytest

from tools.package_firmware import (
    NORMALIZED_ZIP_TIMESTAMP,
    FirmwarePackageError,
    artifact_sha,
    load_package_config,
    normalize_package_zip,
    package_firmware,
)


def test_package_config_and_build_artifact_identity(tmp_path: Path) -> None:
    config_path = tmp_path / "package.toml"
    _ = config_path.write_text(
        "\n".join(
            (
                "schema_version = 1",
                'format = "nrf5-sdk-secure-dfu"',
                "hardware_version = 52",
                'softdevice_requirement = "0x00"',
                "application_version = 1",
                "signed = false",
            )
        )
        + "\n",
        encoding="utf-8",
    )

    config = load_package_config(config_path)
    assert config.hardware_version == 52
    assert config.softdevice_requirement == "0x00"
    assert config.application_version == 1
    assert config.signed is False
    assert (
        artifact_sha(
            {"artifacts": [{"path": "zephyr/zephyr.hex", "sha256": "abc"}]},
            "zephyr/zephyr.hex",
        )
        == "abc"
    )


def test_zip_normalization_removes_generation_time(tmp_path: Path) -> None:
    packages = [tmp_path / "first.zip", tmp_path / "second.zip"]
    timestamps = [(2026, 9, 6, 12, 0, 0), (2026, 9, 6, 12, 1, 0)]
    for path, timestamp in zip(packages, timestamps, strict=True):
        with zipfile.ZipFile(path, "w") as package:
            package.writestr(zipfile.ZipInfo("manifest.json", timestamp), b"{}")
            package.writestr(zipfile.ZipInfo("firmware.bin", timestamp), b"firmware")
        normalize_package_zip(path)

    assert (
        hashlib.sha256(packages[0].read_bytes()).digest()
        == hashlib.sha256(packages[1].read_bytes()).digest()
    )
    with zipfile.ZipFile(packages[0]) as package:
        assert all(info.date_time == NORMALIZED_ZIP_TIMESTAMP for info in package.infolist())


def test_package_orchestration_separates_generate_and_normalize_processes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def run_stage(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        assert kwargs["check"] is False
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr("tools.package_firmware.subprocess.run", run_stage)

    package_firmware("machine.toml")

    assert [command[4] for command in calls] == ["generate", "normalize-validate"]
    assert all(command[-2:] == ["--config", "machine.toml"] for command in calls)


def test_package_config_rejects_signed_phase_zero_package(tmp_path: Path) -> None:
    config_path = tmp_path / "package.toml"
    _ = config_path.write_text(
        "\n".join(
            (
                "schema_version = 1",
                'format = "nrf5-sdk-secure-dfu"',
                "hardware_version = 52",
                'softdevice_requirement = "0x00"',
                "application_version = 1",
                "signed = true",
            )
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(FirmwarePackageError):
        _ = load_package_config(config_path)
