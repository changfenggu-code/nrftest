from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

from host.nrftest.autopts_adapter import (
    AutoPtsAdapterError,
    SerialIdentity,
    identity_document,
    select_application_port,
)
from host.nrftest.target_reset import (
    PyLinkTargetResetDriver,
    TargetResetError,
    TargetResetResult,
    reset_target_and_rediscover,
)
from host.nrftest.telemetry import TelemetryError, write_json_report
from tools.config import ConfigError, ResolvedValue, resolve_current_settings


class TargetResetProbeError(RuntimeError):
    """Raised when the configured target-reset recovery boundary fails."""


def _required_value(settings: dict[str, ResolvedValue], name: str) -> str:
    value = settings[name].value
    if value is None:
        raise TargetResetProbeError(
            f"{name} is not configured; set it in nrftest.local.toml or its NRFTEST_* variable"
        )
    return value


def _debugger_serial(settings: dict[str, ResolvedValue]) -> int:
    value = _required_value(settings, "debugger_serial")
    try:
        serial = int(value, 10)
    except ValueError as error:
        raise TargetResetProbeError("debugger_serial must be a decimal integer") from error
    if serial <= 0:
        raise TargetResetProbeError("debugger_serial must be a positive decimal integer")
    return serial


def _write_report(
    reports_root: Path,
    *,
    started_at: str,
    outcome: str,
    detail: str | None,
    before: SerialIdentity,
    result: TargetResetResult | None,
    jlink_library: Path,
    debugger_serial: int,
) -> Path:
    finished_at = datetime.now(UTC)
    document = {
        "schema_version": 1,
        "started_at_utc": started_at,
        "finished_at_utc": finished_at.isoformat(),
        "outcome": outcome,
        "detail": detail,
        "method": "external SEGGER J-Link over SWD",
        "wire_protocol_changed": False,
        "jlink": {
            "library": str(jlink_library),
            "debugger_serial": str(debugger_serial),
            "target_device": "NRF52840_XXAA",
        },
        "before": identity_document(before),
        "after": identity_document(result.after) if result is not None else None,
        "observations": (
            {
                "application_usb_disappearance_seconds": result.disappearance_seconds,
                "application_usb_reappearance_seconds": result.reappearance_seconds,
                "port_changed": result.before.port.casefold() != result.after.port.casefold(),
                "hardware_serial_preserved": (
                    result.before.serial_number == result.after.serial_number
                ),
            }
            if result is not None
            else None
        ),
        "proof_boundary": (
            "J-Link reset-and-halt forced the target MCU through reset; the native PCA10059 USB "
            "application identity disappeared while halted and reappeared with the same hardware "
            "serial after CPU release. BTP capability and Profile recovery are separate gates."
        ),
    }
    return write_json_report(reports_root, "target-reset", document, now=finished_at)


def run_probe(
    settings: dict[str, ResolvedValue],
    *,
    port_name: str | None = None,
    disappearance_timeout_seconds: float = 10.0,
    reappearance_timeout_seconds: float = 30.0,
) -> Path:
    selected_port = port_name or settings["device_port"].value
    configured_serial = settings["device_serial"].value
    try:
        before = select_application_port(
            port_name=selected_port,
            serial_number=configured_serial,
        )
    except AutoPtsAdapterError as error:
        raise TargetResetProbeError(str(error)) from error
    if before.serial_number is None:
        raise TargetResetProbeError("selected application device has no stable hardware serial")

    jlink_library = Path(_required_value(settings, "jlink_library"))
    debugger_serial = _debugger_serial(settings)
    reports_root = Path(_required_value(settings, "reports_dir"))
    started_at = datetime.now(UTC).isoformat()

    print(f"Selected application identity: {before.port} ({before.hwid})")
    print(f"Selected J-Link debugger serial: {debugger_serial}")
    print(f"Selected J-Link native library: {jlink_library}")
    print("Reset method: external J-Link/SWD; no BTP reset opcode or USB power-cycle")

    result: TargetResetResult | None = None
    try:
        driver = PyLinkTargetResetDriver(jlink_library, debugger_serial)
        result = reset_target_and_rediscover(
            driver,
            before,
            disappearance_timeout_seconds=disappearance_timeout_seconds,
            reappearance_timeout_seconds=reappearance_timeout_seconds,
        )
    except TargetResetError as error:
        report = _write_report(
            reports_root,
            started_at=started_at,
            outcome="reset-failure",
            detail=str(error),
            before=before,
            result=None,
            jlink_library=jlink_library,
            debugger_serial=debugger_serial,
        )
        raise TargetResetProbeError(f"target reset failed; report: {report}") from error

    report = _write_report(
        reports_root,
        started_at=started_at,
        outcome="target-reset-reenumeration-pass",
        detail=None,
        before=before,
        result=result,
        jlink_library=jlink_library,
        debugger_serial=debugger_serial,
    )
    print(
        "Target reset/re-enumeration passed: "
        + f"{result.before.port} -> {result.after.port}; "
        + f"disappearance={result.disappearance_seconds:.3f}s; "
        + f"reappearance={result.reappearance_seconds:.3f}s"
    )
    print(f"Target reset report: {report}")
    return report


class ResetArguments(argparse.Namespace):
    config: str | None = None
    port: str | None = None
    disappearance_timeout: float = 10.0
    reappearance_timeout: float = 30.0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Reset PCA10059 through an explicitly configured external J-Link/SWD probe"
    )
    _ = parser.add_argument("--config", help="path to the machine-local TOML configuration")
    port_help = "current application port; otherwise select by configured serial "
    port_help += "or unique USB identity"
    _ = parser.add_argument("--port", help=port_help)
    _ = parser.add_argument("--disappearance-timeout", type=float, default=10.0)
    _ = parser.add_argument("--reappearance-timeout", type=float, default=30.0)
    return parser


def main() -> int:
    arguments = ResetArguments()
    _ = _parser().parse_args(namespace=arguments)
    try:
        _ = run_probe(
            resolve_current_settings(config_path=arguments.config),
            port_name=arguments.port,
            disappearance_timeout_seconds=arguments.disappearance_timeout,
            reappearance_timeout_seconds=arguments.reappearance_timeout,
        )
    except (ConfigError, TargetResetError, TargetResetProbeError, TelemetryError) as error:
        print(f"target reset error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
