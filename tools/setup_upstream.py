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

from tools.config import PROJECT_ROOT, ResolvedValue, resolve_current_settings

LOCK_PATH = PROJECT_ROOT / "upstream.lock.toml"
SOURCES_READY_MARKER = ".nrftest-sources-ready"


class SetupError(RuntimeError):
    """Raised when a pinned upstream setup cannot proceed safely."""


@dataclass(frozen=True)
class ZephyrPin:
    repository: str
    tag: str
    commit: str
    sdk_version: str
    sdk_gnu_toolchains: tuple[str, ...]


@dataclass(frozen=True)
class AutoPtsPin:
    repository: str
    commit: str


@dataclass(frozen=True)
class UpstreamPins:
    zephyr: ZephyrPin
    autopts: AutoPtsPin


def _required_string(table: Mapping[str, object], key: str, location: str) -> str:
    value = table.get(key)
    if not isinstance(value, str) or not value.strip():
        raise SetupError(f"{location}.{key} must be a non-empty string")
    return value.strip()


def load_upstream_pins(path: Path = LOCK_PATH) -> UpstreamPins:
    try:
        with path.open("rb") as stream:
            document = cast(dict[str, object], tomllib.load(stream))
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise SetupError(f"unable to read upstream lock {path}: {error}") from error
    if document.get("schema_version") != 1:
        raise SetupError("upstream lock schema_version must be 1")
    zephyr_value = document.get("zephyr")
    autopts_value = document.get("autopts")
    if not isinstance(zephyr_value, dict) or not isinstance(autopts_value, dict):
        raise SetupError("upstream lock must contain [zephyr] and [autopts] tables")
    zephyr = cast(dict[str, object], zephyr_value)
    autopts = cast(dict[str, object], autopts_value)
    toolchains_value = zephyr.get("sdk_gnu_toolchains")
    if not isinstance(toolchains_value, list) or not toolchains_value:
        raise SetupError("zephyr.sdk_gnu_toolchains must be a non-empty string array")
    toolchains: list[str] = []
    for item in cast(list[object], toolchains_value):
        if not isinstance(item, str) or not item:
            raise SetupError("zephyr.sdk_gnu_toolchains must contain non-empty strings")
        toolchains.append(item)
    zephyr_pin = ZephyrPin(
        repository=_required_string(zephyr, "repository", "zephyr"),
        tag=_required_string(zephyr, "tag", "zephyr"),
        commit=_required_string(zephyr, "commit", "zephyr"),
        sdk_version=_required_string(zephyr, "sdk_version", "zephyr"),
        sdk_gnu_toolchains=tuple(toolchains),
    )
    autopts_pin = AutoPtsPin(
        repository=_required_string(autopts, "repository", "autopts"),
        commit=_required_string(autopts, "commit", "autopts"),
    )
    for name, commit in (("zephyr", zephyr_pin.commit), ("autopts", autopts_pin.commit)):
        if len(commit) != 40 or any(character not in "0123456789abcdef" for character in commit):
            raise SetupError(f"{name}.commit must be a lowercase 40-character Git SHA")
    return UpstreamPins(zephyr=zephyr_pin, autopts=autopts_pin)


def _required_path(settings: dict[str, ResolvedValue], name: str) -> Path:
    value = settings[name].value
    if value is None:
        raise SetupError(
            f"{name} is not configured; set it in nrftest.local.toml or its NRFTEST_* variable"
        )
    return Path(value)


def _command_environment() -> dict[str, str]:
    environment = dict(os.environ)
    if os.name == "nt":
        environment.update(
            {
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "core.longpaths",
                "GIT_CONFIG_VALUE_0": "true",
            }
        )
    return environment


def _display_command(command: list[str]) -> str:
    return subprocess.list2cmdline(command) if os.name == "nt" else " ".join(command)


def _run(command: list[str], *, cwd: Path | None = None) -> None:
    print(f"+ {_display_command(command)}")
    try:
        _ = subprocess.run(
            command,
            cwd=cwd,
            env=_command_environment(),
            check=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise SetupError(f"command failed: {_display_command(command)} ({error})") from error


def _capture(command: list[str], *, cwd: Path) -> str:
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            env=_command_environment(),
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise SetupError(f"command failed: {_display_command(command)} ({error})") from error
    return result.stdout.strip()


def _require_commands() -> None:
    missing = [name for name in ("git", "west") if shutil.which(name) is None]
    if missing:
        raise SetupError(f"Pixi environment is missing required commands: {', '.join(missing)}")


def _normalized_repository(value: str) -> str:
    return value.rstrip("/").removesuffix(".git").lower()


def _validate_repository(path: Path, repository: str, commit: str, name: str) -> None:
    if not (path / ".git").exists():
        raise SetupError(f"{name} destination is not a Git checkout: {path}")
    remote = _capture(["git", "remote", "get-url", "origin"], cwd=path)
    if _normalized_repository(remote) != _normalized_repository(repository):
        raise SetupError(f"{name} origin mismatch: expected {repository}, found {remote}")
    head = _capture(["git", "rev-parse", "HEAD"], cwd=path)
    if head != commit:
        raise SetupError(f"{name} revision mismatch: expected {commit}, found {head}")
    dirty = _capture(["git", "status", "--porcelain"], cwd=path)
    if dirty:
        raise SetupError(f"{name} checkout contains local changes; refusing to modify {path}")


def _ensure_empty_or_missing(path: Path, description: str) -> None:
    if not path.exists():
        return
    if not path.is_dir():
        raise SetupError(f"{description} destination is not a directory: {path}")
    if any(path.iterdir()):
        raise SetupError(f"{description} destination already contains files: {path}")


def _setup_zephyr_sources(
    pin: ZephyrPin,
    zephyr_root: Path,
    *,
    update: bool,
) -> None:
    workspace_root = zephyr_root.parent
    west_root = workspace_root / ".west"
    marker = workspace_root / SOURCES_READY_MARKER
    if not west_root.exists():
        _ensure_empty_or_missing(workspace_root, "Zephyr workspace")
        workspace_root.parent.mkdir(parents=True, exist_ok=True)
        _run(
            [
                "west",
                "init",
                "-m",
                pin.repository,
                "--mr",
                pin.tag,
                str(workspace_root),
            ]
        )
    _validate_repository(zephyr_root, pin.repository, pin.commit, "Zephyr")
    if update or not marker.exists():
        _run(["west", "update"], cwd=workspace_root)
        _ = marker.write_text(f"zephyr={pin.commit}\n", encoding="utf-8")
    else:
        print(f"Zephyr modules already initialized: {workspace_root}")


def _setup_autopts(pin: AutoPtsPin, autopts_root: Path) -> None:
    if not autopts_root.exists():
        autopts_root.parent.mkdir(parents=True, exist_ok=True)
        _run(
            [
                "git",
                "clone",
                "--filter=blob:none",
                "--no-checkout",
                pin.repository,
                str(autopts_root),
            ]
        )
        _run(["git", "checkout", "--detach", pin.commit], cwd=autopts_root)
    _validate_repository(autopts_root, pin.repository, pin.commit, "AutoPTS")


def setup_sources(
    pins: UpstreamPins,
    settings: dict[str, ResolvedValue],
    *,
    update: bool,
) -> None:
    _require_commands()
    zephyr_root = _required_path(settings, "zephyr_root")
    autopts_root = _required_path(settings, "autopts_root")
    if zephyr_root == autopts_root or zephyr_root in autopts_root.parents:
        raise SetupError("Zephyr and AutoPTS destinations must be independent")
    _setup_zephyr_sources(pins.zephyr, zephyr_root, update=update)
    _setup_autopts(pins.autopts, autopts_root)


def _validate_sdk(sdk_root: Path, version: str) -> None:
    version_file = sdk_root / "sdk_version"
    if not version_file.exists():
        raise SetupError(f"Zephyr SDK installation is incomplete: {version_file} is missing")
    installed_version = version_file.read_text(encoding="utf-8").strip()
    if installed_version != version:
        raise SetupError(
            f"Zephyr SDK version mismatch: expected {version}, found {installed_version}"
        )
    executable = "arm-zephyr-eabi-gcc.exe" if os.name == "nt" else "arm-zephyr-eabi-gcc"
    compiler = sdk_root / "gnu" / "arm-zephyr-eabi" / "bin" / executable
    if not compiler.exists():
        raise SetupError(f"Zephyr ARM GNU toolchain is missing: {compiler}")


def setup_sdk(pins: UpstreamPins, settings: dict[str, ResolvedValue]) -> None:
    zephyr_root = _required_path(settings, "zephyr_root")
    sdk_root = _required_path(settings, "zephyr_sdk_root")
    if not zephyr_root.exists():
        raise SetupError("Zephyr sources are not installed; run setup-upstream-sources first")
    if sdk_root.exists():
        _validate_sdk(sdk_root, pins.zephyr.sdk_version)
        print(f"Zephyr SDK already installed: {sdk_root}")
        return
    sdk_root.parent.mkdir(parents=True, exist_ok=True)
    command = [
        "west",
        "sdk",
        "install",
        "--version",
        pins.zephyr.sdk_version,
        "--install-dir",
        str(sdk_root),
        "--gnu-toolchains",
        *pins.zephyr.sdk_gnu_toolchains,
    ]
    _run(command, cwd=zephyr_root)
    _validate_sdk(sdk_root, pins.zephyr.sdk_version)


def _path_state(path: Path) -> str:
    if not path.exists():
        return "missing"
    if path.is_dir():
        return "directory"
    return "unexpected-file"


def print_status(pins: UpstreamPins, settings: dict[str, ResolvedValue]) -> None:
    zephyr_root = _required_path(settings, "zephyr_root")
    sdk_root = _required_path(settings, "zephyr_sdk_root")
    autopts_root = _required_path(settings, "autopts_root")
    print(f"Zephyr: {pins.zephyr.tag} ({pins.zephyr.commit})")
    print(f"  path: {zephyr_root} [{_path_state(zephyr_root)}]")
    print(f"Zephyr SDK: {pins.zephyr.sdk_version}")
    print(f"  path: {sdk_root} [{_path_state(sdk_root)}]")
    print(f"AutoPTS: {pins.autopts.commit}")
    print(f"  path: {autopts_root} [{_path_state(autopts_root)}]")


def _validate_west_modules(workspace_root: Path) -> int:
    listing = _capture(
        ["west", "list", "-f", "{name}|{abspath}|{revision}"],
        cwd=workspace_root,
    )
    count = 0
    for line in listing.splitlines():
        parts = line.split("|", maxsplit=2)
        if len(parts) != 3:
            raise SetupError(f"unexpected west list entry: {line}")
        name, path_text, revision = parts
        repository_path = Path(path_text)
        expected = _capture(
            ["git", "rev-parse", f"{revision}^{{commit}}"],
            cwd=repository_path,
        )
        head = _capture(["git", "rev-parse", "HEAD"], cwd=repository_path)
        if head != expected:
            raise SetupError(
                f"West project {name} revision mismatch: expected {expected}, found {head}"
            )
        dirty = _capture(["git", "status", "--porcelain"], cwd=repository_path)
        if dirty:
            raise SetupError(f"West project {name} contains local changes: {repository_path}")
        count += 1
    if count == 0:
        raise SetupError("west list returned no active projects")
    return count


def verify_upstream(pins: UpstreamPins, settings: dict[str, ResolvedValue]) -> None:
    _require_commands()
    zephyr_root = _required_path(settings, "zephyr_root")
    sdk_root = _required_path(settings, "zephyr_sdk_root")
    autopts_root = _required_path(settings, "autopts_root")
    marker = zephyr_root.parent / SOURCES_READY_MARKER
    _validate_repository(zephyr_root, pins.zephyr.repository, pins.zephyr.commit, "Zephyr")
    if not marker.exists():
        raise SetupError(f"Zephyr west modules completion marker is missing: {marker}")
    module_count = _validate_west_modules(zephyr_root.parent)
    _validate_repository(autopts_root, pins.autopts.repository, pins.autopts.commit, "AutoPTS")
    _validate_sdk(sdk_root, pins.zephyr.sdk_version)
    print(f"Pinned upstream sources ({module_count} West projects) and Zephyr ARM SDK verified")


class SetupArguments(argparse.Namespace):
    command: str = ""
    config: str | None = None
    update: bool = False


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Install pinned nrftest upstream toolchains")
    _ = parser.add_argument("--config", help="path to the machine-local TOML configuration")
    subparsers = parser.add_subparsers(dest="command", required=True)
    _ = subparsers.add_parser("status", help="show pinned revisions and destination state")
    _ = subparsers.add_parser("verify", help="verify exact sources and SDK without modifying them")
    sources = subparsers.add_parser("sources", help="install pinned Zephyr and AutoPTS sources")
    _ = sources.add_argument(
        "--update",
        action="store_true",
        help="rerun west update for an existing clean pinned workspace",
    )
    _ = subparsers.add_parser("sdk", help="install the pinned Zephyr SDK and ARM toolchain")
    complete = subparsers.add_parser("all", help="install pinned sources and SDK")
    _ = complete.add_argument(
        "--update",
        action="store_true",
        help="rerun west update for an existing clean pinned workspace",
    )
    return parser


def main() -> int:
    arguments = SetupArguments()
    _ = _parser().parse_args(namespace=arguments)
    try:
        pins = load_upstream_pins()
        settings = resolve_current_settings(config_path=arguments.config)
        if arguments.command == "status":
            print_status(pins, settings)
        elif arguments.command == "verify":
            verify_upstream(pins, settings)
        elif arguments.command == "sources":
            setup_sources(pins, settings, update=arguments.update)
        elif arguments.command == "sdk":
            setup_sdk(pins, settings)
        else:
            setup_sources(pins, settings, update=arguments.update)
            setup_sdk(pins, settings)
    except SetupError as error:
        print(f"setup error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
