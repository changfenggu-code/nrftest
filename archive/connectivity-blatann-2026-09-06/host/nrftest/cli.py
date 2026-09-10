from __future__ import annotations

import argparse
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .connectivity import ConnectivityError, ConnectivitySession
from .payload import MIN_BURST_PAYLOAD_BYTES
from .peripheral import (
    NrfPeripheral,
    PeripheralCapabilityError,
    PeripheralEnvironmentError,
    PeripheralError,
)
from .profile import PeripheralProfile, ProfileError
from .protocol import (
    Command,
    ProtocolError,
    parse_command,
    parse_hex,
    required_int,
    required_string,
)
from .telemetry import JsonLineEmitter


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="PC-controlled nRF52840 Peripheral fixture")
    parser.add_argument("--port", required=True, help="Connectivity serial port, for example COM11")
    parser.add_argument("--baud", type=int, default=1_000_000)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("doctor", help="open the Connectivity session and report metadata")
    subparsers.add_parser("serve", help="read control commands from stdin and emit NDJSON")
    return parser


def run_doctor(args: argparse.Namespace, emitter: JsonLineEmitter) -> int:
    session = ConnectivitySession(args.port, args.baud)
    try:
        session.open()
        emitter.write({"type": "doctor", "ok": True, "result": session.metadata().as_dict()})
        return 0
    except ConnectivityError as error:
        emitter.write(
            {
                "type": "doctor",
                "ok": False,
                "error": {"category": "environment", "message": str(error)},
            }
        )
        return 2
    finally:
        try:
            session.close()
        except ConnectivityError:
            pass


def run_server(args: argparse.Namespace, emitter: JsonLineEmitter) -> int:
    peripheral = NrfPeripheral(args.port, emitter, args.baud)
    try:
        peripheral.open()
    except (ConnectivityError, PeripheralError) as error:
        emitter.write(
            {
                "type": "startup_error",
                "error": {"category": "environment", "message": str(error)},
            }
        )
        return 2

    try:
        for line in sys.stdin:
            if not line.strip():
                continue
            try:
                command = parse_command(line)
                result, should_stop = dispatch(peripheral, command)
                emitter.response(command.request_id, state=peripheral.state.value, result=result)
                if should_stop:
                    break
            except (
                ConnectivityError,
                ImportError,
                OSError,
                PeripheralCapabilityError,
                PeripheralEnvironmentError,
                PeripheralError,
                ProfileError,
                ProtocolError,
                ValueError,
            ) as error:
                request_id = _request_id_or_invalid(line)
                emitter.response(
                    request_id,
                    ok=False,
                    state=peripheral.state.value,
                    error={
                        "category": _error_category(error),
                        "message": str(error),
                    },
                )
    except KeyboardInterrupt:
        return 130
    finally:
        try:
            peripheral.close()
        except (ConnectivityError, PeripheralError) as error:
            emitter.telemetry("error", category="environment", message=str(error))
            return 2
    return 0


def dispatch(peripheral: NrfPeripheral, command: Command) -> tuple[Mapping[str, Any], bool]:
    payload = command.payload
    operation = command.operation
    if operation == "doctor":
        return {"status": peripheral.status()}, False
    if operation == "load_profile":
        profile_path = required_string(payload, "profile")
        run_id = required_string(payload, "run_id")
        peripheral.load_profile(PeripheralProfile.load(Path(profile_path)), run_id)
        return {"profile_id": peripheral.status()["profile_id"], "run_id": run_id}, False
    if operation == "start_advertising":
        peripheral.start_advertising()
        return {}, False
    if operation == "stop_advertising":
        peripheral.stop_advertising()
        return {}, False
    if operation == "set_value":
        peripheral.set_value(required_string(payload, "characteristic"), parse_hex(payload.get("bytes")))
        return {}, False
    if operation in {"emit_notification", "emit_indication"}:
        role = required_string(payload, "characteristic")
        value = parse_hex(payload.get("bytes"))
        peripheral.emit_notification(role, value, indication=operation == "emit_indication")
        return {}, False
    if operation in {"emit_notification_burst", "emit_indication_burst"}:
        burst_id = required_string(payload, "burst_id")
        count = required_int(payload, "count", minimum=1)
        interval_us = required_int(payload, "interval_us", minimum=0)
        payload_bytes = required_int(
            payload,
            "payload_bytes",
            minimum=MIN_BURST_PAYLOAD_BYTES,
        )
        mode = "indication" if operation == "emit_indication_burst" else "notification"
        peripheral.emit_burst(mode, burst_id, count, interval_us, payload_bytes)
        return {
            "burst_id": burst_id,
            "mode": mode,
            "count": count,
            "interval_us": interval_us,
            "payload_bytes": payload_bytes,
        }, False
    if operation == "force_disconnect":
        peripheral.force_disconnect()
        return {}, False
    if operation == "reset":
        peripheral.reset()
        return {}, False
    if operation == "status":
        return peripheral.status(), False
    if operation == "shutdown":
        return {}, True
    raise ProtocolError(f"unsupported operation: {operation}")


def _error_category(error: Exception) -> str:
    if isinstance(error, (ConnectivityError, ImportError, OSError, PeripheralEnvironmentError)):
        return "environment"
    if isinstance(error, PeripheralCapabilityError):
        return "capability_skip"
    if isinstance(error, (PeripheralError, ProfileError, ProtocolError, ValueError)):
        return "invalid_request"
    return "scenario"


def _request_id_or_invalid(line: str) -> str:
    try:
        command = parse_command(line)
    except ProtocolError:
        return "invalid"
    return command.request_id


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    emitter = JsonLineEmitter()
    if args.command == "doctor":
        return run_doctor(args, emitter)
    if args.command == "serve":
        return run_server(args, emitter)
    raise AssertionError(f"unhandled command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
