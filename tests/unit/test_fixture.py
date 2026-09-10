from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest

from host.nrftest.autopts_adapter import (
    CapabilitySnapshot,
    ConnectionSnapshot,
    GapSnapshot,
    GattAttributeValueSnapshot,
    GattValueChangedSnapshot,
    SerialIdentity,
)
from host.nrftest.fixture import (
    DisconnectTrigger,
    FixtureError,
    FixtureSession,
    PeripheralFixture,
)
from host.nrftest.gatt_profile import (
    CharacteristicHandles,
    GattProfileAction,
    GattProfileMapping,
)
from host.nrftest.profile import PeripheralProfile
from host.nrftest.subscription import SubscriptionMode


def _identity() -> SerialIdentity:
    return SerialIdentity("COM13", "USB serial", "USB identity", 0x2FE3, 0x0004, "fixture")


def _connection() -> ConnectionSnapshot:
    return ConnectionSnapshot("112233445566", 0, "public", 1)


def _gap(
    *connections: ConnectionSnapshot, powered: bool = True, advertising: bool = False
) -> GapSnapshot:
    return GapSnapshot(
        0,
        "AABBCCDDEEFF",
        1,
        "random",
        {
            "Powered": powered,
            "Connectable": True,
            "Discoverable": True,
            "Advertising": advertising,
        },
        connections,
    )


def _mapping() -> GattProfileMapping:
    characteristics = (
        CharacteristicHandles("read-write", "fff1", 34, 35, None),
        CharacteristicHandles("updates", "fff2", 36, 37, 38),
        CharacteristicHandles("read-only", "fff3", 39, 40, None),
        CharacteristicHandles("write-only", "fff4", 41, 42, None),
    )
    return GattProfileMapping(GattProfileAction.ATTACHED, 33, 0xFFFF, characteristics, ())


def _ensure_profile(*args: object, **kwargs: object) -> GattProfileMapping:
    del args, kwargs
    return _mapping()


class FakeSession:
    def __init__(self) -> None:
        self.cleanup_errors: tuple[str, ...] = ()
        self.cleanup_classification: str = "clean"
        self.current: GapSnapshot = _gap()
        self.calls: list[tuple[object, ...]] = []
        self.attribute_values: dict[int, bytes] = {38: b"\x00\x00"}

    def start(
        self, *, required_services: object, local_name: str | None = None
    ) -> CapabilitySnapshot:
        self.calls.append(("start", tuple(cast(tuple[str, ...], required_services)), local_name))
        return CapabilitySnapshot(0xF, ("CORE", "GAP", "GATT"), {"GATT": 0xFFFFFFFF}, {})

    def gap_snapshot(self) -> GapSnapshot:
        return self.current

    def read_controller_info(self) -> GapSnapshot:
        self.calls.append(("controller",))
        return self.current

    def set_powered(self, enabled: bool) -> GapSnapshot:
        self.calls.append(("powered", enabled))
        self.current = replace(
            self.current, current_settings={**self.current.current_settings, "Powered": enabled}
        )
        return self.current

    def set_connectable(self, enabled: bool) -> GapSnapshot:
        self.calls.append(("connectable", enabled))
        return self.current

    def set_discoverable(self, enabled: bool) -> GapSnapshot:
        self.calls.append(("discoverable", enabled))
        return self.current

    def start_advertising(
        self,
        local_name: str,
        *,
        service_uuid16: str | None = None,
        service_uuid128: str | None = None,
    ) -> GapSnapshot:
        self.calls.append(("advertise", local_name, service_uuid16, service_uuid128))
        self.current = replace(
            self.current, current_settings={**self.current.current_settings, "Advertising": True}
        )
        return self.current

    def stop_advertising(self) -> GapSnapshot:
        self.calls.append(("stop-advertising",))
        self.current = replace(
            self.current, current_settings={**self.current.current_settings, "Advertising": False}
        )
        return self.current

    def wait_for_connection(self, timeout: float) -> GapSnapshot:
        self.calls.append(("wait-connection", timeout))
        self.current = _gap(_connection())
        return self.current

    def disconnect(self, connection: ConnectionSnapshot) -> GapSnapshot:
        self.calls.append(("disconnect", connection))
        return self.current

    def wait_for_disconnection(self, timeout: float, *, address: str | None = None) -> GapSnapshot:
        self.calls.append(("wait-disconnection", timeout, address))
        self.current = _gap(powered=self.current.current_settings["Powered"])
        return self.current

    def gatt_set_value(self, handle: int, value: bytes) -> None:
        self.calls.append(("set-value", handle, value))
        self.attribute_values[handle] = value

    def gatt_get_attribute_value(
        self,
        handle: int,
        *,
        peer_address: str = "000000000000",
        peer_address_type: int = 0,
    ) -> GattAttributeValueSnapshot:
        self.calls.append(("get-value", handle, peer_address, peer_address_type))
        return GattAttributeValueSnapshot(0, self.attribute_values[handle])

    def gatt_add_primary_service(self, uuid: str) -> None:
        raise AssertionError(uuid)

    def gatt_add_characteristic(self, uuid: str, *, properties: int, permissions: int) -> None:
        raise AssertionError((uuid, properties, permissions))

    def gatt_set_last_value(self, value: bytes) -> None:
        raise AssertionError(value)

    def gatt_add_ccc(self, *, permissions: int) -> None:
        raise AssertionError(permissions)

    def gatt_start_server(self) -> None:
        raise AssertionError

    def gatt_get_attributes(self, **kwargs: object) -> tuple[object, ...]:
        raise AssertionError(kwargs)

    def gatt_remove_service_containing_handle(self, handle: int) -> None:
        raise AssertionError(handle)

    def clear_gatt_value_changed(self, handle: int) -> None:
        self.calls.append(("clear", handle))

    def wait_for_gatt_value_changed(self, handle: int, timeout: float) -> GattValueChangedSnapshot:
        self.calls.append(("wait-write", handle, timeout))
        return GattValueChangedSnapshot(handle, b"\x10", 1)

    def close(self, *, unregister_services: bool = False) -> None:
        self.calls.append(("close", unregister_services))


def _fixture(session: FakeSession) -> PeripheralFixture:
    return PeripheralFixture(cast(FixtureSession, cast(object, session)), _identity())


def test_doctor_starts_all_services_once() -> None:
    session = FakeSession()
    fixture = _fixture(session)

    first = fixture.doctor(local_name="Nrftest")
    second = fixture.doctor(local_name="Ignored")

    assert first.identity == _identity()
    assert second.controller.address == "AABBCCDDEEFF"
    assert [call for call in session.calls if call[0] == "start"] == [
        ("start", ("CORE", "GAP", "GATT"), "Nrftest")
    ]


def test_load_profile_rejects_topology_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    session = FakeSession()
    fixture = _fixture(session)
    profile_a = PeripheralProfile.load(Path("profiles/blehub-nrf-basic-v1.json"))
    profile_b = PeripheralProfile.load(Path("profiles/blehub-nrf-rebuild-v1.json"))
    monkeypatch.setattr("host.nrftest.fixture.ensure_gatt_profile", _ensure_profile)

    assert fixture.load_profile(profile_a).action is GattProfileAction.ATTACHED
    with pytest.raises(FixtureError, match="cannot switch Profile topology"):
        _ = fixture.load_profile(profile_b)


def test_write_observation_uses_role_mapping_and_active_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = FakeSession()
    fixture = _fixture(session)
    profile = PeripheralProfile.load(Path("profiles/blehub-nrf-basic-v1.json"))
    monkeypatch.setattr("host.nrftest.fixture.ensure_gatt_profile", _ensure_profile)
    _ = fixture.load_profile(profile)
    fixture.clear_write_events("read-write")
    with pytest.raises(FixtureError, match="without an observed peer connection"):
        _ = fixture.wait_for_write("read-write", timeout=30, expected=b"\x10")

    _ = fixture.start_advertising()
    _ = fixture.wait_for_connection(30)
    changed = fixture.wait_for_write("read-write", timeout=30, expected=b"\x10")

    assert changed.handle == 35
    assert changed.value == b"\x10"
    assert ("clear", 35) in session.calls
    assert ("wait-write", 35, 30) in session.calls


@pytest.mark.parametrize(
    ("mode", "enabled_value"),
    [
        (SubscriptionMode.NOTIFICATION, b"\x01\x00"),
        (SubscriptionMode.INDICATION, b"\x02\x00"),
    ],
)
def test_subscription_state_uses_exact_ccc_mode_and_peer(
    monkeypatch: pytest.MonkeyPatch,
    mode: SubscriptionMode,
    enabled_value: bytes,
) -> None:
    session = FakeSession()
    fixture = _fixture(session)
    profile = PeripheralProfile.load(Path("profiles/blehub-nrf-basic-v1.json"))
    monkeypatch.setattr("host.nrftest.fixture.ensure_gatt_profile", _ensure_profile)
    _ = fixture.load_profile(profile)
    _ = fixture.start_advertising()
    _ = fixture.wait_for_connection(30)

    session.attribute_values[38] = enabled_value
    enabled = fixture.wait_for_subscription_state(mode, enabled=True, timeout=30)
    session.attribute_values[38] = b"\x00\x00"
    disabled = fixture.wait_for_subscription_state(mode, enabled=False, timeout=30)

    assert enabled.value == enabled_value
    assert disabled.value == b"\x00\x00"
    assert [call for call in session.calls if call[0] == "get-value"] == [
        ("get-value", 38, _connection().address, _connection().address_type),
        ("get-value", 38, _connection().address, _connection().address_type),
    ]


def test_subscription_state_requires_an_observed_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = FakeSession()
    fixture = _fixture(session)
    profile = PeripheralProfile.load(Path("profiles/blehub-nrf-basic-v1.json"))
    monkeypatch.setattr("host.nrftest.fixture.ensure_gatt_profile", _ensure_profile)
    _ = fixture.load_profile(profile)

    with pytest.raises(FixtureError, match="without an observed peer connection"):
        _ = fixture.wait_for_subscription_state(
            SubscriptionMode.NOTIFICATION,
            enabled=True,
            timeout=30,
        )

    assert not any(call[0] == "get-value" for call in session.calls)


def test_wait_for_disconnection_clears_fixture_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = FakeSession()
    fixture = _fixture(session)
    profile = PeripheralProfile.load(Path("profiles/blehub-nrf-basic-v1.json"))
    monkeypatch.setattr("host.nrftest.fixture.ensure_gatt_profile", _ensure_profile)
    _ = fixture.load_profile(profile)
    _ = fixture.start_advertising()
    _ = fixture.wait_for_connection(30)

    snapshot = fixture.wait_for_disconnection(30)

    assert snapshot.connections == ()
    assert fixture.connection is None
    assert ("wait-disconnection", 30, _connection().address) in session.calls


def test_radio_stack_disconnect_restores_power(monkeypatch: pytest.MonkeyPatch) -> None:
    session = FakeSession()
    fixture = _fixture(session)
    profile = PeripheralProfile.load(Path("profiles/blehub-nrf-basic-v1.json"))
    monkeypatch.setattr("host.nrftest.fixture.ensure_gatt_profile", _ensure_profile)
    _ = fixture.load_profile(profile)
    _ = fixture.start_advertising()
    assert fixture.wait_for_connection(30) == _connection()

    observation = fixture.disconnect_peer(timeout=30)

    assert observation.trigger is DisconnectTrigger.RADIO_STACK_RESTART
    assert observation.command_snapshot.current_settings["Powered"] is False
    assert observation.event_snapshot.connections == ()
    assert observation.recovery_snapshot is not None
    assert observation.recovery_snapshot.current_settings["Powered"] is True
    assert fixture.connection is None


def test_direct_gap_disconnect_does_not_restart_radio(monkeypatch: pytest.MonkeyPatch) -> None:
    session = FakeSession()
    fixture = _fixture(session)
    profile = PeripheralProfile.load(Path("profiles/blehub-nrf-basic-v1.json"))
    monkeypatch.setattr("host.nrftest.fixture.ensure_gatt_profile", _ensure_profile)
    _ = fixture.load_profile(profile)
    _ = fixture.start_advertising()
    _ = fixture.wait_for_connection(30)

    observation = fixture.disconnect_peer(trigger=DisconnectTrigger.GAP_DISCONNECT, timeout=30)

    assert observation.trigger is DisconnectTrigger.GAP_DISCONNECT
    assert observation.recovery_snapshot is None
    assert ("disconnect", _connection()) in session.calls
    assert not any(call[:2] == ("powered", False) for call in session.calls)
    assert fixture.connection is None


def test_close_is_idempotent_and_stops_owned_advertising(monkeypatch: pytest.MonkeyPatch) -> None:
    session = FakeSession()
    fixture = _fixture(session)
    profile = PeripheralProfile.load(Path("profiles/blehub-nrf-basic-v1.json"))
    monkeypatch.setattr("host.nrftest.fixture.ensure_gatt_profile", _ensure_profile)
    _ = fixture.load_profile(profile)
    _ = fixture.start_advertising()

    fixture.close()
    fixture.close()

    assert session.calls.count(("stop-advertising",)) == 1
    assert session.calls.count(("close", False)) == 1
    with pytest.raises(FixtureError, match="closed"):
        _ = fixture.snapshot()
