from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

from host.nrftest.telemetry import TelemetryError, write_json_report


def test_json_report_is_utf8_newline_terminated_and_atomically_named(tmp_path: Path) -> None:
    report = write_json_report(
        tmp_path,
        "host-doctor",
        {"outcome": "pass", "label": "蓝牙"},
        now=datetime(2026, 9, 10, 1, 2, 3, tzinfo=UTC),
    )

    assert report.parent == tmp_path / "host-doctor"
    assert report.name.startswith("20260910T010203.000000Z-")
    assert report.suffix == ".json"
    assert report.read_bytes().endswith(b"\n")
    assert json.loads(report.read_text(encoding="utf-8")) == {
        "label": "蓝牙",
        "outcome": "pass",
    }
    assert list(report.parent.glob("*.tmp")) == []


def test_json_reports_at_the_same_time_do_not_overwrite_each_other(tmp_path: Path) -> None:
    now = datetime(2026, 9, 10, 1, 2, 3, tzinfo=UTC)

    first = write_json_report(tmp_path, "host-doctor", {"run": 1}, now=now)
    second = write_json_report(tmp_path, "host-doctor", {"run": 2}, now=now)

    assert first != second
    assert json.loads(first.read_text(encoding="utf-8")) == {"run": 1}
    assert json.loads(second.read_text(encoding="utf-8")) == {"run": 2}


def test_json_report_redacts_raw_bluetooth_addresses_by_default(tmp_path: Path) -> None:
    document: dict[str, object] = {
        "controller": {"address": "AABBCCDDEEFF", "address_type": 1},
        "connections": [{"peer_address": "112233445566"}],
        "peripheral_address": "FFEEDDCCBBAA",
        "device_serial": "hardware-serial",
    }

    redacted = write_json_report(tmp_path, "host-doctor", document)
    raw = write_json_report(
        tmp_path,
        "host-doctor",
        document,
        include_raw_bluetooth_addresses=True,
    )

    redacted_document = cast(dict[str, object], json.loads(redacted.read_text(encoding="utf-8")))
    raw_document = cast(dict[str, object], json.loads(raw.read_text(encoding="utf-8")))
    controller = cast(dict[str, object], redacted_document["controller"])
    connections = cast(list[dict[str, object]], redacted_document["connections"])
    assert controller["address"] == "<redacted>"
    assert connections[0]["peer_address"] == "<redacted>"
    assert redacted_document["peripheral_address"] == "<redacted>"
    assert redacted_document["device_serial"] == "hardware-serial"
    assert raw_document == document


def test_json_report_rejects_unsafe_scenario_and_naive_timestamp(tmp_path: Path) -> None:
    with pytest.raises(TelemetryError, match="scenario"):
        _ = write_json_report(tmp_path, "../escape", {})
    with pytest.raises(TelemetryError, match="timezone-aware"):
        _ = write_json_report(tmp_path, "host-doctor", {}, now=datetime(2026, 9, 10))
