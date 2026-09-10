from __future__ import annotations

from dataclasses import dataclass

import pytest

from host.nrftest.autopts_adapter import (
    APPLICATION_USB_PID,
    APPLICATION_USB_VID,
    SerialIdentity,
    select_application_port,
)
from host.nrftest.target_reset import TargetResetError, reset_target_and_rediscover


@dataclass
class FakePort:
    device: str
    serial_number: str | None = "fixture-serial"
    vid: int | None = APPLICATION_USB_VID
    pid: int | None = APPLICATION_USB_PID
    description: str = "nrftest application"
    hwid: str = "USB fixture"


class FakeResetDriver:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def open(self) -> None:
        self.calls.append("open")

    def reset_and_halt(self) -> None:
        self.calls.append("reset-and-halt")

    def run(self) -> None:
        self.calls.append("run")

    def close(self) -> None:
        self.calls.append("close")


class OpenFailingDriver(FakeResetDriver):
    def open(self) -> None:
        super().open()
        raise RuntimeError("probe open failed")


class PortSequence:
    def __init__(self, snapshots: list[list[FakePort]]) -> None:
        self.snapshots = snapshots

    def __call__(self) -> list[FakePort]:
        if len(self.snapshots) > 1:
            return self.snapshots.pop(0)
        return self.snapshots[0]


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def monotonic(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.value += seconds


def test_target_reset_observes_disappearance_and_rediscovers_changed_port() -> None:
    before = select_application_port(ports=[FakePort("COM13")])
    driver = FakeResetDriver()
    ports = PortSequence(
        [
            [FakePort("COM13")],
            [],
            [],
            [FakePort("COM15")],
        ]
    )
    clock = FakeClock()

    result = reset_target_and_rediscover(
        driver,
        before,
        ports=ports,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )

    assert driver.calls == ["open", "reset-and-halt", "run", "close"]
    assert result.before.port == "COM13"
    assert result.after.port == "COM15"
    assert result.after.serial_number == "fixture-serial"
    assert result.disappearance_seconds == pytest.approx(0.05)
    assert result.reappearance_seconds == pytest.approx(0.05)


def test_disappearance_timeout_still_releases_and_closes_target() -> None:
    before = select_application_port(ports=[FakePort("COM13")])
    driver = FakeResetDriver()
    clock = FakeClock()

    with pytest.raises(TargetResetError, match="did not disappear"):
        _ = reset_target_and_rediscover(
            driver,
            before,
            disappearance_timeout_seconds=0.2,
            poll_interval_seconds=0.1,
            ports=lambda: [FakePort("COM13")],
            monotonic=clock.monotonic,
            sleep=clock.sleep,
        )

    assert driver.calls == ["open", "reset-and-halt", "run", "close"]


def test_open_failure_still_closes_partially_open_driver() -> None:
    before = select_application_port(ports=[FakePort("COM13")])
    driver = OpenFailingDriver()

    with pytest.raises(TargetResetError, match="probe open failed"):
        _ = reset_target_and_rediscover(driver, before)

    assert driver.calls == ["open", "close"]


def test_target_reset_rejects_device_without_stable_hardware_serial() -> None:
    driver = FakeResetDriver()
    before = SerialIdentity(
        port="COM13",
        description="nrftest application",
        hwid="USB fixture",
        vid=APPLICATION_USB_VID,
        pid=APPLICATION_USB_PID,
        serial_number=None,
    )

    with pytest.raises(TargetResetError, match="stable application hardware serial"):
        _ = reset_target_and_rediscover(driver, before)

    assert driver.calls == []
