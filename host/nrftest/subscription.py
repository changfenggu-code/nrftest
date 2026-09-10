from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from time import monotonic, sleep
from typing import Protocol

from host.nrftest.autopts_adapter import GattAttributeValueSnapshot

ATT_SUCCESS = 0x00
ATT_INSUFFICIENT_ENCRYPTION_KEY_SIZE = 0x0C
_CCC_ACCEPTED_ATT_RESPONSES = {ATT_SUCCESS, ATT_INSUFFICIENT_ENCRYPTION_KEY_SIZE}


class SubscriptionError(RuntimeError):
    """Raised when the fixed Tester subscription state violates the expected scenario."""


class SubscriptionMode(StrEnum):
    NOTIFICATION = "notification"
    INDICATION = "indication"

    @property
    def ccc_value(self) -> bytes:
        return b"\x01\x00" if self is self.NOTIFICATION else b"\x02\x00"


class SubscriptionSession(Protocol):
    def gatt_set_value(self, handle: int, value: bytes) -> None: ...

    def gatt_get_attribute_value(
        self,
        handle: int,
        *,
        peer_address: str = "000000000000",
        peer_address_type: int = 0,
    ) -> GattAttributeValueSnapshot: ...


@dataclass(frozen=True)
class CccObservation:
    handle: int
    value: bytes
    att_response: int
    attempts: int


@dataclass(frozen=True)
class ValueUpdateObservation:
    handle: int
    value: bytes
    att_response: int


def wait_for_ccc(
    session: SubscriptionSession,
    handle: int,
    *,
    peer_address: str,
    peer_address_type: int,
    expected: bytes,
    allowed_pending: tuple[bytes, ...],
    timeout: float,
    poll_interval: float = 0.05,
) -> CccObservation:
    if len(expected) != 2 or any(len(value) != 2 for value in allowed_pending):
        raise SubscriptionError("CCC values must be exactly two bytes")
    if timeout <= 0:
        raise SubscriptionError("CCC wait timeout must be greater than zero")
    if poll_interval < 0:
        raise SubscriptionError("CCC poll interval must not be negative")

    deadline = monotonic() + timeout
    attempts = 0
    last_value: bytes | None = None
    while True:
        snapshot = session.gatt_get_attribute_value(
            handle,
            peer_address=peer_address,
            peer_address_type=peer_address_type,
        )
        attempts += 1
        if snapshot.att_response not in _CCC_ACCEPTED_ATT_RESPONSES:
            raise SubscriptionError(
                f"CCC handle {handle} returned ATT status 0x{snapshot.att_response:02x}"
            )
        if len(snapshot.value) != 2:
            raise SubscriptionError(
                f"CCC handle {handle} returned {len(snapshot.value)} bytes instead of two"
            )
        last_value = snapshot.value
        if last_value == expected:
            return CccObservation(handle, last_value, snapshot.att_response, attempts)
        if last_value not in allowed_pending:
            raise SubscriptionError(
                f"CCC handle {handle} changed to unexpected value {last_value.hex()}; "
                + f"expected {expected.hex()}"
            )
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise SubscriptionError(
                f"CCC handle {handle} remained {last_value.hex()} while waiting for "
                + f"{expected.hex()} within {timeout:g} seconds"
            )
        sleep(min(poll_interval, remaining))


def set_and_verify_value(
    session: SubscriptionSession,
    handle: int,
    value: bytes,
    *,
    peer_address: str,
    peer_address_type: int,
) -> ValueUpdateObservation:
    session.gatt_set_value(handle, value)
    snapshot = session.gatt_get_attribute_value(
        handle,
        peer_address=peer_address,
        peer_address_type=peer_address_type,
    )
    if snapshot.att_response != ATT_SUCCESS:
        raise SubscriptionError(
            f"value handle {handle} returned ATT status "
            + f"0x{snapshot.att_response:02x} after Set Value"
        )
    if snapshot.value != value:
        raise SubscriptionError(
            f"value handle {handle} readback mismatch after Set Value: "
            + f"expected {value.hex()}, got {snapshot.value.hex()}"
        )
    return ValueUpdateObservation(handle, snapshot.value, snapshot.att_response)
