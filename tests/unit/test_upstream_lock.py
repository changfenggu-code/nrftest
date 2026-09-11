from pathlib import Path

import pytest

from tools import setup_upstream
from tools.setup_upstream import LOCK_PATH, ZephyrPin, load_upstream_pins


def test_upstream_lock_uses_repository_and_full_commits() -> None:
    pins = load_upstream_pins(LOCK_PATH)

    assert pins.zephyr.repository.endswith("/zephyr.git")
    assert len(pins.zephyr.commit) == 40
    assert pins.zephyr.commit == pins.zephyr.commit.lower()
    assert pins.zephyr.sdk_version == "1.0.1"
    assert pins.zephyr.sdk_gnu_toolchains == ("arm-zephyr-eabi",)
    assert len(pins.autopts.commit) == 40


def test_zephyr_tag_is_not_required(tmp_path: Path) -> None:
    lock = tmp_path / "upstream.lock.toml"
    _ = lock.write_text(
        """
        schema_version = 1
        [zephyr]
        repository = "https://github.com/VIDLG/zephyr.git"
        commit = "0123456789abcdef0123456789abcdef01234567"
        sdk_version = "1.0.1"
        sdk_gnu_toolchains = ["arm-zephyr-eabi"]
        [autopts]
        repository = "https://github.com/auto-pts/auto-pts.git"
        commit = "fedcba9876543210fedcba9876543210fedcba98"
        """,
        encoding="utf-8",
    )

    pins = load_upstream_pins(lock)

    assert pins.zephyr.repository == "https://github.com/VIDLG/zephyr.git"
    assert not hasattr(pins.zephyr, "tag")


def _zephyr_pin() -> ZephyrPin:
    return ZephyrPin(
        repository="https://github.com/VIDLG/zephyr.git",
        commit="0123456789abcdef0123456789abcdef01234567",
        sdk_version="1.0.1",
        sdk_gnu_toolchains=("arm-zephyr-eabi",),
    )


def test_zephyr_init_uses_full_commit(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    zephyr_root = tmp_path / "workspace" / "zephyr"
    commands: list[list[str]] = []
    monkeypatch.setattr(setup_upstream, "_run", lambda command, **_: commands.append(command))
    monkeypatch.setattr(setup_upstream, "_validate_repository", lambda *args: None)

    setup_upstream._setup_zephyr_sources(_zephyr_pin(), zephyr_root, update=False)

    init = commands[0]
    assert init[:2] == ["west", "init"]
    assert init[init.index("--mr") + 1] == "main"
    assert commands[1] == ["git", "fetch", "origin", _zephyr_pin().commit]
    assert commands[2] == ["git", "checkout", "--detach", _zephyr_pin().commit]
    assert commands[3] == ["west", "update"]


def test_existing_mismatched_checkout_is_rejected_without_reset(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    zephyr_root = tmp_path / "workspace" / "zephyr"
    (zephyr_root / ".git").mkdir(parents=True)
    commands: list[list[str]] = []

    def capture(command: list[str], *, cwd: Path) -> str:
        assert cwd == zephyr_root
        assert command == ["git", "remote", "get-url", "origin"]
        return "https://github.com/zephyrproject-rtos/zephyr.git"

    monkeypatch.setattr(setup_upstream, "_capture", capture)
    monkeypatch.setattr(setup_upstream, "_run", lambda command, **_: commands.append(command))

    with pytest.raises(setup_upstream.SetupError, match="independent workspace"):
        setup_upstream._setup_zephyr_sources(_zephyr_pin(), zephyr_root, update=False)

    assert commands == []


def test_old_completion_marker_is_not_treated_as_ready(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    workspace_root = tmp_path / "workspace"
    zephyr_root = workspace_root / "zephyr"
    (workspace_root / ".west").mkdir(parents=True)
    (zephyr_root / ".git").mkdir(parents=True)
    marker = workspace_root / setup_upstream.SOURCES_READY_MARKER
    _ = marker.write_text("zephyr=old\n", encoding="utf-8")
    commands: list[list[str]] = []
    monkeypatch.setattr(setup_upstream, "_validate_repository", lambda *args: None)
    monkeypatch.setattr(setup_upstream, "_run", lambda command, **_: commands.append(command))

    setup_upstream._setup_zephyr_sources(_zephyr_pin(), zephyr_root, update=False)

    assert commands == [["west", "update"]]
    assert marker.read_text(encoding="utf-8") == f"zephyr={_zephyr_pin().commit}\n"
