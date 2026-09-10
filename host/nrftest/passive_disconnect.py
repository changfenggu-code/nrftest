from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from host.nrftest.autopts_adapter import ConnectionSnapshot, GapSnapshot


class PassiveDisconnectError(RuntimeError):
    """Raised when a Peripheral-initiated disconnect is not observed cleanly."""


class PassiveDisconnectSession(Protocol):
    def disconnect(self, connection: ConnectionSnapshot) -> GapSnapshot: ...

    def wait_for_disconnection(
        self,
        timeout: float,
        *,
        address: str | None = None,
    ) -> GapSnapshot: ...


@dataclass(frozen=True)
class PassiveDisconnectObservation:
    command_snapshot: GapSnapshot
    event_snapshot: GapSnapshot


def require_peer_disconnected(
    snapshot: GapSnapshot,
    connection: ConnectionSnapshot,
) -> None:
    if any(peer.address == connection.address for peer in snapshot.connections):
        raise PassiveDisconnectError(
            "BTP disconnected event arrived but the peer remains in GAP connection state"
        )


def request_peripheral_disconnect(
    session: PassiveDisconnectSession,
    connection: ConnectionSnapshot,
    *,
    timeout: float,
) -> PassiveDisconnectObservation:
    if timeout <= 0:
        raise PassiveDisconnectError("disconnect timeout must be greater than zero")

    command_snapshot = session.disconnect(connection)
    event_snapshot = session.wait_for_disconnection(timeout, address=connection.address)
    require_peer_disconnected(event_snapshot, connection)
    return PassiveDisconnectObservation(command_snapshot, event_snapshot)
