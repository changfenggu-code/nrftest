from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from host.nrftest.autopts_adapter import (
    APPLICATION_USB_PID,
    APPLICATION_USB_VID,
    AutoPtsAdapterError,
    AutoPtsEventTimeout,
    AutoPtsSession,
    LoadedAutoPts,
    ServiceStartupMode,
    load_autopts,
    select_application_port,
)


@dataclass
class FakePort:
    device: str
    description: str = "nrftest"
    hwid: str = "USB"
    vid: int | None = APPLICATION_USB_VID
    pid: int | None = APPLICATION_USB_PID
    serial_number: str | None = "fixture-1"


class FakeProperty:
    def __init__(self, data: object) -> None:
        self.data = data


class FakeGap:
    def __init__(self) -> None:
        self.current_settings = FakeProperty(
            {
                "Powered": False,
                "Connectable": False,
                "Discoverable": False,
                "Advertising": False,
            }
        )
        self.iut_bd_addr = FakeProperty({"address": None, "type": None})
        self.connections: dict[str, object] = {}
        self.connection_result = False
        self.disconnection_result = False

    def wait_for_connection(
        self, timeout: float, conn_count: int = 1, addr: str | None = None
    ) -> bool:
        del timeout, conn_count, addr
        return self.connection_result

    def wait_for_disconnection(self, timeout: float, addr: str | None = None) -> bool:
        del timeout, addr
        return self.disconnection_result


class FakeGatt:
    def __init__(self) -> None:
        self.values: dict[int, bytes | None] = {}
        self.changed_counts: dict[int, int] = {}
        self.wait_timeouts: list[float | None] = []

    def attr_value_clr_changed(self, handle: int) -> None:
        self.values[handle] = None
        self.changed_counts[handle] = 0

    def attr_value_get(self, handle: int) -> object:
        return self.values.get(handle)

    def attr_value_get_changed_cnt(self, handle: int) -> int:
        return self.changed_counts.get(handle, 0)

    def wait_attr_value_changed(self, handle: int, timeout: float | None = None) -> object:
        self.wait_timeouts.append(timeout)
        return self.values.get(handle)


class FakeStack:
    def __init__(self) -> None:
        self.supported_svcs = 0b111
        self.supported_cmds: dict[str, int] = {}
        self.gap = FakeGap()
        self.gatt = FakeGatt()

    def core_init(self) -> None:
        pass

    def gap_init(self, name: str | bytes | None = None) -> None:
        del name
        self.gap = FakeGap()

    def gatt_init(self) -> None:
        self.gatt = FakeGatt()

    def is_svc_supported(self, service: str) -> bool:
        return service in {"CORE", "GAP", "GATT"}


class FakeController:
    def __init__(self, arguments: object) -> None:
        self.arguments = arguments
        self.stack = FakeStack()
        self.started = False
        self.stopped = False

    def get_stack(self) -> FakeStack:
        return self.stack

    def start(self, test_case: object) -> None:
        del test_case
        self.started = True

    def stop(self) -> None:
        self.stopped = True


class BTPError(Exception):
    pass


class FakeBtp:
    def __init__(
        self,
        *,
        cleanup_defect: bool = False,
        service_probe_error: Exception | None = None,
    ) -> None:
        self.controller: FakeController | None = None
        self.cleanup_defect = cleanup_defect
        self.service_probe_error = service_probe_error
        self.powered_on_calls = 0
        self.connectable_calls = 0
        self.register_calls: list[str] = []
        self.unregister_calls: list[str] = []
        self.registered_services: set[str] = set()
        self.pts_addresses: list[tuple[str, int]] = []
        self.advertising_calls: list[tuple[dict[int, bytes], dict[int, bytes] | None]] = []
        self.removed_gatt_handles: list[int] = []

    @property
    def stack(self) -> FakeStack:
        assert self.controller is not None
        return self.controller.stack

    def init(self, get_iut: object) -> None:
        self.controller = get_iut()  # type: ignore[operator]

    def read_supp_svcs(self) -> None:
        pass

    def read_supported_commands(self, service: str) -> None:
        if service != "CORE" and self.service_probe_error is not None:
            raise self.service_probe_error
        if service != "CORE" and service not in self.registered_services:
            raise BTPError("Error opcode in response!")
        self.stack.supported_cmds[service] = {
            "CORE": 0x1E,
            "GAP": 0x123,
            "GATT": 0x456,
        }[service]

    def core_reg_svc_gap(self) -> None:
        self.register_calls.append("GAP")
        self.registered_services.add("GAP")
        self.stack.supported_cmds["GAP"] = 0x123

    def core_reg_svc_gatt(self) -> None:
        self.register_calls.append("GATT")
        self.registered_services.add("GATT")
        self.stack.supported_cmds["GATT"] = 0x456

    def core_unreg_svc_gap(self) -> None:
        self.unregister_calls.append("GAP")
        self.registered_services.discard("GAP")
        if self.cleanup_defect:
            raise BTPError("Unexpected response received!")

    def core_unreg_svc_gatt(self) -> None:
        self.unregister_calls.append("GATT")
        self.registered_services.discard("GATT")
        if self.cleanup_defect:
            raise BTPError("Error opcode in response!")

    def gap_read_controller_info(self) -> None:
        self.stack.gap.iut_bd_addr.data = {"address": "AABBCCDDEEFF", "type": 1}
        self._setting("Powered", True)
        self._setting("Connectable", True)

    def gap_set_powered_on(self) -> None:
        self.powered_on_calls += 1
        self._setting("Powered", True)

    def gap_set_powered_off(self) -> None:
        self._setting("Powered", False)

    def gap_set_connectable(self) -> None:
        self.connectable_calls += 1
        self._setting("Connectable", True)

    def gap_set_non_connectable(self) -> None:
        self._setting("Connectable", False)

    def gap_set_general_discoverable(self) -> None:
        self._setting("Discoverable", True)

    def gap_set_non_discoverable(self) -> None:
        self._setting("Discoverable", False)

    def gap_start_advertising(
        self, ad: dict[int, bytes], sd: dict[int, bytes] | None = None
    ) -> None:
        self.advertising_calls.append((ad, sd))
        self._setting("Advertising", True)

    def gap_stop_advertising(self) -> None:
        self._setting("Advertising", False)

    def gap_disconnect(self, bd_addr: str, bd_addr_type: int) -> None:
        del bd_addr_type
        self.stack.gap.connections.pop(bd_addr)

    def set_pts_addr(self, addr: str, addr_type: int) -> None:
        self.pts_addresses.append((addr, addr_type))

    def remove_handle_from_db(self, handle: int) -> None:
        self.removed_gatt_handles.append(handle)

    def _setting(self, name: str, enabled: bool) -> None:
        settings = self.stack.gap.current_settings.data
        assert isinstance(settings, dict)
        settings[name] = enabled


class FakeAdType:
    name_full = 9
    uuid16_all = 3


class FakeAddrType:
    le_public = 0
    le_random = 1


def _loaded(btp: FakeBtp, controllers: list[FakeController]) -> LoadedAutoPts:
    def factory(arguments: object) -> FakeController:
        controller = FakeController(arguments)
        controllers.append(controller)
        return controller

    return LoadedAutoPts(
        controller_factory=factory,
        btp=btp,
        btp_error_type=BTPError,
        ad_type=FakeAdType(),
        addr_type=FakeAddrType(),
        uuid_to_le_bytes=lambda value: bytes.fromhex(str(value).replace("-", ""))[::-1],
    )


def _identity():
    return select_application_port(ports=[FakePort("COM13")])


def _write_fake_autopts(root: Path) -> None:
    files = {
        "autopts/__init__.py": "",
        "autopts/ptsprojects/__init__.py": "",
        "autopts/ptsprojects/iutctl.py": "class IutCtl:\n    pass\n",
        "autopts/pybtp/__init__.py": "",
        "autopts/pybtp/btp.py": "",
        "autopts/pybtp/types.py": (
            "class BTPError(Exception):\n    pass\n"
            "class AdType:\n    pass\n"
            "class Addr:\n    pass\n"
            "def uuid_to_le_bytes(value):\n    return bytes()\n"
        ),
    }
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text(content, encoding="utf-8")


def _remove_new_autopts_modules(before: set[str]) -> None:
    for name in set(sys.modules) - before:
        if name == "autopts" or name.startswith("autopts."):
            del sys.modules[name]


def test_load_autopts_binds_to_requested_root_and_restores_sys_path(tmp_path: Path) -> None:
    root = tmp_path / "auto-pts"
    _write_fake_autopts(root)
    before_modules = set(sys.modules)
    before_path = list(sys.path)
    try:
        loaded = load_autopts(root)
    finally:
        _remove_new_autopts_modules(before_modules)

    assert loaded.controller_factory.__name__ == "IutCtl"
    assert sys.path == before_path


def test_load_autopts_rejects_preloaded_module_from_another_root(
    tmp_path: Path, monkeypatch
) -> None:
    expected = tmp_path / "expected"
    foreign = tmp_path / "foreign" / "autopts" / "__init__.py"
    _write_fake_autopts(expected)
    foreign.parent.mkdir(parents=True)
    _ = foreign.write_text("", encoding="utf-8")
    module = ModuleType("autopts")
    module.__file__ = str(foreign)
    monkeypatch.setitem(sys.modules, "autopts", module)

    with pytest.raises(AutoPtsAdapterError, match="not fixed root"):
        _ = load_autopts(expected)


def test_application_port_is_selected_by_usb_identity_and_serial() -> None:
    selected = select_application_port(
        serial_number="fixture-2",
        ports=[FakePort("COM9", serial_number="fixture-2"), FakePort("COM13")],
    )
    assert selected.port == "COM9"
    assert selected.serial_number == "fixture-2"


def test_explicit_non_application_port_is_rejected() -> None:
    with pytest.raises(AutoPtsAdapterError, match="is not the nrftest application identity"):
        _ = select_application_port(
            port_name="COM7",
            ports=[FakePort("COM7", vid=0x1915, pid=0x521F)],
        )


def test_automatic_selection_rejects_multiple_application_devices() -> None:
    with pytest.raises(AutoPtsAdapterError, match="ambiguous or empty"):
        _ = select_application_port(ports=[FakePort("COM9"), FakePort("COM13")])


def test_gap_commands_produce_stable_snapshots(tmp_path) -> None:
    controllers: list[FakeController] = []
    btp = FakeBtp()
    session = AutoPtsSession(
        _loaded(btp, controllers),
        identity=_identity(),
        log_dir=tmp_path / "logs",
        socat_path=tmp_path / "socat.exe",
        platform="win32",
    )
    try:
        capabilities = session.start(required_services=("CORE", "GAP"), local_name="NrftestP1")
        assert capabilities.supported_command_masks == {"CORE": 0x1E, "GAP": 0x123}
        assert capabilities.service_actions == {"GAP": "registered"}

        controller = session.read_controller_info()
        assert controller.controller_index == 0
        assert controller.address == "AABBCCDDEEFF"
        assert controller.address_type_name == "random"

        assert session.set_powered(True).current_settings["Powered"] is True
        assert session.set_connectable(True).current_settings["Connectable"] is True
        assert btp.powered_on_calls == 0
        assert btp.connectable_calls == 0
        assert session.set_discoverable(True).current_settings["Discoverable"] is True
        assert (
            session.start_advertising("NrftestP1", service_uuid16="fdf0").current_settings[
                "Advertising"
            ]
            is True
        )
        assert btp.advertising_calls == [
            ({3: b"\xf0\xfd", 9: b"NrftestP1"}, {}),
        ]
        assert session.stop_advertising().current_settings["Advertising"] is False
    finally:
        session.close()

    assert controllers[0].started is True
    assert controllers[0].stopped is True
    assert session.cleanup_classification == "clean"


def test_128_bit_service_advertising_uses_scan_response_for_the_name(tmp_path) -> None:
    controllers: list[FakeController] = []
    btp = FakeBtp()
    session = AutoPtsSession(
        _loaded(btp, controllers),
        identity=_identity(),
        log_dir=tmp_path / "logs",
        socat_path=tmp_path / "socat.exe",
        platform="win32",
    )
    try:
        _ = session.start(required_services=("CORE", "GAP"))
        _ = session.start_advertising(
            "BleHubNrf52840",
            service_uuid128="fd000000-0000-4000-8000-000000000000",
        )
    finally:
        session.close()

    assert btp.advertising_calls == [
        (
            {7: bytes.fromhex("fd000000000040008000000000000000")[::-1]},
            {9: b"BleHubNrf52840"},
        )
    ]


def test_transport_can_attach_to_a_resident_service_without_registering_again(tmp_path) -> None:
    controllers: list[FakeController] = []
    btp = FakeBtp()
    loaded = _loaded(btp, controllers)

    first = AutoPtsSession(
        loaded,
        identity=_identity(),
        log_dir=tmp_path / "first",
        socat_path=tmp_path / "socat.exe",
        platform="win32",
    )
    _ = first.start(required_services=("CORE", "GAP"))
    first.close(unregister_services=False)

    second = AutoPtsSession(
        loaded,
        identity=_identity(),
        log_dir=tmp_path / "second",
        socat_path=tmp_path / "socat.exe",
        platform="win32",
    )
    capabilities = second.start(required_services=("CORE", "GAP"))
    second.close(unregister_services=False)

    assert capabilities.supported_command_masks == {"CORE": 0x1E, "GAP": 0x123}
    assert capabilities.service_actions == {"GAP": "attached"}
    assert btp.register_calls == ["GAP"]
    assert btp.unregister_calls == []
    assert btp.registered_services == {"GAP"}
    assert all(controller.stopped for controller in controllers)


def test_attach_rejects_a_service_that_is_not_resident(tmp_path) -> None:
    controllers: list[FakeController] = []
    session = AutoPtsSession(
        _loaded(FakeBtp(), controllers),
        identity=_identity(),
        log_dir=tmp_path / "logs",
        socat_path=tmp_path / "socat.exe",
        platform="win32",
    )

    with pytest.raises(AutoPtsAdapterError, match="attach to resident BTP GAP"):
        _ = session.start(
            required_services=("CORE", "GAP"),
            service_mode=ServiceStartupMode.ATTACH,
        )

    assert controllers[0].stopped is True


def test_auto_mode_does_not_register_after_a_probe_timeout(tmp_path) -> None:
    controllers: list[FakeController] = []
    btp = FakeBtp(service_probe_error=TimeoutError())
    session = AutoPtsSession(
        _loaded(btp, controllers),
        identity=_identity(),
        log_dir=tmp_path / "logs",
        socat_path=tmp_path / "socat.exe",
        platform="win32",
    )

    with pytest.raises(AutoPtsAdapterError, match="without a registration fallback"):
        _ = session.start(required_services=("CORE", "GAP"))

    assert btp.register_calls == []
    assert controllers[0].stopped is True


def test_connection_wait_sets_autopts_peer_for_followup_gap_events(tmp_path) -> None:
    controllers: list[FakeController] = []
    btp = FakeBtp()
    session = AutoPtsSession(
        _loaded(btp, controllers),
        identity=_identity(),
        log_dir=tmp_path / "logs",
        socat_path=tmp_path / "socat.exe",
        platform="win32",
    )
    try:
        _ = session.start(required_services=("CORE", "GAP"))
        gap = btp.stack.gap
        gap.connection_result = True
        gap.connections["112233445566"] = SimpleNamespace(
            addr_type=0,
            sec_level=1,
        )
        snapshot = session.wait_for_connection(0.01)
    finally:
        session.close()

    assert snapshot.connections[0].address == "112233445566"
    assert btp.pts_addresses == [("112233445566", 0)]


def test_gatt_remove_uses_fixed_autopts_wrapper_and_validates_handle(tmp_path) -> None:
    controllers: list[FakeController] = []
    btp = FakeBtp()
    session = AutoPtsSession(
        _loaded(btp, controllers),
        identity=_identity(),
        log_dir=tmp_path / "logs",
        socat_path=tmp_path / "socat.exe",
        platform="win32",
    )
    try:
        _ = session.start(required_services=("CORE", "GATT"))
        session.gatt_remove_service_containing_handle(33)
        with pytest.raises(AutoPtsAdapterError, match="range 1..=65535"):
            session.gatt_remove_service_containing_handle(0)
    finally:
        session.close()

    assert btp.removed_gatt_handles == [33]


def test_gatt_value_changed_is_decoded_from_autopts_state(tmp_path) -> None:
    controllers: list[FakeController] = []
    btp = FakeBtp()
    session = AutoPtsSession(
        _loaded(btp, controllers),
        identity=_identity(),
        log_dir=tmp_path / "logs",
        socat_path=tmp_path / "socat.exe",
        platform="win32",
    )
    try:
        _ = session.start(required_services=("CORE", "GATT"))
        session.clear_gatt_value_changed(35)
        btp.stack.gatt.values[35] = b"ab"
        btp.stack.gatt.changed_counts[35] = 1
        changed = session.wait_for_gatt_value_changed(35, 0.01)
    finally:
        session.close()

    assert changed.handle == 35
    assert changed.value == b"\xab"
    assert changed.changed_count == 1
    assert btp.stack.gatt.wait_timeouts == [0, 0.01]


def test_connection_wait_timeout_is_not_silently_discarded(tmp_path) -> None:
    controllers: list[FakeController] = []
    session = AutoPtsSession(
        _loaded(FakeBtp(), controllers),
        identity=_identity(),
        log_dir=tmp_path / "logs",
        socat_path=tmp_path / "socat.exe",
        platform="win32",
    )
    try:
        _ = session.start(required_services=("CORE", "GAP"))
        with pytest.raises(AutoPtsEventTimeout, match="no GAP connected event"):
            _ = session.wait_for_connection(0.01)
    finally:
        session.close()


def test_known_upstream_unregister_defect_is_separate_from_main_outcome(tmp_path) -> None:
    controllers: list[FakeController] = []
    session = AutoPtsSession(
        _loaded(FakeBtp(cleanup_defect=True), controllers),
        identity=_identity(),
        log_dir=tmp_path / "logs",
        socat_path=tmp_path / "socat.exe",
        platform="win32",
    )
    _ = session.start(
        required_services=("CORE", "GAP", "GATT"),
        service_mode=ServiceStartupMode.REGISTER,
    )
    session.close(unregister_services=True)

    assert session.cleanup_classification == "known-upstream-unregister-status-defect"
    assert set(session.cleanup_errors) == {
        "GATT unregister: BTPError: Error opcode in response!",
        "GAP unregister: BTPError: Unexpected response received!",
    }


def test_start_failure_releases_host_session_ownership(tmp_path: Path) -> None:
    failed_controllers: list[FakeController] = []
    failed = AutoPtsSession(
        _loaded(FakeBtp(service_probe_error=TimeoutError()), failed_controllers),
        identity=_identity(),
        log_dir=tmp_path / "failed",
        socat_path=tmp_path / "socat.exe",
        platform="win32",
    )
    with pytest.raises(AutoPtsAdapterError, match="without a registration fallback"):
        _ = failed.start(required_services=("CORE", "GAP"))

    controllers: list[FakeController] = []
    recovered = AutoPtsSession(
        _loaded(FakeBtp(), controllers),
        identity=_identity(),
        log_dir=tmp_path / "recovered",
        socat_path=tmp_path / "socat.exe",
        platform="win32",
    )
    _ = recovered.start(required_services=("CORE", "GAP"))
    recovered.close()

    assert controllers[0].stopped is True


def test_close_restores_an_originally_missing_path(tmp_path: Path, monkeypatch) -> None:
    controllers: list[FakeController] = []
    session = AutoPtsSession(
        _loaded(FakeBtp(), controllers),
        identity=_identity(),
        log_dir=tmp_path / "logs",
        socat_path=tmp_path / "socat.exe",
        platform="win32",
    )
    monkeypatch.delenv("PATH", raising=False)

    _ = session.start(required_services=("CORE", "GAP"))
    assert str(tmp_path) in os.environ["PATH"]
    session.close()

    assert "PATH" not in os.environ


def test_connection_snapshot_is_detached_from_autopts_objects(tmp_path) -> None:
    controllers: list[FakeController] = []
    session = AutoPtsSession(
        _loaded(FakeBtp(), controllers),
        identity=_identity(),
        log_dir=tmp_path / "logs",
        socat_path=tmp_path / "socat.exe",
        platform="win32",
    )
    try:
        _ = session.start(required_services=("CORE", "GAP"))
        session.stack.gap.connections["112233445566"] = SimpleNamespace(
            addr_type=0,
            sec_level=1,
        )
        snapshot = session.gap_snapshot()
        session.stack.gap.connections.clear()
    finally:
        session.close()

    assert snapshot.connections[0].address == "112233445566"
    assert snapshot.connections[0].address_type_name == "public"
    assert snapshot.connections[0].security_level == 1
