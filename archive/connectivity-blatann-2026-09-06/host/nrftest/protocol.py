from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping


PROTOCOL_VERSION = 1


class ProtocolError(ValueError):
    """Raised when an NDJSON control message is malformed."""


@dataclass(frozen=True)
class Command:
    request_id: str
    operation: str
    payload: Mapping[str, Any]


def parse_hex(value: Any, field: str = "bytes") -> bytes:
    if not isinstance(value, str):
        raise ProtocolError(f"{field} must be a hexadecimal string")
    try:
        return bytes.fromhex(value)
    except ValueError as error:
        raise ProtocolError(f"{field} must contain an even number of hex digits") from error


def parse_command(line: str) -> Command:
    try:
        raw = json.loads(line)
    except json.JSONDecodeError as error:
        raise ProtocolError(f"invalid JSON: {error.msg}") from error
    if not isinstance(raw, dict):
        raise ProtocolError("command must be a JSON object")

    version = raw.get("protocol_version", PROTOCOL_VERSION)
    if version != PROTOCOL_VERSION:
        raise ProtocolError(f"unsupported protocol_version: {version}")
    request_id = raw.get("request_id")
    if not isinstance(request_id, str) or not request_id:
        raise ProtocolError("request_id must be a non-empty string")
    operation = raw.get("op")
    if not isinstance(operation, str) or not operation:
        raise ProtocolError("op must be a non-empty string")
    return Command(request_id, operation, raw)


def required_string(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ProtocolError(f"{key} must be a non-empty string")
    return value


def required_int(payload: Mapping[str, Any], key: str, *, minimum: int | None = None) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProtocolError(f"{key} must be an integer")
    if minimum is not None and value < minimum:
        raise ProtocolError(f"{key} must be greater than or equal to {minimum}")
    return value


def optional_int(
    payload: Mapping[str, Any],
    key: str,
    *,
    default: int,
    minimum: int | None = None,
) -> int:
    if key not in payload:
        return default
    return required_int(payload, key, minimum=minimum)


def encode_message(message: Mapping[str, Any]) -> str:
    return json.dumps(message, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
