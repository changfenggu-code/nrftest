from __future__ import annotations

from types import SimpleNamespace

from .nrftest.cli import dispatch
from .nrftest.protocol import parse_command


class FakePeripheral:
    state = SimpleNamespace(value="connected")

    def __init__(self):
        self.burst_args = None

    def emit_burst(self, *args):
        self.burst_args = args


def test_dispatches_notification_burst_without_exposing_driver_api():
    peripheral = FakePeripheral()
    command = parse_command(
        '{"request_id":"r1","op":"emit_notification_burst",'
        '"burst_id":"batch-1","count":3,"interval_us":500,'
        '"payload_bytes":32}'
    )

    result, should_stop = dispatch(peripheral, command)

    assert not should_stop
    assert peripheral.burst_args == ("notification", "batch-1", 3, 500, 32)
    assert result == {
        "burst_id": "batch-1",
        "mode": "notification",
        "count": 3,
        "interval_us": 500,
        "payload_bytes": 32,
    }


def test_dispatches_indication_burst_as_a_distinct_mode():
    peripheral = FakePeripheral()
    command = parse_command(
        '{"request_id":"r1","op":"emit_indication_burst",'
        '"burst_id":"batch-2","count":2,"interval_us":0,'
        '"payload_bytes":16}'
    )

    dispatch(peripheral, command)

    assert peripheral.burst_args == ("indication", "batch-2", 2, 0, 16)
