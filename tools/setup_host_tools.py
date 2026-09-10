from __future__ import annotations

import argparse
import hashlib
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
import urllib.request
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, cast

from tools.config import PROJECT_ROOT, ConfigError, ResolvedValue, resolve_current_settings

LOCK_PATH = PROJECT_ROOT / "host-tools.lock.toml"


class HostToolError(RuntimeError):
    """Raised when a pinned host tool cannot be installed or verified safely."""


@dataclass(frozen=True)
class DtcPin:
    version: str
    minimum_version: str
    url: str
    archive: str
    sha256: str
    executable: Path


@dataclass(frozen=True)
class SocatPin:
    version: str
    minimum_version: str
    url: str
    archive: str
    sha256: str
    executable: Path


def _required_string(table: Mapping[str, object], key: str, location: str) -> str:
    value = table.get(key)
    if not isinstance(value, str) or not value.strip():
        raise HostToolError(f"{location}.{key} must be a non-empty string")
    return value.strip()


def load_dtc_pin(path: Path = LOCK_PATH) -> DtcPin:
    try:
        with path.open("rb") as stream:
            document = cast(dict[str, object], tomllib.load(stream))
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise HostToolError(f"unable to read host-tools lock {path}: {error}") from error
    if document.get("schema_version") != 1:
        raise HostToolError("host-tools lock schema_version must be 1")
    windows_value = document.get("windows")
    if not isinstance(windows_value, dict):
        raise HostToolError("host-tools lock must contain [windows.dtc]")
    windows = cast(dict[str, object], windows_value)
    dtc_value = windows.get("dtc")
    if not isinstance(dtc_value, dict):
        raise HostToolError("host-tools lock must contain [windows.dtc]")
    dtc = cast(dict[str, object], dtc_value)
    sha256 = _required_string(dtc, "sha256", "windows.dtc")
    if len(sha256) != 64 or any(character not in "0123456789abcdef" for character in sha256):
        raise HostToolError("windows.dtc.sha256 must be a lowercase SHA-256 digest")
    executable = Path(_required_string(dtc, "executable", "windows.dtc"))
    if executable.is_absolute() or ".." in executable.parts:
        raise HostToolError("windows.dtc.executable must stay inside the managed tool directory")
    return DtcPin(
        version=_required_string(dtc, "version", "windows.dtc"),
        minimum_version=_required_string(dtc, "minimum_version", "windows.dtc"),
        url=_required_string(dtc, "url", "windows.dtc"),
        archive=_required_string(dtc, "archive", "windows.dtc"),
        sha256=sha256,
        executable=executable,
    )


def load_socat_pin(path: Path = LOCK_PATH) -> SocatPin:
    try:
        with path.open("rb") as stream:
            document = cast(dict[str, object], tomllib.load(stream))
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise HostToolError(f"unable to read host-tools lock {path}: {error}") from error
    if document.get("schema_version") != 1:
        raise HostToolError("host-tools lock schema_version must be 1")
    windows_value = document.get("windows")
    if not isinstance(windows_value, dict):
        raise HostToolError("host-tools lock must contain [windows.socat]")
    windows = cast(dict[str, object], windows_value)
    socat_value = windows.get("socat")
    if not isinstance(socat_value, dict):
        raise HostToolError("host-tools lock must contain [windows.socat]")
    socat = cast(dict[str, object], socat_value)
    sha256 = _required_string(socat, "sha256", "windows.socat")
    if len(sha256) != 64 or any(character not in "0123456789abcdef" for character in sha256):
        raise HostToolError("windows.socat.sha256 must be a lowercase SHA-256 digest")
    executable = Path(_required_string(socat, "executable", "windows.socat"))
    if executable.is_absolute() or ".." in executable.parts:
        raise HostToolError("windows.socat.executable must stay inside the managed tool directory")
    return SocatPin(
        version=_required_string(socat, "version", "windows.socat"),
        minimum_version=_required_string(socat, "minimum_version", "windows.socat"),
        url=_required_string(socat, "url", "windows.socat"),
        archive=_required_string(socat, "archive", "windows.socat"),
        sha256=sha256,
        executable=executable,
    )


def _required_path(settings: Mapping[str, ResolvedValue], name: str) -> Path:
    value = settings[name].value
    if value is None:
        raise HostToolError(
            f"{name} is not configured; set it in nrftest.local.toml or its NRFTEST_* variable"
        )
    return Path(value)


def _version_tuple(value: str, tool: str) -> tuple[int, ...]:
    match = re.search(r"\d+(?:\.\d+)+", value)
    if match is None:
        raise HostToolError(f"unable to parse {tool} version from: {value}")
    return tuple(int(component) for component in match.group(0).split("."))


def _verify_dtc(executable: Path | str, minimum_version: str, expected_version: str | None) -> str:
    try:
        result = subprocess.run(
            [str(executable), "--version"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        raise HostToolError(f"unable to execute DTC at {executable}: {error}") from error
    output = (result.stdout or result.stderr).strip()
    installed = _version_tuple(output, "DTC")
    if installed < _version_tuple(minimum_version, "DTC"):
        raise HostToolError(f"DTC {output} is older than the required minimum {minimum_version}")
    if expected_version is not None and installed != _version_tuple(expected_version, "DTC"):
        raise HostToolError(
            f"managed DTC version mismatch: expected {expected_version}, found {output}"
        )
    return output


def _windows_paths(
    pin: DtcPin,
    settings: Mapping[str, ResolvedValue],
) -> tuple[Path, Path, Path]:
    host_tools_root = _required_path(settings, "host_tools_root")
    downloads_dir = _required_path(settings, "downloads_dir")
    install_root = host_tools_root / f"dtc-{pin.version}"
    return downloads_dir / pin.archive, install_root, install_root / pin.executable


def _windows_socat_paths(
    pin: SocatPin,
    settings: Mapping[str, ResolvedValue],
) -> tuple[Path, Path, Path]:
    host_tools_root = _required_path(settings, "host_tools_root")
    downloads_dir = _required_path(settings, "downloads_dir")
    install_root = host_tools_root / f"socat-{pin.version}"
    return downloads_dir / pin.archive, install_root, install_root / pin.executable


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
    except OSError as error:
        raise HostToolError(f"unable to hash {path}: {error}") from error
    return digest.hexdigest()


def _download(url: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_suffix(f"{destination.suffix}.part")
    if partial.exists():
        partial.unlink()
    request = urllib.request.Request(url, headers={"User-Agent": "nrftest-host-tool-setup"})
    try:
        response = cast(BinaryIO, urllib.request.urlopen(request, timeout=60))
        with response, partial.open("wb") as output:
            shutil.copyfileobj(response, output)
        _ = partial.replace(destination)
    except OSError as error:
        if partial.exists():
            partial.unlink()
        raise HostToolError(f"unable to download {url}: {error}") from error


def _safe_extract(archive: Path, destination: Path) -> None:
    try:
        with zipfile.ZipFile(archive) as package:
            destination_root = destination.resolve()
            for member in package.infolist():
                member_path = destination / Path(member.filename.replace("\\", "/"))
                if not member_path.resolve().is_relative_to(destination_root):
                    raise HostToolError(f"unsafe ZIP member: {member.filename}")
            package.extractall(destination)
    except (OSError, zipfile.BadZipFile) as error:
        raise HostToolError(f"unable to extract {archive}: {error}") from error


def _windows_setup(pin: DtcPin, settings: Mapping[str, ResolvedValue]) -> Path:
    archive, install_root, executable = _windows_paths(pin, settings)
    if install_root.exists():
        _ = _verify_dtc(executable, pin.minimum_version, pin.version)
        print(f"Managed Windows DTC already installed: {executable}")
        return executable
    if archive.exists():
        actual = _sha256(archive)
        if actual != pin.sha256:
            raise HostToolError(
                f"cached DTC archive SHA-256 mismatch: expected {pin.sha256}, found {actual}"
            )
    else:
        print(f"Downloading pinned Windows DTC {pin.version} to {archive}")
        _download(pin.url, archive)
        actual = _sha256(archive)
        if actual != pin.sha256:
            raise HostToolError(
                f"downloaded DTC archive SHA-256 mismatch: expected {pin.sha256}, found {actual}"
            )
    install_root.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=install_root.parent) as temporary:
        staging = Path(temporary) / "payload"
        staging.mkdir()
        _safe_extract(archive, staging)
        staged_executable = staging / pin.executable
        _ = _verify_dtc(staged_executable, pin.minimum_version, pin.version)
        _ = shutil.move(str(staging), str(install_root))
    _ = _verify_dtc(executable, pin.minimum_version, pin.version)
    print(f"Installed pinned Windows DTC: {executable}")
    return executable


def _verify_socat(
    executable: Path | str, minimum_version: str, expected_version: str | None
) -> str:
    try:
        result = subprocess.run(
            [str(executable), "-V"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        raise HostToolError(f"unable to execute socat at {executable}: {error}") from error
    output = (result.stdout or result.stderr).strip()
    installed = _version_tuple(output, "socat")
    if installed < _version_tuple(minimum_version, "socat"):
        raise HostToolError(
            f"socat {installed} is older than the required minimum {minimum_version}"
        )
    if expected_version is not None and installed != _version_tuple(expected_version, "socat"):
        raise HostToolError(
            f"managed socat version mismatch: expected {expected_version}, found {installed}"
        )
    return output.splitlines()[0]


def _windows_socat_setup(pin: SocatPin, settings: Mapping[str, ResolvedValue]) -> Path:
    archive, install_root, executable = _windows_socat_paths(pin, settings)
    if install_root.exists():
        _ = _verify_socat(executable, pin.minimum_version, pin.version)
        print(f"Managed Windows socat already installed: {executable}")
        return executable
    if archive.exists():
        actual = _sha256(archive)
        if actual != pin.sha256:
            raise HostToolError(
                f"cached socat archive SHA-256 mismatch: expected {pin.sha256}, found {actual}"
            )
    else:
        print(f"Downloading pinned Windows socat {pin.version} to {archive}")
        _download(pin.url, archive)
        actual = _sha256(archive)
        if actual != pin.sha256:
            raise HostToolError(
                f"downloaded socat archive SHA-256 mismatch: expected {pin.sha256}, found {actual}"
            )
    install_root.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=install_root.parent) as temporary:
        staging = Path(temporary) / "payload"
        staging.mkdir()
        _safe_extract(archive, staging)
        staged_executable = staging / pin.executable
        _ = _verify_socat(staged_executable, pin.minimum_version, pin.version)
        _ = shutil.move(str(staging), str(install_root))
    _ = _verify_socat(executable, pin.minimum_version, pin.version)
    print(f"Installed pinned Windows socat: {executable}")
    return executable


def _non_windows_dtc(pin: DtcPin) -> Path:
    executable = shutil.which("dtc")
    if executable is None:
        raise HostToolError("Pixi environment does not provide dtc on this platform")
    output = _verify_dtc(executable, pin.minimum_version, None)
    print(f"Pixi DTC verified: {executable} ({output})")
    return Path(executable)


def _non_windows_socat(pin: SocatPin, settings: Mapping[str, ResolvedValue]) -> Path:
    configured = settings["socat"].value or "socat"
    executable = configured if Path(configured).is_absolute() else shutil.which(configured)
    if executable is None:
        raise HostToolError("Pixi environment does not provide socat on this platform")
    output = _verify_socat(executable, pin.minimum_version, None)
    print(f"Pixi socat verified: {executable} ({output})")
    return Path(executable)


def managed_dtc_path(pin: DtcPin, settings: Mapping[str, ResolvedValue]) -> Path:
    if platform.system() == "Windows":
        _, _, executable = _windows_paths(pin, settings)
        return executable
    executable = shutil.which("dtc")
    return Path(executable) if executable is not None else Path("dtc")


def managed_socat_path(pin: SocatPin, settings: Mapping[str, ResolvedValue]) -> Path:
    if platform.system() == "Windows":
        _, _, executable = _windows_socat_paths(pin, settings)
        return executable
    configured = settings["socat"].value or "socat"
    executable = configured if Path(configured).is_absolute() else shutil.which(configured)
    return Path(executable) if executable is not None else Path(configured)


def _require_managed_windows_selection(
    name: str,
    settings: Mapping[str, ResolvedValue],
    executable: Path,
) -> None:
    configured = settings[name].value
    if configured is None:
        raise HostToolError(
            f"{name} is not configured; select the managed executable explicitly: {executable}"
        )
    if Path(configured).resolve() != executable.resolve():
        raise HostToolError(
            f"configured {name} does not select the managed install; set {name} to {executable}"
        )


def preflight_host_tools(
    dtc_pin: DtcPin,
    socat_pin: SocatPin,
    settings: Mapping[str, ResolvedValue],
) -> None:
    if platform.system() != "Windows":
        return
    _, _, dtc = _windows_paths(dtc_pin, settings)
    _, _, socat = _windows_socat_paths(socat_pin, settings)
    _require_managed_windows_selection("dtc", settings, dtc)
    _require_managed_windows_selection("socat", settings, socat)


def setup_host_tools(pin: DtcPin, settings: Mapping[str, ResolvedValue]) -> None:
    if platform.system() == "Windows":
        executable = _windows_setup(pin, settings)
        configured = settings["dtc"].value
        if configured is not None and Path(configured).resolve() != executable.resolve():
            raise HostToolError(
                f"configured DTC does not select the managed install; set dtc to {executable}"
            )
    else:
        _ = _non_windows_dtc(pin)


def setup_socat(pin: SocatPin, settings: Mapping[str, ResolvedValue]) -> None:
    if platform.system() == "Windows":
        executable = _windows_socat_setup(pin, settings)
        configured = settings["socat"].value
        if configured is not None and Path(configured).resolve() != executable.resolve():
            raise HostToolError(
                f"configured socat does not select the managed install; set socat to {executable}"
            )
    else:
        _ = _non_windows_socat(pin, settings)


def verify_host_tools(pin: DtcPin, settings: Mapping[str, ResolvedValue]) -> None:
    executable = managed_dtc_path(pin, settings)
    if platform.system() == "Windows":
        _require_managed_windows_selection("dtc", settings, executable)
    expected = pin.version if platform.system() == "Windows" else None
    output = _verify_dtc(executable, pin.minimum_version, expected)
    print(f"Host DTC verified: {executable} ({output})")


def verify_socat(pin: SocatPin, settings: Mapping[str, ResolvedValue]) -> None:
    executable = managed_socat_path(pin, settings)
    if platform.system() == "Windows":
        _require_managed_windows_selection("socat", settings, executable)
    expected = pin.version if platform.system() == "Windows" else None
    output = _verify_socat(executable, pin.minimum_version, expected)
    print(f"Host socat verified: {executable} ({output})")


def print_status(
    dtc_pin: DtcPin,
    socat_pin: SocatPin,
    settings: Mapping[str, ResolvedValue],
) -> None:
    dtc = managed_dtc_path(dtc_pin, settings)
    dtc_state = "installed" if dtc.exists() else "missing"
    socat = managed_socat_path(socat_pin, settings)
    socat_state = "installed" if socat.exists() else "missing"
    print(f"Host OS: {platform.system()} {platform.machine()}")
    print(f"DTC required minimum: {dtc_pin.minimum_version}")
    print(f"DTC managed Windows version: {dtc_pin.version}")
    print(f"DTC path: {dtc} [{dtc_state}]")
    print(f"socat required minimum: {socat_pin.minimum_version}")
    print(f"socat managed Windows version: {socat_pin.version}")
    print(f"socat path: {socat} [{socat_state}]")


class HostToolArguments(argparse.Namespace):
    command: str = ""
    config: str | None = None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Install pinned nrftest host build tools")
    _ = parser.add_argument("--config", help="path to the machine-local TOML configuration")
    subparsers = parser.add_subparsers(dest="command", required=True)
    _ = subparsers.add_parser("status", help="show platform-specific host-tool state")
    _ = subparsers.add_parser("setup", help="install or verify required host tools")
    _ = subparsers.add_parser("verify", help="verify required host tools without modifying them")
    return parser


def main() -> int:
    arguments = HostToolArguments()
    _ = _parser().parse_args(namespace=arguments)
    try:
        dtc_pin = load_dtc_pin()
        socat_pin = load_socat_pin()
        settings = resolve_current_settings(config_path=arguments.config)
        if arguments.command == "status":
            print_status(dtc_pin, socat_pin, settings)
        elif arguments.command == "setup":
            preflight_host_tools(dtc_pin, socat_pin, settings)
            setup_host_tools(dtc_pin, settings)
            setup_socat(socat_pin, settings)
        else:
            verify_host_tools(dtc_pin, settings)
            verify_socat(socat_pin, settings)
    except (ConfigError, HostToolError) as error:
        print(f"host-tool error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
