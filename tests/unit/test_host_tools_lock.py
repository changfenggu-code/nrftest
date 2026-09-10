from tools.setup_host_tools import LOCK_PATH, load_dtc_pin, load_socat_pin


def test_windows_dtc_lock_pins_portable_archive() -> None:
    pin = load_dtc_pin(LOCK_PATH)

    assert pin.version == "1.6.1"
    assert pin.minimum_version == "1.4.6"
    assert pin.sha256 == "7aac366f989fd2450d5e641e118734653ea29d0ddb7dbfa33521d57afe852ae3"
    assert pin.executable.as_posix() == "usr/bin/dtc.exe"


def test_windows_socat_lock_pins_autopts_recommended_archive() -> None:
    pin = load_socat_pin(LOCK_PATH)

    assert pin.version == "1.7.3.2"
    assert pin.minimum_version == "1.7.3.2"
    assert pin.sha256 == "b86e84d2a5dab2032b0b20558f58f3e8ae899a9bccaca74f6237b2af446ff019"
    assert pin.executable.as_posix() == "socat-1.7.3.2-1-x86_64/socat.exe"
