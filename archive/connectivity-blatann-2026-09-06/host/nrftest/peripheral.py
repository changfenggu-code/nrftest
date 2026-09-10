from __future__ import annotations

import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .connectivity import ConnectivitySession
from .payload import (
    MAX_BURST_PAYLOAD_BYTES,
    make_burst_payload,
    validate_burst_parameters,
)
from .profile import CharacteristicSpec, PeripheralProfile
from .telemetry import JsonLineEmitter


class PeripheralError(RuntimeError):
    """Raised when an operation is invalid for the current Peripheral state."""


class PeripheralEnvironmentError(PeripheralError):
    """Raised when the Connectivity device cannot perform an operation."""


class PeripheralCapabilityError(PeripheralError):
    """Raised when the loaded profile does not provide a requested capability."""


class PeripheralState(str, Enum):
    NEW = "new"
    CONNECTIVITY_READY = "connectivity_ready"
    PROFILE_LOADED = "profile_loaded"
    ADVERTISING = "advertising"
    CONNECTED = "connected"
    CLOSED = "closed"


@dataclass
class _PendingUpdate:
    mode: str
    characteristic: str
    payload: bytes
    sequence: int | None = None
    burst_id: str | None = None
    done: threading.Event = field(default_factory=threading.Event)
    completion_reason: str | None = None


@dataclass(frozen=True)
class _BurstRequest:
    mode: str
    burst_id: str
    count: int
    interval_us: int
    payload_bytes: int


class _BurstCancelled(Exception):
    """Internal signal used when lifecycle cleanup cancels a running burst."""


class NrfPeripheral:
    """A profile-driven nRF52840 GATT Server controlled by Blatann."""

    def __init__(self, port: str, emitter: JsonLineEmitter, baud: int = 1_000_000) -> None:
        self._session = ConnectivitySession(port, baud)
        self._emitter = emitter
        self._lock = threading.RLock()
        self._state = PeripheralState.NEW
        self._profile: PeripheralProfile | None = None
        self._run_id: str | None = None
        self._services: dict[str, Any] = {}
        self._characteristics: dict[str, Any] = {}
        self._peer: Any | None = None
        self._connection_handle: int | None = None
        self._pending_updates: dict[int | None, _PendingUpdate] = {}
        self._burst_thread: threading.Thread | None = None
        self._burst_cancel: threading.Event | None = None
        self._burst_request: _BurstRequest | None = None

    @property
    def state(self) -> PeripheralState:
        with self._lock:
            return self._state

    @property
    def port(self) -> str:
        return self._session.port

    def open(self) -> None:
        with self._lock:
            if self._state not in {PeripheralState.NEW, PeripheralState.CLOSED}:
                return
            self._session.open()
            self._register_connection_callbacks()
            self._state = PeripheralState.CONNECTIVITY_READY
        self._emitter.telemetry("connectivity_ready", **self._session.metadata().as_dict())

    def close(self) -> None:
        with self._lock:
            if self._state == PeripheralState.CLOSED:
                return
            self._stop_and_disconnect_locked()
            try:
                self._session.close()
            finally:
                self._state = PeripheralState.CLOSED
                self._profile = None
                self._run_id = None
                self._services.clear()
                self._characteristics.clear()
                self._peer = None
                self._connection_handle = None
                self._pending_updates.clear()
                self._burst_request = None
                self._burst_cancel = None
                self._burst_thread = None

    def load_profile(self, profile: PeripheralProfile, run_id: str) -> None:
        if not run_id:
            raise PeripheralError("run_id must be non-empty")
        with self._lock:
            self._require_open_locked()
            if self._state in {PeripheralState.ADVERTISING, PeripheralState.CONNECTED}:
                raise PeripheralError("stop advertising and disconnect before loading a profile")
            if self._profile is not None:
                if self._profile == profile and self._run_id == run_id:
                    return
                raise PeripheralError("a profile is already loaded; reset before loading another profile")
            try:
                self._build_database_locked(profile)
            except Exception as error:
                self._services.clear()
                self._characteristics.clear()
                raise PeripheralEnvironmentError(
                    f"unable to build GATT profile: {error}"
                ) from error
            self._profile = profile
            self._run_id = run_id
            self._state = PeripheralState.PROFILE_LOADED
            metadata = self._session.metadata().as_dict()
        self._emitter.telemetry(
            "ready",
            run_id=run_id,
            profile_id=profile.profile_id,
            service_uuids=list(profile.advertised_service_uuids),
            **metadata,
        )

    def start_advertising(self) -> None:
        with self._lock:
            self._require_profile_locked()
            if self._state == PeripheralState.ADVERTISING:
                return
            if self._state == PeripheralState.CONNECTED:
                raise PeripheralError("cannot advertise while a Central is connected")
            device = self._require_device_locked()
            from blatann.gap.advertise_data import AdvertisingData
            from blatann.uuid import Uuid128

            service_uuid = Uuid128(self._profile.service.uuid)
            advertise_data = AdvertisingData(flags=0x06, service_uuid128s=[service_uuid])
            scan_response = AdvertisingData(local_name=self._profile.local_name)
            device.advertiser.set_advertise_data(advertise_data, scan_response)
            device.advertiser.start(timeout_sec=0, auto_restart=False)
            self._state = PeripheralState.ADVERTISING

    def stop_advertising(self) -> None:
        with self._lock:
            if self._session.device is None:
                return
            try:
                self._session.device.advertiser.stop()
            except Exception as error:
                raise PeripheralEnvironmentError(
                    f"unable to stop advertising: {error}"
                ) from error
            if self._state == PeripheralState.ADVERTISING:
                self._state = PeripheralState.PROFILE_LOADED

    def set_value(self, role: str, value: bytes) -> None:
        with self._lock:
            characteristic = self._characteristic_locked(role)
            try:
                characteristic.set_value(value)
            except Exception as error:
                raise PeripheralEnvironmentError(
                    f"unable to set {role} value: {error}"
                ) from error

    def emit_notification(
        self,
        role: str,
        value: bytes,
        *,
        indication: bool = False,
        sequence: int | None = None,
        burst_id: str | None = None,
    ) -> None:
        self._send_update(
            role,
            value,
            indication=indication,
            sequence=sequence,
            burst_id=burst_id,
        )

    def emit_burst(
        self,
        mode: str,
        burst_id: str,
        count: int,
        interval_us: int,
        payload_bytes: int,
    ) -> None:
        if mode not in {"notification", "indication"}:
            raise PeripheralError(f"unsupported burst mode: {mode}")
        validate_burst_parameters(burst_id, count, interval_us, payload_bytes)
        request = _BurstRequest(mode, burst_id, count, interval_us, payload_bytes)
        with self._lock:
            self._require_profile_locked()
            self._validate_update_locked("updates", indication=mode == "indication")
            if self._burst_request is not None:
                raise PeripheralError(
                    f"burst {self._burst_request.burst_id} is already running"
                )
            cancel = threading.Event()
            thread = threading.Thread(
                target=self._run_burst,
                args=(request, cancel),
                name=f"nrftest-{mode}-burst-{burst_id}",
                daemon=True,
            )
            self._burst_request = request
            self._burst_cancel = cancel
            self._burst_thread = thread
            try:
                thread.start()
            except RuntimeError:
                self._burst_request = None
                self._burst_cancel = None
                self._burst_thread = None
                raise

    def _send_update(
        self,
        role: str,
        value: bytes,
        *,
        indication: bool,
        sequence: int | None = None,
        burst_id: str | None = None,
    ) -> _PendingUpdate:
        with self._lock:
            characteristic = self._characteristic_locked(role)
            self._validate_update_locked(role, indication=indication)
            try:
                waitable = characteristic.notify(value)
            except Exception as error:
                raise PeripheralEnvironmentError(
                    f"unable to send {role} update: {error}"
                ) from error

            waitable_id = getattr(waitable, "id", None)
            pending = _PendingUpdate(
                mode="indication" if indication else "notification",
                characteristic=role,
                payload=bytes(value),
                sequence=sequence,
                burst_id=burst_id,
            )
            self._pending_updates[waitable_id] = pending
            fields: dict[str, Any] = {
                "characteristic": role,
                "mode": pending.mode,
                "payload_hex": pending.payload.hex(),
                "payload_length": len(pending.payload),
            }
            if sequence is not None:
                fields["sequence"] = sequence
            if burst_id is not None:
                fields["burst_id"] = burst_id
            if waitable_id is not None:
                fields["notification_id"] = waitable_id
        self._emitter.telemetry(
            "indication_accepted" if indication else "notification_accepted",
            **fields,
        )
        return pending

    def _run_burst(self, request: _BurstRequest, cancel: threading.Event) -> None:
        try:
            for sequence in range(request.count):
                if cancel.is_set():
                    raise _BurstCancelled
                payload = make_burst_payload(request.burst_id, sequence, request.payload_bytes)
                try:
                    pending = self._send_update(
                        "updates",
                        payload,
                        indication=request.mode == "indication",
                        sequence=sequence,
                        burst_id=request.burst_id,
                    )
                except Exception:
                    if cancel.is_set():
                        raise _BurstCancelled
                    raise
                if not pending.done.wait(timeout=30.0):
                    if cancel.is_set():
                        raise _BurstCancelled
                    raise PeripheralError(
                        f"timed out waiting for {request.mode} completion "
                        f"in burst {request.burst_id} at sequence {sequence}"
                    )
                if cancel.is_set():
                    raise _BurstCancelled
                reason = pending.completion_reason or "unknown"
                if reason != "success":
                    raise PeripheralError(
                        f"{request.mode} completion failed in burst {request.burst_id} "
                        f"at sequence {sequence}: {reason}"
                    )
                if sequence + 1 < request.count and cancel.wait(request.interval_us / 1_000_000):
                    raise _BurstCancelled
            self._emitter.telemetry(
                "burst_completed",
                burst_id=request.burst_id,
                mode=request.mode,
                count=request.count,
                interval_us=request.interval_us,
                payload_bytes=request.payload_bytes,
            )
        except _BurstCancelled:
            self._emitter.telemetry(
                "burst_cancelled",
                burst_id=request.burst_id,
                mode=request.mode,
            )
        except (OSError, PeripheralError, ValueError) as error:
            category = "scenario"
            if isinstance(error, (PeripheralEnvironmentError, OSError)):
                category = "environment"
            elif isinstance(error, PeripheralCapabilityError):
                category = "capability_skip"
            self._emitter.telemetry(
                "burst_failed",
                burst_id=request.burst_id,
                mode=request.mode,
                message=str(error),
            )
            self._emitter.telemetry(
                "error",
                category=category,
                message=str(error),
                burst_id=request.burst_id,
            )
        finally:
            with self._lock:
                if self._burst_request == request:
                    self._burst_request = None
                    self._burst_cancel = None
                    self._burst_thread = None

    def force_disconnect(self) -> None:
        with self._lock:
            peer = self._peer
            if peer is None or not getattr(peer, "connected", False):
                return
            self._cancel_burst_locked()
            self._release_pending_updates_locked("client_disconnected")
            try:
                peer.disconnect()
            except Exception as error:
                raise PeripheralEnvironmentError(
                    f"unable to disconnect Central: {error}"
                ) from error

    def reset(self) -> None:
        with self._lock:
            old_profile = self._profile
            old_run_id = self._run_id
            self._emitter.telemetry("reset_started", run_id=old_run_id)
            self._stop_and_disconnect_locked()
            try:
                self._session.close()
            finally:
                self._state = PeripheralState.NEW
                self._profile = None
                self._run_id = None
                self._services.clear()
                self._characteristics.clear()
                self._peer = None
                self._connection_handle = None
                self._pending_updates.clear()
        self.open()
        if old_profile is not None and old_run_id is not None:
            self.load_profile(old_profile, old_run_id)
        self._emitter.telemetry("reset_completed", run_id=old_run_id)

    def status(self) -> dict[str, Any]:
        with self._lock:
            device = self._session.device
            advertiser = getattr(device, "advertiser", None)
            peer = self._peer
            return {
                "state": self._state.value,
                "port": self._session.port,
                "profile_id": self._profile.profile_id if self._profile else None,
                "run_id": self._run_id,
                "advertising": bool(advertiser and advertiser.is_advertising),
                "connected": bool(peer and getattr(peer, "connected", False)),
                "connection_handle": self._connection_handle,
                "burst": self._burst_status_locked(),
            }

    def _build_database_locked(self, profile: PeripheralProfile) -> None:
        device = self._require_device_locked()
        from blatann.gatt.gatts import GattsCharacteristicProperties
        from blatann.uuid import Uuid128

        service = device.database.add_service(Uuid128(profile.service.uuid))
        self._services[profile.service.uuid] = service
        for spec in profile.service.characteristics:
            properties = GattsCharacteristicProperties(
                read=spec.readable,
                write=spec.writable,
                write_no_response=spec.writable_without_response,
                notify=spec.notifiable,
                indicate=spec.indicatable,
                max_length=max(
                    20,
                    len(spec.initial_value),
                    MAX_BURST_PAYLOAD_BYTES if spec.role == "updates" else 0,
                ),
                variable_length=True,
            )
            characteristic = service.add_characteristic(
                Uuid128(spec.uuid),
                properties,
                spec.initial_value,
                prefer_indications=spec.indicatable,
            )
            characteristic.on_read.register(
                lambda sender, event_args, role=spec.role: self._on_read(role, sender, event_args)
            )
            characteristic.on_write.register(
                lambda sender, event_args, role=spec.role: self._on_write(role, event_args)
            )
            characteristic.on_subscription_change.register(
                lambda sender, event_args, role=spec.role: self._on_subscription(role, event_args)
            )
            characteristic.on_notify_complete.register(
                lambda sender, event_args, role=spec.role: self._on_update_complete(role, event_args)
            )
            self._characteristics[spec.role] = characteristic

    def _register_connection_callbacks(self) -> None:
        device = self._require_device_locked()
        device.client.on_connect.register(self._on_connect)
        device.client.on_disconnect.register(self._on_disconnect)

    def _on_connect(self, peer: Any, _event_args: Any) -> None:
        with self._lock:
            self._peer = peer
            self._connection_handle = getattr(peer, "conn_handle", None)
            self._state = PeripheralState.CONNECTED
            connection_handle = self._connection_handle
        self._emitter.telemetry("central_connected", connection_handle=connection_handle)

    def _on_disconnect(self, _peer: Any, event_args: Any) -> None:
        with self._lock:
            self._peer = None
            connection_handle = self._connection_handle
            self._connection_handle = None
            self._cancel_burst_locked()
            self._release_pending_updates_locked("client_disconnected")
            if self._profile is not None and self._state != PeripheralState.CLOSED:
                self._state = PeripheralState.PROFILE_LOADED
            reason = str(getattr(event_args, "reason", "unknown"))
        self._emitter.telemetry(
            "disconnected",
            connection_handle=connection_handle,
            reason=reason,
        )

    def _on_read(self, role: str, characteristic: Any, _event_args: Any) -> None:
        try:
            value = bytes(characteristic.value)
        except (TypeError, ValueError):
            value = b""
        with self._lock:
            connection_handle = self._connection_handle
        self._emitter.telemetry(
            "read",
            characteristic=role,
            value_hex=value.hex(),
            connection_handle=connection_handle,
        )

    def _on_write(self, role: str, event_args: Any) -> None:
        value = bytes(getattr(event_args, "value", b""))
        with self._lock:
            connection_handle = self._connection_handle
        self._emitter.telemetry(
            "write",
            characteristic=role,
            value_hex=value.hex(),
            write_type="unknown",
            connection_handle=connection_handle,
        )

    def _on_subscription(self, role: str, event_args: Any) -> None:
        state = getattr(getattr(event_args, "subscription_state", None), "name", "unknown")
        event = "unsubscribed" if state == "NOT_SUBSCRIBED" else "subscribed"
        with self._lock:
            connection_handle = self._connection_handle
        self._emitter.telemetry(
            event,
            characteristic=role,
            mode=state.lower(),
            connection_handle=connection_handle,
        )

    def _on_update_complete(self, role: str, event_args: Any) -> None:
        notification_id = getattr(event_args, "id", None)
        reason = self._operation_reason(event_args)
        with self._lock:
            pending = self._pending_updates.pop(notification_id, None)
            if pending is not None:
                pending.completion_reason = reason
        mode = pending.mode if pending is not None else "unknown"
        event = "indication_confirmed" if mode == "indication" else "notification_sent"
        payload = bytes(
            getattr(event_args, "data", pending.payload if pending is not None else b"")
        )
        fields: dict[str, Any] = {
            "characteristic": pending.characteristic if pending is not None else role,
            "notification_id": notification_id,
            "mode": mode,
            "payload_hex": payload.hex(),
            "payload_length": len(payload),
            "reason": reason,
        }
        if pending is not None and pending.sequence is not None:
            fields["sequence"] = pending.sequence
        if pending is not None and pending.burst_id is not None:
            fields["burst_id"] = pending.burst_id
        try:
            self._emitter.telemetry(event, **fields)
        finally:
            if pending is not None:
                pending.done.set()

    def _validate_update_locked(self, role: str, *, indication: bool) -> None:
        characteristic = self._characteristic_locked(role)
        if not characteristic.client_subscribed:
            raise PeripheralError(f"Central is not subscribed to {role}")
        spec = self._spec_for_role_locked(role)
        if indication and not spec.indicatable:
            raise PeripheralCapabilityError(f"{role} does not support indications")
        if not indication and not spec.notifiable:
            raise PeripheralCapabilityError(f"{role} does not support notifications")
        actual_mode = self._subscription_mode_locked(characteristic)
        expected_mode = "indication" if indication else "notification"
        if actual_mode != expected_mode:
            raise PeripheralError(
                f"Central subscription for {role} is {actual_mode}; "
                f"{expected_mode} is required"
            )

    @staticmethod
    def _subscription_mode_locked(characteristic: Any) -> str:
        state = getattr(characteristic, "cccd_state", None)
        name = getattr(state, "name", str(state)).lower()
        if name == "notify":
            return "notification"
        if name == "indication":
            return "indication"
        if name == "not_subscribed":
            return "not_subscribed"
        return name

    @staticmethod
    def _operation_reason(event_args: Any) -> str:
        reason = getattr(getattr(event_args, "reason", None), "name", None)
        return (reason or str(getattr(event_args, "reason", "unknown"))).lower()

    def _burst_status_locked(self) -> dict[str, Any] | None:
        request = self._burst_request
        if request is None:
            return None
        return {
            "burst_id": request.burst_id,
            "mode": request.mode,
            "count": request.count,
            "interval_us": request.interval_us,
            "payload_bytes": request.payload_bytes,
        }

    def _cancel_burst_locked(self) -> None:
        if self._burst_cancel is not None:
            self._burst_cancel.set()

    def _release_pending_updates_locked(self, reason: str) -> None:
        pending_updates = tuple(self._pending_updates.values())
        self._pending_updates.clear()
        for pending in pending_updates:
            pending.completion_reason = reason
            pending.done.set()

    def _spec_for_role(self, role: str) -> CharacteristicSpec:
        with self._lock:
            return self._spec_for_role_locked(role)

    def _spec_for_role_locked(self, role: str) -> CharacteristicSpec:
        self._require_profile_locked()
        for spec in self._profile.service.characteristics:
            if spec.role == role:
                return spec
        raise PeripheralError(f"unknown characteristic role: {role}")

    def _characteristic_locked(self, role: str) -> Any:
        self._require_profile_locked()
        try:
            return self._characteristics[role]
        except KeyError as error:
            raise PeripheralError(f"unknown characteristic role: {role}") from error

    def _require_open_locked(self) -> None:
        if self._session.device is None or self._state == PeripheralState.CLOSED:
            raise PeripheralError("Connectivity session is not open")

    def _require_profile_locked(self) -> None:
        self._require_open_locked()
        if self._profile is None:
            raise PeripheralError("load a profile before this operation")

    def _require_device_locked(self) -> Any:
        if self._session.device is None:
            raise PeripheralError("Connectivity session is not open")
        return self._session.device

    def _stop_and_disconnect_locked(self) -> None:
        self._cancel_burst_locked()
        self._release_pending_updates_locked("client_disconnected")
        device = self._session.device
        if device is None:
            return
        try:
            device.advertiser.stop()
        except Exception:
            pass
        peer = self._peer
        if peer is not None and getattr(peer, "connected", False):
            try:
                peer.disconnect()
            except Exception:
                pass
