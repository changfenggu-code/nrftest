from __future__ import annotations

from collections import deque

import pytest

from host.nrftest.autopts_adapter import GattAttributeValueSnapshot
from host.nrftest.subscription import (
    SubscriptionError,
    SubscriptionMode,
    set_and_verify_value,
    wait_for_ccc,
)


class FakeSession:
    def __init__(self, snapshots: list[GattAttributeValueSnapshot]) -> None:
        self.snapshots = deque(snapshots)
        self.last_snapshot = snapshots[-1]
        self.set_calls: list[tuple[int, bytes]] = []
        self.read_calls: list[tuple[int, str, int]] = []

    def gatt_set_value(self, handle: int, value: bytes) -> None:
        self.set_calls.append((handle, value))

    def gatt_get_attribute_value(
        self,
        handle: int,
        *,
        peer_address: str = "000000000000",
        peer_address_type: int = 0,
    ) -> GattAttributeValueSnapshot:
        self.read_calls.append((handle, peer_address, peer_address_type))
        if self.snapshots:
            self.last_snapshot = self.snapshots.popleft()
        return self.last_snapshot


def test_ccc_mode_values_match_bluetooth_assignments() -> None:
    assert SubscriptionMode.NOTIFICATION.ccc_value == b"\x01\x00"
    assert SubscriptionMode.INDICATION.ccc_value == b"\x02\x00"


def test_ccc_wait_uses_real_peer_and_accepts_tester_metadata_status() -> None:
    session = FakeSession(
        [
            GattAttributeValueSnapshot(0x0C, b"\x00\x00"),
            GattAttributeValueSnapshot(0x0C, b"\x02\x00"),
        ]
    )

    observation = wait_for_ccc(
        session,
        38,
        peer_address="112233445566",
        peer_address_type=1,
        expected=b"\x02\x00",
        allowed_pending=(b"\x00\x00",),
        timeout=0.1,
        poll_interval=0,
    )

    assert observation.value == b"\x02\x00"
    assert observation.att_response == 0x0C
    assert observation.attempts == 2
    assert session.read_calls == [
        (38, "112233445566", 1),
        (38, "112233445566", 1),
    ]


def test_ccc_wait_rejects_another_active_mode() -> None:
    session = FakeSession([GattAttributeValueSnapshot(0, b"\x01\x00")])

    with pytest.raises(SubscriptionError, match="unexpected value 0100"):
        _ = wait_for_ccc(
            session,
            38,
            peer_address="112233445566",
            peer_address_type=0,
            expected=b"\x02\x00",
            allowed_pending=(b"\x00\x00",),
            timeout=0.1,
            poll_interval=0,
        )


def test_set_value_requires_successful_exact_readback() -> None:
    session = FakeSession([GattAttributeValueSnapshot(0, b"\x11")])

    observation = set_and_verify_value(
        session,
        37,
        b"\x11",
        peer_address="112233445566",
        peer_address_type=0,
    )

    assert observation.value == b"\x11"
    assert session.set_calls == [(37, b"\x11")]


def test_set_value_success_response_cannot_hide_length_mismatch() -> None:
    session = FakeSession([GattAttributeValueSnapshot(0, b"\x00")])

    with pytest.raises(SubscriptionError, match="readback mismatch"):
        _ = set_and_verify_value(
            session,
            37,
            b"\x11\x22",
            peer_address="112233445566",
            peer_address_type=0,
        )
