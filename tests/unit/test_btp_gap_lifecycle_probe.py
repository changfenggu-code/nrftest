from __future__ import annotations

import json
from pathlib import Path
from typing import cast

from host.nrftest.autopts_adapter import SerialIdentity
from tools.btp_gap_lifecycle_probe import (
    _write_report,  # pyright: ignore[reportPrivateUsage]
)


def test_lifecycle_report_separates_no_reset_inference_from_proof(tmp_path: Path) -> None:
    report = _write_report(
        tmp_path,
        run_id="run-1",
        started_at="2026-09-08T00:00:00+00:00",
        outcome="pass-with-known-upstream-unregister-status-defect",
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
        local_name="NrftestLifecycle",
        requested_cycles=2,
        settle_seconds=0.5,
        cycles=[
            {"cycle": 1, "outcome": "pass"},
            {"cycle": 2, "outcome": "pass"},
        ],
    )

    document = cast(dict[str, object], json.loads(report.read_text(encoding="utf-8")))
    assert document["completed_cycles"] == 2
    assert document["diagnostic_sequence"] == [
        "register GAP",
        "start and stop advertising",
        "BTP GAP SET_POWERED(false)",
        "BTP GAP SET_POWERED(true) in the same session",
        "start and stop advertising after re-enable",
        "BTP GAP SET_POWERED(false)",
        "BTP Core unregister GAP",
        "close and rebuild the AutoPTS transport",
    ]
    assert document["shutdown_sequence"] == [
        "stop advertising",
        "BTP GAP SET_POWERED(false)",
        "BTP Core unregister GAP",
        "close AutoPTS transport",
    ]
    reset_boundary = cast(dict[str, object], document["reset_boundary"])
    assert reset_boundary["commanded_reset"] is False
    assert reset_boundary["commanded_power_cycle"] is False
    assert "firmware exposes no boot ID" in str(reset_boundary["proof_limit"])
