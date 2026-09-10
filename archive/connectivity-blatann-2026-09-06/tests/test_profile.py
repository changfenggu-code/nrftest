from __future__ import annotations

import json
from pathlib import Path

import pytest

from .nrftest.profile import PeripheralProfile, ProfileError


PROJECT_ROOT = Path(__file__).resolve().parents[1]


PROFILE = {
    "schema_version": 1,
    "profile_id": "blehub-nrf52840-basic-gatt-v1",
    "advertising": {
        "local_name": "BleHubNrf52840",
        "require_local_name": False,
        "service_uuids": ["fd000000-0000-4000-8000-000000000000"],
    },
    "services": [
        {
            "uuid": "fd000000-0000-4000-8000-000000000000",
            "primary": True,
            "advertise": True,
            "characteristics": [
                {
                    "role": "read-write",
                    "uuid": "fff10000-0000-4000-8000-000000000000",
                    "properties": ["read", "write", "write_without_response"],
                    "initial_value_hex": "00",
                },
                {
                    "role": "updates",
                    "uuid": "fff20000-0000-4000-8000-000000000000",
                    "properties": ["read", "notify", "indicate"],
                    "initial_value_hex": "00",
                },
            ],
        }
    ],
}


def test_checked_in_nrf_profile_loads():
    profile = PeripheralProfile.load(
        PROJECT_ROOT / "profiles" / "nrf52840-basic-gatt-v1.json"
    )

    assert profile.profile_id == "blehub-nrf52840-basic-gatt-v1"
    assert profile.local_name == "BleHubNrf52840"
    assert {item.role for item in profile.service.characteristics} == {
        "read-write",
        "updates",
        "read-only",
        "write-only",
    }


def test_loads_and_exposes_provider_independent_signature(tmp_path):
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(PROFILE), encoding="utf-8")

    profile = PeripheralProfile.load(path)

    assert profile.profile_id == "blehub-nrf52840-basic-gatt-v1"
    assert profile.service.characteristics[0].initial_value == b"\x00"
    assert profile.service.characteristics[1].indicatable
    assert profile.semantic_signature()["characteristics"][0]["role"] == "read-write"


def test_rejects_advertising_root_service_mismatch():
    document = json.loads(json.dumps(PROFILE))
    document["advertising"]["service_uuids"] = ["fd000001-0000-4000-8000-000000000000"]

    with pytest.raises(ProfileError, match="advertised service"):
        PeripheralProfile.from_document(document)


def test_rejects_duplicate_characteristic_roles():
    document = json.loads(json.dumps(PROFILE))
    duplicate = dict(document["services"][0]["characteristics"][0])
    document["services"][0]["characteristics"].append(duplicate)

    with pytest.raises(ProfileError, match="roles must be unique"):
        PeripheralProfile.from_document(document)
