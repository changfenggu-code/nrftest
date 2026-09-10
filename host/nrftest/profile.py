from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import cast


class ProfileError(ValueError):
    """Raised when a Peripheral profile violates the fixture contract."""


_ROLE_PROPERTIES: dict[str, frozenset[str]] = {
    "read-write": frozenset({"read", "write", "write_without_response"}),
    "updates": frozenset({"read", "notify", "indicate"}),
    "read-only": frozenset({"read"}),
    "write-only": frozenset({"write", "write_without_response"}),
}
_ALLOWED_PROPERTIES: frozenset[str] = frozenset(
    {"read", "write", "write_without_response", "notify", "indicate"}
)
BASIC_GATT_ROLES = frozenset(_ROLE_PROPERTIES)


def _require_object(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ProfileError(f"{field} must be an object")
    untyped = cast(Mapping[object, object], value)
    if any(not isinstance(key, str) for key in untyped):
        raise ProfileError(f"{field} must use string keys")
    return cast(Mapping[str, object], untyped)


def _require_list(value: object, field: str) -> list[object]:
    if not isinstance(value, list):
        raise ProfileError(f"{field} must be a list")
    return cast(list[object], value)


def _require_string(document: Mapping[str, object], key: str) -> str:
    value = document.get(key)
    if not isinstance(value, str) or not value:
        raise ProfileError(f"{key} must be a non-empty string")
    return value


def _parse_uuid(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise ProfileError(f"{field} must be a UUID string")
    try:
        parsed = uuid.UUID(value)
    except ValueError as error:
        raise ProfileError(f"{field} must be a valid UUID") from error
    if len(value.replace("-", "")) != 32:
        raise ProfileError(f"{field} must be a 128-bit UUID")
    return str(parsed)


def _parse_hex(value: object, field: str) -> bytes:
    if value is None:
        return b""
    if not isinstance(value, str):
        raise ProfileError(f"{field} must be a hexadecimal string")
    try:
        return bytes.fromhex(value)
    except ValueError as error:
        raise ProfileError(f"{field} must contain an even number of hex digits") from error


@dataclass(frozen=True)
class CharacteristicSpec:
    role: str
    uuid: str
    properties: frozenset[str]
    initial_value: bytes

    @classmethod
    def from_document(cls, document: Mapping[str, object], index: int) -> CharacteristicSpec:
        role = _require_string(document, "role")
        if role not in BASIC_GATT_ROLES:
            raise ProfileError(f"characteristics[{index}].role is not supported: {role}")

        char_uuid = _parse_uuid(document.get("uuid"), f"characteristics[{index}].uuid")
        raw_properties = _require_list(
            document.get("properties"), f"characteristics[{index}].properties"
        )
        if not raw_properties:
            raise ProfileError(f"characteristics[{index}].properties must be a non-empty list")
        if any(not isinstance(item, str) for item in raw_properties):
            raise ProfileError(f"characteristics[{index}].properties must contain strings")
        string_properties = cast(list[str], raw_properties)
        properties = frozenset(string_properties)
        unsupported = properties - _ALLOWED_PROPERTIES
        if unsupported:
            names = ", ".join(sorted(unsupported))
            raise ProfileError(f"characteristics[{index}] has unsupported properties: {names}")
        if len(properties) != len(string_properties):
            raise ProfileError(f"characteristics[{index}].properties must not contain duplicates")
        expected_properties = _ROLE_PROPERTIES[role]
        if properties != expected_properties:
            expected = ", ".join(sorted(expected_properties))
            raise ProfileError(
                f"characteristics[{index}].properties for role {role} must be: {expected}"
            )

        initial_value = _parse_hex(
            document.get("initial_value_hex"),
            f"characteristics[{index}].initial_value_hex",
        )
        return cls(role, char_uuid, properties, initial_value)

    @property
    def readable(self) -> bool:
        return "read" in self.properties

    @property
    def writable(self) -> bool:
        return "write" in self.properties

    @property
    def writable_without_response(self) -> bool:
        return "write_without_response" in self.properties

    @property
    def notifiable(self) -> bool:
        return "notify" in self.properties

    @property
    def indicatable(self) -> bool:
        return "indicate" in self.properties


@dataclass(frozen=True)
class ServiceSpec:
    uuid: str
    primary: bool
    advertise: bool
    characteristics: tuple[CharacteristicSpec, ...]

    @classmethod
    def from_document(cls, document: Mapping[str, object]) -> ServiceSpec:
        service_uuid = _parse_uuid(document.get("uuid"), "services[0].uuid")
        primary = document.get("primary")
        advertise = document.get("advertise")
        if not isinstance(primary, bool):
            raise ProfileError("services[0].primary must be a boolean")
        if not isinstance(advertise, bool):
            raise ProfileError("services[0].advertise must be a boolean")
        raw_characteristics = _require_list(
            document.get("characteristics"), "services[0].characteristics"
        )
        if not raw_characteristics:
            raise ProfileError("services[0].characteristics must be a non-empty list")
        characteristics = tuple(
            CharacteristicSpec.from_document(
                _require_object(item, f"services[0].characteristics[{index}]"), index
            )
            for index, item in enumerate(raw_characteristics)
        )
        roles = [item.role for item in characteristics]
        if len(set(roles)) != len(roles):
            raise ProfileError("characteristic roles must be unique")
        characteristic_uuids = [item.uuid for item in characteristics]
        if len(set(characteristic_uuids)) != len(characteristic_uuids):
            raise ProfileError("characteristic UUIDs must be unique")
        if service_uuid in characteristic_uuids:
            raise ProfileError("service and characteristic UUIDs must be unique")
        return cls(service_uuid, primary, advertise, characteristics)


@dataclass(frozen=True)
class PeripheralProfile:
    schema_version: int
    profile_id: str
    local_name: str
    require_local_name: bool
    advertised_service_uuids: tuple[str, ...]
    service: ServiceSpec

    @classmethod
    def from_document(cls, raw: object) -> PeripheralProfile:
        document = _require_object(raw, "profile root")
        schema_version = document.get("schema_version")
        if (
            not isinstance(schema_version, int)
            or isinstance(schema_version, bool)
            or schema_version != 1
        ):
            raise ProfileError("schema_version must be 1")

        profile_id = _require_string(document, "profile_id")
        advertising = _require_object(document.get("advertising"), "advertising")
        local_name = _require_string(advertising, "local_name")
        require_local_name = advertising.get("require_local_name")
        if not isinstance(require_local_name, bool):
            raise ProfileError("advertising.require_local_name must be a boolean")
        raw_advertised = _require_list(
            advertising.get("service_uuids"), "advertising.service_uuids"
        )
        if not raw_advertised:
            raise ProfileError("advertising.service_uuids must be a non-empty list")
        advertised = tuple(
            _parse_uuid(value, f"advertising.service_uuids[{index}]")
            for index, value in enumerate(raw_advertised)
        )

        raw_services = _require_list(document.get("services"), "services")
        if len(raw_services) != 1:
            raise ProfileError("exactly one root service is required")
        service = ServiceSpec.from_document(_require_object(raw_services[0], "services[0]"))
        if advertised != (service.uuid,):
            raise ProfileError("advertised service must be the single root service")
        if not service.primary or not service.advertise:
            raise ProfileError("the root service must be primary and advertised")
        return cls(
            schema_version,
            profile_id,
            local_name,
            require_local_name,
            advertised,
            service,
        )

    @classmethod
    def load(cls, path: str | Path) -> PeripheralProfile:
        profile_path = Path(path)
        try:
            raw = cast(object, json.loads(profile_path.read_text(encoding="utf-8")))
        except OSError as error:
            raise ProfileError(f"unable to read profile {profile_path}: {error}") from error
        except json.JSONDecodeError as error:
            raise ProfileError(f"invalid JSON in profile {profile_path}: {error}") from error
        return cls.from_document(raw)

    def characteristic_for_role(self, role: str) -> CharacteristicSpec:
        for characteristic in self.service.characteristics:
            if characteristic.role == role:
                return characteristic
        raise ProfileError(f"profile does not define characteristic role: {role}")

    def semantic_signature(self) -> dict[str, object]:
        """Return provider-independent semantics for profile drift checks."""
        return {
            "schema_version": self.schema_version,
            "require_local_name": self.require_local_name,
            "primary": self.service.primary,
            "advertise": self.service.advertise,
            "characteristics": [
                {
                    "role": item.role,
                    "uuid": item.uuid,
                    "properties": sorted(item.properties),
                    "initial_value_hex": item.initial_value.hex(),
                }
                for item in self.service.characteristics
            ],
        }


def require_basic_gatt_roles(profile: PeripheralProfile) -> None:
    defined = {characteristic.role for characteristic in profile.service.characteristics}
    missing = BASIC_GATT_ROLES - defined
    if missing:
        names = ", ".join(sorted(missing))
        raise ProfileError(f"basic GATT profile is missing required roles: {names}")


def profile_source_sha256(path: str | Path) -> str:
    profile_path = Path(path)
    try:
        content = profile_path.read_bytes()
    except OSError as error:
        raise ProfileError(f"unable to read profile {profile_path}: {error}") from error
    return sha256(content).hexdigest()
