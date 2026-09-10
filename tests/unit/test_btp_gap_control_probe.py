from __future__ import annotations

import json

import pytest

from host.nrftest.autopts_adapter import CapabilitySnapshot, GapSnapshot, SerialIdentity
from tools.btp_gap_control_probe import (
    BtpGapControlProbeError,
    _require_settings,  # pyright: ignore[reportPrivateUsage]
    _write_report,  # pyright: ignore[reportPrivateUsage]
)


def _gap_snapshot(*, advertising: bool) -> GapSnapshot:
    return GapSnapshot(
        controller_index=0,
        address="AABBCCDDEEFF",
        address_type=1,
        address_type_name="random",
        current_settings={
            "Powered": True,
            "Connectable": True,
            "Discoverable": True,
            "Advertising": advertising,
        },
        connections=(),
    )


def test_settings_mismatch_is_a_control_failure() -> None:
    with pytest.raises(BtpGapControlProbeError, match="Advertising"):
        _require_settings(_gap_snapshot(advertising=False), Advertising=True)


def test_gap_control_report_does_not_claim_rf_observation(tmp_path) -> None:
    report = _write_report(
        tmp_path,
        started_at="2026-09-08T00:00:00+00:00",
        outcome="pass",
        detail=None,
        identity=SerialIdentity(
            port="COM13",
            description="USB serial",
            hwid="USB VID:PID=2FE3:0004",
            vid=0x2FE3,
            pid=0x0004,
            serial_number="fixture-1",
        ),
        autopts_commit="54e81c7f3495bce72e5f688e9c996b85b8272799",
        local_name="NrftestP1",
        capabilities=CapabilitySnapshot(
            supported_services_mask=0x2000000F,
            supported_services=("CORE", "GAP", "GATT"),
            supported_command_masks={"CORE": 0x1E, "GAP": 0xEF7FE1F7FFF6E},
        ),
        steps=[],
        cleanup_errors=[],
        cleanup_classification="clean",
    )

    document = json.loads(report.read_text(encoding="utf-8"))
    assert document["scope"] == "BTP Core/GAP control only; no independent BLE RF observation"
    assert document["controller_selection"] == {
        "controller_index_list": (
            "not executed: fixed AutoPTS revision defines the opcode but exposes no wrapper"
        ),
        "index": 0,
        "source": "fixed AutoPTS CONTROLLER_INDEX",
    }
