from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import cast

from host.nrftest.profile import (
    PeripheralProfile,
    ProfileError,
    profile_source_sha256,
    require_basic_gatt_roles,
)
from tools.config import PROJECT_ROOT

DEFAULT_PROFILE = PROJECT_ROOT / "profiles" / "blehub-nrf-basic-v1.json"


def profile_document(path: Path) -> dict[str, object]:
    profile = PeripheralProfile.load(path)
    require_basic_gatt_roles(profile)
    return {
        "schema_version": profile.schema_version,
        "profile_id": profile.profile_id,
        "source_path": str(path),
        "source_sha256": profile_source_sha256(path),
        "advertising": {
            "local_name": profile.local_name,
            "require_local_name": profile.require_local_name,
            "service_uuids": list(profile.advertised_service_uuids),
        },
        "root_service_uuid": profile.service.uuid,
        "roles": [item.role for item in profile.service.characteristics],
        "semantic_signature": profile.semantic_signature(),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate an nrftest basic GATT Profile")
    _ = parser.add_argument("--profile", type=Path, default=DEFAULT_PROFILE)
    return parser


def main() -> int:
    arguments = _parser().parse_args()
    profile_path = cast(Path, arguments.profile)
    try:
        document = profile_document(profile_path)
    except ProfileError as error:
        print(f"Profile validation failed: {error}", file=sys.stderr)
        return 1

    print(json.dumps(document, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
