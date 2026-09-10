from __future__ import annotations

import json
from pathlib import Path
from typing import cast

from host.nrftest.autopts_adapter import SerialIdentity, ServiceStartupMode
from tools.btp_gap_transport_reuse_probe import (
    _write_report,  # pyright: ignore[reportPrivateUsage]
)


def test_report_marks_transport_reuse_without_claiming_a_device_reset(tmp_path: Path) -> None:
    report = _write_report(
        tmp_path,
        run_id="run-1",
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
        local_name="NrftestResidentGap",
        requested_cycles=2,
        settle_seconds=0.5,
        initial_mode=ServiceStartupMode.REGISTER,
        cycles=[
            {"cycle": 1, "service_startup_mode": "register", "outcome": "pass"},
            {"cycle": 2, "service_startup_mode": "attach", "outcome": "pass"},
        ],
    )

    document = cast(dict[str, object], json.loads(report.read_text(encoding="utf-8")))
    assert document["completed_cycles"] == 2
    lifecycle = cast(dict[str, object], document["lifecycle_boundary"])
    assert lifecycle["host_transport_rebuilt_each_cycle"] is True
    assert lifecycle["gap_service_registered_by_this_run"] is True
    assert lifecycle["gap_service_expected_resident_at_start"] is False
    assert lifecycle["gap_service_unregistered"] is False
    assert lifecycle["bluetooth_power_disabled"] is False
    assert lifecycle["commanded_reset"] is False
    assert lifecycle["commanded_power_cycle"] is False
    assert lifecycle["normal_exit_leaves_gap_service_resident"] is True
    assert "firmware exposes no boot ID" in str(lifecycle["proof_limit"])
