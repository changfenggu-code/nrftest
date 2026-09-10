from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


class ProfileError(ValueError):
    """Raised when a Peripheral profile violates the fixture contract."""


_ALLOWED_PROPERTIES = {
    "read",
    "write",
    "write_without_response",
    "notify",
    "indicate",
}
_ALLOWED_ROLES = {"read-write", "updates", "read-only", "write-only"}


def _require_string(document: Mapping[str, Any], key: str) -> str:
    value = document.get(key)
    if not isinstance(value, str) or not value:
        raise ProfileError(f"{key} must be a non-empty string")
    return value


def _parse_uuid(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise ProfileError(f"{field} must be a UUID string")
    try:
        parsed = uuid.UUID(value)
    except ValueError as error:
        raise ProfileError(f"{field} must be a valid UUID") from error
    if parsed.version is None or len(value.replace("-", "")) != 32:
        raise ProfileError(f"{field} must be a 128-bit UUID")
    return str(parsed)


def _parse_hex(value: Any, field: str) -> bytes:
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
    def from_document(cls, document: Mapping[str, Any], index: int) -> CharacteristicSpec:
        role = _require_string(document, "role")
        if role not in _ALLOWED_ROLES:
            raise ProfileError(f"characteristics[{index}].role is not supported: {role}")

        char_uuid = _parse_uuid(document.get("uuid"), f"characteristics[{index}].uuid")
        raw_properties = document.get("properties")
        if not isinstance(raw_properties, list) or not raw_properties:
            raise ProfileError(f"characteristics[{index}].properties must be a non-empty list")
        if any(not isinstance(item, str) for item in raw_properties):
            raise ProfileError(f"characteristics[{index}].properties must contain strings")
        properties = frozenset(raw_properties)
        unsupported = properties - _ALLOWED_PROPERTIES
        if unsupported:
            names = ", ".join(sorted(unsupported))
            raise ProfileError(f"characteristics[{index}] has unsupported properties: {names}")
        if len(properties) != len(raw_properties):
            raise ProfileError(f"characteristics[{index}].properties must not contain duplicates")

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
    def from_document(cls, document: Mapping[str, Any]) -> ServiceSpec:
        service_uuid = _parse_uuid(document.get("uuid"), "services[0].uuid")
        primary = document.get("primary")
        advertise = document.get("advertise")
        if not isinstance(primary, bool):
            raise ProfileError("services[0].primary must be a boolean")
        if not isinstance(advertise, bool):
            raise ProfileError("services[0].advertise must be a boolean")
        raw_characteristics = document.get("characteristics")
        if not isinstance(raw_characteristics, list) or not raw_characteristics:
            raise ProfileError("services[0].characteristics must be a non-empty list")
        characteristics = tuple(
            CharacteristicSpec.from_document(item, index)
            for index, item in enumerate(raw_characteristics)
            if isinstance(item, Mapping)
        )
        if len(characteristics) != len(raw_characteristics):
            raise ProfileError("each characteristic must be an object")
        roles = [item.role for item in characteristics]
        if len(set(roles)) != len(roles):
            raise ProfileError("characteristic roles must be unique")
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
    def from_document(cls, raw: Any) -> PeripheralProfile:
        if not isinstance(raw, Mapping):
            raise ProfileError("profile root must be an object")
        schema_version = raw.get("schema_version")
        if schema_version != 1:
            raise ProfileError("schema_version must be 1")

        profile_id = _require_string(raw, "profile_id")
        advertising = raw.get("advertising")
        if not isinstance(advertising, Mapping):
            raise ProfileError("advertising must be an object")
        local_name = _require_string(advertising, "local_name")
        require_local_name = advertising.get("require_local_name")
        if not isinstance(require_local_name, bool):
            raise ProfileError("advertising.require_local_name must be a boolean")
        raw_advertised = advertising.get("service_uuids")
        if not isinstance(raw_advertised, list) or not raw_advertised:
            raise ProfileError("advertising.service_uuids must be a non-empty list")
        advertised = tuple(
            _parse_uuid(value, f"advertising.service_uuids[{index}]")
            for index, value in enumerate(raw_advertised)
        )

        raw_services = raw.get("services")
        if not isinstance(raw_services, list) or len(raw_services) != 1:
            raise ProfileError("exactly one root service is required")
        if not isinstance(raw_services[0], Mapping):
            raise ProfileError("services[0] must be an object")
        service = ServiceSpec.from_document(raw_services[0])
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
            raw = json.loads(profile_path.read_text(encoding="utf-8"))
        except OSError as error:
            raise ProfileError(f"unable to read profile {profile_path}: {error}") from error
        except json.JSONDecodeError as error:
            raise ProfileError(f"invalid JSON in profile {profile_path}: {error}") from error
        return cls.from_document(raw)

    def semantic_signature(self) -> dict[str, Any]:
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
