from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO, cast

from tools.config import PROJECT_ROOT, ConfigError, ResolvedValue, resolve_current_settings

LOCK_PATH = PROJECT_ROOT / "firmware-tools.lock.toml"


class FirmwareToolError(RuntimeError):
    """Raised when pinned firmware packaging tools cannot be installed or verified safely."""


@dataclass(frozen=True)
class CommandPin:
    name: str
    version: str
    commit: str
    inner_version: str


@dataclass(frozen=True)
class PlatformPin:
    selector: str
    target: str
    cli_url: str
    cli_archive: str
    cli_sha256: str
    cli_archive_root: str
    cli_executable: Path
    command_url: str
    command_archive: str
    command_sha256: str


@dataclass(frozen=True)
class FirmwareToolPins:
    cli_version: str
    cli_commit: str
    command: CommandPin
    platforms: Mapping[str, PlatformPin]


def _required_string(table: Mapping[str, object], key: str, location: str) -> str:
    value = table.get(key)
    if not isinstance(value, str) or not value.strip():
        raise FirmwareToolError(f"{location}.{key} must be a non-empty string")
    return value.strip()


def _sha_field(table: Mapping[str, object], key: str, location: str) -> str:
    value = _required_string(table, key, location)
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise FirmwareToolError(f"{location}.{key} must be a lowercase SHA-256 digest")
    return value


def _relative_path(table: Mapping[str, object], key: str, location: str) -> Path:
    value = Path(_required_string(table, key, location))
    if value.is_absolute() or ".." in value.parts:
        raise FirmwareToolError(f"{location}.{key} must stay inside the managed install")
    return value


def load_firmware_tool_pins(path: Path = LOCK_PATH) -> FirmwareToolPins:
    try:
        with path.open("rb") as stream:
            document = cast(dict[str, object], tomllib.load(stream))
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise FirmwareToolError(f"unable to read firmware-tools lock {path}: {error}") from error
    if document.get("schema_version") != 1:
        raise FirmwareToolError("firmware-tools lock schema_version must be 1")
    nrfutil_value = document.get("nrfutil")
    platforms_value = document.get("platforms")
    if not isinstance(nrfutil_value, dict) or not isinstance(platforms_value, dict):
        raise FirmwareToolError("firmware-tools lock must contain [nrfutil] and [platforms]")
    nrfutil = cast(dict[str, object], nrfutil_value)
    platform_tables = cast(dict[str, object], platforms_value)
    command_value = nrfutil.get("command")
    if not isinstance(command_value, dict):
        raise FirmwareToolError("firmware-tools lock must contain [nrfutil.command]")
    command_table = cast(dict[str, object], command_value)
    command = CommandPin(
        name=_required_string(command_table, "name", "nrfutil.command"),
        version=_required_string(command_table, "version", "nrfutil.command"),
        commit=_required_string(command_table, "commit", "nrfutil.command"),
        inner_version=_required_string(command_table, "inner_version", "nrfutil.command"),
    )
    platforms: dict[str, PlatformPin] = {}
    for selector in ("win-64", "osx-arm64", "linux-64"):
        value = platform_tables.get(selector)
        if not isinstance(value, dict):
            raise FirmwareToolError(f"firmware-tools lock must contain [platforms.{selector}]")
        table = cast(dict[str, object], value)
        platforms[selector] = PlatformPin(
            selector=selector,
            target=_required_string(table, "target", f"platforms.{selector}"),
            cli_url=_required_string(table, "cli_url", f"platforms.{selector}"),
            cli_archive=_required_string(table, "cli_archive", f"platforms.{selector}"),
            cli_sha256=_sha_field(table, "cli_sha256", f"platforms.{selector}"),
            cli_archive_root=_required_string(table, "cli_archive_root", f"platforms.{selector}"),
            cli_executable=_relative_path(table, "cli_executable", f"platforms.{selector}"),
            command_url=_required_string(table, "command_url", f"platforms.{selector}"),
            command_archive=_required_string(table, "command_archive", f"platforms.{selector}"),
            command_sha256=_sha_field(table, "command_sha256", f"platforms.{selector}"),
        )
    return FirmwareToolPins(
        cli_version=_required_string(nrfutil, "version", "nrfutil"),
        cli_commit=_required_string(nrfutil, "commit", "nrfutil"),
        command=command,
        platforms=platforms,
    )


def current_platform_pin(pins: FirmwareToolPins) -> PlatformPin:
    system = platform.system()
    machine = platform.machine().lower()
    selector = {
        ("Windows", "amd64"): "win-64",
        ("Windows", "x86_64"): "win-64",
        ("Darwin", "arm64"): "osx-arm64",
        ("Darwin", "aarch64"): "osx-arm64",
        ("Linux", "x86_64"): "linux-64",
        ("Linux", "amd64"): "linux-64",
    }.get((system, machine))
    if selector is None:
        raise FirmwareToolError(f"unsupported firmware-tool host: {system} {platform.machine()}")
    return pins.platforms[selector]


def _required_path(settings: Mapping[str, ResolvedValue], name: str) -> Path:
    value = settings[name].value
    if value is None:
        raise FirmwareToolError(
            f"{name} is not configured; set it in nrftest.local.toml or its NRFTEST_* variable"
        )
    return Path(value)


def managed_firmware_tool_paths(
    pins: FirmwareToolPins,
    selected: PlatformPin,
    settings: Mapping[str, ResolvedValue],
) -> tuple[Path, Path, Path, Path]:
    downloads = _required_path(settings, "downloads_dir")
    install_root = _required_path(settings, "host_tools_root") / f"nrfutil-{pins.cli_version}"
    return (
        downloads / selected.cli_archive,
        downloads / selected.command_archive,
        install_root,
        install_root / selected.cli_executable,
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
    except OSError as error:
        raise FirmwareToolError(f"unable to hash {path}: {error}") from error
    return digest.hexdigest()


def _download(url: str, destination: Path, expected_sha256: str) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        actual = _sha256(destination)
        if actual != expected_sha256:
            detail = (
                f"cached archive SHA-256 mismatch for {destination}: expected "
                f"{expected_sha256}, found {actual}"
            )
            raise FirmwareToolError(detail)
        return
    partial = destination.with_suffix(f"{destination.suffix}.part")
    if partial.exists():
        partial.unlink()
    request = urllib.request.Request(url, headers={"User-Agent": "nrftest-firmware-tool-setup"})
    try:
        response = cast(BinaryIO, urllib.request.urlopen(request, timeout=60))
        with response, partial.open("wb") as output:
            shutil.copyfileobj(response, output)
        actual = _sha256(partial)
        if actual != expected_sha256:
            raise FirmwareToolError(
                f"downloaded archive SHA-256 mismatch: expected {expected_sha256}, found {actual}"
            )
        _ = partial.replace(destination)
    except OSError as error:
        if partial.exists():
            partial.unlink()
        raise FirmwareToolError(f"unable to download {url}: {error}") from error


def _safe_extract_cli(archive: Path, destination: Path, archive_root: str) -> None:
    try:
        with tarfile.open(archive, "r:gz") as package:
            for member in package.getmembers():
                path = PurePosixPath(member.name)
                if path.is_absolute() or ".." in path.parts:
                    raise FirmwareToolError(f"unsafe tar member: {member.name}")
                if member.issym() or member.islnk():
                    raise FirmwareToolError(f"linked tar member is not allowed: {member.name}")
            package.extractall(destination, filter="data")
    except (OSError, tarfile.TarError) as error:
        raise FirmwareToolError(f"unable to extract {archive}: {error}") from error
    data_root = destination / archive_root / "data"
    if not data_root.is_dir():
        raise FirmwareToolError(f"nRF Util archive is missing its data directory: {data_root}")


def _environment(install_root: Path) -> dict[str, str]:
    environment = dict(os.environ)
    environment["NRFUTIL_HOME"] = str(install_root)
    environment["PATH"] = str(install_root / "bin") + os.pathsep + environment.get("PATH", "")
    return environment


def _capture(command: list[str], *, environment: Mapping[str, str]) -> str:
    try:
        result = subprocess.run(
            command,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        raise FirmwareToolError(
            f"command failed: {subprocess.list2cmdline(command)} ({error})"
        ) from error
    return (result.stdout or result.stderr).strip()


def _verify_cli(executable: Path, pins: FirmwareToolPins, install_root: Path) -> None:
    output = _capture([str(executable), "--version"], environment=_environment(install_root))
    if f"nrfutil {pins.cli_version} " not in output or pins.cli_commit not in output:
        raise FirmwareToolError(f"managed nRF Util identity mismatch:\n{output}")


def _command_manifest(install_root: Path, selected: PlatformPin, command: CommandPin) -> Path:
    return (
        install_root / "installed" / f"nrfutil-{command.name}-{selected.target}" / "manifest.json"
    )


def _verify_command(
    executable: Path,
    pins: FirmwareToolPins,
    selected: PlatformPin,
    install_root: Path,
) -> None:
    manifest_path = _command_manifest(install_root, selected, pins.command)
    try:
        manifest = cast(
            dict[str, object],
            json.loads(manifest_path.read_text(encoding="utf-8")),
        )
    except (OSError, json.JSONDecodeError) as error:
        raise FirmwareToolError(
            f"unable to read installed command manifest {manifest_path}"
        ) from error
    expected_name = f"nrfutil-{pins.command.name}"
    expected = {
        "name": expected_name,
        "version": pins.command.version,
        "platform": selected.target,
        "build": pins.command.commit,
    }
    mismatches = [
        f"{name}: expected {value}, found {manifest.get(name)}"
        for name, value in expected.items()
        if manifest.get(name) != value
    ]
    if mismatches:
        raise FirmwareToolError(
            "installed command identity mismatch:\n- " + "\n- ".join(mismatches)
        )
    output = _capture(
        [str(executable), pins.command.name, "--version"],
        environment=_environment(install_root),
    )
    if (
        f"{expected_name} {pins.command.version} " not in output
        or pins.command.commit not in output
        or f"inner-executable-version: nrfutil version {pins.command.inner_version}" not in output
    ):
        raise FirmwareToolError(f"managed {pins.command.name} identity mismatch:\n{output}")


def _install_cli(
    archive: Path,
    selected: PlatformPin,
    install_root: Path,
    executable: Path,
) -> None:
    install_root.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=install_root.parent) as temporary:
        staging = Path(temporary)
        _safe_extract_cli(archive, staging, selected.cli_archive_root)
        data_root = staging / selected.cli_archive_root / "data"
        _ = shutil.move(str(data_root), str(install_root))
    if os.name != "nt":
        executable.chmod(executable.stat().st_mode | 0o111)


def _require_programmer_selection(settings: Mapping[str, ResolvedValue], executable: Path) -> None:
    configured = settings["programmer"].value
    if configured is None:
        raise FirmwareToolError(
            f"programmer is not configured; select the managed nRF Util explicitly: {executable}"
        )
    if Path(configured).resolve() != executable.resolve():
        raise FirmwareToolError(
            f"configured programmer does not select the managed install: use {executable}"
        )


def setup_firmware_tools(
    pins: FirmwareToolPins,
    selected: PlatformPin,
    settings: Mapping[str, ResolvedValue],
) -> None:
    cli_archive, command_archive, install_root, executable = managed_firmware_tool_paths(
        pins, selected, settings
    )
    _require_programmer_selection(settings, executable)
    _download(selected.cli_url, cli_archive, selected.cli_sha256)
    _download(selected.command_url, command_archive, selected.command_sha256)
    if not install_root.exists():
        _install_cli(cli_archive, selected, install_root, executable)
    elif not executable.is_file():
        raise FirmwareToolError(f"managed nRF Util install is incomplete: {executable}")
    _verify_cli(executable, pins, install_root)
    if not _command_manifest(install_root, selected, pins.command).is_file():
        command = [
            str(executable),
            "--log-output=stdout",
            "--log-level=warn",
            "install",
            "--tarball",
            str(command_archive),
        ]
        try:
            _ = subprocess.run(command, env=_environment(install_root), check=True, timeout=180)
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
            raise FirmwareToolError(f"unable to install {pins.command.name}: {error}") from error
    _verify_command(executable, pins, selected, install_root)
    print(f"Firmware tools installed and verified: {executable}")


def verify_firmware_tools(
    pins: FirmwareToolPins,
    selected: PlatformPin,
    settings: Mapping[str, ResolvedValue],
) -> None:
    _, _, install_root, executable = managed_firmware_tool_paths(pins, selected, settings)
    _require_programmer_selection(settings, executable)
    _verify_cli(executable, pins, install_root)
    _verify_command(executable, pins, selected, install_root)
    print(f"Firmware tools verified: {executable}")


def print_status(
    pins: FirmwareToolPins,
    selected: PlatformPin,
    settings: Mapping[str, ResolvedValue],
) -> None:
    _, _, install_root, executable = managed_firmware_tool_paths(pins, selected, settings)
    command_manifest = _command_manifest(install_root, selected, pins.command)
    print(f"Host selector: {selected.selector} ({selected.target})")
    print(f"nRF Util required: {pins.cli_version} ({pins.cli_commit})")
    print(f"nRF Util path: {executable} [{'installed' if executable.is_file() else 'missing'}]")
    print(f"{pins.command.name} required: {pins.command.version} ({pins.command.commit})")
    command_state = "installed" if command_manifest.is_file() else "missing"
    print(f"{pins.command.name} manifest: {command_manifest} [{command_state}]")


class FirmwareToolArguments(argparse.Namespace):
    command: str = ""
    config: str | None = None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Install pinned Nordic firmware packaging tools")
    _ = parser.add_argument("--config", help="path to the machine-local TOML configuration")
    subparsers = parser.add_subparsers(dest="command", required=True)
    _ = subparsers.add_parser("status", help="show managed firmware-tool state")
    _ = subparsers.add_parser("setup", help="download and install pinned firmware tools")
    _ = subparsers.add_parser("verify", help="verify pinned firmware tools without modifying them")
    return parser


def main() -> int:
    arguments = FirmwareToolArguments()
    _ = _parser().parse_args(namespace=arguments)
    try:
        pins = load_firmware_tool_pins()
        selected = current_platform_pin(pins)
        settings = resolve_current_settings(config_path=arguments.config)
        if arguments.command == "status":
            print_status(pins, selected, settings)
        elif arguments.command == "setup":
            setup_firmware_tools(pins, selected, settings)
        else:
            verify_firmware_tools(pins, selected, settings)
    except (ConfigError, FirmwareToolError) as error:
        print(f"firmware-tool error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
