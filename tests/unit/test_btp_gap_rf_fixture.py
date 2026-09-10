from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest

from host.nrftest.autopts_adapter import (
    CapabilitySnapshot,
    ConnectionSnapshot,
    GapSnapshot,
    SerialIdentity,
)
from tools.btp_gap_rf_fixture import (
    BtpGapRfFixtureError,
    _ready_document,  # pyright: ignore[reportPrivateUsage]
    _write_report,  # pyright: ignore[reportPrivateUsage]
    canonical_uuid16,
    normalize_uuid16,
)


def _identity() -> SerialIdentity:
    return SerialIdentity(
        port="COM13",
        description="USB serial",
        hwid="USB VID:PID=2FE3:0004",
        vid=0x2FE3,
        pid=0x0004,
        serial_number="fixture-1",
    )


def _snapshot() -> GapSnapshot:
    return GapSnapshot(
        controller_index=0,
        address="AABBCCDDEEFF",
        address_type=1,
        address_type_name="random",
        current_settings={
            "Powered": True,
            "Connectable": True,
            "Discoverable": True,
            "Advertising": True,
        },
        connections=(
            ConnectionSnapshot(
                address="112233445566",
                address_type=1,
                address_type_name="random",
                security_level=1,
            ),
        ),
    )


def test_uuid16_selector_is_normalized_without_owning_btp_encoding() -> None:
    assert normalize_uuid16(" 0xFDF0 ") == "fdf0"
    assert canonical_uuid16("FDF0") == "0000fdf0-0000-1000-8000-00805f9b34fb"

    for invalid in ("fdf", "fdf00", "zzzz"):
        with pytest.raises(BtpGapRfFixtureError, match="four hex digits"):
            _ = normalize_uuid16(invalid)


def test_ready_marker_only_tells_an_external_runner_to_start_the_central() -> None:
    document = _ready_document(
        run_id="run-1",
        identity=_identity(),
        local_name="NrftestP1",
        service_uuid16="fdf0",
        controller=_snapshot(),
    )

    assert document["next_action"] == "start the independent Central/DUT process now"
    assert document["service_uuid"] == "0000fdf0-0000-1000-8000-00805f9b34fb"
    assert set(document) == {
        "run_id",
        "port",
        "local_name",
        "service_uuid16",
        "service_uuid",
        "peripheral_address",
        "next_action",
    }


def test_report_contains_only_peripheral_facts(tmp_path: Path) -> None:
    report = _write_report(
        tmp_path,
        run_id="run-1",
        started_at="2026-09-08T00:00:00+00:00",
        outcome="peripheral-pass",
        detail=None,
        identity=_identity(),
        autopts_commit="54e81c7f3495bce72e5f688e9c996b85b8272799",
        local_name="NrftestP1",
        service_uuid16="fdf0",
        capabilities=CapabilitySnapshot(
            supported_services_mask=0x2000000F,
            supported_services=("CORE", "GAP", "GATT"),
            supported_command_masks={"CORE": 0x1E, "GAP": 0xEF7FE1F7FFF6E},
        ),
        steps=[{"name": "peripheral-observed-connected", "gap": {}}],
        cleanup_errors=[],
        cleanup_classification="clean",
    )

    document = cast(dict[str, object], json.loads(report.read_text(encoding="utf-8")))
    assert "dut" not in document
    assert "root" not in document
    assert document["fact_boundary"] == {
        "included": "BTP responses and nRF GAP connection/disconnection events",
        "excluded": "Central/DUT process execution, events, result, and pass/fail judgment",
        "correlation": "use run_id and timestamps in an external HIL runner or report",
    }
    advertising = cast(dict[str, object], document["advertising"])
    assert advertising["selector_scope"] == "advertising only; no dynamic GATT service"
