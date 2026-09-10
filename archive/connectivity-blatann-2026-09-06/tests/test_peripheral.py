from __future__ import annotations

import io
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from .nrftest.peripheral import NrfPeripheral, PeripheralError, PeripheralState
from .nrftest.profile import PeripheralProfile
from .nrftest.telemetry import JsonLineEmitter


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class FakeCharacteristic:
    def __init__(self, peripheral: NrfPeripheral, state: str):
        self._peripheral = peripheral
        self.client_subscribed = True
        self.cccd_state = SimpleNamespace(name=state)
        self._next_id = 1
        self.sent: list[bytes] = []

    def notify(self, value: bytes):
        self.sent.append(bytes(value))
        notification_id = self._next_id
        self._next_id += 1
        return SimpleNamespace(id=notification_id)


def make_peripheral(state: str):
    stream = io.StringIO()
    peripheral = NrfPeripheral("TEST", JsonLineEmitter(stream))
    peripheral._session.device = object()
    peripheral._state = PeripheralState.CONNECTED
    peripheral._profile = PeripheralProfile.load(
        PROJECT_ROOT / "profiles" / "nrf52840-basic-gatt-v1.json"
    )
    peripheral._run_id = "test-run"
    peripheral._peer = SimpleNamespace(connected=True)
    characteristic = FakeCharacteristic(peripheral, state)
    peripheral._characteristics["updates"] = characteristic
    return peripheral, characteristic, stream


@pytest.mark.parametrize(
    ("mode", "subscription", "completion_event"),
    [
        ("notification", "NOTIFY", "notification_sent"),
        ("indication", "INDICATION", "indication_confirmed"),
    ],
)
def test_burst_waits_for_each_completion_and_preserves_sequence(
    mode: str,
    subscription: str,
    completion_event: str,
):
    peripheral, characteristic, stream = make_peripheral(subscription)

    peripheral.emit_burst(mode, "batch-1", 3, 0, 16)
    thread = peripheral._burst_thread
    assert thread is not None

    deadline = time.monotonic() + 1
    while thread.is_alive():
        with peripheral._lock:
            pending = tuple(peripheral._pending_updates.items())
        if pending:
            notification_id, operation = pending[0]
            peripheral._on_update_complete(
                "updates",
                SimpleNamespace(
                    id=notification_id,
                    data=operation.payload,
                    reason=SimpleNamespace(name="SUCCESS"),
                ),
            )
        if time.monotonic() >= deadline:
            raise AssertionError("burst did not finish")
        time.sleep(0.001)
    thread.join(timeout=1)

    messages = [json.loads(line) for line in stream.getvalue().splitlines()]
    accepted = [
        message
        for message in messages
        if message.get("event") == f"{mode}_accepted"
    ]
    completed = [
        message
        for message in messages
        if message.get("event") == completion_event
    ]

    assert [message["sequence"] for message in accepted] == [0, 1, 2]
    assert [message["sequence"] for message in completed] == [0, 1, 2]
    assert [payload[4:8] for payload in characteristic.sent] == [
        (0).to_bytes(4, "little"),
        (1).to_bytes(4, "little"),
        (2).to_bytes(4, "little"),
    ]
    assert any(message.get("event") == "burst_completed" for message in messages)


def test_update_mode_must_match_central_subscription():
    peripheral, _, _ = make_peripheral("NOTIFY")

    with pytest.raises(PeripheralError, match="indication is required"):
        peripheral.emit_notification("updates", b"payload", indication=True)
