from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Protocol, cast

from serial.tools import list_ports

from host.nrftest.telemetry import TelemetryError, write_json_report
from tools.build_firmware import BOARD, OUTPUT_DIRECTORY_NAME, FirmwareBuildError, sha256_file
from tools.config import ConfigError, ResolvedValue, resolve_current_settings
from tools.package_firmware import (
    BUILD_MANIFEST_NAME,
    FirmwarePackageError,
    PackageConfig,
    load_package_config,
)
from tools.setup_firmware_tools import (
    FirmwareToolError,
    current_platform_pin,
    load_firmware_tool_pins,
    managed_firmware_tool_paths,
    verify_firmware_tools,
)

PCA10059_BOOTLOADER_VID = 0x1915
PCA10059_BOOTLOADER_PID = 0x521F
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")


class FirmwareFlashError(RuntimeError):
    """Raised when an explicitly selected firmware flash cannot proceed safely."""


class PortInfo(Protocol):
    device: str
    description: str
    hwid: str
    vid: int | None
    pid: int | None
    serial_number: str | None


@dataclass(frozen=True)
class PackageIdentity:
    path: Path
    sha256: str


def _required_path(settings: Mapping[str, ResolvedValue], name: str) -> Path:
    value = settings[name].value
    if value is None:
        raise FirmwareFlashError(
            f"{name} is not configured; set it in nrftest.local.toml or its NRFTEST_* variable"
        )
    return Path(value)


def _required_text(settings: Mapping[str, ResolvedValue], name: str) -> str:
    resolved = settings.get(name)
    value = resolved.value if resolved is not None else None
    if value is None or not value.strip():
        sources = "device.bootloader_serial, NRFTEST_BOOTLOADER_SERIAL, or --bootloader-serial"
        raise FirmwareFlashError(f"{name} is not configured; set {sources}")
    return value


def _safe_package_path(build_root: Path, value: object) -> Path:
    if not isinstance(value, str) or not value:
        raise FirmwareFlashError("DFU package path in build manifest must be a relative path")
    relative = PurePosixPath(value)
    windows_path = PureWindowsPath(value)
    if (
        "\\" in value
        or relative.is_absolute()
        or windows_path.is_absolute()
        or bool(windows_path.drive)
        or relative == PurePosixPath(".")
        or ".." in relative.parts
    ):
        raise FirmwareFlashError(
            "DFU package path in build manifest must be a safe relative POSIX path"
        )
    resolved_root = build_root.resolve()
    path = (resolved_root / Path(*relative.parts)).resolve()
    if not path.is_relative_to(resolved_root):
        raise FirmwareFlashError("DFU package path resolves outside the firmware build root")
    return path


def _matches_parameter(value: object, expected: str | int) -> bool:
    if isinstance(expected, int):
        return isinstance(value, int) and not isinstance(value, bool) and value == expected
    return isinstance(value, str) and value == expected


def load_package_identity(
    build_root: Path,
    *,
    config: PackageConfig | None = None,
) -> PackageIdentity:
    package_config = load_package_config() if config is None else config
    manifest_path = build_root / BUILD_MANIFEST_NAME
    try:
        document = cast(object, json.loads(manifest_path.read_text(encoding="utf-8")))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise FirmwareFlashError(
            f"unable to read build manifest {manifest_path}: {error}"
        ) from error
    if not isinstance(document, dict):
        raise FirmwareFlashError("build manifest root must be an object")
    manifest = cast(dict[str, object], document)
    schema_version = manifest.get("schema_version")
    if (
        not isinstance(schema_version, int)
        or isinstance(schema_version, bool)
        or schema_version != 1
    ):
        raise FirmwareFlashError("build manifest schema_version must be 1")
    if manifest.get("board") != BOARD:
        raise FirmwareFlashError(f"build manifest board must be {BOARD}")

    package_value = manifest.get("dfu_package")
    if not isinstance(package_value, dict):
        raise FirmwareFlashError("build manifest does not contain a validated DFU package")
    package = cast(dict[str, object], package_value)
    if package.get("format") != package_config.format:
        raise FirmwareFlashError("DFU package format does not match firmware/package.toml")
    if package.get("signed") is not False or package.get("signed") is not package_config.signed:
        raise FirmwareFlashError("DFU package must match signed=false in firmware/package.toml")

    parameters_value = package.get("parameters")
    if not isinstance(parameters_value, dict):
        raise FirmwareFlashError("DFU package parameters in build manifest must be an object")
    parameters = cast(dict[str, object], parameters_value)
    expected_parameters: dict[str, str | int] = {
        "hardware_version": package_config.hardware_version,
        "softdevice_requirement": package_config.softdevice_requirement,
        "application_version": package_config.application_version,
    }
    mismatches = [
        name
        for name, expected in expected_parameters.items()
        if not _matches_parameter(parameters.get(name), expected)
    ]
    if mismatches:
        raise FirmwareFlashError(
            "DFU package parameters do not match firmware/package.toml: " + ", ".join(mismatches)
        )

    path = _safe_package_path(build_root, package.get("path"))
    sha256 = package.get("sha256")
    if not isinstance(sha256, str) or SHA256_PATTERN.fullmatch(sha256) is None:
        raise FirmwareFlashError(
            "DFU package sha256 in build manifest must be 64 canonical lowercase hex characters"
        )
    if not path.is_file():
        raise FirmwareFlashError(f"validated DFU package is missing: {path}")
    actual = sha256_file(path)
    if actual != sha256:
        raise FirmwareFlashError(
            f"DFU package changed after validation: expected {sha256}, found {actual}"
        )
    return PackageIdentity(path=path, sha256=sha256)


def _select_unique_port(port_name: str, ports: Sequence[PortInfo]) -> PortInfo:
    matches = [port for port in ports if port.device.casefold() == port_name.casefold()]
    if len(matches) != 1:
        available = ", ".join(port.device for port in ports) or "<none>"
        raise FirmwareFlashError(
            f"explicit port {port_name!r} is not uniquely present; available ports: {available}"
        )
    return matches[0]


def _validate_bootloader_identity(port: PortInfo, expected_bootloader_serial: str) -> PortInfo:
    if not expected_bootloader_serial.strip():
        raise FirmwareFlashError("expected bootloader serial must be explicitly configured")
    if port.vid != PCA10059_BOOTLOADER_VID or port.pid != PCA10059_BOOTLOADER_PID:
        observed = (
            f"{port.vid:04X}:{port.pid:04X}"
            if port.vid is not None and port.pid is not None
            else "<missing>"
        )
        raise FirmwareFlashError(
            f"explicit port is not the PCA10059 bootloader: expected 1915:521F, found {observed}"
        )
    serial = port.serial_number
    if not isinstance(serial, str) or not serial.strip():
        raise FirmwareFlashError("explicit PCA10059 bootloader port has no USB serial identity")
    if serial != expected_bootloader_serial:
        mismatch = f"expected {expected_bootloader_serial!r}, found {serial!r}"
        raise FirmwareFlashError(f"PCA10059 bootloader serial mismatch: {mismatch}")
    return port


def select_explicit_port(
    port_name: str,
    expected_bootloader_serial: str,
    ports: Sequence[PortInfo],
) -> PortInfo:
    port = _select_unique_port(port_name, ports)
    return _validate_bootloader_identity(port, expected_bootloader_serial)


def _port_identity(port: PortInfo) -> tuple[str, int | None, int | None, str | None]:
    return (port.device, port.vid, port.pid, port.serial_number)


def revalidate_explicit_port(
    selected: PortInfo,
    port_name: str,
    expected_bootloader_serial: str,
    ports: Sequence[PortInfo],
) -> PortInfo:
    current = _select_unique_port(port_name, ports)
    expected_identity = _port_identity(selected)
    current_identity = _port_identity(current)
    if current_identity != expected_identity:
        change = f"expected {expected_identity!r}, found {current_identity!r}"
        raise FirmwareFlashError(
            f"PCA10059 bootloader identity changed before nRF Util launch: {change}"
        )
    return _validate_bootloader_identity(current, expected_bootloader_serial)


def _port_snapshot(port: PortInfo) -> dict[str, object]:
    return {
        "device": port.device,
        "description": port.description,
        "hwid": port.hwid,
        "vid": f"0x{port.vid:04x}" if port.vid is not None else None,
        "pid": f"0x{port.pid:04x}" if port.pid is not None else None,
        "serial_number": port.serial_number,
    }


def _nrfutil_environment(install_root: Path) -> dict[str, str]:
    environment = dict(os.environ)
    environment["NRFUTIL_HOME"] = str(install_root)
    environment["PATH"] = str(install_root / "bin") + os.pathsep + environment.get("PATH", "")
    return environment


def _write_report(
    reports_root: Path,
    package: PackageIdentity,
    port: PortInfo,
    started_at: str,
    outcome: str,
    detail: str | None,
) -> Path:
    timestamp = datetime.now(UTC)
    report = {
        "schema_version": 1,
        "started_at_utc": started_at,
        "finished_at_utc": timestamp.isoformat(),
        "outcome": outcome,
        "detail": detail,
        "package": {"path": str(package.path), "sha256": package.sha256},
        "port": _port_snapshot(port),
    }
    return write_json_report(reports_root, "firmware-flash", report, now=timestamp)


def flash_firmware(
    settings: dict[str, ResolvedValue],
    *,
    port_name: str,
    confirmed_sha256: str,
) -> None:
    expected_bootloader_serial = _required_text(settings, "bootloader_serial")
    pins = load_firmware_tool_pins()
    selected = current_platform_pin(pins)
    verify_firmware_tools(pins, selected, settings)
    _, _, install_root, executable = managed_firmware_tool_paths(pins, selected, settings)
    configured = _required_path(settings, "programmer")
    if configured.resolve() != executable.resolve():
        raise FirmwareFlashError(
            f"configured programmer must select managed nRF Util: {executable}"
        )

    build_root = _required_path(settings, "build_dir") / OUTPUT_DIRECTORY_NAME
    config = load_package_config()
    package = load_package_identity(build_root, config=config)
    if confirmed_sha256.lower() != package.sha256:
        raise FirmwareFlashError(
            f"package confirmation mismatch: expected exact SHA-256 {package.sha256}"
        )

    ports = cast(list[PortInfo], list(list_ports.comports()))
    port = select_explicit_port(port_name, expected_bootloader_serial, ports)
    print(f"Selected port: {port.device} ({port.description}; {port.hwid})")
    print(f"Selected package: {package.path}")
    print(f"Package SHA-256: {package.sha256}")

    reports_root = _required_path(settings, "reports_dir")
    verified_port = revalidate_explicit_port(
        port,
        port_name,
        expected_bootloader_serial,
        cast(list[PortInfo], list(list_ports.comports())),
    )
    command = [
        str(executable),
        pins.command.name,
        "dfu",
        "usb-serial",
        "-pkg",
        str(package.path),
        "-p",
        verified_port.device,
    ]
    print(f"Revalidated bootloader identity: {_port_snapshot(verified_port)}")
    print(f"+ {subprocess.list2cmdline(command)}")
    started_at = datetime.now(UTC).isoformat()
    try:
        _ = subprocess.run(
            command,
            env=_nrfutil_environment(install_root),
            check=True,
            timeout=600,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        report = _write_report(
            reports_root,
            package,
            verified_port,
            started_at,
            "environment-failure",
            str(error),
        )
        raise FirmwareFlashError(f"firmware flash failed; report: {report}") from error
    report = _write_report(
        reports_root,
        package,
        verified_port,
        started_at,
        "command-succeeded",
        None,
    )
    print(f"Firmware flash command succeeded; report: {report}")


class FlashArguments(argparse.Namespace):
    config: str | None = None
    port: str = ""
    bootloader_serial: str | None = None
    build_dir: str | None = None
    confirm_sha256: str = ""


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Flash the validated PCA10059 Tester DFU package")
    _ = parser.add_argument("--config", help="path to the machine-local TOML configuration")
    _ = parser.add_argument("--port", required=True, help="exact current bootloader serial port")
    _ = parser.add_argument("--build-dir", help="firmware build root override")
    _ = parser.add_argument(
        "--bootloader-serial",
        help=(
            "exact PCA10059 bootloader USB serial; overrides device.bootloader_serial and "
            "NRFTEST_BOOTLOADER_SERIAL"
        ),
    )
    _ = parser.add_argument(
        "--confirm-sha256",
        required=True,
        help="exact SHA-256 printed by firmware-package",
    )
    return parser


def main() -> int:
    arguments = FlashArguments()
    _ = _parser().parse_args(namespace=arguments)
    try:
        flash_firmware(
            resolve_current_settings(
                {
                    "bootloader_serial": arguments.bootloader_serial,
                    "build_dir": arguments.build_dir,
                },
                config_path=arguments.config,
            ),
            port_name=arguments.port,
            confirmed_sha256=arguments.confirm_sha256,
        )
    except (
        ConfigError,
        FirmwareBuildError,
        FirmwareFlashError,
        FirmwarePackageError,
        FirmwareToolError,
        TelemetryError,
    ) as error:
        print(f"firmware flash error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
