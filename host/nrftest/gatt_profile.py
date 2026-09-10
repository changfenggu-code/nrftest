from __future__ import annotations

import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from host.nrftest.autopts_adapter import (
    GattAttributeSnapshot,
    GattAttributeValueSnapshot,
)
from host.nrftest.profile import (
    CharacteristicSpec,
    PeripheralProfile,
    ProfileError,
    require_basic_gatt_roles,
)

PRIMARY_SERVICE_UUID = "2800"
SECONDARY_SERVICE_UUID = "2801"
CHARACTERISTIC_DECLARATION_UUID = "2803"
CCC_UUID = "2902"
ATT_SUCCESS = 0x00
ATT_INSUFFICIENT_ENCRYPTION_KEY_SIZE = 0x0C
MAX_TESTER_ATTRIBUTES = 49
MAX_TESTER_CCC = 2
TESTER_PREPARE_WRITE_PERMISSION = 0x40

_PROPERTY_BITS = {
    "read": 1 << 1,
    "write_without_response": 1 << 2,
    "write": 1 << 3,
    "notify": 1 << 4,
    "indicate": 1 << 5,
}
_PERMISSION_BITS = {
    "read": 1 << 0,
    "write": 1 << 1,
}
_REQUIRED_GATT_COMMANDS = {
    0x02: "add-service",
    0x03: "add-characteristic",
    0x04: "add-descriptor",
    0x06: "set-value",
    0x07: "start-server",
    0x1C: "get-attributes",
    0x1D: "get-attribute-value",
}
_REMOVE_SERVICE_COMMAND = 0x23


class GattProfileError(RuntimeError):
    """Raised when a Profile cannot be represented or verified by the fixed Tester."""


class GattProfileAction(StrEnum):
    BUILT = "built"
    ATTACHED = "attached"


class GattProfileSession(Protocol):
    def gatt_add_primary_service(self, uuid: str) -> None: ...

    def gatt_add_characteristic(self, uuid: str, *, properties: int, permissions: int) -> None: ...

    def gatt_set_value(self, handle: int, value: bytes) -> None: ...

    def gatt_set_last_value(self, value: bytes) -> None: ...

    def gatt_add_ccc(self, *, permissions: int) -> None: ...

    def gatt_start_server(self) -> None: ...

    def gatt_get_attributes(
        self,
        *,
        start_handle: int = 0x0001,
        end_handle: int = 0xFFFF,
        type_uuid: str | None = None,
    ) -> tuple[GattAttributeSnapshot, ...]: ...

    def gatt_get_attribute_value(
        self,
        handle: int,
        *,
        peer_address: str = "000000000000",
        peer_address_type: int = 0,
    ) -> GattAttributeValueSnapshot: ...

    def gatt_remove_service_containing_handle(self, handle: int) -> None: ...


@dataclass(frozen=True)
class CharacteristicHandles:
    role: str
    uuid: str
    declaration_handle: int
    value_handle: int
    ccc_handle: int | None


@dataclass(frozen=True)
class GattProfileMapping:
    action: GattProfileAction
    service_handle: int
    service_end_handle: int
    characteristics: tuple[CharacteristicHandles, ...]
    attributes: tuple[GattAttributeSnapshot, ...]

    def characteristic_for_role(self, role: str) -> CharacteristicHandles:
        for characteristic in self.characteristics:
            if characteristic.role == role:
                return characteristic
        raise GattProfileError(f"GATT mapping does not define characteristic role: {role}")


@dataclass(frozen=True)
class GattProfileRemoval:
    service_handle: int
    removed_attribute_handles: tuple[int, ...]
    remaining_attribute_count: int


def require_gatt_commands(command_mask: int) -> None:
    missing = [
        name
        for opcode, name in _REQUIRED_GATT_COMMANDS.items()
        if command_mask & (1 << opcode) == 0
    ]
    if missing:
        raise GattProfileError(
            f"fixed Tester GATT command mask 0x{command_mask:x} is missing: " + ", ".join(missing)
        )


def require_gatt_remove_command(command_mask: int) -> None:
    if command_mask & (1 << _REMOVE_SERVICE_COMMAND) == 0:
        raise GattProfileError(
            f"fixed Tester GATT command mask 0x{command_mask:x} is missing: remove-handle-from-db"
        )


def gatt_profile_service_range(
    session: GattProfileSession, profile: PeripheralProfile
) -> tuple[int, int] | None:
    return _find_service_range(session, profile.service.uuid)


def remove_gatt_profile(
    session: GattProfileSession,
    profile: PeripheralProfile,
    mapping: GattProfileMapping,
    *,
    command_mask: int,
) -> GattProfileRemoval:
    require_gatt_remove_command(command_mask)
    current_range = _find_service_range(session, profile.service.uuid)
    expected_range = (mapping.service_handle, mapping.service_end_handle)
    if current_range != expected_range:
        raise GattProfileError(
            f"profile removal mapping is stale: expected {expected_range}, found {current_range}"
        )

    removed_handles = tuple(attribute.handle for attribute in mapping.attributes)
    try:
        session.gatt_remove_service_containing_handle(mapping.service_handle)
    except Exception as error:
        raise GattProfileError(
            f"dynamic service removal for handle {mapping.service_handle} failed: "
            + f"{type(error).__name__}: {error}"
        ) from error

    if _find_service_range(session, profile.service.uuid) is not None:
        raise GattProfileError("dynamic root service remained visible after removal")
    remaining = session.gatt_get_attributes()
    remaining_handles = {attribute.handle for attribute in remaining}
    stale_handles = sorted(remaining_handles.intersection(removed_handles))
    if stale_handles:
        raise GattProfileError(
            "removed dynamic attributes remain in the effective database: "
            + ", ".join(str(handle) for handle in stale_handles)
        )
    return GattProfileRemoval(mapping.service_handle, removed_handles, len(remaining))


def ensure_gatt_profile(
    session: GattProfileSession,
    profile: PeripheralProfile,
    *,
    command_mask: int,
) -> GattProfileMapping:
    require_gatt_commands(command_mask)
    try:
        require_basic_gatt_roles(profile)
    except ProfileError as error:
        raise GattProfileError(str(error)) from error
    _validate_tester_resource_boundary(profile)

    existing = _find_service_range(session, profile.service.uuid)
    if existing is None:
        _build_profile(session, profile)
        action = GattProfileAction.BUILT
    else:
        action = GattProfileAction.ATTACHED

    mapping = _map_profile(session, profile, action)
    if action is GattProfileAction.ATTACHED:
        for characteristic in profile.service.characteristics:
            handles = mapping.characteristic_for_role(characteristic.role)
            try:
                session.gatt_set_value(handles.value_handle, characteristic.initial_value)
            except Exception as error:
                raise GattProfileError(
                    f"role {characteristic.role} value reset failed: "
                    + f"{type(error).__name__}: {error}"
                ) from error
    _verify_readable_initial_values(session, profile, mapping)
    return mapping


def _validate_tester_resource_boundary(profile: PeripheralProfile) -> None:
    ccc_count = sum(_needs_ccc(item) for item in profile.service.characteristics)
    attribute_count = 1 + 2 * len(profile.service.characteristics) + ccc_count
    if attribute_count > MAX_TESTER_ATTRIBUTES:
        raise GattProfileError(
            f"Profile requires {attribute_count} attributes; fixed Tester allows "
            + f"{MAX_TESTER_ATTRIBUTES} dynamic attributes"
        )
    if ccc_count > MAX_TESTER_CCC:
        raise GattProfileError(
            f"Profile requires {ccc_count} CCC descriptors; fixed Tester allows {MAX_TESTER_CCC}"
        )
    for characteristic in profile.service.characteristics:
        if (characteristic.writable or characteristic.writable_without_response) and not (
            characteristic.initial_value
        ):
            raise GattProfileError(
                f"writable role {characteristic.role} requires a non-empty initial value "
                + "to allocate the fixed Tester value buffer"
            )


def _build_profile(session: GattProfileSession, profile: PeripheralProfile) -> None:
    session.gatt_add_primary_service(profile.service.uuid)
    for characteristic in profile.service.characteristics:
        session.gatt_add_characteristic(
            characteristic.uuid,
            properties=_property_bits(characteristic),
            permissions=_permission_bits(characteristic),
        )
        session.gatt_set_last_value(characteristic.initial_value)
        if _needs_ccc(characteristic):
            session.gatt_add_ccc(permissions=_PERMISSION_BITS["read"] | _PERMISSION_BITS["write"])
    session.gatt_start_server()


def _map_profile(
    session: GattProfileSession,
    profile: PeripheralProfile,
    action: GattProfileAction,
) -> GattProfileMapping:
    service_range = _find_service_range(session, profile.service.uuid)
    if service_range is None:
        raise GattProfileError("dynamic root service was not present after Start Server")
    service_handle, service_end_handle = service_range
    attributes = session.gatt_get_attributes(
        start_handle=service_handle,
        end_handle=service_end_handle,
    )
    expected_count = (
        1
        + 2 * len(profile.service.characteristics)
        + sum(_needs_ccc(item) for item in profile.service.characteristics)
    )
    if len(attributes) != expected_count:
        raise GattProfileError(
            f"dynamic service exposes {len(attributes)} attributes; expected {expected_count}"
        )

    declarations = tuple(
        attribute
        for attribute in attributes
        if _normalize_uuid(attribute.type_uuid) == CHARACTERISTIC_DECLARATION_UUID
    )
    if len(declarations) != len(profile.service.characteristics):
        raise GattProfileError(
            f"dynamic service exposes {len(declarations)} characteristic declarations; "
            + f"expected {len(profile.service.characteristics)}"
        )

    mapped: list[CharacteristicHandles] = []
    for characteristic in profile.service.characteristics:
        value_attributes = tuple(
            attribute
            for attribute in attributes
            if _normalize_uuid(attribute.type_uuid) == characteristic.uuid
        )
        if len(value_attributes) != 1:
            raise GattProfileError(
                f"role {characteristic.role} has {len(value_attributes)} value attributes; "
                + "expected 1"
            )
        value_attribute = value_attributes[0]
        expected_permissions = _permission_bits(characteristic) | TESTER_PREPARE_WRITE_PERMISSION
        if value_attribute.permissions != expected_permissions:
            raise GattProfileError(
                f"role {characteristic.role} permissions are 0x{value_attribute.permissions:x}; "
                + f"expected 0x{expected_permissions:x}"
            )

        declaration = _find_declaration(
            session, declarations, characteristic, value_attribute.handle
        )

        ccc_handle = _find_ccc_handle(
            attributes,
            declarations,
            declaration.handle,
            value_attribute.handle,
            required=_needs_ccc(characteristic),
            role=characteristic.role,
        )
        mapped.append(
            CharacteristicHandles(
                role=characteristic.role,
                uuid=characteristic.uuid,
                declaration_handle=declaration.handle,
                value_handle=value_attribute.handle,
                ccc_handle=ccc_handle,
            )
        )

    service_attribute = tuple(
        attribute for attribute in attributes if attribute.handle == service_handle
    )
    if (
        len(service_attribute) != 1
        or _normalize_uuid(service_attribute[0].type_uuid) != PRIMARY_SERVICE_UUID
    ):
        raise GattProfileError("dynamic root service handle is not a primary service declaration")

    return GattProfileMapping(
        action=action,
        service_handle=service_handle,
        service_end_handle=service_end_handle,
        characteristics=tuple(mapped),
        attributes=attributes,
    )


def _find_service_range(session: GattProfileSession, service_uuid: str) -> tuple[int, int] | None:
    services = sorted(
        (
            *session.gatt_get_attributes(type_uuid=PRIMARY_SERVICE_UUID),
            *session.gatt_get_attributes(type_uuid=SECONDARY_SERVICE_UUID),
        ),
        key=lambda item: item.handle,
    )
    matches = [
        index
        for index, service in enumerate(services)
        if _read_uuid_attribute(session, service.handle) == service_uuid
    ]
    if not matches:
        return None
    if len(matches) != 1:
        raise GattProfileError(f"root service UUID {service_uuid} appears {len(matches)} times")
    index = matches[0]
    service_handle = services[index].handle
    end_handle = services[index + 1].handle - 1 if index + 1 < len(services) else 0xFFFF
    return service_handle, end_handle


def _find_declaration(
    session: GattProfileSession,
    declarations: tuple[GattAttributeSnapshot, ...],
    characteristic: CharacteristicSpec,
    value_handle: int,
) -> GattAttributeSnapshot:
    matches: list[GattAttributeSnapshot] = []
    for declaration in declarations:
        value = _read_metadata_value(session, declaration.handle)
        properties, declared_handle, declared_uuid = _decode_characteristic_declaration(value)
        if declared_uuid != characteristic.uuid:
            continue
        if properties != _property_bits(characteristic):
            raise GattProfileError(
                f"role {characteristic.role} declaration properties are 0x{properties:x}; "
                + f"expected 0x{_property_bits(characteristic):x}"
            )
        if declared_handle != value_handle:
            raise GattProfileError(
                f"role {characteristic.role} declaration points to {declared_handle}; "
                + f"enumeration returned {value_handle}"
            )
        matches.append(declaration)
    if len(matches) != 1:
        raise GattProfileError(
            f"role {characteristic.role} has {len(matches)} matching declarations; expected 1"
        )
    return matches[0]


def _find_ccc_handle(
    attributes: tuple[GattAttributeSnapshot, ...],
    declarations: tuple[GattAttributeSnapshot, ...],
    declaration_handle: int,
    value_handle: int,
    *,
    required: bool,
    role: str,
) -> int | None:
    next_declarations = [
        declaration.handle
        for declaration in declarations
        if declaration.handle > declaration_handle
    ]
    segment_end = min(next_declarations) if next_declarations else 0x10000
    ccc = tuple(
        attribute
        for attribute in attributes
        if value_handle < attribute.handle < segment_end
        and _normalize_uuid(attribute.type_uuid) == CCC_UUID
    )
    expected_count = 1 if required else 0
    if len(ccc) != expected_count:
        raise GattProfileError(
            f"role {role} has {len(ccc)} CCC descriptors; expected {expected_count}"
        )
    return ccc[0].handle if ccc else None


def _verify_readable_initial_values(
    session: GattProfileSession,
    profile: PeripheralProfile,
    mapping: GattProfileMapping,
) -> None:
    for characteristic in profile.service.characteristics:
        if not characteristic.readable:
            continue
        handles = mapping.characteristic_for_role(characteristic.role)
        actual = _require_success_value(session, handles.value_handle)
        if actual != characteristic.initial_value:
            raise GattProfileError(
                f"role {characteristic.role} initial value is {actual.hex()}; "
                + f"expected {characteristic.initial_value.hex()}"
            )


def _require_success_value(session: GattProfileSession, handle: int) -> bytes:
    try:
        response = session.gatt_get_attribute_value(handle)
    except Exception as error:
        raise GattProfileError(
            f"local attribute read for handle {handle} failed: {type(error).__name__}: {error}"
        ) from error
    if response.att_response != ATT_SUCCESS:
        raise GattProfileError(
            f"local attribute read for handle {handle} returned ATT 0x{response.att_response:02x}"
        )
    return response.value


def _decode_characteristic_declaration(value: bytes) -> tuple[int, int, str]:
    if len(value) not in {5, 19}:
        raise GattProfileError(
            f"characteristic declaration length is {len(value)}; expected 5 or 19"
        )
    properties = value[0]
    value_handle = int.from_bytes(value[1:3], "little")
    return properties, value_handle, _decode_uuid_le(value[3:])


def _read_uuid_attribute(session: GattProfileSession, handle: int) -> str:
    return _decode_uuid_le(_read_metadata_value(session, handle))


def _read_metadata_value(session: GattProfileSession, handle: int) -> bytes:
    try:
        response = session.gatt_get_attribute_value(handle)
    except Exception as error:
        raise GattProfileError(
            f"local metadata read for handle {handle} failed: {type(error).__name__}: {error}"
        ) from error
    if response.att_response not in {ATT_SUCCESS, ATT_INSUFFICIENT_ENCRYPTION_KEY_SIZE}:
        raise GattProfileError(
            f"local metadata read for handle {handle} returned ATT 0x{response.att_response:02x}"
        )
    # Fixed Tester casts every dynamic attribute user_data to gatt_value when applying its
    # encryption-key-size test. Service and characteristic declarations use different
    # user_data layouts, so they can carry valid metadata with a spurious ATT 0x0c status.
    return response.value


def _decode_uuid_le(value: bytes) -> str:
    if len(value) == 2:
        return f"{int.from_bytes(value, 'little'):04x}"
    if len(value) == 16:
        return str(uuid.UUID(bytes=value[::-1]))
    raise GattProfileError(f"attribute UUID value has unsupported length {len(value)}")


def _normalize_uuid(value: str) -> str:
    compact = value.replace("-", "").lower()
    if len(compact) == 4:
        return compact
    if len(compact) == 32:
        try:
            return str(uuid.UUID(hex=compact))
        except ValueError as error:
            raise GattProfileError(f"AutoPTS returned invalid attribute UUID: {value}") from error
    raise GattProfileError(f"AutoPTS returned unsupported attribute UUID: {value}")


def _property_bits(characteristic: CharacteristicSpec) -> int:
    return sum(_PROPERTY_BITS[property_name] for property_name in characteristic.properties)


def _permission_bits(characteristic: CharacteristicSpec) -> int:
    permissions = 0
    if characteristic.readable:
        permissions |= _PERMISSION_BITS["read"]
    if characteristic.writable or characteristic.writable_without_response:
        permissions |= _PERMISSION_BITS["write"]
    return permissions


def _needs_ccc(characteristic: CharacteristicSpec) -> bool:
    return characteristic.notifiable or characteristic.indicatable
