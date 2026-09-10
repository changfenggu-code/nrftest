from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tomllib
import urllib.request
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, cast
from urllib.parse import urlsplit
from uuid import uuid4

from tools.config import PROJECT_ROOT, ConfigError, ResolvedValue, resolve_current_settings

LOCK_PATH = PROJECT_ROOT / "platform-prerequisites.lock.toml"
DRIVER_MARKER = ".nrftest-nrf-device-lib-driver-installed"
UDEV_RULE_GLOBS = ("*nrf*.rules", "*nordic*.rules")


class PlatformPrerequisiteError(RuntimeError):
    """Raised when a platform prerequisite cannot be installed or verified."""


@dataclass(frozen=True)
class PackagePin:
    version: str
    url: str
    archive: str
    sha256: str


@dataclass(frozen=True)
class PlatformPins:
    nrf_device_lib: PackagePin
    nrf_udev: PackagePin


def _required_string(table: Mapping[str, object], key: str, location: str) -> str:
    value = table.get(key)
    if not isinstance(value, str) or not value.strip():
        raise PlatformPrerequisiteError(f"{location}.{key} must be a non-empty string")
    return value.strip()


def _sha_field(table: Mapping[str, object], key: str, location: str) -> str:
    value = _required_string(table, key, location)
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise PlatformPrerequisiteError(f"{location}.{key} must be a lowercase SHA-256 digest")
    return value


def _package_pin(table: Mapping[str, object], location: str) -> PackagePin:
    url = _required_string(table, "url", location)
    if urlsplit(url).scheme != "https":
        raise PlatformPrerequisiteError(f"{location}.url must use HTTPS")
    archive = _required_string(table, "archive", location)
    archive_path = Path(archive)
    if archive_path.name != archive or archive_path.is_absolute() or ".." in archive_path.parts:
        raise PlatformPrerequisiteError(f"{location}.archive must be one safe file name")
    return PackagePin(
        version=_required_string(table, "version", location),
        url=url,
        archive=archive,
        sha256=_sha_field(table, "sha256", location),
    )


def load_platform_pins(path: Path = LOCK_PATH) -> PlatformPins:
    try:
        with path.open("rb") as stream:
            document = cast(dict[str, object], tomllib.load(stream))
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise PlatformPrerequisiteError(
            f"unable to read platform prerequisite lock: {error}"
        ) from error
    if document.get("schema_version") != 1:
        raise PlatformPrerequisiteError("platform prerequisite lock schema_version must be 1")
    windows_value = document.get("windows")
    linux_value = document.get("linux")
    if not isinstance(windows_value, dict) or not isinstance(linux_value, dict):
        raise PlatformPrerequisiteError("platform prerequisite lock must contain windows and linux")
    windows = cast(dict[str, object], windows_value)
    linux = cast(dict[str, object], linux_value)
    driver_value = windows.get("nrf_device_lib")
    udev_value = linux.get("nrf_udev")
    if not isinstance(driver_value, dict) or not isinstance(udev_value, dict):
        raise PlatformPrerequisiteError("platform prerequisite lock is incomplete")
    return PlatformPins(
        nrf_device_lib=_package_pin(
            cast(dict[str, object], driver_value), "windows.nrf_device_lib"
        ),
        nrf_udev=_package_pin(cast(dict[str, object], udev_value), "linux.nrf_udev"),
    )


def _required_path(settings: Mapping[str, ResolvedValue], name: str) -> Path:
    value = settings[name].value
    if value is None:
        raise PlatformPrerequisiteError(
            f"{name} is not configured; set it in nrftest.local.toml or its NRFTEST_* variable"
        )
    return Path(value)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
    except OSError as error:
        raise PlatformPrerequisiteError(f"unable to hash {path}: {error}") from error
    return digest.hexdigest()


def _download(pin: PackagePin, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        actual = _sha256(destination)
        if actual != pin.sha256:
            raise PlatformPrerequisiteError(
                f"cached prerequisite SHA-256 mismatch for {destination}: "
                + f"expected {pin.sha256}, found {actual}"
            )
        return
    partial = destination.with_suffix(f"{destination.suffix}.part")
    if partial.exists():
        partial.unlink()
    request = urllib.request.Request(
        pin.url, headers={"User-Agent": "nrftest-platform-prerequisite-setup"}
    )
    try:
        response = cast(BinaryIO, urllib.request.urlopen(request, timeout=60))
        with response, partial.open("wb") as output:
            shutil.copyfileobj(response, output)
        actual = _sha256(partial)
        if actual != pin.sha256:
            raise PlatformPrerequisiteError(
                f"downloaded prerequisite SHA-256 mismatch: expected {pin.sha256}, found {actual}"
            )
        _ = partial.replace(destination)
    except OSError as error:
        if partial.exists():
            partial.unlink()
        raise PlatformPrerequisiteError(f"unable to download {pin.url}: {error}") from error


def _platform_name() -> str:
    system = platform.system()
    machine = platform.machine().lower()
    if system == "Windows" and machine in {"amd64", "x86_64"}:
        return "win-64"
    if system == "Linux" and machine in {"x86_64", "amd64"}:
        return "linux-64"
    if system == "Darwin" and machine in {"arm64", "aarch64"}:
        return "osx-arm64"
    raise PlatformPrerequisiteError(f"unsupported platform: {system} {platform.machine()}")


def _driver_marker(settings: Mapping[str, ResolvedValue]) -> Path:
    return _required_path(settings, "host_tools_root") / DRIVER_MARKER


def _driver_receipt_matches(path: Path, pin: PackagePin) -> bool:
    try:
        document = cast(object, json.loads(path.read_text(encoding="utf-8")))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    return isinstance(document, dict) and document == {
        "platform": "win-64",
        "schema_version": 1,
        "sha256": pin.sha256,
        "version": pin.version,
    }


def _write_driver_receipt(path: Path, pin: PackagePin) -> None:
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    document = {
        "platform": "win-64",
        "schema_version": 1,
        "sha256": pin.sha256,
        "version": pin.version,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            _ = stream.write(json.dumps(document, indent=2, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        _ = temporary.replace(path)
    except (OSError, TypeError, ValueError) as error:
        with suppress(OSError):
            temporary.unlink(missing_ok=True)
        raise PlatformPrerequisiteError(
            f"unable to write driver receipt {path}: {error}"
        ) from error


def _linux_udev_rules_present() -> bool:
    roots = (
        Path("/etc/udev/rules.d"),
        Path("/lib/udev/rules.d"),
        Path("/usr/lib/udev/rules.d"),
    )
    return any(any(root.glob(pattern)) for root in roots for pattern in UDEV_RULE_GLOBS)


def _configured_serial_accessible(settings: Mapping[str, ResolvedValue]) -> bool | None:
    port = settings["device_port"].value
    if port is None:
        return None
    path = Path(port)
    return path.exists() and os.access(path, os.R_OK | os.W_OK)


def _linux_distribution() -> str:
    try:
        for line in Path("/etc/os-release").read_text(encoding="utf-8").splitlines():
            if line.startswith("ID="):
                return line.removeprefix("ID=").strip().strip('"')
    except OSError:
        pass
    return "unknown"


def print_status(pins: PlatformPins, settings: Mapping[str, ResolvedValue]) -> None:
    selected = _platform_name()
    print(f"Platform: {selected}")
    if selected == "win-64":
        print("VC++ runtime: managed by the Pixi win-64 environment")
        marker = _driver_marker(settings)
        driver_state = (
            "valid"
            if _driver_receipt_matches(marker, pins.nrf_device_lib)
            else "missing-or-invalid"
        )
        print(f"nRF device-lib managed-install receipt: {driver_state}")
        driver_cache = _required_path(settings, "downloads_dir") / pins.nrf_device_lib.archive
        print(f"Driver installer cache: {driver_cache}")
        print("Connected-device functionality: verify with host-doctor or firmware-flash")
    elif selected == "linux-64":
        print(f"Linux distribution: {_linux_distribution()}")
        print(f"nrf-udev rules: {'present' if _linux_udev_rules_present() else 'missing'}")
        serial_access = _configured_serial_accessible(settings)
        if serial_access is None:
            print("Configured serial access: not configured")
        else:
            print(f"Configured serial access: {'ok' if serial_access else 'missing'}")
    else:
        print("macOS: no additional Nordic system package is selected")


def _run_windows_driver_installer(path: Path) -> None:
    escaped = str(path).replace("'", "''")
    command = (
        f"$process = Start-Process -FilePath '{escaped}' -Verb RunAs -Wait -PassThru; "
        + "exit $process.ExitCode"
    )
    try:
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                command,
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=900,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise PlatformPrerequisiteError(
            f"Windows nRF device-lib driver installer failed: {error}"
        ) from error
    if result.returncode != 0:
        raise PlatformPrerequisiteError(
            f"Windows nRF device-lib driver installer returned exit {result.returncode}"
        )


def _linux_udev_commands() -> tuple[str, str]:
    if _linux_distribution() not in {"debian", "ubuntu", "linuxmint", "pop"}:
        raise PlatformPrerequisiteError(
            "automatic nrf-udev installation supports Debian-family Linux only; "
            + "install equivalent udev rules explicitly for this distribution"
        )
    sudo = shutil.which("sudo")
    dpkg = shutil.which("dpkg")
    if sudo is None or dpkg is None:
        raise PlatformPrerequisiteError("Linux nrf-udev setup requires both sudo and dpkg")
    return sudo, dpkg


def _run_linux_udev_installer(path: Path) -> None:
    sudo, dpkg = _linux_udev_commands()
    try:
        _ = subprocess.run([sudo, dpkg, "--install", str(path)], check=True, timeout=120)
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        raise PlatformPrerequisiteError(f"Linux nrf-udev installation failed: {error}") from error


def setup_platform_prerequisites(pins: PlatformPins, settings: Mapping[str, ResolvedValue]) -> None:
    selected = _platform_name()
    downloads = _required_path(settings, "downloads_dir")
    marker: Path | None = None
    if selected == "win-64":
        marker = _driver_marker(settings)
    elif selected == "linux-64" and not _linux_udev_rules_present():
        _ = _linux_udev_commands()

    if selected == "win-64":
        driver_path = downloads / pins.nrf_device_lib.archive
        assert marker is not None
        if not _driver_receipt_matches(marker, pins.nrf_device_lib):
            _download(pins.nrf_device_lib, driver_path)
            print(f"Installing nRF device-lib driver with UAC: {driver_path}")
            _run_windows_driver_installer(driver_path)
            _write_driver_receipt(marker, pins.nrf_device_lib)
    elif selected == "linux-64" and not _linux_udev_rules_present():
        udev_path = downloads / pins.nrf_udev.archive
        _download(pins.nrf_udev, udev_path)
        print(f"Installing nrf-udev {pins.nrf_udev.version} with sudo: {udev_path}")
        _run_linux_udev_installer(udev_path)
    verify_platform_prerequisites(pins, settings)


def verify_platform_prerequisites(
    pins: PlatformPins, settings: Mapping[str, ResolvedValue]
) -> None:
    selected = _platform_name()
    if selected == "win-64":
        marker = _driver_marker(settings)
        if not _driver_receipt_matches(marker, pins.nrf_device_lib):
            raise PlatformPrerequisiteError(
                "nRF device-lib managed-install receipt is missing or does not match the lock"
            )
    elif selected == "linux-64" and not _linux_udev_rules_present():
        raise PlatformPrerequisiteError("nrf-udev rules are not installed")
    print_status(pins, settings)


class Arguments(argparse.Namespace):
    command: str = ""
    config: str | None = None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Install and verify managed platform driver/udev provisioning"
    )
    _ = parser.add_argument("--config", help="path to the machine-local TOML configuration")
    subparsers = parser.add_subparsers(dest="command", required=True)
    _ = subparsers.add_parser("status", help="report managed platform provisioning state")
    _ = subparsers.add_parser("setup", help="install explicitly selected driver/udev packages")
    _ = subparsers.add_parser("verify", help="verify managed provisioning provenance")
    return parser


def main() -> int:
    arguments = Arguments()
    _ = _parser().parse_args(namespace=arguments)
    try:
        pins = load_platform_pins()
        settings = resolve_current_settings(config_path=arguments.config)
        if arguments.command == "status":
            print_status(pins, settings)
        elif arguments.command == "setup":
            setup_platform_prerequisites(pins, settings)
        else:
            verify_platform_prerequisites(pins, settings)
    except (ConfigError, PlatformPrerequisiteError) as error:
        print(f"platform provisioning error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
