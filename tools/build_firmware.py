from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from intelhex import IntelHex  # pyright: ignore[reportMissingTypeStubs]

from tools.config import PROJECT_ROOT, ConfigError, ResolvedValue, resolve_current_settings
from tools.setup_host_tools import load_dtc_pin, managed_dtc_path, verify_host_tools
from tools.setup_upstream import load_upstream_pins, verify_upstream

BOARD = "nrf52840dongle/nrf52840"
APPLICATION_RELATIVE_PATH = Path("tests/bluetooth/tester")
CONFIG_PATH = PROJECT_ROOT / "firmware" / "app" / "pca10059.conf"
OVERLAY_PATH = PROJECT_ROOT / "firmware" / "app" / "pca10059.overlay"

OUTPUT_DIRECTORY_NAME = "pca10059-tester"
NRF52840_FLASH_END = 0x100000
APPLICATION_START = 0x1000
BOOTLOADER_START = 0xE0000

REQUIRED_CONFIG = {
    "CONFIG_BOARD_HAS_NRF5_BOOTLOADER": "y",
    "CONFIG_BOOTLOADER_MCUBOOT": "n",
    "CONFIG_USE_DT_CODE_PARTITION": "n",
    "CONFIG_FLASH_LOAD_OFFSET": "0x1000",
    "CONFIG_UART_PIPE": "y",
    "CONFIG_UART_CONSOLE": "n",
    "CONFIG_HWINFO": "y",
    "CONFIG_BOOT_BANNER": "n",
    "CONFIG_TEST_LOGGING_DEFAULTS": "n",
    "CONFIG_LOG": "n",
    "CONFIG_BT": "y",
    "CONFIG_BT_PERIPHERAL": "y",
    "CONFIG_BT_GATT_DYNAMIC_DB": "y",
    "CONFIG_BT_HCI": "y",
    "CONFIG_BT_HCI_HOST": "y",
    "CONFIG_BT_LL_SW_SPLIT": "y",
    "CONFIG_HAS_BT_CTLR": "y",
    "CONFIG_BT_CTLR_HCI": "y",
}


class FirmwareBuildError(RuntimeError):
    """Raised when the pinned Tester firmware cannot be built or validated safely."""


def _required_path(settings: Mapping[str, ResolvedValue], name: str) -> Path:
    value = settings[name].value
    if value is None:
        raise FirmwareBuildError(
            f"{name} is not configured; set it in nrftest.local.toml or its NRFTEST_* variable"
        )
    return Path(value)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
    except OSError as error:
        raise FirmwareBuildError(f"unable to hash {path}: {error}") from error
    return digest.hexdigest()


def parse_dotconfig(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise FirmwareBuildError(f"unable to read generated Kconfig at {path}: {error}") from error
    for line in lines:
        if line.startswith("CONFIG_") and "=" in line:
            name, value = line.split("=", maxsplit=1)
            values[name] = value
        elif line.startswith("# CONFIG_") and line.endswith(" is not set"):
            values[line.removeprefix("# ").removesuffix(" is not set")] = "n"
    return values


def validate_required_config(values: Mapping[str, str]) -> None:
    mismatches = [
        f"{name}: expected {expected}, found {values.get(name, '<missing>')}"
        for name, expected in REQUIRED_CONFIG.items()
        if values.get(name, "n") != expected
    ]
    if mismatches:
        raise FirmwareBuildError(
            "generated Kconfig violates firmware invariants:\n- " + "\n- ".join(mismatches)
        )


def flash_segments_from_hex(path: Path) -> list[tuple[int, int]]:
    try:
        image = IntelHex(str(path))
    except (OSError, ValueError) as error:
        raise FirmwareBuildError(
            f"unable to parse generated Intel HEX at {path}: {error}"
        ) from error
    segments = cast(list[tuple[int, int]], image.segments())
    return [(int(start), int(end)) for start, end in segments]


def validate_flash_segments(segments: Sequence[tuple[int, int]]) -> list[tuple[int, int]]:
    flash_segments = [
        (start, end) for start, end in segments if start < NRF52840_FLASH_END and end > 0
    ]
    if not flash_segments:
        raise FirmwareBuildError("generated HEX contains no nRF52840 internal flash data")
    for start, end in flash_segments:
        if start < APPLICATION_START:
            raise FirmwareBuildError(
                f"generated HEX overlaps the MBR-reserved range: 0x{start:x}-0x{end:x}"
            )
        if end > BOOTLOADER_START:
            raise FirmwareBuildError(
                f"generated HEX overlaps the onboard bootloader range: 0x{start:x}-0x{end:x}"
            )
    return flash_segments


def _display_command(command: Sequence[str]) -> str:
    return subprocess.list2cmdline(command) if os.name == "nt" else " ".join(command)


def _run_build(command: list[str], *, cwd: Path, environment: Mapping[str, str]) -> None:
    print(f"+ {_display_command(command)}")
    try:
        _ = subprocess.run(command, cwd=cwd, env=environment, check=True)
    except (OSError, subprocess.CalledProcessError) as error:
        raise FirmwareBuildError(f"firmware build failed: {error}") from error


def _build_environment(
    settings: Mapping[str, ResolvedValue], zephyr_root: Path, sdk_root: Path
) -> dict[str, str]:
    environment = dict(os.environ)
    environment["ZEPHYR_BASE"] = str(zephyr_root)
    environment["ZEPHYR_TOOLCHAIN_VARIANT"] = "zephyr"
    environment["ZEPHYR_SDK_INSTALL_DIR"] = str(sdk_root)
    dtc = managed_dtc_path(load_dtc_pin(), settings)
    if not dtc.is_file():
        raise FirmwareBuildError("dtc is unavailable; run pixi run just setup-host-tools")
    environment["PATH"] = str(dtc.parent) + os.pathsep + environment.get("PATH", "")
    return environment


def _artifact_entry(path: Path, build_root: Path) -> dict[str, object]:
    return {
        "path": path.relative_to(build_root).as_posix(),
        "size": path.stat().st_size,
        "sha256": sha256_file(path),
    }


def _write_manifest(
    build_root: Path,
    flash_segments: Sequence[tuple[int, int]],
    generated_config: Mapping[str, str],
) -> Path:
    pins = load_upstream_pins()
    zephyr_output = build_root / "zephyr"
    artifacts: list[dict[str, object]] = []
    for name in ("zephyr.elf", "zephyr.hex", "zephyr.bin"):
        path = zephyr_output / name
        if not path.is_file():
            raise FirmwareBuildError(f"expected build artifact is missing: {path}")
        artifacts.append(_artifact_entry(path, build_root))
    manifest = {
        "schema_version": 1,
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "board": BOARD,
        "application": APPLICATION_RELATIVE_PATH.as_posix(),
        "zephyr": {
            "repository": pins.zephyr.repository,
            "commit": pins.zephyr.commit,
            "sdk_version": pins.zephyr.sdk_version,
            "gnu_toolchains": list(pins.zephyr.sdk_gnu_toolchains),
        },
        "inputs": {
            "config": {
                "path": CONFIG_PATH.relative_to(PROJECT_ROOT).as_posix(),
                "sha256": sha256_file(CONFIG_PATH),
            },
            "overlay": {
                "path": OVERLAY_PATH.relative_to(PROJECT_ROOT).as_posix(),
                "sha256": sha256_file(OVERLAY_PATH),
            },
            # Retained for schema compatibility: local patches are no longer supported.
            "tester_patch": None,
        },
        "validated_config": {name: generated_config.get(name, "n") for name in REQUIRED_CONFIG},
        "nrf52840_flash_segments": [
            {"start": f"0x{start:x}", "end_exclusive": f"0x{end:x}"}
            for start, end in flash_segments
        ],
        "artifacts": artifacts,
        "license": {
            "upstream_tester": "Apache-2.0",
            "notice": (
                "Built directly from the locked Zephyr repository and commit; "
                + "no local Tester patch is applied and no Tester source is vendored."
            ),
        },
    }
    path = build_root / "nrftest-build-manifest.json"
    _ = path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def build_firmware(settings: dict[str, ResolvedValue]) -> None:
    pins = load_upstream_pins()
    verify_upstream(pins, settings)
    verify_host_tools(load_dtc_pin(), settings)

    zephyr_root = _required_path(settings, "zephyr_root")
    sdk_root = _required_path(settings, "zephyr_sdk_root")
    build_root = _required_path(settings, "build_dir") / OUTPUT_DIRECTORY_NAME
    upstream_application = zephyr_root / APPLICATION_RELATIVE_PATH
    for required in (upstream_application, CONFIG_PATH, OVERLAY_PATH):
        if not required.exists():
            raise FirmwareBuildError(f"required firmware input does not exist: {required}")
    build_root.parent.mkdir(parents=True, exist_ok=True)

    command = [
        "west",
        "build",
        "--pristine=always",
        "--board",
        BOARD,
        "--build-dir",
        str(build_root),
        str(upstream_application),
        "--",
        f"-DEXTRA_CONF_FILE={CONFIG_PATH.as_posix()}",
        f"-DDTC_OVERLAY_FILE={OVERLAY_PATH.as_posix()}",
    ]
    _run_build(
        command,
        cwd=zephyr_root.parent,
        environment=_build_environment(settings, zephyr_root, sdk_root),
    )

    generated_config = parse_dotconfig(build_root / "zephyr" / ".config")
    validate_required_config(generated_config)
    all_segments = flash_segments_from_hex(build_root / "zephyr" / "zephyr.hex")
    flash_segments = validate_flash_segments(all_segments)
    manifest = _write_manifest(
        build_root,
        flash_segments,
        generated_config,
    )
    print(f"Firmware verified: {build_root / 'zephyr' / 'zephyr.hex'}")
    print(f"Build manifest: {manifest}")


class FirmwareArguments(argparse.Namespace):
    config: str | None = None
    build_dir: str | None = None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build the pinned Zephyr Tester for PCA10059")
    _ = parser.add_argument("--config", help="path to the machine-local TOML configuration")
    _ = parser.add_argument("--build-dir", help="firmware build root override")
    return parser


def main() -> int:
    arguments = FirmwareArguments()
    _ = _parser().parse_args(namespace=arguments)
    try:
        build_firmware(
            resolve_current_settings(
                {"build_dir": arguments.build_dir},
                config_path=arguments.config,
            )
        )
    except (ConfigError, FirmwareBuildError) as error:
        print(f"firmware build error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
