"""RAY-479 `color-binding-wizard`：左右模块按颜色配对、按 MAC 分左右、未绑定即阻断。

用户拍板（2026-09-16）：左脚蓝色模块、右脚橙色模块；先左后右，每一步只开那一台；
绑定持久化在设备级配置目录；绑定后真实传感器只按 MAC 分左右；未绑定或不符时自检阻断；
演示模式不需要绑定。

**不碰真实蓝牙**：扫描、传输、身份读取全部注入假的。平台地址与 MAC 故意取不同的值 ——
否则「按 MAC 分」与「按扫描到的地址分」在测试里无法区分。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from wt901 import Battery, DiscoveredDevice
from wt901.transport.memory import MemoryTransport

from gait.app import protocol
from gait.app.__main__ import bindings_from_environment, build_source
from gait.app.bindings import BINDING_LOG_FILENAME, BindingStore, mask_mac
from gait.app.blesource import BleDeviceSource, DeviceOps
from gait.app.errors import check_code
from gait.app.replay import ReplayDeviceSource, frames_from_synthetic
from gait.app.service import TerminalService
from gait.app.sources import StubDeviceSource
from gait.device.ble import AppliedConfig, StreamConfig
from gait.device.identity import mac_identity, platform_identity

BLUE_MAC = "F1:11:11:11:11:11"
ORANGE_MAC = "F2:22:22:22:22:22"
STRANGER_MAC = "F9:99:99:99:99:99"

#: 平台地址 → 设备自报 MAC。macOS 上平台地址是 CoreBluetooth UUID，与 MAC 毫无关系。
BLUE = ("uuid-blue", BLUE_MAC)
ORANGE = ("uuid-orange", ORANGE_MAC)
STRANGER = ("uuid-stranger", STRANGER_MAC)


class _World:
    """一屋子假模块：`powered` 是此刻开着（会被扫到）的那些。"""

    def __init__(self, *modules: tuple[str, str]) -> None:
        self.macs = dict(modules)
        self.powered: list[str] = [address for address, _ in modules]
        self.unreadable: set[str] = set()
        self.unreachable: set[str] = set()
        self.opened: list[str] = []
        self.closed: list[str] = []
        self.scan_calls = 0

    def power(self, *modules: tuple[str, str]) -> None:
        for address, mac in modules:
            self.macs[address] = mac
        self.powered = [address for address, _ in modules]

    async def scan(self, _timeout: float):
        self.scan_calls += 1
        return [DiscoveredDevice(address=a, name="WT901BLE68", rssi=-50) for a in self.powered]

    def transport(self, discovered: DiscoveredDevice):
        world = self

        class _Transport(MemoryTransport):
            async def connect(self) -> None:
                if discovered.address in world.unreachable:
                    raise OSError("peripheral went away")
                world.opened.append(discovered.address)
                await super().connect()

        return _Transport(discovered.address)

    def ops(self) -> DeviceOps:
        async def battery(_device):
            return Battery(raw=80, percent=80)

        async def identity(device, *, platform_address):
            if platform_address in self.unreadable:
                return platform_identity(platform_address), "读不到设备自报 MAC"
            return mac_identity(self.macs[platform_address]), None

        async def firmware(_device):
            return "1.4.2"

        async def configure(_device):
            return AppliedConfig(StreamConfig(), 3, 1, 0, 0, None, ())

        async def start(_device, applied):
            return applied

        async def close(device):
            self.closed.append(device.device_id)
            await device.close()

        return DeviceOps(
            scan=self.scan,
            transport=self.transport,
            read_battery=battery,
            resolve_identity=identity,
            read_firmware=firmware,
            configure=configure,
            start=start,
            close=close,
        )


def _clock() -> datetime:
    return datetime(2026, 9, 16, 8, 30, tzinfo=UTC)


def _service(world: _World, root: Path, **kwargs) -> tuple[TerminalService, BleDeviceSource]:
    store = BindingStore(root, clock=_clock)
    source = BleDeviceSource(ops=world.ops(), bindings=store, connect_wait_s=5)
    return TerminalService(source=source, bindings=store, **kwargs), source


def _call(service: TerminalService, method: str, **params) -> dict:
    return service.handle({"id": "1", "method": method, "params": params})


def _bind_both(service: TerminalService, world: _World) -> None:
    world.power(BLUE)
    assert _call(service, "bindFoot", foot="L")["status"] == "ok"
    world.power(ORANGE)
    assert _call(service, "bindFoot", foot="R")["status"] == "ok"


# ── 契约 ──────────────────────────────────────────────────────────────────


def test_new_methods_and_codes_are_registered() -> None:
    assert {"bindingStatus", "bindFoot"} <= protocol.METHODS
    for code in ("E-BLE-1030", "E-BLE-1031"):
        assert check_code(code) == code


# ── 配对：先左后右，每步只开一台 ─────────────────────────────────────────


def test_binds_left_then_right_and_persists_with_a_record(tmp_path: Path) -> None:
    world = _World(BLUE)
    service, source = _service(world, tmp_path)
    try:
        status = _call(service, "bindingStatus")["result"]
        assert status == {
            "available": True,
            "required": True,
            "left": None,
            "right": None,
            "complete": False,
            "boundAt": None,
            "problem": status["problem"],
        }
        assert "左脚尚未绑定" in status["problem"]

        left = _call(service, "bindFoot", foot="L")
        assert left["status"] == "ok", left
        assert left["result"]["foot"] == "L"
        assert left["result"]["mac"] == BLUE_MAC
        assert left["result"]["masked"] == "…:11:11"
        assert left["result"]["binding"]["complete"] is False

        world.power(ORANGE)
        right = _call(service, "bindFoot", foot="R")["result"]
        assert right["mac"] == ORANGE_MAC
        final = right["binding"]
        assert final["complete"] is True and final["problem"] is None
        assert final["left"] == {"mac": BLUE_MAC, "masked": "…:11:11", "boundAt": "2026-09-16T08:30:00+00:00"}
        assert final["right"]["mac"] == ORANGE_MAC
        assert final["boundAt"] == "2026-09-16T08:30:00+00:00"
    finally:
        source.close()

    # 「操作将被记录」是真的：每次绑定一行，带时间、脚、身份、被替换的旧值。
    lines = [
        json.loads(line)
        for line in (tmp_path / BINDING_LOG_FILENAME).read_text(encoding="utf-8").splitlines()
    ]
    assert [(entry["foot"], entry["identity"]["value"]) for entry in lines] == [
        ("L", BLUE_MAC),
        ("R", ORANGE_MAC),
    ]
    assert all(entry["at"] == "2026-09-16T08:30:00+00:00" for entry in lines)
    assert lines[0]["replaced"] is None and lines[0]["via"] == "bindFoot"


def test_binding_is_read_back_by_a_new_process(tmp_path: Path) -> None:
    world = _World(BLUE)
    service, source = _service(world, tmp_path)
    try:
        _bind_both(service, world)
    finally:
        source.close()

    # 新进程：从环境变量重新构造，不共享任何内存状态。
    env = {"GAIT_DEVICE_SOURCE": "ble", "GAIT_CONFIG_ROOT": str(tmp_path)}
    fresh = build_source(env)
    assert isinstance(fresh, BleDeviceSource) and fresh.bindings is not None
    status = TerminalService(source=fresh, bindings=bindings_from_environment(env)).handle(
        {"id": "1", "method": "bindingStatus"}
    )["result"]
    assert status["complete"] is True
    assert (status["left"]["mac"], status["right"]["mac"]) == (BLUE_MAC, ORANGE_MAC)


def test_zero_modules_tells_the_operator_to_power_on_the_blue_one(tmp_path: Path) -> None:
    world = _World()
    service, source = _service(world, tmp_path)
    try:
        response = _call(service, "bindFoot", foot="L")
        assert response["status"] == "error"
        error = response["error"]
        assert error["code"] == "E-BLE-1031"
        assert "没有发现模块" in error["message"] and "蓝色（左脚）" in error["message"]
        assert _call(service, "bindingStatus")["result"]["left"] is None
    finally:
        source.close()


@pytest.mark.parametrize(
    ("foot", "color"), [("L", "蓝色（左脚）"), ("R", "橙色（右脚）")]
)
def test_multiple_unbound_modules_are_never_guessed(tmp_path: Path, foot: str, color: str) -> None:
    world = _World(BLUE, STRANGER)
    service, source = _service(world, tmp_path)
    try:
        response = _call(service, "bindFoot", foot=foot)
        assert response["status"] == "error"
        assert "发现多个未绑定模块" in response["error"]["message"]
        assert f"请只打开{color}模块" in response["error"]["action"]
        assert not (tmp_path / "device-binding.json").exists()
    finally:
        source.close()


def test_an_unreadable_second_module_still_counts_as_ambiguous(tmp_path: Path) -> None:
    world = _World(BLUE, STRANGER)
    world.unreadable.add(STRANGER[0])
    service, source = _service(world, tmp_path)
    try:
        response = _call(service, "bindFoot", foot="L")
        assert "发现多个" in response["error"]["message"]
    finally:
        source.close()


def test_the_module_already_bound_to_the_other_foot_is_not_a_candidate(tmp_path: Path) -> None:
    world = _World(BLUE)
    service, source = _service(world, tmp_path)
    try:
        assert _call(service, "bindFoot", foot="L")["status"] == "ok"
        # 配右脚时蓝色模块忘了关：它已是左脚，不算候选，橙色是唯一的。
        world.power(BLUE, ORANGE)
        right = _call(service, "bindFoot", foot="R")
        assert right["status"] == "ok", right
        assert right["result"]["mac"] == ORANGE_MAC

        # 只剩蓝色开着时去配右脚：说清楚没看见橙色，而不是把蓝色挪过去。
        world.power(BLUE)
        again = _call(service, "bindFoot", foot="R")
        assert again["status"] == "error"
        assert "只发现已绑定为蓝色（左脚）的模块" in again["error"]["message"]
    finally:
        source.close()


def test_identity_and_connection_failures_are_honest_errors(tmp_path: Path) -> None:
    world = _World(BLUE)
    world.unreadable.add(BLUE[0])
    service, source = _service(world, tmp_path)
    try:
        unreadable = _call(service, "bindFoot", foot="L")
        assert unreadable["error"]["code"] == "E-BLE-1031"
        assert "读不到它自报的 MAC" in unreadable["error"]["message"]

        world.unreadable.clear()
        world.unreachable.add(BLUE[0])
        unreachable = _call(service, "bindFoot", foot="L")
        assert "连接失败" in unreachable["error"]["message"]
    finally:
        source.close()


def test_bind_foot_rejects_a_bad_foot_label(tmp_path: Path) -> None:
    world = _World(BLUE)
    service, source = _service(world, tmp_path)
    try:
        with pytest.raises(protocol.ProtocolError):
            _call(service, "bindFoot", foot="left")
    finally:
        source.close()


def test_bind_foot_without_a_config_root_is_reported_not_faked() -> None:
    world = _World(BLUE)
    source = BleDeviceSource(ops=world.ops())
    try:
        service = TerminalService(source=source)
        status = _call(service, "bindingStatus")["result"]
        assert status["available"] is False and status["required"] is True
        response = _call(service, "bindFoot", foot="L")
        assert response["error"]["code"] == "E-BLE-1030"
    finally:
        source.close()


# ── 连接：绑定之后只按 MAC 分左右 ─────────────────────────────────────────


@pytest.mark.parametrize("order", [(BLUE, ORANGE), (ORANGE, BLUE)])
def test_connect_assigns_feet_by_bound_mac_regardless_of_scan_order(
    tmp_path: Path, order
) -> None:
    world = _World(BLUE)
    service, source = _service(world, tmp_path)
    try:
        _bind_both(service, world)
        world.power(*order)
        assert source.refresh(timeout=5) == "connected"
        readings = source.device_readings()
        assert readings["L"]["value"] == BLUE_MAC
        assert readings["R"]["value"] == ORANGE_MAC
        assert source.provenance()["foot_assignment"] == "binding"

        items = {item["id"]: item for item in _call(service, "runPreflight")["result"]}
        assert items["binding"]["status"] == "pass"
        assert items["binding"]["hint"] == "蓝色（左脚）…:11:11 · 橙色（右脚）…:22:22"
        # 两次连续连接左右不变（验收 6 的离线半边）。
        source.close()
        again = BleDeviceSource(ops=world.ops(), bindings=BindingStore(tmp_path))
        try:
            world.power(*reversed(order))
            assert again.refresh(timeout=5) == "connected"
            assert again.device_readings()["L"]["value"] == BLUE_MAC
        finally:
            again.close()
    finally:
        source.close()


def test_an_extra_unbound_module_nearby_does_not_block_once_both_are_found(
    tmp_path: Path,
) -> None:
    world = _World(BLUE)
    service, source = _service(world, tmp_path)
    try:
        _bind_both(service, world)
        world.power(STRANGER, ORANGE, BLUE)
        assert source.refresh(timeout=5) == "connected"
        assert STRANGER[0] in world.closed  # 认不出就断开，不留着
        assert source.device_readings()["R"]["value"] == ORANGE_MAC
    finally:
        source.close()


def test_a_missing_bound_module_blocks_and_says_which_one(tmp_path: Path) -> None:
    world = _World(BLUE)
    service, source = _service(world, tmp_path)
    try:
        _bind_both(service, world)
        # 橙色没开，旁边却开着一台陌生模块：不拿它顶替右脚。
        world.power(BLUE, STRANGER)
        assert source.refresh(timeout=5) == "failed"
        assert source.read_batteries() == {"L": None, "R": None}
        assert set(world.opened) >= {BLUE[0], STRANGER[0]}
        assert BLUE[0] in world.closed  # 已认出的左脚也断开：不能单脚开流

        items = {item["id"]: item for item in _call(service, "runPreflight")["result"]}
        binding = items["binding"]
        assert binding["status"] == "fail"
        assert binding["error"]["code"] == "E-BLE-1030"
        assert "橙色（右脚）模块 …:22:22 没有找到" in binding["error"]["message"]
        assert "扫描到未绑定的模块 …:99:99" in binding["error"]["message"]
        assert items["link-r"]["status"] == "fail"
    finally:
        source.close()


def test_without_a_binding_real_sensors_do_not_connect_by_scan_order(tmp_path: Path) -> None:
    world = _World(BLUE, ORANGE)
    service, source = _service(world, tmp_path)
    try:
        assert source.refresh(timeout=5) == "failed"
        assert world.scan_calls == 0 and world.opened == []
        assert source.provenance()["foot_assignment"] is None

        items = _call(service, "runPreflight")["result"]
        assert items[0]["id"] == "binding"  # 根因放第一项
        assert items[0]["status"] == "fail"
        assert items[0]["error"]["action"] == (
            "请到「设备与支持」点「重新配对模块」：先只打开蓝色（左脚）模块，"
            "再只打开橙色（右脚）模块。"
        )
        snapshot = _call(service, "snapshot")["result"]
        assert snapshot["binding"]["required"] is True
        assert snapshot["binding"]["complete"] is False
    finally:
        source.close()


def test_a_corrupt_binding_file_blocks_and_rebinding_repairs_it(tmp_path: Path) -> None:
    (tmp_path / "device-binding.json").write_text("{ 坏了", encoding="utf-8")
    world = _World(BLUE)
    service, source = _service(world, tmp_path)
    try:
        status = _call(service, "bindingStatus")["result"]
        assert status["complete"] is False and "读不回来" in status["problem"]
        assert source.refresh(timeout=5) == "failed"

        _bind_both(service, world)
        assert _call(service, "bindingStatus")["result"]["complete"] is True
        log = (tmp_path / BINDING_LOG_FILENAME).read_text(encoding="utf-8").splitlines()
        assert json.loads(log[0])["previousUnreadable"]
    finally:
        source.close()


def test_rebinding_a_foot_overwrites_only_that_foot(tmp_path: Path) -> None:
    world = _World(BLUE)
    service, source = _service(world, tmp_path)
    try:
        _bind_both(service, world)
        replacement = ("uuid-new-blue", "F3:33:33:33:33:33")
        world.power(replacement)
        result = _call(service, "bindFoot", foot="L")["result"]
        assert result["binding"]["left"]["mac"] == replacement[1]
        assert result["binding"]["right"]["mac"] == ORANGE_MAC
        last = json.loads(
            (tmp_path / BINDING_LOG_FILENAME).read_text(encoding="utf-8").splitlines()[-1]
        )
        assert last["replaced"]["value"] == BLUE_MAC
    finally:
        source.close()


def test_bind_foot_is_refused_while_a_session_is_running(tmp_path: Path) -> None:
    world = _World(BLUE)
    service, source = _service(world, tmp_path)
    try:
        _call(service, "startSession", now=0.0)
        response = _call(service, "bindFoot", foot="L")
        assert response["error"]["code"] == "E-BLE-1031"
        assert "检测进行中" in response["error"]["message"]
    finally:
        source.close()


# ── 演示模式不需要绑定 ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "make_source",
    [
        StubDeviceSource,
        lambda: ReplayDeviceSource(loader=lambda: frames_from_synthetic(5.0), label="synthetic"),
    ],
)
def test_demo_sources_need_no_binding(tmp_path: Path, make_source) -> None:
    service = TerminalService(source=make_source(), bindings=BindingStore(tmp_path))
    items = _call(service, "runPreflight")["result"]
    assert "binding" not in {item["id"] for item in items}
    status = _call(service, "bindingStatus")["result"]
    assert status["required"] is False
    assert _call(service, "snapshot")["result"]["binding"]["required"] is False
    refused = _call(service, "bindFoot", foot="L")
    assert refused["error"]["code"] == "E-BLE-1030"
    assert "演示模式无需绑定" in refused["error"]["action"]


def test_config_root_environment_is_optional() -> None:
    assert bindings_from_environment({}) is None
    assert bindings_from_environment({"GAIT_CONFIG_ROOT": "  "}) is None


def test_mask_mac_keeps_only_the_tail() -> None:
    assert mask_mac(BLUE_MAC) == "…:11:11"
    assert mask_mac(None) is None
