from tools.setup_firmware_tools import LOCK_PATH, load_firmware_tool_pins


def test_firmware_tools_lock_pins_supported_host_artifacts() -> None:
    pins = load_firmware_tool_pins(LOCK_PATH)

    assert pins.cli_version == "8.2.1"
    assert pins.cli_commit == "350d1fdcdf82d30c115ebb08bdbb9717937f37ac"
    assert pins.command.name == "nrf5sdk-tools"
    assert pins.command.version == "1.1.0"
    assert pins.command.commit == "30dc218a99ce1abd296f9ff5de836eacda2cc474"
    assert set(pins.platforms) == {"win-64", "osx-arm64", "linux-64"}
    assert pins.platforms["win-64"].cli_sha256 == (
        "701f1b0b7c3131c9f4df1e4de3aba6a311ece67656438a3c8a4a37cb7a2df147"
    )
    assert pins.platforms["osx-arm64"].target == "aarch64-apple-darwin"
    assert pins.platforms["linux-64"].command_sha256 == (
        "84b4214c46b917696b171a692a656b0f93e7200dc93dc3ff33fafd6d88b31e4f"
    )
