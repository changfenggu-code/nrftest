from __future__ import annotations

import pytest

from host.nrftest.autopts_adapter import ConnectionSnapshot, GapSnapshot
from host.nrftest.passive_disconnect import (
    PassiveDisconnectError,
    request_peripheral_disconnect,
    require_peer_disconnected,
)


def _connection() -> ConnectionSnapshot:
    return ConnectionSnapshot(
        address="112233445566",
        address_type=1,
        address_type_name="random",
        security_level=1,
    )


def _snapshot(*connections: ConnectionSnapshot) -> GapSnapshot:
    return GapSnapshot(
        controller_index=0,
        address="AABBCCDDEEFF",
        address_type=1,
        address_type_name="random",
        current_settings={"Powered": True, "Advertising": False},
        connections=connections,
    )


class FakeSession:
    def __init__(self, event_snapshot: GapSnapshot) -> None:
        self.event_snapshot = event_snapshot
        self.calls: list[tuple[object, ...]] = []

    def disconnect(self, connection: ConnectionSnapshot) -> GapSnapshot:
        self.calls.append(("disconnect", connection))
        return _snapshot(connection)

    def wait_for_disconnection(
        self,
        timeout: float,
        *,
        address: str | None = None,
    ) -> GapSnapshot:
        self.calls.append(("wait", timeout, address))
        return self.event_snapshot


def test_disconnect_command_is_followed_by_matching_event_wait() -> None:
    connection = _connection()
    session = FakeSession(_snapshot())

    observation = request_peripheral_disconnect(session, connection, timeout=30)

    assert session.calls == [("disconnect", connection), ("wait", 30, connection.address)]
    assert observation.command_snapshot.connections == (connection,)
    assert observation.event_snapshot.connections == ()


def test_disconnect_rejects_event_that_leaves_peer_connected() -> None:
    connection = _connection()
    session = FakeSession(_snapshot(connection))

    with pytest.raises(PassiveDisconnectError, match="peer remains"):
        request_peripheral_disconnect(session, connection, timeout=30)


def test_disconnect_rejects_non_positive_timeout_before_command() -> None:
    session = FakeSession(_snapshot())

    with pytest.raises(PassiveDisconnectError, match="greater than zero"):
        request_peripheral_disconnect(session, _connection(), timeout=0)

    assert session.calls == []


def test_peer_absence_check_supports_non_disconnect_fault_triggers() -> None:
    connection = _connection()

    require_peer_disconnected(_snapshot(), connection)
    with pytest.raises(PassiveDisconnectError, match="peer remains"):
        require_peer_disconnected(_snapshot(connection), connection)
