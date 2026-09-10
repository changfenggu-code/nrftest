from tools.setup_upstream import LOCK_PATH, load_upstream_pins


def test_upstream_lock_pins_exact_revisions() -> None:
    pins = load_upstream_pins(LOCK_PATH)

    assert pins.zephyr.tag == "v4.4.2"
    assert pins.zephyr.commit == "dccb09599635bdff17633fa7e9dab014b91dce90"
    assert pins.zephyr.sdk_version == "1.0.1"
    assert pins.zephyr.sdk_gnu_toolchains == ("arm-zephyr-eabi",)
    assert pins.autopts.commit == "54e81c7f3495bce72e5f688e9c996b85b8272799"
