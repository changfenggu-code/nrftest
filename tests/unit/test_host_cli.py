from pathlib import Path

from host.nrftest.cli import DEFAULT_PROFILE
from host.nrftest.profile import PeripheralProfile


def test_default_profile_is_independent_of_current_working_directory(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)

    profile = PeripheralProfile.load(DEFAULT_PROFILE)

    assert DEFAULT_PROFILE.is_absolute()
    assert profile.profile_id == "blehub-nrf52840-basic-gatt-v1"
