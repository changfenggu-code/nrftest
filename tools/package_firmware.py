from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tomllib
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import cast

from tools.build_firmware import (
    BOARD,
    OUTPUT_DIRECTORY_NAME,
    FirmwareBuildError,
    flash_segments_from_hex,
    sha256_file,
    validate_flash_segments,
)
from tools.config import PROJECT_ROOT, ConfigError, ResolvedValue, resolve_current_settings
from tools.setup_firmware_tools import (
    FirmwareToolError,
    current_platform_pin,
    load_firmware_tool_pins,
    managed_firmware_tool_paths,
    verify_firmware_tools,
)

PACKAGE_CONFIG_PATH = PROJECT_ROOT / "firmware" / "package.toml"
BUILD_MANIFEST_NAME = "nrftest-build-manifest.json"
NORMALIZED_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)


class FirmwarePackageError(RuntimeError):
    """Raised when a validated firmware build cannot be packaged safely."""


@dataclass(frozen=True)
class PackageConfig:
    format: str
    hardware_version: int
    softdevice_requirement: str
    application_version: int
    signed: bool


def load_package_config(path: Path = PACKAGE_CONFIG_PATH) -> PackageConfig:
    try:
        with path.open("rb") as stream:
            document = cast(dict[str, object], tomllib.load(stream))
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise FirmwarePackageError(
            f"unable to read firmware package config {path}: {error}"
        ) from error
    if document.get("schema_version") != 1:
        raise FirmwarePackageError("firmware package schema_version must be 1")
    format_value = document.get("format")
    hardware_version = document.get("hardware_version")
    softdevice_requirement = document.get("softdevice_requirement")
    application_version = document.get("application_version")
    signed = document.get("signed")
    if not isinstance(format_value, str) or format_value != "nrf5-sdk-secure-dfu":
        raise FirmwarePackageError("firmware package format must be nrf5-sdk-secure-dfu")
    if not isinstance(hardware_version, int) or hardware_version <= 0:
        raise FirmwarePackageError("firmware package hardware_version must be positive")
    if not isinstance(softdevice_requirement, str) or not softdevice_requirement.startswith("0x"):
        raise FirmwarePackageError("firmware package softdevice_requirement must be hexadecimal")
    if not isinstance(application_version, int) or application_version <= 0:
        raise FirmwarePackageError("firmware package application_version must be positive")
    if signed is not False:
        raise FirmwarePackageError(
            "the Phase 0 development package must explicitly set signed=false"
        )
    return PackageConfig(
        format=format_value,
        hardware_version=hardware_version,
        softdevice_requirement=softdevice_requirement,
        application_version=application_version,
        signed=signed,
    )


def _required_path(settings: Mapping[str, ResolvedValue], name: str) -> Path:
    value = settings[name].value
    if value is None:
        raise FirmwarePackageError(
            f"{name} is not configured; set it in nrftest.local.toml or its NRFTEST_* variable"
        )
    return Path(value)


def _load_build_manifest(path: Path) -> dict[str, object]:
    try:
        manifest = cast(dict[str, object], json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError) as error:
        raise FirmwarePackageError(f"unable to read build manifest {path}: {error}") from error
    if manifest.get("schema_version") != 1 or manifest.get("board") != BOARD:
        raise FirmwarePackageError("build manifest schema or board identity does not match")
    return manifest


def artifact_sha(manifest: Mapping[str, object], artifact_path: str) -> str:
    artifacts_value = manifest.get("artifacts")
    if not isinstance(artifacts_value, list):
        raise FirmwarePackageError("build manifest artifacts must be an array")
    for value in cast(list[object], artifacts_value):
        if not isinstance(value, dict):
            continue
        artifact = cast(dict[str, object], value)
        if artifact.get("path") == artifact_path and isinstance(artifact.get("sha256"), str):
            return cast(str, artifact["sha256"])
    raise FirmwarePackageError(f"build manifest does not contain {artifact_path}")


def _validate_build(build_root: Path, manifest: Mapping[str, object]) -> Path:
    hex_path = build_root / "zephyr" / "zephyr.hex"
    if not hex_path.is_file():
        raise FirmwarePackageError(f"validated firmware HEX is missing: {hex_path}")
    expected_sha = artifact_sha(manifest, "zephyr/zephyr.hex")
    actual_sha = sha256_file(hex_path)
    if actual_sha != expected_sha:
        detail = f"firmware HEX changed: expected {expected_sha}, found {actual_sha}"
        raise FirmwarePackageError(detail)
    _ = validate_flash_segments(flash_segments_from_hex(hex_path))
    return hex_path


def _nrfutil_environment(install_root: Path) -> dict[str, str]:
    environment = dict(os.environ)
    environment["NRFUTIL_HOME"] = str(install_root)
    environment["PATH"] = str(install_root / "bin") + os.pathsep + environment.get("PATH", "")
    return environment


def _generate_package(
    executable: Path,
    install_root: Path,
    command_name: str,
    config: PackageConfig,
    hex_path: Path,
    package_path: Path,
) -> None:
    if package_path.exists():
        package_path.unlink()
    command = [
        str(executable),
        command_name,
        "pkg",
        "generate",
        "--hw-version",
        str(config.hardware_version),
        "--sd-req",
        config.softdevice_requirement,
        "--application",
        str(hex_path),
        "--application-version",
        str(config.application_version),
        str(package_path),
    ]
    print(f"+ {subprocess.list2cmdline(command)}")
    try:
        _ = subprocess.run(
            command,
            env=_nrfutil_environment(install_root),
            check=True,
            timeout=180,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        raise FirmwarePackageError(f"nRF5 SDK DFU package generation failed: {error}") from error
    if not package_path.is_file():
        raise FirmwarePackageError(f"nRF Util did not create the expected package: {package_path}")


def normalize_package_zip(path: Path) -> None:
    normalized_path = path.with_suffix(".normalized.zip")
    if normalized_path.exists():
        normalized_path.unlink()
    try:
        with (
            zipfile.ZipFile(path) as source,
            zipfile.ZipFile(normalized_path, "w", compression=zipfile.ZIP_STORED) as destination,
        ):
            for name in sorted(source.namelist()):
                member = PurePosixPath(name)
                if member.is_absolute() or ".." in member.parts:
                    raise FirmwarePackageError(f"unsafe DFU ZIP member: {name}")
                info = zipfile.ZipInfo(name, NORMALIZED_ZIP_TIMESTAMP)
                info.compress_type = zipfile.ZIP_STORED
                info.create_system = 3
                info.external_attr = 0o600 << 16
                destination.writestr(info, source.read(name))
        _ = normalized_path.replace(path)
    except (OSError, zipfile.BadZipFile) as error:
        if normalized_path.exists():
            normalized_path.unlink()
        raise FirmwarePackageError(f"unable to normalize DFU ZIP {path}: {error}") from error


def _validate_package_with_nrfutil(
    executable: Path,
    install_root: Path,
    command_name: str,
    package_path: Path,
) -> None:
    command = [str(executable), command_name, "pkg", "display", str(package_path)]
    try:
        _ = subprocess.run(
            command,
            env=_nrfutil_environment(install_root),
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        raise FirmwarePackageError(f"Nordic rejected the normalized DFU ZIP: {error}") from error


def _inspect_package(path: Path) -> tuple[list[str], dict[str, object]]:
    try:
        with zipfile.ZipFile(path) as package:
            names = package.namelist()
            for name in names:
                member = PurePosixPath(name)
                if member.is_absolute() or ".." in member.parts:
                    raise FirmwarePackageError(f"unsafe DFU ZIP member: {name}")
            if "manifest.json" not in names:
                raise FirmwarePackageError("DFU ZIP does not contain manifest.json")
            embedded = cast(dict[str, object], json.loads(package.read("manifest.json")))
    except (OSError, zipfile.BadZipFile, json.JSONDecodeError) as error:
        raise FirmwarePackageError(
            f"unable to inspect generated DFU ZIP {path}: {error}"
        ) from error
    manifest_value = embedded.get("manifest")
    if not isinstance(manifest_value, dict) or "application" not in manifest_value:
        raise FirmwarePackageError("DFU ZIP manifest does not describe an application image")
    return sorted(names), embedded


def _write_package_evidence(
    manifest_path: Path,
    manifest: dict[str, object],
    package_path: Path,
    config: PackageConfig,
    contents: list[str],
    embedded_manifest: Mapping[str, object],
    executable: Path,
) -> None:
    pins = load_firmware_tool_pins()
    manifest["dfu_package"] = {
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "path": package_path.relative_to(manifest_path.parent).as_posix(),
        "size": package_path.stat().st_size,
        "sha256": sha256_file(package_path),
        "format": config.format,
        "signed": config.signed,
        "container_normalized": True,
        "normalized_zip_timestamp": "1980-01-01T00:00:00",
        "parameters": {
            "hardware_version": config.hardware_version,
            "softdevice_requirement": config.softdevice_requirement,
            "application_version": config.application_version,
        },
        "contents": contents,
        "embedded_manifest": embedded_manifest,
        "tool": {
            "executable": str(executable),
            "nrfutil_version": pins.cli_version,
            "nrfutil_commit": pins.cli_commit,
            "command": pins.command.name,
            "command_version": pins.command.version,
            "command_commit": pins.command.commit,
            "inner_version": pins.command.inner_version,
        },
        "license": {
            "identifier": "LicenseRef-Nordic-1-Clause",
            "redistribution": "prohibited",
            "notice": "Nordic tools are installed locally and are not bundled.",
        },
    }
    _ = manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


@dataclass(frozen=True)
class PackageContext:
    executable: Path
    install_root: Path
    command_name: str
    manifest_path: Path
    manifest: dict[str, object]
    hex_path: Path
    config: PackageConfig
    package_path: Path


def _package_context(settings: dict[str, ResolvedValue]) -> PackageContext:
    pins = load_firmware_tool_pins()
    selected = current_platform_pin(pins)
    verify_firmware_tools(pins, selected, settings)
    _, _, install_root, executable = managed_firmware_tool_paths(pins, selected, settings)
    configured = _required_path(settings, "programmer")
    if configured.resolve() != executable.resolve():
        raise FirmwarePackageError(
            f"configured programmer must select managed nRF Util: {executable}"
        )

    build_root = _required_path(settings, "build_dir") / OUTPUT_DIRECTORY_NAME
    manifest_path = build_root / BUILD_MANIFEST_NAME
    manifest = _load_build_manifest(manifest_path)
    hex_path = _validate_build(build_root, manifest)
    config = load_package_config()
    package_path = build_root / f"nrftest-pca10059-tester-v{config.application_version}.zip"
    return PackageContext(
        executable=executable,
        install_root=install_root,
        command_name=pins.command.name,
        manifest_path=manifest_path,
        manifest=manifest,
        hex_path=hex_path,
        config=config,
        package_path=package_path,
    )


def generate_package(settings: dict[str, ResolvedValue]) -> None:
    context = _package_context(settings)
    _generate_package(
        context.executable,
        context.install_root,
        context.command_name,
        context.config,
        context.hex_path,
        context.package_path,
    )
    print(f"DFU package generated: {context.package_path}")


def normalize_and_validate_package(settings: dict[str, ResolvedValue]) -> None:
    context = _package_context(settings)
    if not context.package_path.is_file():
        raise FirmwarePackageError(
            f"generated package is missing before normalization: {context.package_path}"
        )
    normalize_package_zip(context.package_path)
    _validate_package_with_nrfutil(
        context.executable,
        context.install_root,
        context.command_name,
        context.package_path,
    )
    contents, embedded_manifest = _inspect_package(context.package_path)
    _write_package_evidence(
        context.manifest_path,
        context.manifest,
        context.package_path,
        context.config,
        contents,
        embedded_manifest,
        context.executable,
    )
    print(f"DFU package verified: {context.package_path}")
    print(f"DFU package SHA-256: {sha256_file(context.package_path)}")


def package_firmware(config_path: str | None, build_dir: str | None = None) -> None:
    for stage in ("generate", "normalize-validate"):
        command = [sys.executable, "-m", "tools.package_firmware", "--stage", stage]
        if config_path is not None:
            command.extend(("--config", config_path))
        if build_dir is not None:
            command.extend(("--build-dir", build_dir))
        print(f"+ {subprocess.list2cmdline(command)}")
        result = subprocess.run(command, cwd=PROJECT_ROOT, check=False)
        if result.returncode != 0:
            raise FirmwarePackageError(
                f"firmware package stage {stage!r} failed with exit code {result.returncode}"
            )


class PackageArguments(argparse.Namespace):
    config: str | None = None
    stage: str | None = None
    build_dir: str | None = None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Package the validated PCA10059 Tester firmware")
    _ = parser.add_argument("--config", help="path to the machine-local TOML configuration")
    _ = parser.add_argument("--build-dir", help="firmware build root override")
    _ = parser.add_argument(
        "--stage",
        choices=("generate", "normalize-validate"),
        help=argparse.SUPPRESS,
    )
    return parser


def main() -> int:
    arguments = PackageArguments()
    _ = _parser().parse_args(namespace=arguments)
    try:
        if arguments.stage is None:
            package_firmware(arguments.config, arguments.build_dir)
        else:
            settings = resolve_current_settings(
                {"build_dir": arguments.build_dir}, config_path=arguments.config
            )
            if arguments.stage == "generate":
                generate_package(settings)
            else:
                normalize_and_validate_package(settings)
    except (ConfigError, FirmwarePackageError, FirmwareBuildError, FirmwareToolError) as error:
        print(f"firmware package error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
