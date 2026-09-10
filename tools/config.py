"""解析和检查 nrftest 的本机工具链、外部工具、设备及输出路径配置。

配置优先级为 CLI 参数、NRFTEST_* 环境变量、nrftest.local.toml、默认值；
Just 的 toolchain-paths 和 check-tools recipe 使用本模块统一处理这些来源。
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LOCAL_CONFIG = PROJECT_ROOT / "nrftest.local.toml"


class ConfigError(ValueError):
    """Raised when machine-local configuration is invalid."""


@dataclass(frozen=True)
class FieldSpec:
    name: str
    section: str
    environment: str
    kind: str
    default: str | None = None


@dataclass(frozen=True)
class ResolvedValue:
    value: str | None
    source: str


FIELD_SPECS = (
    FieldSpec("zephyr_root", "toolchains", "NRFTEST_ZEPHYR_ROOT", "path"),
    FieldSpec("ncs_root", "toolchains", "NRFTEST_NCS_ROOT", "path"),
    FieldSpec("zephyr_sdk_root", "toolchains", "NRFTEST_ZEPHYR_SDK_ROOT", "path"),
    FieldSpec("autopts_root", "toolchains", "NRFTEST_AUTOPTS_ROOT", "path"),
    FieldSpec("host_tools_root", "tools", "NRFTEST_HOST_TOOLS_ROOT", "path"),
    FieldSpec("dtc", "tools", "NRFTEST_DTC", "tool"),
    FieldSpec("socat", "tools", "NRFTEST_SOCAT", "tool"),
    FieldSpec("programmer", "tools", "NRFTEST_PROGRAMMER", "tool"),
    FieldSpec("jlink_library", "tools", "NRFTEST_JLINK_LIBRARY", "path"),
    FieldSpec("device_serial", "device", "NRFTEST_DEVICE_SERIAL", "text"),
    FieldSpec(
        "bootloader_serial",
        "device",
        "NRFTEST_BOOTLOADER_SERIAL",
        "text",
    ),
    FieldSpec("debugger_serial", "device", "NRFTEST_DEBUGGER_SERIAL", "text"),
    FieldSpec("device_port", "device", "NRFTEST_DEVICE_PORT", "text"),
    FieldSpec("build_dir", "paths", "NRFTEST_BUILD_DIR", "path", ".work/build"),
    FieldSpec("cache_dir", "paths", "NRFTEST_CACHE_DIR", "path", ".work/cache"),
    FieldSpec("downloads_dir", "paths", "NRFTEST_DOWNLOADS_DIR", "path", ".work/downloads"),
    FieldSpec("reports_dir", "paths", "NRFTEST_REPORTS_DIR", "path", ".work/reports"),
)


def _optional_text(value: object, location: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ConfigError(f"{location} must be a string")
    stripped = value.strip()
    return stripped or None


def load_local_document(path: Path, *, required: bool) -> Mapping[str, object]:
    if not path.exists():
        if required:
            raise ConfigError(f"local config does not exist: {path}")
        return {}
    try:
        with path.open("rb") as stream:
            document = cast(dict[str, object], tomllib.load(stream))
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ConfigError(f"unable to read local config {path}: {error}") from error
    return document


def _local_value(document: Mapping[str, object], spec: FieldSpec) -> str | None:
    section_value = document.get(spec.section, {})
    if not isinstance(section_value, dict):
        raise ConfigError(f"[{spec.section}] must be a table")
    section = cast(dict[str, object], section_value)
    key = spec.name.removeprefix("device_").removesuffix("_dir")
    return _optional_text(section.get(key), f"{spec.section}.{key}")


def _normalize(value: str, kind: str, project_root: Path) -> str:
    if kind == "path":
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = project_root / path
        return str(path.resolve())
    if kind == "tool" and any(separator in value for separator in ("/", "\\")):
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = project_root / path
        return str(path.resolve())
    return value


def resolve_settings(
    explicit: Mapping[str, str | None],
    *,
    environ: Mapping[str, str],
    local_document: Mapping[str, object],
    project_root: Path = PROJECT_ROOT,
) -> dict[str, ResolvedValue]:
    resolved: dict[str, ResolvedValue] = {}
    for spec in FIELD_SPECS:
        candidates = (
            (_optional_text(explicit.get(spec.name), f"CLI {spec.name}"), "cli"),
            (
                _optional_text(environ.get(spec.environment), spec.environment),
                f"env:{spec.environment}",
            ),
            (
                _local_value(local_document, spec),
                f"local:{spec.section}.{spec.name.removeprefix('device_').removesuffix('_dir')}",
            ),
            (_optional_text(spec.default, f"default {spec.name}"), "default"),
        )
        selected = next(((value, source) for value, source in candidates if value), None)
        if selected is None:
            resolved[spec.name] = ResolvedValue(None, "unset")
            continue
        value, source = selected
        resolved[spec.name] = ResolvedValue(
            _normalize(value, spec.kind, project_root),
            source,
        )
    return resolved


def _config_path(explicit: str | None, environ: Mapping[str, str]) -> tuple[Path, bool]:
    explicit_value = _optional_text(explicit, "CLI config path")
    environment_value = _optional_text(environ.get("NRFTEST_CONFIG"), "NRFTEST_CONFIG")
    selected = explicit_value or environment_value
    if selected is None:
        return DEFAULT_LOCAL_CONFIG, False
    path = Path(selected).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path.resolve(), True


def _add_resolution_arguments(parser: argparse.ArgumentParser) -> None:
    _ = parser.add_argument("--config", help="path to the machine-local TOML configuration")
    for spec in FIELD_SPECS:
        _ = parser.add_argument(f"--{spec.name.replace('_', '-')}", dest=spec.name)


def resolve_current_settings(
    explicit: Mapping[str, str | None] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    config_path: str | None = None,
) -> dict[str, ResolvedValue]:
    current_environment = os.environ if environ is None else environ
    selected_path, required = _config_path(config_path, current_environment)
    local_document = load_local_document(selected_path, required=required)
    return resolve_settings(
        explicit or {},
        environ=current_environment,
        local_document=local_document,
    )


class ConfigArguments(argparse.Namespace):
    command: str = ""
    config: str | None = None


def _resolve_from_arguments(arguments: ConfigArguments) -> dict[str, ResolvedValue]:
    argument_values = cast(dict[str, object], vars(arguments))
    explicit = {
        spec.name: _optional_text(argument_values.get(spec.name), f"CLI {spec.name}")
        for spec in FIELD_SPECS
    }
    return resolve_current_settings(explicit, config_path=arguments.config)


def _print_paths(settings: Mapping[str, ResolvedValue]) -> None:
    for spec in FIELD_SPECS:
        resolved = settings[spec.name]
        value = resolved.value if resolved.value is not None else "<unset>"
        print(f"{spec.name}={value} [{resolved.source}]")


def _command_version(name: str, command: list[str]) -> bool:
    executable = command[0] if Path(command[0]).is_absolute() else shutil.which(command[0])
    if executable is None:
        print(f"{name}=missing")
        return False
    command[0] = str(executable)
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        print(f"{name}=error ({error})")
        return False
    output = (result.stdout or result.stderr).strip().splitlines()
    detail = output[0] if output else f"exit {result.returncode}"
    status = "ok" if result.returncode == 0 else "error"
    print(f"{name}={status} ({detail})")
    return result.returncode == 0


def _configured_path_status(name: str, resolved: ResolvedValue) -> bool:
    if resolved.value is None:
        print(f"{name}=not-configured")
        return True
    exists = Path(resolved.value).exists()
    print(f"{name}={'ok' if exists else 'missing'} ({resolved.value}; {resolved.source})")
    return exists


def _configured_tool_status(name: str, resolved: ResolvedValue) -> bool:
    if resolved.value is None:
        print(f"{name}=not-configured")
        return True
    value = resolved.value
    executable = value if Path(value).is_absolute() else shutil.which(value)
    exists = executable is not None and Path(executable).exists()
    print(f"{name}={'ok' if exists else 'missing'} ({value}; {resolved.source})")
    return exists


def _check_tools(settings: Mapping[str, ResolvedValue]) -> int:
    dtc = settings["dtc"].value or "dtc"
    checks = [
        _command_version("python", [sys.executable, "--version"]),
        _command_version("git", ["git", "--version"]),
        _command_version("west", ["west", "--version"]),
        _command_version("cmake", ["cmake", "--version"]),
        _command_version("ninja", ["ninja", "--version"]),
        _command_version("dtc", [dtc, "--version"]),
        _command_version("gperf", ["gperf", "--version"]),
        _command_version("7zip", ["7z", "--help"]),
        _command_version("just", ["just", "--version"]),
        _command_version("pytest", ["pytest", "--version"]),
        _command_version("ruff", ["ruff", "--version"]),
        _configured_path_status("zephyr_root", settings["zephyr_root"]),
        _configured_path_status("ncs_root", settings["ncs_root"]),
        _configured_path_status("zephyr_sdk_root", settings["zephyr_sdk_root"]),
        _configured_path_status("autopts_root", settings["autopts_root"]),
        _configured_tool_status("socat", settings["socat"]),
        _configured_tool_status("programmer", settings["programmer"]),
        _configured_path_status("jlink_library", settings["jlink_library"]),
    ]
    return 0 if all(checks) else 1


def require_local_config(explicit: str | None, environ: Mapping[str, str]) -> Path:
    path, _ = _config_path(explicit, environ)
    _ = load_local_document(path, required=True)
    return path


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Resolve nrftest machine-local configuration")
    subparsers = parser.add_subparsers(dest="command", required=True)
    paths = subparsers.add_parser("paths", help="print resolved paths and selectors")
    checks = subparsers.add_parser("check-tools", help="validate managed and configured tools")
    require_local = subparsers.add_parser(
        "require-local", help="require an explicit machine-local configuration file"
    )
    _add_resolution_arguments(paths)
    _add_resolution_arguments(checks)
    _ = require_local.add_argument("--config", help="path to the machine-local TOML configuration")
    return parser


def main() -> int:
    arguments = ConfigArguments()
    _ = _parser().parse_args(namespace=arguments)
    try:
        if arguments.command == "require-local":
            path = require_local_config(arguments.config, os.environ)
            print(f"Machine-local configuration: {path}")
            return 0
        settings = _resolve_from_arguments(arguments)
    except ConfigError as error:
        print(f"configuration error: {error}", file=sys.stderr)
        return 2
    if arguments.command == "paths":
        _print_paths(settings)
        return 0
    return _check_tools(settings)


if __name__ == "__main__":
    raise SystemExit(main())
