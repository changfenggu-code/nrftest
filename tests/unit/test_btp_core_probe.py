from __future__ import annotations

import pytest

from host.nrftest.autopts_adapter import (
    KNOWN_UPSTREAM_UNREGISTER_ERRORS,
    AutoPtsAdapterError,
    autopts_tty_file,
    cleanup_classification,
)


def test_windows_com_port_maps_to_cygwin_tty_for_autopts_socat() -> None:
    assert autopts_tty_file("COM1", platform="win32") == "/dev/ttyS0"
    assert autopts_tty_file("com13", platform="win32") == "/dev/ttyS12"


def test_non_windows_serial_path_is_unchanged() -> None:
    assert autopts_tty_file("/dev/ttyACM0", platform="linux") == "/dev/ttyACM0"
    assert autopts_tty_file("/dev/cu.usbmodem123", platform="darwin") == "/dev/cu.usbmodem123"


def test_known_upstream_unregister_status_defect_is_classified_separately() -> None:
    assert cleanup_classification([]) == "clean"
    assert (
        cleanup_classification(sorted(KNOWN_UPSTREAM_UNREGISTER_ERRORS))
        == "known-upstream-unregister-status-defect"
    )
    assert cleanup_classification(["controller stop: TimeoutError"]) == "unexpected"


@pytest.mark.parametrize("port", ["COM0", "COM", "ttyS12", "COM-1"])
def test_invalid_windows_com_port_is_rejected(port: str) -> None:
    with pytest.raises(AutoPtsAdapterError, match="invalid Windows COM port"):
        _ = autopts_tty_file(port, platform="win32")
