import hashlib
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

import tools.flash_firmware as flash_module
from tools.build_firmware import BOARD, FirmwareBuildError
from tools.config import ConfigError, ResolvedValue
from tools.flash_firmware import (
    PCA10059_BOOTLOADER_PID,
    PCA10059_BOOTLOADER_VID,
    FirmwareFlashError,
    PackageIdentity,
    flash_firmware,
    load_package_identity,
    revalidate_explicit_port,
    select_explicit_port,
)
from tools.package_firmware import FirmwarePackageError, PackageConfig

BOOTLOADER_SERIAL = "BOOTLOADER-123"
PACKAGE_CONFIG = PackageConfig(
    format="nrf5-sdk-secure-dfu",
    hardware_version=52,
    softdevice_requirement="0x00",
    application_version=1,
    signed=False,
)


@dataclass
class FakePort:
    device: str
    description: str = "test"
    hwid: str = "test"
    vid: int | None = PCA10059_BOOTLOADER_VID
    pid: int | None = PCA10059_BOOTLOADER_PID
    serial_number: str | None = BOOTLOADER_SERIAL


def _valid_manifest(package: Path, digest: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "board": BOARD,
        "dfu_package": {
            "path": package.name,
            "sha256": digest,
            "format": PACKAGE_CONFIG.format,
            "signed": False,
            "parameters": {
                "hardware_version": PACKAGE_CONFIG.hardware_version,
                "softdevice_requirement": PACKAGE_CONFIG.softdevice_requirement,
                "application_version": PACKAGE_CONFIG.application_version,
            },
        },
    }


def _write_manifest(build_root: Path, manifest: dict[str, object]) -> None:
    _ = (build_root / "nrftest-build-manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )


def _set_manifest_value(manifest: dict[str, object], path: tuple[str, ...], value: object) -> None:
    current = manifest
    for name in path[:-1]:
        child = current[name]
        assert isinstance(child, dict)
        current = child
    current[path[-1]] = value


def test_flash_requires_valid_manifest_hash_and_bootloader_identity(tmp_path: Path) -> None:
    package = tmp_path / "tester.zip"
    _ = package.write_bytes(b"package")
    digest = hashlib.sha256(package.read_bytes()).hexdigest()
    _write_manifest(tmp_path, _valid_manifest(package, digest))

    identity = load_package_identity(tmp_path, config=PACKAGE_CONFIG)
    assert identity.path == package
    assert identity.sha256 == digest

    selected = select_explicit_port(
        "com7",
        BOOTLOADER_SERIAL,
        [FakePort("COM7"), FakePort("COM8", serial_number="OTHER")],
    )
    assert selected.device == "COM7"
    assert selected.vid == 0x1915
    assert selected.pid == 0x521F
    assert selected.serial_number == BOOTLOADER_SERIAL


def test_flash_rejects_missing_or_ambiguous_explicit_port() -> None:
    with pytest.raises(FirmwareFlashError, match="not uniquely present"):
        _ = select_explicit_port("COM7", BOOTLOADER_SERIAL, [FakePort("COM8")])
    with pytest.raises(FirmwareFlashError, match="not uniquely present"):
        _ = select_explicit_port(
            "COM7",
            BOOTLOADER_SERIAL,
            [FakePort("COM7"), FakePort("com7")],
        )


@pytest.mark.parametrize(
    ("port", "expected_serial", "message"),
    [
        (FakePort("COM7", vid=0x1234), BOOTLOADER_SERIAL, "1915:521F"),
        (FakePort("COM7", pid=0x1234), BOOTLOADER_SERIAL, "1915:521F"),
        (FakePort("COM7", serial_number=None), BOOTLOADER_SERIAL, "no USB serial"),
        (FakePort("COM7", serial_number="  "), BOOTLOADER_SERIAL, "no USB serial"),
        (FakePort("COM7", serial_number="other"), BOOTLOADER_SERIAL, "serial mismatch"),
        (FakePort("COM7"), "", "explicitly configured"),
    ],
)
def test_flash_rejects_untrusted_bootloader_identity(
    port: FakePort,
    expected_serial: str,
    message: str,
) -> None:
    with pytest.raises(FirmwareFlashError, match=message):
        _ = select_explicit_port("COM7", expected_serial, [port])


def test_revalidation_detects_bootloader_identity_change() -> None:
    initial = FakePort("COM7", description="initial")
    verified = FakePort("COM7", description="verified")

    assert revalidate_explicit_port(initial, "COM7", BOOTLOADER_SERIAL, [verified]) is verified
    with pytest.raises(FirmwareFlashError, match="identity changed"):
        _ = revalidate_explicit_port(
            initial,
            "COM7",
            BOOTLOADER_SERIAL,
            [FakePort("COM7", serial_number="replacement")],
        )


@pytest.mark.parametrize(
    ("path", "value", "message"),
    [
        (("schema_version",), True, "schema_version"),
        (("schema_version",), 2, "schema_version"),
        (("board",), "other/board", "board"),
        (("dfu_package", "format"), "other-format", "format"),
        (("dfu_package", "signed"), True, "signed=false"),
        (
            ("dfu_package", "parameters", "hardware_version"),
            53,
            "hardware_version",
        ),
        (
            ("dfu_package", "parameters", "softdevice_requirement"),
            "0x01",
            "softdevice_requirement",
        ),
        (
            ("dfu_package", "parameters", "application_version"),
            2,
            "application_version",
        ),
    ],
)
def test_manifest_must_match_build_and_package_configuration(
    tmp_path: Path,
    path: tuple[str, ...],
    value: object,
    message: str,
) -> None:
    package = tmp_path / "tester.zip"
    _ = package.write_bytes(b"package")
    digest = hashlib.sha256(package.read_bytes()).hexdigest()
    manifest = _valid_manifest(package, digest)
    _set_manifest_value(manifest, path, value)
    _write_manifest(tmp_path, manifest)

    with pytest.raises(FirmwareFlashError, match=message):
        _ = load_package_identity(tmp_path, config=PACKAGE_CONFIG)


@pytest.mark.parametrize(
    "path_value",
    ["../outside.zip", "/outside.zip", "C:/outside.zip", "nested\\outside.zip"],
)
def test_manifest_rejects_unsafe_package_paths(tmp_path: Path, path_value: str) -> None:
    package = tmp_path / "tester.zip"
    _ = package.write_bytes(b"package")
    digest = hashlib.sha256(package.read_bytes()).hexdigest()
    manifest = _valid_manifest(package, digest)
    _set_manifest_value(manifest, ("dfu_package", "path"), path_value)
    _write_manifest(tmp_path, manifest)

    with pytest.raises(FirmwareFlashError, match="safe relative"):
        _ = load_package_identity(tmp_path, config=PACKAGE_CONFIG)


@pytest.mark.parametrize("digest", ["A" * 64, "g" * 64, "0" * 63])
def test_manifest_requires_canonical_lowercase_sha256(tmp_path: Path, digest: str) -> None:
    package = tmp_path / "tester.zip"
    _ = package.write_bytes(b"package")
    _write_manifest(tmp_path, _valid_manifest(package, digest))

    with pytest.raises(FirmwareFlashError, match="canonical lowercase"):
        _ = load_package_identity(tmp_path, config=PACKAGE_CONFIG)


def test_manifest_hash_must_match_package_bytes(tmp_path: Path) -> None:
    package = tmp_path / "tester.zip"
    _ = package.write_bytes(b"package")
    _write_manifest(tmp_path, _valid_manifest(package, "0" * 64))

    with pytest.raises(FirmwareFlashError, match="changed after validation"):
        _ = load_package_identity(tmp_path, config=PACKAGE_CONFIG)


def _flash_settings(tmp_path: Path) -> dict[str, ResolvedValue]:
    return {
        "bootloader_serial": ResolvedValue(BOOTLOADER_SERIAL, "test"),
        "programmer": ResolvedValue(str(tmp_path / "nrfutil.exe"), "test"),
        "build_dir": ResolvedValue(str(tmp_path / "build"), "test"),
        "reports_dir": ResolvedValue(str(tmp_path / "reports"), "test"),
    }


def _mock_flash_inputs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    port_batches: list[list[FakePort]],
) -> PackageIdentity:
    programmer = tmp_path / "nrfutil.exe"
    package_path = tmp_path / "tester.zip"
    _ = package_path.write_bytes(b"package")
    package = PackageIdentity(package_path, hashlib.sha256(b"package").hexdigest())
    pins = SimpleNamespace(command=SimpleNamespace(name="nrf5sdk-tools"))
    selected = object()

    monkeypatch.setattr(flash_module, "load_firmware_tool_pins", lambda: pins)
    monkeypatch.setattr(flash_module, "current_platform_pin", lambda _pins: selected)
    monkeypatch.setattr(flash_module, "verify_firmware_tools", lambda *_args: None)
    monkeypatch.setattr(
        flash_module,
        "managed_firmware_tool_paths",
        lambda *_args: (tmp_path, tmp_path, tmp_path / "nrfutil-home", programmer),
    )
    monkeypatch.setattr(flash_module, "load_package_config", lambda: PACKAGE_CONFIG)
    monkeypatch.setattr(
        flash_module,
        "load_package_identity",
        lambda *_args, **_kwargs: package,
    )

    def comports() -> list[FakePort]:
        assert port_batches
        return port_batches.pop(0)

    monkeypatch.setattr(flash_module.list_ports, "comports", comports)
    return package


def test_flash_reenumerates_and_refuses_identity_change_before_nrfutil(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    batches = [
        [FakePort("COM7")],
        [FakePort("COM7", serial_number="replacement")],
    ]
    package = _mock_flash_inputs(monkeypatch, tmp_path, batches)

    def unexpected_run(*_args: object, **_kwargs: object) -> None:
        pytest.fail("nRF Util must not start after bootloader identity changes")

    monkeypatch.setattr(flash_module.subprocess, "run", unexpected_run)

    with pytest.raises(FirmwareFlashError, match="identity changed"):
        flash_firmware(
            _flash_settings(tmp_path),
            port_name="COM7",
            confirmed_sha256=package.sha256,
        )
    assert batches == []


def test_flash_report_uses_revalidated_bootloader_identity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    batches = [
        [FakePort("COM7", description="initial")],
        [FakePort("COM7", description="revalidated", hwid="USB VID:PID=1915:521F")],
    ]
    package = _mock_flash_inputs(monkeypatch, tmp_path, batches)
    commands: list[list[str]] = []

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        assert kwargs["check"] is True
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(flash_module.subprocess, "run", run)

    flash_firmware(
        _flash_settings(tmp_path),
        port_name="COM7",
        confirmed_sha256=package.sha256,
    )

    assert len(commands) == 1
    reports = list((tmp_path / "reports" / "firmware-flash").glob("*.json"))
    assert len(reports) == 1
    report = json.loads(reports[0].read_text(encoding="utf-8"))
    assert report["port"] == {
        "description": "revalidated",
        "device": "COM7",
        "hwid": "USB VID:PID=1915:521F",
        "pid": "0x521f",
        "serial_number": BOOTLOADER_SERIAL,
        "vid": "0x1915",
    }


def test_flash_requires_explicit_bootloader_serial_before_tool_or_port_access(
    tmp_path: Path,
) -> None:
    with pytest.raises(FirmwareFlashError, match="bootloader_serial is not configured"):
        flash_firmware(
            {
                "programmer": ResolvedValue(str(tmp_path / "nrfutil.exe"), "test"),
                "build_dir": ResolvedValue(str(tmp_path / "build"), "test"),
                "reports_dir": ResolvedValue(str(tmp_path / "reports"), "test"),
            },
            port_name="COM7",
            confirmed_sha256="0" * 64,
        )


def test_cli_bootloader_serial_overrides_resolved_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def resolve(
        explicit: dict[str, str | None], *, config_path: str | None
    ) -> dict[str, ResolvedValue]:
        captured["explicit"] = explicit
        captured["config_path"] = config_path
        return {}

    def flash(
        settings: dict[str, ResolvedValue],
        *,
        port_name: str,
        confirmed_sha256: str,
    ) -> None:
        captured["settings"] = settings
        captured["port"] = port_name
        captured["sha256"] = confirmed_sha256

    monkeypatch.setattr(flash_module, "resolve_current_settings", resolve)
    monkeypatch.setattr(flash_module, "flash_firmware", flash)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "flash_firmware.py",
            "--config",
            "machine.toml",
            "--port",
            "COM7",
            "--bootloader-serial",
            "CLI-BOOTLOADER",
            "--confirm-sha256",
            "0" * 64,
        ],
    )

    assert flash_module.main() == 0
    assert captured["explicit"] == {
        "bootloader_serial": "CLI-BOOTLOADER",
        "build_dir": None,
    }
    assert captured["config_path"] == "machine.toml"


@pytest.mark.parametrize(
    "error",
    [
        ConfigError("bad configuration"),
        FirmwarePackageError("bad package"),
        FirmwareBuildError("bad build evidence"),
    ],
    ids=["config", "package", "build"],
)
def test_main_reports_expected_failures(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    error: Exception,
) -> None:
    monkeypatch.setattr(flash_module, "resolve_current_settings", lambda *_args, **_kwargs: {})

    def fail(*_args: object, **_kwargs: object) -> None:
        raise error

    monkeypatch.setattr(flash_module, "flash_firmware", fail)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "flash_firmware.py",
            "--port",
            "COM7",
            "--confirm-sha256",
            "0" * 64,
        ],
    )

    assert flash_module.main() == 2
    assert str(error) in capsys.readouterr().err
