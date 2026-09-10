import tomllib
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from tools.config import PROJECT_ROOT, ResolvedValue
from tools.setup_platform_prerequisites import (
    LOCK_PATH,
    PackagePin,
    PlatformPrerequisiteError,
    _driver_receipt_matches,  # pyright: ignore[reportPrivateUsage]
    _run_windows_driver_installer,  # pyright: ignore[reportPrivateUsage]
    _write_driver_receipt,  # pyright: ignore[reportPrivateUsage]
    load_platform_pins,
    print_status,
    setup_platform_prerequisites,
)


def test_platform_prerequisite_lock_pins_supported_installers() -> None:
    pins = load_platform_pins(LOCK_PATH)

    assert pins.nrf_device_lib.sha256 == (
        "c914a1572b2b14ca02ecb895ed7415e3dca40df4ef942aeaf2e645c00788fa2a"
    )
    assert pins.nrf_udev.version == "1.0.1"
    assert pins.nrf_udev.sha256 == (
        "e527d5fc187ef829797e8d2f5a4a4a6ab19e04db2aeec0900d63c57dec12e41f"
    )


def test_windows_runtime_is_a_direct_pixi_dependency_not_a_system_installer() -> None:
    with (PROJECT_ROOT / "pixi.toml").open("rb") as stream:
        pixi = cast(dict[str, object], tomllib.load(stream))
    with LOCK_PATH.open("rb") as stream:
        provisioning = cast(dict[str, object], tomllib.load(stream))

    targets = cast(dict[str, object], pixi["target"])
    windows = cast(dict[str, object], targets["win-64"])
    dependencies = cast(dict[str, object], windows["dependencies"])
    provisioning_windows = cast(dict[str, object], provisioning["windows"])

    assert dependencies["vc14_runtime"] == ">=14.51.36247,<15"
    assert "vc_redist_x64" not in provisioning_windows


def test_windows_status_separates_provenance_from_device_function(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    pins = load_platform_pins(LOCK_PATH)
    settings = {
        "downloads_dir": ResolvedValue(str(tmp_path / "downloads"), "test"),
        "host_tools_root": ResolvedValue(str(tmp_path / "tools"), "test"),
    }
    monkeypatch.setattr("tools.setup_platform_prerequisites._platform_name", lambda: "win-64")

    print_status(pins, settings)

    output = capsys.readouterr().out
    assert "VC++ runtime: managed by the Pixi win-64 environment" in output
    assert "managed-install receipt: missing-or-invalid" in output
    assert "Connected-device functionality: verify with host-doctor or firmware-flash" in output
    assert "Redistributable" not in output


def test_driver_receipt_is_atomic_and_bound_to_the_pin(tmp_path: Path) -> None:
    pin = PackagePin("v1", "https://example.test/driver.exe", "driver.exe", "a" * 64)
    marker = tmp_path / "driver-receipt.json"

    _write_driver_receipt(marker, pin)

    assert _driver_receipt_matches(marker, pin)
    assert not _driver_receipt_matches(
        marker,
        PackagePin("v2", pin.url, pin.archive, pin.sha256),
    )
    assert list(tmp_path.glob("*.tmp")) == []


def test_driver_installer_propagates_elevated_process_exit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    observed: list[list[str]] = []

    def fake_run(command: list[str], **kwargs: object) -> SimpleNamespace:
        del kwargs
        observed.append(command)
        return SimpleNamespace(returncode=5)

    monkeypatch.setattr("tools.setup_platform_prerequisites.subprocess.run", fake_run)

    with pytest.raises(PlatformPrerequisiteError, match="exit 5"):
        _run_windows_driver_installer(tmp_path / "driver.exe")

    assert "exit $process.ExitCode" in observed[0][-1]


def test_setup_preflight_rejects_missing_windows_root_before_download(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pins = load_platform_pins(LOCK_PATH)
    settings = {
        "downloads_dir": ResolvedValue("D:/downloads", "test"),
        "host_tools_root": ResolvedValue(None, "unset"),
    }
    downloads: list[Path] = []
    monkeypatch.setattr("tools.setup_platform_prerequisites._platform_name", lambda: "win-64")

    def record_download(pin: PackagePin, path: Path) -> None:
        del pin
        downloads.append(path)

    monkeypatch.setattr("tools.setup_platform_prerequisites._download", record_download)

    with pytest.raises(PlatformPrerequisiteError, match="host_tools_root"):
        setup_platform_prerequisites(pins, settings)

    assert downloads == []
