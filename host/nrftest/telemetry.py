from __future__ import annotations

import json
import os
import re
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import uuid4

_SCENARIO_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_BLUETOOTH_ADDRESS_KEYS = {"address", "peer_address", "peripheral_address"}


class TelemetryError(RuntimeError):
    """Raised when a structured nrftest report cannot be written safely."""


def _redact_bluetooth_addresses(value: object) -> object:
    if isinstance(value, dict):
        document = cast(dict[object, object], value)
        return {
            key: (
                "<redacted>"
                if isinstance(key, str)
                and key in _BLUETOOTH_ADDRESS_KEYS
                and isinstance(item, str)
                and item
                else _redact_bluetooth_addresses(item)
            )
            for key, item in document.items()
        }
    if isinstance(value, list):
        return [_redact_bluetooth_addresses(item) for item in cast(list[object], value)]
    if isinstance(value, tuple):
        return [_redact_bluetooth_addresses(item) for item in cast(tuple[object, ...], value)]
    return value


def write_json_report(
    reports_root: Path,
    scenario: str,
    document: dict[str, object],
    *,
    now: datetime | None = None,
    include_raw_bluetooth_addresses: bool = False,
) -> Path:
    if _SCENARIO_PATTERN.fullmatch(scenario) is None:
        raise TelemetryError(
            "report scenario must contain lowercase alphanumeric dash-separated segments"
        )

    timestamp = now or datetime.now(UTC)
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise TelemetryError("report timestamp must be timezone-aware")

    report_dir = reports_root / scenario
    run_id = uuid4().hex
    timestamp_text = timestamp.astimezone(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    report_path = report_dir / f"{timestamp_text}-{run_id}.json"
    temporary_path = report_dir / f".{report_path.name}.tmp"
    serialized_document = (
        document if include_raw_bluetooth_addresses else _redact_bluetooth_addresses(document)
    )
    try:
        report_dir.mkdir(parents=True, exist_ok=True)
        with temporary_path.open("x", encoding="utf-8", newline="\n") as stream:
            _ = stream.write(json.dumps(serialized_document, indent=2, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        _ = temporary_path.replace(report_path)
    except (OSError, TypeError, ValueError) as error:
        with suppress(OSError):
            temporary_path.unlink(missing_ok=True)
        raise TelemetryError(f"unable to write report {report_path}: {error}") from error
    return report_path
