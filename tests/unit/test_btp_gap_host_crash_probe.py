from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import cast

from host.nrftest.autopts_adapter import CapabilitySnapshot, SerialIdentity
from tools.btp_gap_host_crash_probe import (
    _child_command,  # pyright: ignore[reportPrivateUsage]
    _write_report,  # pyright: ignore[reportPrivateUsage]
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


def test_child_command_uses_the_current_pixi_python_without_a_shell() -> None:
    command = _child_command(
        port="COM13",
        local_name="NrftestCrashRecovery",
        run_id="run-1",
        config_path="machine.toml",
    )

    assert command[:4] == [sys.executable, "-u", "-m", "tools.btp_gap_host_crash_probe"]
    assert command[command.index("--port") + 1] == "COM13"
    assert command[command.index("--config") + 1] == "machine.toml"


def test_crash_report_does_not_claim_a_target_reset(tmp_path: Path) -> None:
    report = _write_report(
        tmp_path,
        run_id="run-1",
        started_at="2026-09-08T00:00:00+00:00",
        outcome="pass",
        detail=None,
        identity=_identity(),
        autopts_commit="54e81c7f3495bce72e5f688e9c996b85b8272799",
        local_name="NrftestCrashRecovery",
        child_command=[sys.executable, "-m", "tools.btp_gap_host_crash_probe", "--child"],
        child_pid=123,
        child_returncode=1,
        child_ready={"service_actions": {"GAP": "attached"}},
        child_output=["NRFTEST_CRASH_CHILD_READY {}"],
        serial_release={"outcome": "released", "attempts": 2, "elapsed_seconds": 0.2},
        recovery_capabilities=CapabilitySnapshot(
            supported_services_mask=0x2000000F,
            supported_services=("CORE", "GAP", "GATT"),
            supported_command_masks={"CORE": 0x1E, "GAP": 0xEF7FE1F7FFF6E},
            service_actions={"GAP": "attached"},
        ),
        recovery_steps=[{"name": "stop-orphaned-advertising"}],
        cleanup_errors=[],
    )

    document = cast(dict[str, object], json.loads(report.read_text(encoding="utf-8")))
    child = cast(dict[str, object], document["child"])
    assert child["termination"] == "subprocess.kill"
    boundary = cast(dict[str, object], document["reset_boundary"])
    assert boundary["commanded_target_reset"] is False
    assert boundary["commanded_power_cycle"] is False
    assert boundary["service_unregistered"] is False
    capabilities = cast(dict[str, object], document["recovery_capabilities"])
    assert capabilities["service_actions"] == {"GAP": "attached"}
