"""`gait.app.blesource` 的离线测试。**不碰真实蓝牙** —— 扫描、传输、配置全部注入假的。

真机那一半由人在自己的终端里跑 `python -m gait.app.blesource --probe`（macOS 的 TCC
不让代理会话访问蓝牙）；这里验的是不需要硬件就能验的部分：字节扇出、会话视图的
登记/注销、到达率窗口、链路分档、连接编排与失败降级、以及真实落盘。
"""

from __future__ import annotations

import asyncio
import struct
import time
from pathlib import Path

import pytest
from wt901 import Battery, DiscoveredDevice
from wt901.transport.memory import MemoryTransport

from gait.app.blesource import (
    FAIR_ARRIVAL,
    GOOD_ARRIVAL,
    ArrivalWindow,
    BleDeviceSource,
    DeviceOps,
    SessionPort,
    StepCounter,
    TapTransport,
    grade_link,
    mask_address,
)
from gait.app.sources import LINK_GRADES, DeviceSource
from gait.contracts import SessionMeta
from gait.device.ble import AppliedConfig, StreamConfig
from gait.device.capture import SessionCapture
from gait.device.identity import mac_identity
from gait.io.session import create_session, new_session_id, new_subject_uuid, raw_path


def _frame(seed: int, gyro: tuple[int, int, int] = (0, 0, 0)) -> bytes:
    """一帧 0x55 0x61 运动数据：acc(3) gyr(3) ang(3) int16 计数。"""
    return b"\x55\x61" + struct.pack("<9h", seed, seed, seed, *gyro, 0, 0, 0)


def _run(coro):
    return asyncio.run(coro)


# ── TapTransport / SessionPort ──────────────────────────────────────────────


class TestTapFanOut:
    def test_bytes_reach_both_the_device_callback_and_attached_listeners(self):
        inner = MemoryTransport("dev-L")
        tap = TapTransport(inner)
        to_device: list[bytes] = []
        to_listener: list[bytes] = []
        tap.on_data(to_device.append)
        tap.attach(to_listener.append)
        inner.feed(b"abc")
        assert to_device == [b"abc"]
        assert to_listener == [b"abc"]

    def test_detach_stops_delivery_to_that_listener_only(self):
        inner = MemoryTransport()
        tap = TapTransport(inner)
        to_device: list[bytes] = []
        seen: list[bytes] = []
        tap.on_data(to_device.append)
        tap.attach(seen.append)
        tap.attach(seen.append)  # 重复登记不会重复收
        inner.feed(b"1")
        tap.detach(seen.append)
        inner.feed(b"2")
        assert seen == [b"1"]
        assert to_device == [b"1", b"2"]
        assert tap.listener_count == 0

    def test_a_broken_listener_does_not_starve_the_device(self):
        inner = MemoryTransport()
        tap = TapTransport(inner)
        to_device: list[bytes] = []
        tap.on_data(to_device.append)

        def boom(_: bytes) -> None:
            raise RuntimeError("disk gone")

        tap.attach(boom)
        inner.feed(b"x")
        assert to_device == [b"x"]

    def test_lifecycle_and_writes_forward_to_the_inner_transport(self):
        inner = MemoryTransport("dev-R")
        tap = TapTransport(inner)

        async def scenario() -> None:
            await tap.connect()
            assert tap.is_connected
            await tap.write(b"\xff\xaa")
            assert await tap.read_rssi() is None
            await tap.disconnect()

        _run(scenario())
        assert tap.device_id == "dev-R"
        assert inner.writes == [b"\xff\xaa"]
        assert (inner.connect_calls, inner.disconnect_calls) == (1, 1)

    def test_inner_disconnect_propagates_to_the_device_side(self):
        inner = MemoryTransport()
        tap = TapTransport(inner)
        lost: list[bool] = []
        tap.on_disconnect(lambda: lost.append(True))
        inner.drop()
        assert lost == [True]


class TestSessionPort:
    def test_connect_attaches_and_disconnect_detaches_without_touching_the_link(self):
        inner = MemoryTransport("dev-L")
        tap = TapTransport(inner)
        port = SessionPort("L")
        port.bind(tap)
        got: list[bytes] = []
        port.on_data(got.append)

        inner.feed(b"before")
        _run(port.connect())
        inner.feed(b"during")
        _run(port.disconnect())
        inner.feed(b"after")

        assert got == [b"during"]
        # 会话视图的开关不是链路的开关。
        assert inner.connect_calls == 0 and inner.disconnect_calls == 0

    def test_rebinding_moves_an_active_registration_to_the_new_tap(self):
        first, second = MemoryTransport("a"), MemoryTransport("b")
        port = SessionPort("R")
        got: list[bytes] = []
        port.on_data(got.append)
        port.bind(TapTransport(first))
        _run(port.connect())
        port.bind(TapTransport(second))
        first.feed(b"old")
        second.feed(b"new")
        assert got == [b"new"]
        assert port.device_id == "b"

    def test_unbound_port_has_a_placeholder_id_and_refuses_writes(self):
        port = SessionPort("L")
        assert port.device_id == "ble-unconnected-L"
        assert not port.is_connected
        with pytest.raises(ConnectionError):
            _run(port.write(b"x"))

    def test_write_is_handed_to_the_owning_loop_from_another_loop(self):
        import threading

        owner = asyncio.new_event_loop()
        thread = threading.Thread(target=owner.run_forever, daemon=True)
        thread.start()
        try:
            inner = MemoryTransport()
            tap = TapTransport(inner)
            asyncio.run_coroutine_threadsafe(inner.connect(), owner).result(2)
            port = SessionPort("L", loop=owner)
            port.bind(tap)
            _run(port.write(b"cmd"))
            assert inner.writes == [b"cmd"]
        finally:
            owner.call_soon_threadsafe(owner.stop)
            thread.join(2)
            owner.close()

    def test_session_capture_records_bytes_fed_into_the_tap(self, tmp_path: Path):
        session_id = new_session_id()
        create_session(tmp_path, _meta(session_id))
        inner = MemoryTransport("dev-L")
        tap = TapTransport(inner)
        device_side: list[bytes] = []
        tap.on_data(device_side.append)
        port = SessionPort("L")
        port.bind(tap)
        frames = [_frame(i) for i in range(5)]

        async def scenario() -> None:
            with SessionCapture(tmp_path, session_id) as capture:
                recording = capture.wrap("L", port)
                await recording.connect()
                for frame in frames:
                    inner.feed(frame)
                await recording.disconnect()
                inner.feed(_frame(99))  # 会话结束后的字节不进文件，但设备照收

        _run(scenario())
        text = raw_path(tmp_path, session_id, "L").read_text(encoding="utf-8")
        assert text.count("\n") >= len(frames)  # 头 + 每块一行
        assert frames[0].hex() in text and _frame(99).hex() not in text
        assert len(device_side) == len(frames) + 1


def _meta(session_id: str) -> SessionMeta:
    return SessionMeta(
        session_id=session_id,
        created_at="2026-09-15T00:00:00Z",
        subject_uuid=new_subject_uuid(),
        scenario="walk",
        devices={"L": {"mac": "AA:BB:CC:DD:EE:01"}, "R": {"mac": "AA:BB:CC:DD:EE:02"}},
        config_snapshot={"state": "pending"},
        calib_snapshot={"state": "unimplemented"},
        algo_version="test",
        algo_params={"duration_s": 60},
        sync_report={"state": "pending"},
        integrity_report={"state": "pending"},
        protocol_config={"duration_s": 60},
    )


# ── 纯计算 ────────────────────────────────────────────────────────────────


class TestArrivalWindow:
    def test_rate_is_samples_in_the_last_second_over_nominal(self):
        window = ArrivalWindow(nominal_hz=200.0)
        for i in range(190):
            window.record(10.0 + i / 200)
        assert window.rate(now=10.95) == pytest.approx(0.95)

    def test_old_samples_slide_out(self):
        window = ArrivalWindow(nominal_hz=200.0)
        for i in range(200):
            window.record(i / 200)
        assert window.count(now=1.5) == 99  # 只剩 (0.5, 1.0) 那一段：i = 101..199
        assert window.rate(now=5.0) == 0.0

    def test_rate_is_clamped_to_one(self):
        window = ArrivalWindow(nominal_hz=10.0)
        for i in range(30):
            window.record(1.0 + i / 100)
        assert window.rate(now=1.3) == 1.0

    def test_injected_clock_is_used_when_now_is_omitted(self):
        now = [100.0]
        window = ArrivalWindow(nominal_hz=4.0, clock=lambda: now[0])
        for t in (99.2, 99.5, 99.9):
            window.record(t)
        assert window.rate() == pytest.approx(0.75)
        now[0] = 100.6
        assert window.rate() == pytest.approx(0.25)


class TestLinkGrade:
    def test_thresholds(self):
        good, fair, bad = LINK_GRADES
        assert grade_link(1.0) == good
        assert grade_link(GOOD_ARRIVAL) == good
        assert grade_link(GOOD_ARRIVAL - 1e-9) == fair
        assert grade_link(FAIR_ARRIVAL) == fair
        assert grade_link(FAIR_ARRIVAL - 1e-9) == bad
        assert grade_link(0.0) == bad


class TestStepCounter:
    def test_counts_rising_edges_with_a_refractory_period(self):
        counter = StepCounter(threshold=2.0, refractory_s=0.3)
        trace = [(0.0, 0.1), (0.1, 3.0), (0.15, 3.5), (0.2, 0.5),  # 一步
                 (0.25, 3.0), (0.26, 0.1),                         # 不应期内，不计
                 (0.8, 4.0), (0.9, 0.2)]                           # 第二步
        for t, g in trace:
            counter.feed(t, g)
        assert counter.count == 2
        counter.reset()
        assert counter.count == 0


def test_mask_address_keeps_only_the_tail():
    assert mask_address("F9:B3:4F:46:C9:4C") == "…:C9:4C"
    assert mask_address("1234ABCD-0000-1111-2222-3333DEADBEEF") == "…BEEF"
    assert mask_address(None) is None


# ── BleDeviceSource ─────────────────────────────────────────────────────────


def test_from_environment_parses_filters_and_timeout(tmp_path: Path):
    source = BleDeviceSource.from_environment(
        {
            "GAIT_BLE_LEFT": " C9:4C ",
            "GAIT_BLE_RIGHT": "",
            "GAIT_BLE_SCAN_TIMEOUT": "7.5",
            "GAIT_SESSION_ROOT": str(tmp_path),
        }
    )
    assert source.left == "C9:4C"
    assert source.right is None
    assert source.scan_timeout == 7.5
    assert source.session_root == tmp_path
    fallback = BleDeviceSource.from_environment({"GAIT_BLE_SCAN_TIMEOUT": "abc"})
    assert fallback.scan_timeout == 5.0
    assert fallback.left is None and fallback.session_root is None


def test_it_satisfies_the_device_source_protocol():
    source = BleDeviceSource()
    for name in [n for n in vars(DeviceSource) if not n.startswith("_")]:
        assert callable(getattr(source, name)), name
    assert callable(source.refresh) and callable(source.close)


def test_readings_when_never_connected(tmp_path: Path):
    source = BleDeviceSource(session_root=tmp_path / "not" / "yet")
    assert source.read_batteries() == {"L": None, "R": None}
    assert source.arrival_rates() == {"L": 0.0, "R": 0.0}
    assert source.link_grades() == {"L": "bad", "R": "bad"}
    assert source.step_counts() == {"L": 0, "R": 0}
    readings = source.device_readings()
    assert set(readings) == {"L", "R"}
    assert readings["L"]["kind"] == "platform-address"
    assert source.disk_free_bytes() > 0
    assert [m["batteryPercent"] for m in source.module_info()] == [None, None]
    ports = source.transports()
    assert ports is not source.transports() and ports == source.transports()
    assert ports["L"] is source.transports()["L"]
    provenance = source.provenance()
    assert provenance["source"] == "ble" and provenance["hardware"] is True
    assert provenance["foot_assignment"] is None
    source.begin_stream()
    source.end_stream()
    source.close()  # 从没起过循环也能关


class _FakeWorld:
    """两台假设备：扫描返回它们，传输是 `MemoryTransport`，配置直接成功。"""

    def __init__(self, addresses: list[str], *, scans_until_found: int = 1) -> None:
        self.discovered = [
            DiscoveredDevice(address=a, name="WT901BLE68", rssi=-50) for a in addresses
        ]
        self.transports: dict[str, MemoryTransport] = {}
        self.scan_calls = 0
        self.scans_until_found = scans_until_found
        self.closed: list[str] = []
        self.started: list[str] = []
        self.fail_open: str | None = None

    async def scan(self, _timeout: float):
        self.scan_calls += 1
        return self.discovered if self.scan_calls >= self.scans_until_found else []

    def transport(self, discovered: DiscoveredDevice):
        world = self

        class _Transport(MemoryTransport):
            async def connect(self) -> None:
                if world.fail_open == discovered.address:
                    raise OSError("peripheral went away")
                await super().connect()

        transport = _Transport(discovered.address)
        self.transports[discovered.address] = transport
        return transport

    def ops(self) -> DeviceOps:
        async def battery(_device):
            return Battery(raw=80, percent=80)

        async def identity(device, *, platform_address):
            return mac_identity(platform_address), None

        async def firmware(_device):
            return "1.4.2"

        async def configure(_device):
            return AppliedConfig(StreamConfig(), 3, 1, 0, 0, None, ())

        async def start(device, applied):
            self.started.append(device.device_id)
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


def _wait_for(predicate, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("条件未在时限内成立")


class TestConnectOrchestration:
    def test_connects_in_scan_order_and_streams_readings(self, monkeypatch):
        monkeypatch.setattr("gait.app.blesource.SCAN_ATTEMPTS", 4)
        world = _FakeWorld(["AA:00:00:00:00:01", "AA:00:00:00:00:02"], scans_until_found=3)
        source = BleDeviceSource(ops=world.ops())
        try:
            assert source.refresh(timeout=5) == "connected"
            assert world.scan_calls == 3
            assert sorted(world.started) == sorted(world.transports)
            assert source.read_batteries()["L"].percent == 80
            readings = source.device_readings()
            assert readings["L"] == {
                "kind": "mac",
                "value": "AA:00:00:00:00:01",
                "provenance": readings["L"]["provenance"],
                "firmware": "1.4.2",
            }
            assert source.provenance()["foot_assignment"] == "scan_order"
            assert source.module_info()[1]["maskedAddress"] == "…:00:02"

            left = world.transports["AA:00:00:00:00:01"]
            loop = source._loop
            assert loop is not None

            def feed(n: int, gyro=(0, 0, 0)) -> None:
                for i in range(n):
                    left.feed(_frame(i, gyro))

            loop.call_soon_threadsafe(feed, 195)
            _wait_for(lambda: source.arrival_rates()["L"] >= 0.95)
            assert source.link_grades() == {"L": "good", "R": "bad"}

            # 显示用步数：一次大角速度上升沿记一步；begin_stream 清零。
            loop.call_soon_threadsafe(feed, 1, (30000, 0, 0))
            _wait_for(lambda: source.step_counts()["L"] == 1)
            source.begin_stream()
            assert source.step_counts()["L"] == 0

            # refresh 已连上时是空操作。
            assert source.refresh(timeout=1) == "connected"
            assert world.scan_calls == 3
        finally:
            source.close()
        assert sorted(world.closed) == sorted(world.transports)
        assert source.state == "closed"
        assert source.read_batteries() == {"L": None, "R": None}

    def test_explicit_filters_assign_feet(self):
        world = _FakeWorld(["AA:00:00:00:00:01", "BB:00:00:00:00:02"])
        source = BleDeviceSource(left="bb:00", right="AA:00", ops=world.ops())
        try:
            assert source.refresh(timeout=5) == "connected"
            assert source.device_readings()["L"]["value"] == "BB:00:00:00:00:02"
            assert source.provenance()["foot_assignment"] == "explicit_mac"
        finally:
            source.close()

    def test_scan_failure_degrades_and_a_later_refresh_reconnects(self):
        world = _FakeWorld(["AA:00:00:00:00:01", "AA:00:00:00:00:02"], scans_until_found=6)
        source = BleDeviceSource(ops=world.ops())
        try:
            assert source.refresh(timeout=5) == "failed"
            assert world.scan_calls == 4
            assert "未凑齐" in (source.last_error or "")
            assert source.read_batteries() == {"L": None, "R": None}
            assert source.link_grades() == {"L": "bad", "R": "bad"}
            assert source.refresh(timeout=5) == "connected"
        finally:
            source.close()

    def test_open_failure_closes_what_was_already_opened(self):
        world = _FakeWorld(["AA:00:00:00:00:01", "AA:00:00:00:00:02"])
        world.fail_open = "AA:00:00:00:00:02"
        source = BleDeviceSource(ops=world.ops())
        try:
            assert source.refresh(timeout=5) == "failed"
            assert "OSError" in (source.last_error or "")
            assert "AA:00:00:00:00:01" in world.closed
            assert source.arrival_rates() == {"L": 0.0, "R": 0.0}
        finally:
            source.close()

    def test_a_dropped_link_reads_as_disconnected_and_refresh_reconnects(self):
        world = _FakeWorld(["AA:00:00:00:00:01", "AA:00:00:00:00:02"])
        source = BleDeviceSource(ops=world.ops())
        try:
            assert source.refresh(timeout=5) == "connected"
            port = source.transports()["R"]
            _run(port.connect())  # 一个正在录的会话
            right = world.transports["AA:00:00:00:00:02"]
            loop = source._loop
            assert loop is not None
            loop.call_soon_threadsafe(right.drop)
            _wait_for(lambda: source.read_batteries()["R"] is None)
            assert source.state != "connected"

            assert source.refresh(timeout=5) == "connected"
            got: list[bytes] = []
            port.on_data(got.append)
            fresh = world.transports["AA:00:00:00:00:02"]
            assert fresh is not right
            loop.call_soon_threadsafe(fresh.feed, b"after-reconnect")
            _wait_for(lambda: got == [b"after-reconnect"])
        finally:
            source.close()


def test_one_foot_connected_still_reports_both_feet_and_preflight_does_not_raise():
    """service 的标定准入与自检按「两只脚的键都在」来用读数，少一只就抛。

    真实场景只有一种能走到「一只连着、一只没连」：连上后其中一只断链。
    """
    from gait.app.service import TerminalService

    world = _FakeWorld(["AA:00:00:00:00:01", "AA:00:00:00:00:02"])
    source = BleDeviceSource(ops=world.ops())
    try:
        assert source.refresh(timeout=5) == "connected"
        loop = source._loop
        assert loop is not None
        loop.call_soon_threadsafe(world.transports["AA:00:00:00:00:02"].drop)
        _wait_for(lambda: source.read_batteries()["R"] is None)

        feet = {"L", "R"}
        for reading in (
            source.read_batteries(),
            source.arrival_rates(),
            source.link_grades(),
            source.step_counts(),
            source.device_readings(),
            source.transports(),
        ):
            assert set(reading) == feet
        assert source.read_batteries()["L"].percent == 80
        assert source.arrival_rates()["R"] == 0.0
        assert source.link_grades()["R"] == "bad"
        right = source.device_readings()["R"]
        assert set(right) == {"kind", "value", "provenance", "firmware"}
        assert right["kind"] == "platform-address" and right["firmware"] == "unknown"
        assert source.device_readings()["L"]["kind"] == "mac"
        assert [m["side"] for m in source.module_info()] == ["left", "right"]

        # 自检前 service 会先调 `source.refresh()`（RAY-493 sidecar-preview-runtime）——
        # 「重新检查」本就该尝试重连。所以这里要验的是「单足断链时自检不抛」，
        # 而不是「断的那只仍然 fail」：假世界里右足还在广播，refresh 会把它连回来。
        # 右足**真的**不在时自检如实报 E-BLE-1001，见下一条用例。
        items = TerminalService(source=source).handle(
            {"id": "1", "method": "runPreflight"}
        )["result"]
        by_id = {item["id"]: item for item in items}
        assert by_id["link-l"]["status"] == "pass"
        assert by_id["link-r"]["status"] == "pass"
    finally:
        source.close()


def test_preflight_reports_the_missing_foot_when_refresh_cannot_reconnect_it():
    """右足断链且不再广播：自检里的 refresh 重连失败，读数回到断开占位，自检如实阻断。"""
    from gait.app.service import TerminalService

    world = _FakeWorld(["AA:00:00:00:00:01", "AA:00:00:00:00:02"])
    source = BleDeviceSource(ops=world.ops(), connect_wait_s=5)
    try:
        assert source.refresh(timeout=5) == "connected"
        loop = source._loop
        assert loop is not None
        world.discovered = world.discovered[:1]  # 右足从此扫不到
        loop.call_soon_threadsafe(world.transports["AA:00:00:00:00:02"].drop)
        _wait_for(lambda: source.read_batteries()["R"] is None)

        items = TerminalService(source=source).handle(
            {"id": "1", "method": "runPreflight"}
        )["result"]
        by_id = {item["id"]: item for item in items}
        assert by_id["link-r"]["status"] == "fail"
        assert by_id["link-r"]["error"]["code"] == "E-BLE-1001"
    finally:
        source.close()


# ── 电量重读（WT901 RAY-182：寄存器偶发回原始值 0）─────────────────────────────


class _ScriptedBattery:
    """按脚本依次交出读数，记下被调了几次。"""

    def __init__(self, *results):
        self.results = list(results)
        self.calls = 0

    async def __call__(self, _device):
        self.calls += 1
        return self.results.pop(0)


class _NamedDevice:
    device_id = "AA:00:00:00:00:09"


def test_battery_retry_recovers_from_a_transient_zero_read():
    from wt901 import Battery

    from gait.app.blesource import read_battery_with_retry

    read = _ScriptedBattery(Battery(raw=0, percent=None), Battery(raw=411, percent=100))
    got = asyncio.run(read_battery_with_retry(_NamedDevice(), read=read, delay_s=0))
    assert got == Battery(raw=411, percent=100)
    assert read.calls == 2


def test_battery_retry_recovers_from_a_missing_read():
    from wt901 import Battery

    from gait.app.blesource import read_battery_with_retry

    read = _ScriptedBattery(None, None, Battery(raw=409, percent=100))
    got = asyncio.run(read_battery_with_retry(_NamedDevice(), read=read, delay_s=0))
    assert got.percent == 100
    assert read.calls == 3


def test_battery_retry_stops_at_the_first_plausible_read():
    from wt901 import Battery

    from gait.app.blesource import read_battery_with_retry

    read = _ScriptedBattery(Battery(raw=419, percent=100), Battery(raw=0, percent=None))
    asyncio.run(read_battery_with_retry(_NamedDevice(), read=read, delay_s=0))
    assert read.calls == 1


def test_battery_retry_gives_up_and_returns_the_last_raw_value():
    """三次都不可信时交出最后那份原始值：自检据此说「读数无效（原始值 0）」而不是笼统的读不到。"""
    from wt901 import Battery

    from gait.app.blesource import BATTERY_READ_ATTEMPTS, read_battery_with_retry

    read = _ScriptedBattery(*[Battery(raw=0, percent=None)] * BATTERY_READ_ATTEMPTS)
    got = asyncio.run(read_battery_with_retry(_NamedDevice(), read=read, delay_s=0))
    assert got == Battery(raw=0, percent=None)
    assert read.calls == BATTERY_READ_ATTEMPTS


def test_device_ops_reads_battery_with_retry_by_default():
    from gait.app.blesource import DeviceOps, read_battery_with_retry

    assert DeviceOps().read_battery is read_battery_with_retry
