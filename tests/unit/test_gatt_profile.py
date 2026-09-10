from __future__ import annotations

import uuid
from dataclasses import replace
from pathlib import Path

import pytest

from host.nrftest.autopts_adapter import (
    GattAttributeSnapshot,
    GattAttributeValueSnapshot,
)
from host.nrftest.gatt_profile import (
    ATT_SUCCESS,
    GattProfileAction,
    GattProfileError,
    ensure_gatt_profile,
    remove_gatt_profile,
    require_gatt_commands,
    require_gatt_remove_command,
)
from host.nrftest.profile import PeripheralProfile

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ACTIVE_PROFILE = PROJECT_ROOT / "profiles" / "blehub-nrf-basic-v1.json"
REBUILD_PROFILE = PROJECT_ROOT / "profiles" / "blehub-nrf-rebuild-v1.json"
GATT_COMMAND_MASK = 0xFFDBFFEFE

_PROPERTY_BITS = {
    "read-write": 0x0E,
    "updates": 0x32,
    "read-only": 0x02,
    "write-only": 0x0C,
}
_PERMISSION_BITS = {
    "read-write": 0x03,
    "updates": 0x01,
    "read-only": 0x01,
    "write-only": 0x02,
}


class FakeGattSession:
    def __init__(self, profile: PeripheralProfile, *, built: bool = False) -> None:
        self.profile: PeripheralProfile = profile
        self.calls: list[tuple[object, ...]] = []
        self.attributes: list[GattAttributeSnapshot] = []
        self.values: dict[int, GattAttributeValueSnapshot] = {}
        self.uuid_handles: dict[str, int] = {}
        self.pending: list[dict[str, object]] = []
        self.removed_handles: list[int] = []
        if built:
            self._install_database()

    def gatt_add_primary_service(self, uuid: str) -> None:
        self.calls.append(("add-service", uuid))

    def gatt_add_characteristic(self, uuid: str, *, properties: int, permissions: int) -> None:
        self.calls.append(("add-characteristic", uuid, properties, permissions))
        self.pending.append(
            {
                "uuid": uuid,
                "properties": properties,
                "permissions": permissions,
                "value": b"",
                "ccc": False,
            }
        )

    def gatt_set_value(self, handle: int, value: bytes) -> None:
        self.calls.append(("set-value", handle, value))
        existing = self.values[handle]
        self.values[handle] = GattAttributeValueSnapshot(existing.att_response, value)

    def gatt_set_last_value(self, value: bytes) -> None:
        self.calls.append(("set-last-value", value))
        self.pending[-1]["value"] = value

    def gatt_add_ccc(self, *, permissions: int) -> None:
        self.calls.append(("add-ccc", permissions))
        self.pending[-1]["ccc"] = True

    def gatt_start_server(self) -> None:
        self.calls.append(("start-server",))
        self._install_database()

    def gatt_get_attributes(
        self,
        *,
        start_handle: int = 0x0001,
        end_handle: int = 0xFFFF,
        type_uuid: str | None = None,
    ) -> tuple[GattAttributeSnapshot, ...]:
        return tuple(
            attribute
            for attribute in self.attributes
            if start_handle <= attribute.handle <= end_handle
            and (type_uuid is None or attribute.type_uuid == type_uuid)
        )

    def gatt_get_attribute_value(
        self,
        handle: int,
        *,
        peer_address: str = "000000000000",
        peer_address_type: int = 0,
    ) -> GattAttributeValueSnapshot:
        del peer_address, peer_address_type
        return self.values[handle]

    def gatt_get_handle_from_uuid(self, uuid: str) -> int:
        return self.uuid_handles[uuid]

    def gatt_remove_service_containing_handle(self, handle: int) -> None:
        self.calls.append(("remove-service", handle))
        if handle not in {attribute.handle for attribute in self.attributes}:
            raise RuntimeError(f"unknown handle {handle}")
        self.removed_handles.append(handle)
        self.attributes.clear()
        self.values.clear()
        self.uuid_handles.clear()
        self.pending.clear()

    def _install_database(self) -> None:
        characteristics = self.profile.service.characteristics
        if self.pending:
            assert len(self.pending) == len(characteristics)
        else:
            self.pending = [
                {
                    "uuid": item.uuid,
                    "properties": _PROPERTY_BITS[item.role],
                    "permissions": _PERMISSION_BITS[item.role],
                    "value": item.initial_value,
                    "ccc": item.notifiable or item.indicatable,
                }
                for item in characteristics
            ]

        self.attributes.clear()
        self.values.clear()
        self.uuid_handles.clear()
        handle = 10
        self.attributes.append(GattAttributeSnapshot(handle, 0x01, "2800"))
        self.values[handle] = GattAttributeValueSnapshot(
            ATT_SUCCESS,
            uuid.UUID(self.profile.service.uuid).bytes[::-1],
        )
        handle += 1
        for item in self.pending:
            characteristic_uuid = item["uuid"]
            properties = item["properties"]
            permissions = item["permissions"]
            value = item["value"]
            assert isinstance(characteristic_uuid, str)
            assert isinstance(properties, int)
            assert isinstance(permissions, int)
            assert isinstance(value, bytes)
            declaration_handle = handle
            value_handle = declaration_handle + 1
            self.attributes.append(GattAttributeSnapshot(declaration_handle, 0x01, "2803"))
            self.values[declaration_handle] = GattAttributeValueSnapshot(
                ATT_SUCCESS,
                bytes([properties])
                + value_handle.to_bytes(2, "little")
                + uuid.UUID(characteristic_uuid).bytes[::-1],
            )
            self.attributes.append(
                GattAttributeSnapshot(value_handle, permissions | 0x40, characteristic_uuid)
            )
            read_status = ATT_SUCCESS if permissions & 0x01 else 0x02
            self.values[value_handle] = GattAttributeValueSnapshot(read_status, value)
            self.uuid_handles[characteristic_uuid] = value_handle
            handle += 2
            if bool(item["ccc"]):
                self.attributes.append(GattAttributeSnapshot(handle, 0x03, "2902"))
                self.values[handle] = GattAttributeValueSnapshot(ATT_SUCCESS, b"\x00\x00")
                handle += 1


def test_fresh_profile_builds_in_order_and_maps_actual_handles() -> None:
    profile = PeripheralProfile.load(ACTIVE_PROFILE)
    session = FakeGattSession(profile)

    mapping = ensure_gatt_profile(session, profile, command_mask=GATT_COMMAND_MASK)

    assert mapping.action is GattProfileAction.BUILT
    assert mapping.service_handle == 10
    assert len(mapping.attributes) == 10
    assert mapping.characteristic_for_role("read-write").value_handle == 12
    assert mapping.characteristic_for_role("updates").ccc_handle == 15
    assert mapping.characteristic_for_role("read-only").value_handle == 17
    assert mapping.characteristic_for_role("write-only").value_handle == 19
    assert session.calls == [
        ("add-service", profile.service.uuid),
        ("add-characteristic", profile.characteristic_for_role("read-write").uuid, 0x0E, 0x03),
        ("set-last-value", b"\x00"),
        ("add-characteristic", profile.characteristic_for_role("updates").uuid, 0x32, 0x01),
        ("set-last-value", b"\x00"),
        ("add-ccc", 0x03),
        ("add-characteristic", profile.characteristic_for_role("read-only").uuid, 0x02, 0x01),
        ("set-last-value", b"BleHub"),
        ("add-characteristic", profile.characteristic_for_role("write-only").uuid, 0x0C, 0x02),
        ("set-last-value", b"\x00"),
        ("start-server",),
    ]


def test_metadata_mapping_accepts_fixed_tester_spurious_key_size_status() -> None:
    profile = PeripheralProfile.load(ACTIVE_PROFILE)
    session = FakeGattSession(profile, built=True)
    metadata_handles = [
        attribute.handle
        for attribute in session.attributes
        if attribute.type_uuid in {"2800", "2803"}
    ]
    for handle in metadata_handles:
        session.values[handle] = GattAttributeValueSnapshot(
            0x0C,
            session.values[handle].value,
        )

    mapping = ensure_gatt_profile(session, profile, command_mask=GATT_COMMAND_MASK)

    assert mapping.action is GattProfileAction.ATTACHED
    assert len(mapping.attributes) == 10


def test_resident_profile_is_attached_and_values_are_reset_without_rebuilding() -> None:
    profile = PeripheralProfile.load(ACTIVE_PROFILE)
    session = FakeGattSession(profile, built=True)
    read_write_handle = session.uuid_handles[profile.characteristic_for_role("read-write").uuid]
    session.values[read_write_handle] = GattAttributeValueSnapshot(ATT_SUCCESS, b"\xff")

    mapping = ensure_gatt_profile(session, profile, command_mask=GATT_COMMAND_MASK)

    assert mapping.action is GattProfileAction.ATTACHED
    assert not any(call[0].startswith("add-") for call in session.calls if isinstance(call[0], str))
    assert ("start-server",) not in session.calls
    assert session.values[read_write_handle].value == b"\x00"
    assert [call[0] for call in session.calls] == ["set-value"] * 4


def test_profile_is_removed_by_actual_service_handle_before_distinct_rebuild() -> None:
    profile_a = PeripheralProfile.load(ACTIVE_PROFILE)
    profile_b = PeripheralProfile.load(REBUILD_PROFILE)
    session = FakeGattSession(profile_a, built=True)

    mapping_a = ensure_gatt_profile(session, profile_a, command_mask=GATT_COMMAND_MASK)
    removal = remove_gatt_profile(
        session,
        profile_a,
        mapping_a,
        command_mask=GATT_COMMAND_MASK,
    )
    session.profile = profile_b
    mapping_b = ensure_gatt_profile(session, profile_b, command_mask=GATT_COMMAND_MASK)

    assert mapping_a.action is GattProfileAction.ATTACHED
    assert removal.service_handle == mapping_a.service_handle == 10
    assert removal.removed_attribute_handles == tuple(range(10, 20))
    assert removal.remaining_attribute_count == 0
    assert session.removed_handles == [10]
    assert mapping_b.action is GattProfileAction.BUILT
    assert mapping_b.service_handle == 10
    assert mapping_b.characteristic_for_role("read-write").uuid.startswith("fff10001")
    assert ("remove-service", 10) in session.calls


def test_missing_gatt_command_is_rejected_before_build() -> None:
    with pytest.raises(GattProfileError, match="set-value"):
        require_gatt_commands(GATT_COMMAND_MASK & ~(1 << 0x06))
    with pytest.raises(GattProfileError, match="remove-handle-from-db"):
        require_gatt_remove_command(GATT_COMMAND_MASK & ~(1 << 0x23))


def test_writable_characteristic_requires_an_explicit_nonempty_initial_value() -> None:
    profile = PeripheralProfile.load(ACTIVE_PROFILE)
    characteristics = tuple(
        replace(item, initial_value=b"") if item.role == "write-only" else item
        for item in profile.service.characteristics
    )
    invalid = replace(profile, service=replace(profile.service, characteristics=characteristics))
    session = FakeGattSession(invalid)

    with pytest.raises(GattProfileError, match="write-only requires a non-empty initial value"):
        _ = ensure_gatt_profile(session, invalid, command_mask=GATT_COMMAND_MASK)

    assert session.calls == []
