"""真实 BLE 设备源：`DeviceSource` 的硬件实现（RAY-493）。

## 为什么需要它

`sources.StubDeviceSource` 只能让落盘那条路上流过合成字节。真正的 BLE 采集此前只
活在 CLI 里（`cli/v3prime.py` 的 `_connect` / `_run_live`、`cli/linktest.py`），
sidecar 拿不到。本模块把那套**已被真机验证过的时序**原样搬进一个 `DeviceSource`：
扫描重试 4 次、左右足按 MAC 过滤或扫描顺序、先各自配置（`defer_rate=True`）再
`gather` 一起写速率开流、消费者丢弃开流前的积压。**不改任何共享算法路径**，只 import。

## 三个线程，各做各的

* **BLE 循环线程**（本模块自己的）：bleak 客户端绑定在创建它的事件循环上，所以
  扫描、连接、配置、`device.samples()` 的消费全部在这一个循环里跑。
* **service 的 `TransportLoop` 线程**：`SessionCapture.wrap` 包出来的
  `RecordingTransport` 的 `connect()` / `disconnect()` 在那里跑。它调到的是
  `SessionPort`，而 `SessionPort` 只翻转监听登记，**不碰 BLE 循环** —— 所以两个
  循环之间没有任何「在错的循环上 await 一个 bleak future」的可能。
* **service 主线程**：同步地读 `read_batteries()` / `arrival_rates()` 等。这些读数
  由 BLE 线程写、主线程读，全部经锁。

字节的走向：bleak 通知（BLE 线程）→ `TapTransport` → 先给已登记的 `SessionPort`
（→ `RecordingTransport` → `ThreadedRecordingWriter` 入队，线程安全）→ 再给
`WT901Device` 解帧。**先落盘后计算**（原则 6）在这里就是一个调用顺序。

## 流为什么从自检就开着

PRD §6.1：自检通过后开启并持续记录。更实际的理由是：到达率与链路档位**只能从正在
流的数据里量出来**。所以 `refresh()` 连上即开流，`begin_stream()` / `end_stream()`
对流是空操作 —— 会话的边界由 `SessionPort` 的登记/注销划定，而不是由开关流划定。
反复开关流还会重演 `configure_streaming` 文档里那段「第二台过渡期」的劣化。

## 连不上时不崩

任何连接失败都只写 stderr，读数停在「未连接」的值（电量 `None`、到达率 0、链路
`bad`）—— 自检于是如实报 `E-BLE-1001`，而不是让 sidecar 进程死掉。再调一次
`refresh()` 即重连。
"""

from __future__ import annotations

import argparse
import asyncio
import concurrent.futures
import contextlib
import json
import math
import os
import shutil
import sys
import threading
import time
from collections import deque
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NoReturn

from wt901 import Battery, BleTransport, DiscoveredDevice, Transport, WT901Device, scan
from wt901.transport.base import DataCallback

from gait.app.bindings import FOOT_COLORS, BindingStore, IdentifyFailure, mask_mac
from gait.app.sources import LINK_GRADES
from gait.device.binding import (
    BindingError,
    DeviceIdentity,
    FootBinding,
    admit_for_session,
)
from gait.device.ble import (
    AppliedConfig,
    close_quietly,
    configure_streaming,
    read_battery_at_low_rate,
    start_streaming,
)
from gait.device.identity import (
    MAC_PROVENANCE,
    PLATFORM_PROVENANCE,
    resolve_recording_identity,
)

__all__ = [
    "ARRIVAL_WINDOW_S",
    "CONNECT_WAIT_S",
    "NOMINAL_RATE_HZ",
    "ArrivalWindow",
    "BleDeviceSource",
    "DeviceOps",
    "IdentifyFailure",
    "SessionPort",
    "StepCounter",
    "TapTransport",
    "grade_link",
    "mask_address",
    "read_battery_with_retry",
]

FEET: tuple[str, str] = ("L", "R")

#: 名义采样率。到达率 = 最近 1 s 的样本数 / 它。
NOMINAL_RATE_HZ = 200.0
ARRIVAL_WINDOW_S = 1.0
#: `refresh()` 最多等这么久。连接没完成就先返回 —— 自检请求不能被一次卡住的 BLE
#: 连接挂死，后台连接照常跑完，下一次 `refresh()` 看得到结果。
CONNECT_WAIT_S = 15.0
SCAN_ATTEMPTS = 4
DEFAULT_SCAN_TIMEOUT_S = 5.0
#: 链路三档的阈值（到达率）。
GOOD_ARRIVAL = 0.95
FAIR_ARRIVAL = 0.80
#: 关闭时每台设备的等待上限；`close_quietly` 自身也有超时，这里是整体兜底。
CLOSE_WAIT_S = 12.0
#: 电量读数不可用时的重试（WT901 RAY-182）：寄存器偶发回原始值 0 —— flash 写入期间必现、
#: 不写直接读也有 1/8。flash 写完的恢复实测 299~327 ms，所以间隔取 0.3 s。
BATTERY_READ_ATTEMPTS = 3
BATTERY_RETRY_DELAY_S = 0.3
#: 配对识别（RAY-479）的整体等待上限：两次扫描 + 逐台连上读 MAC 再断开。sidecar 的
#: 请求超时是 120 s（`apps/terminal/main/runtimeConfig.js`），这里要留出余量。
IDENTIFY_WAIT_S = 90.0
#: 配对识别至少扫这么多次再下结论。「只开了一台」要靠「没扫到第二台」来证明，而
#: 一次扫描扫不全是常态（见 `_select`）—— 只扫一次就可能把两台里的一台当成唯一。
IDENTIFY_MIN_SCANS = 2


def _log(message: str) -> None:
    """诊断一律走 stderr：stdout 是 JSON Lines 协议通道（见 `__main__`）。"""
    print(f"[blesource] {message}", file=sys.stderr, flush=True)


# ── 字节扇出 ──────────────────────────────────────────────────────────────


class TapTransport(Transport):
    """包住真实传输，把收到的字节同时交给上层设备与若干可随时挂接的监听者。

    wt901 的 `Transport.on_data` 只容纳**一个**回调，而这里有两个消费者：
    `WT901Device`（解帧算读数）与会话录制（落盘）。录制只在会话期间存在，设备则从
    自检起一直在；所以设备占住 `on_data`，录制走可挂可摘的监听集合。

    监听集合是一个不可变元组，写时在锁内整体替换、读时不加锁 —— BLE 回调热路径上
    不该有锁竞争。
    """

    def __init__(self, inner: Transport) -> None:
        super().__init__()
        self._inner = inner
        self._listeners: tuple[DataCallback, ...] = ()
        self._lock = threading.Lock()
        self._listener_errors = 0
        inner.on_data(self._fan_out)
        inner.on_disconnect(self._emit_disconnect)

    @property
    def inner(self) -> Transport:
        return self._inner

    @property
    def device_id(self) -> str:
        return self._inner.device_id

    @property
    def is_connected(self) -> bool:
        return self._inner.is_connected

    @property
    def listener_count(self) -> int:
        return len(self._listeners)

    async def connect(self) -> None:
        await self._inner.connect()

    async def disconnect(self) -> None:
        await self._inner.disconnect()

    async def write(self, data: bytes) -> None:
        await self._inner.write(data)

    async def read_rssi(self) -> int | None:
        return await self._inner.read_rssi()

    def attach(self, listener: DataCallback) -> None:
        with self._lock:
            if listener not in self._listeners:
                self._listeners = (*self._listeners, listener)

    def detach(self, listener: DataCallback) -> None:
        with self._lock:
            self._listeners = tuple(item for item in self._listeners if item != listener)

    def _fan_out(self, data: bytes) -> None:
        # 录制在前、解帧在后：原始字节是唯一不可再生的东西（原则 6）。
        for listener in self._listeners:
            try:
                listener(data)
            except Exception as error:  # noqa: BLE001 - 一个监听者坏了不该饿死设备
                self._listener_errors += 1
                if self._listener_errors == 1:
                    _log(f"{self.device_id} 的字节监听者抛异常（后续同类不再打印）：{error!r}")
        self._emit_data(data)


class SessionPort(Transport):
    """交给 `SessionCapture.wrap` 的那一条「会话视图」传输。

    ## 为什么不直接把 `TapTransport` 交出去

    `RecordingTransport.connect()` 会调 `inner.on_data(...)` 与 `inner.connect()`，
    `disconnect()` 会调 `inner.disconnect()`。直接交出 tap 的话，会话一结束就会把
    **BLE 链路本身**断掉，并且把设备的解帧回调顶掉。而链路从自检起就开着、会话之后
    还要继续给设备页供读数。

    所以这里的 connect / disconnect 只是**登记与注销**：连上时挂到 tap 的监听集合，
    断开时摘下。它们是纯内存操作，可以在 service 自己的事件循环里安全 await，
    与 BLE 循环没有任何交集。

    ## 跨重连保持同一实例

    `transports()` 必须每次返回同一实例（service 靠它对齐会话记录），而重连会换出
    新的 tap。`bind()` 把登记从旧 tap 挪到新 tap，已经在录的会话因此不断录。
    """

    def __init__(self, foot: str, *, loop: asyncio.AbstractEventLoop | None = None) -> None:
        super().__init__()
        self._foot = foot
        self._loop = loop
        self._tap: TapTransport | None = None
        self._attached = False
        self._lock = threading.Lock()

    @property
    def foot(self) -> str:
        return self._foot

    @property
    def device_id(self) -> str:
        tap = self._tap
        return tap.device_id if tap is not None else f"ble-unconnected-{self._foot}"

    @property
    def is_connected(self) -> bool:
        tap = self._tap
        return self._attached and tap is not None and tap.is_connected

    @property
    def attached(self) -> bool:
        return self._attached

    def bind(self, tap: TapTransport | None) -> None:
        """换绑到新的 tap（或解绑）。已登记的会话随之挪过去。"""
        with self._lock:
            old = self._tap
            if old is not None and self._attached:
                old.detach(self._deliver)
            self._tap = tap
            if tap is not None and self._attached:
                tap.attach(self._deliver)

    async def connect(self) -> None:
        with self._lock:
            self._attached = True
            if self._tap is not None:
                self._tap.attach(self._deliver)

    async def disconnect(self) -> None:
        with self._lock:
            self._attached = False
            if self._tap is not None:
                self._tap.detach(self._deliver)

    async def write(self, data: bytes) -> None:
        """下行写转给 tap。tap 的写属于 BLE 循环，从别的循环调时要递过去。"""
        tap = self._tap
        if tap is None:
            raise ConnectionError(f"{self._foot} 足未连接，写入被拒绝")
        loop = self._loop
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:  # pragma: no cover - 协程里总有运行中的循环
            running = None
        if loop is None or loop is running:
            await tap.write(data)
            return
        await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(tap.write(data), loop))

    def _deliver(self, data: bytes) -> None:
        self._emit_data(data)


# ── 读数的纯计算部分（可离线测）─────────────────────────────────────────────


class ArrivalWindow:
    """最近 `ARRIVAL_WINDOW_S` 秒内到达的样本时刻。线程安全。

    时刻用 `ImuSample.t_host`（wt901 在通知回调里取的 `time.monotonic()`），读的时候
    用同一个钟的「现在」 —— 两者同源，差值才有意义。
    """

    def __init__(
        self,
        *,
        window_s: float = ARRIVAL_WINDOW_S,
        nominal_hz: float = NOMINAL_RATE_HZ,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._window = window_s
        self._nominal = nominal_hz
        self._clock = clock
        self._times: deque[float] = deque()
        self._lock = threading.Lock()

    def record(self, t: float) -> None:
        with self._lock:
            self._times.append(t)
            self._trim(t)

    def clear(self) -> None:
        with self._lock:
            self._times.clear()

    def count(self, now: float | None = None) -> int:
        with self._lock:
            self._trim(self._clock() if now is None else now)
            return len(self._times)

    def rate(self, now: float | None = None) -> float:
        """0–1。超过名义速率（BLE 批量到达的抖动）按 1 截断。"""
        return min(1.0, max(0.0, self.count(now) / (self._window * self._nominal)))

    def _trim(self, now: float) -> None:
        horizon = now - self._window
        times = self._times
        while times and times[0] <= horizon:
            times.popleft()


def grade_link(rate: float) -> str:
    """到达率 → 链路三档。档名取自 `LINK_GRADES`，不另起一套词。"""
    good, fair, bad = LINK_GRADES
    if rate >= GOOD_ARRIVAL:
        return good
    if rate >= FAIR_ARRIVAL:
        return fair
    return bad


class StepCounter:
    """**仅供显示**的朴素步数：陀螺模长越过阈值的上升沿，带不应期。

    这不是步态算法的步数 —— 那条在 `core/` 里，离线跑、有验收。这里只是让采集界面
    上的数字随脚的摆动而动，好让操作员一眼看出「这只脚在出数」。它从不进报告、
    不进会话元数据，任何指标都不该从它算。

    一只脚一次摆动产生一个角速度峰，所以每只脚数的是**这只脚**的步。
    """

    def __init__(self, *, threshold: float = 2.5, refractory_s: float = 0.3) -> None:
        self._threshold = threshold
        self._refractory = refractory_s
        self._above = False
        self._last: float | None = None
        self._count = 0
        self._lock = threading.Lock()

    @property
    def count(self) -> int:
        return self._count

    def reset(self) -> None:
        with self._lock:
            self._count = 0
            self._above = False
            self._last = None

    def feed(self, t: float, gyro_magnitude: float) -> None:
        with self._lock:
            if gyro_magnitude < self._threshold:
                self._above = False
                return
            if self._above:
                return
            self._above = True
            if self._last is not None and t - self._last < self._refractory:
                return
            self._last = t
            self._count += 1


def mask_address(address: str | None) -> str | None:
    """只留尾巴。macOS 上是 CoreBluetooth UUID，Windows/Linux 上是 MAC。"""
    if not address:
        return None
    if ":" in address:
        return "…:" + ":".join(address.split(":")[-2:])
    return "…" + address[-4:]


# ── 设备操作（可注入，测试里换成假的）──────────────────────────────────────


async def read_battery_with_retry(
    device: WT901Device,
    *,
    read: Callable[[WT901Device], Awaitable[Battery | None]] = read_battery_at_low_rate,
    attempts: int = BATTERY_READ_ATTEMPTS,
    delay_s: float = BATTERY_RETRY_DELAY_S,
) -> Battery | None:
    """读电量；读不到或读到不可信的原始值就隔一会儿重读。

    ## 为什么要重读，而不是交给自检让操作员「重新检查」

    电量只在**连接时**读一次（高速流期间寄存器读来不及回复，手册 §6）。已连上时点
    「重新检查设备」不会断开重连，所以这一次读偏了，自检就会一直停在「电量读数无效」
    直到断开 —— 操作员没有任何能自己做的动作。2026-09-16 真机首次 `--probe` 两脚都是这样。

    成因与 WT901 RAY-182 一致：寄存器偶发回原始值 0，wt901 如实给 `percent=None`
    （不把它映射成 0%）。那是瞬时的，隔 ~300 ms 再读即恢复。

    返回最后一次的结果：三次都不可信时仍把那份原始值交出去，自检据此给出「读数无效
    （原始值 N）」—— 比笼统的「读不到」多一条可查的线索。
    """
    result: Battery | None = None
    for attempt in range(1, attempts + 1):
        result = await read(device)
        if result is not None and result.is_plausible:
            return result
        if attempt < attempts:
            _log(
                f"{device.device_id} 电量读数不可用（{_battery_text(result)}），"
                f"{delay_s:.1f} s 后重读（{attempt}/{attempts}）"
            )
            await asyncio.sleep(delay_s)
    return result


def _battery_text(battery: Battery | None) -> str:
    return "未读到" if battery is None else f"原始值 {battery.raw}"


async def _read_firmware(device: WT901Device) -> str:
    try:
        return await asyncio.wait_for(device.telemetry.read_version(), timeout=3.0)
    except Exception:  # noqa: BLE001 - 读不到版本就如实记 unknown
        return "unknown"


@dataclass
class DeviceOps:
    """把 BLE 上的每一步做成可替换的，好让连接编排在没有硬件时被完整测到。

    默认值就是真实实现：wt901 的 `scan` / `BleTransport`，`device/ble.py` 与
    `device/identity.py` 里已被真机验证过的函数。
    """

    scan: Callable[[float], Awaitable[Sequence[DiscoveredDevice]]] = field(
        default=lambda timeout: scan(timeout=timeout)
    )
    transport: Callable[[DiscoveredDevice], Transport] = field(default=BleTransport)
    read_battery: Callable[[WT901Device], Awaitable[Battery | None]] = field(
        default=read_battery_with_retry
    )
    resolve_identity: Callable[..., Awaitable[tuple[DeviceIdentity, str | None]]] = field(
        default=resolve_recording_identity
    )
    read_firmware: Callable[[WT901Device], Awaitable[str]] = field(default=_read_firmware)
    configure: Callable[..., Awaitable[AppliedConfig]] = field(
        default=lambda device: configure_streaming(device, defer_rate=True)
    )
    start: Callable[..., Awaitable[AppliedConfig]] = field(
        default=lambda device, applied: start_streaming(device, applied.requested, applied)
    )
    close: Callable[[WT901Device], Awaitable[str | None]] = field(default=close_quietly)


@dataclass
class _Foot:
    """一只脚连上之后的全部状态。"""

    discovered: DiscoveredDevice
    tap: TapTransport
    device: WT901Device
    battery: Battery | None = None
    identity: DeviceIdentity | None = None
    identity_degraded: str | None = None
    firmware: str = "unknown"
    applied: AppliedConfig | None = None


class _ConnectError(RuntimeError):
    pass


# ── 设备源 ────────────────────────────────────────────────────────────────


class BleDeviceSource:
    """两只 WT901 的真实设备源。见模块文档。"""

    def __init__(
        self,
        *,
        left: str | None = None,
        right: str | None = None,
        scan_timeout: float = DEFAULT_SCAN_TIMEOUT_S,
        session_root: Path | None = None,
        ops: DeviceOps | None = None,
        clock: Callable[[], float] = time.monotonic,
        connect_wait_s: float = CONNECT_WAIT_S,
        bindings: BindingStore | None = None,
    ) -> None:
        self.left = left
        self.right = right
        self.scan_timeout = scan_timeout
        self.session_root = session_root
        #: 左右绑定（RAY-479）。**有它就只按绑定的 MAC 分左右**，没有绑定就不连；
        #: 没有它（CLI 探针、离线测试）才退回地址过滤 / 扫描顺序。
        self.bindings = bindings
        self._ops = ops or DeviceOps()
        self._clock = clock
        self._connect_wait = connect_wait_s

        self._lock = threading.RLock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._pending: concurrent.futures.Future[Any] | None = None
        self._closed = False

        self._feet: dict[str, _Foot] = {}
        self._consumers: list[asyncio.Task[None]] = []
        self._streaming = False
        self._foot_assignment: str | None = None
        self._last_error: str | None = None
        self._binding_problem: str | None = None
        self._windows = {label: ArrivalWindow(clock=clock) for label in FEET}
        self._steps = {label: StepCounter() for label in FEET}
        self._ports = {label: SessionPort(label) for label in FEET}

    # ── 构造 ──

    #: 真实传感器必须先绑定左右才能开正式会话（RAY-479）。service 据此在自检里加一项。
    binding_required = True

    @classmethod
    def from_environment(cls, env: Mapping[str, str]) -> BleDeviceSource:
        """`GAIT_BLE_LEFT` / `GAIT_BLE_RIGHT`：地址子串（可选）；
        `GAIT_BLE_SCAN_TIMEOUT`：单次扫描秒数，缺省 5；`GAIT_SESSION_ROOT` 供磁盘余量；
        `GAIT_CONFIG_ROOT`：左右绑定所在的设备配置目录 —— 设了就按绑定分左右，
        地址过滤随之不再参与。

        写坏的超时回落到缺省值而不是抛：进程入口在这里抛异常只会退回「设备不可用」，
        而一个打错的数字不值得让操作员连设备都看不到。
        """
        try:
            timeout = float(env.get("GAIT_BLE_SCAN_TIMEOUT", "") or DEFAULT_SCAN_TIMEOUT_S)
            if not math.isfinite(timeout) or timeout <= 0:
                raise ValueError(timeout)
        except ValueError:
            timeout = DEFAULT_SCAN_TIMEOUT_S
        root = (env.get("GAIT_SESSION_ROOT") or "").strip()
        config = (env.get("GAIT_CONFIG_ROOT") or "").strip()
        return cls(
            left=(env.get("GAIT_BLE_LEFT") or "").strip() or None,
            right=(env.get("GAIT_BLE_RIGHT") or "").strip() or None,
            scan_timeout=timeout,
            session_root=Path(root) if root else None,
            bindings=BindingStore(Path(config)) if config else None,
        )

    # ── 生命周期 ──

    @property
    def state(self) -> str:
        with self._lock:
            if self._closed:
                return "closed"
            if self._pending is not None and not self._pending.done():
                return "connecting"
            if self._all_connected():
                return "connected"
            return "failed" if self._last_error else "idle"

    @property
    def last_error(self) -> str | None:
        return self._last_error

    @property
    def binding_problem(self) -> str | None:
        """上一次按绑定连接为什么没连成（未绑定、绑定的模块不在场、扫到陌生模块）。

        自检的「左右模块绑定」一项据此把**这一次**的具体情况说出来。
        """
        return self._binding_problem

    def refresh(self, timeout: float | None = None) -> str:
        """没连上且没在连，就在后台开始连；然后最多等 `timeout` 秒。幂等。

        返回 `state`。等超时不是错误：连接继续在 BLE 循环里跑，下次调用看结果。
        """
        with self._lock:
            if self._closed:
                return "closed"
            if self._all_connected():
                return "connected"
            if self._pending is None or self._pending.done():
                loop = self._ensure_loop()
                self._pending = asyncio.run_coroutine_threadsafe(self._connect(), loop)
            pending = self._pending
        with contextlib.suppress(Exception, concurrent.futures.CancelledError):
            pending.result(self._connect_wait if timeout is None else timeout)
        return self.state

    def close(self) -> None:
        """关掉两台设备（带超时）并停下 BLE 循环。重复调用无副作用。"""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            loop, thread = self._loop, self._thread
            pending = self._pending
        if pending is not None and not pending.done():
            # 连接还在跑就先取消它，否则它可能在 teardown 之后又把设备连上。
            pending.cancel()
        if loop is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(self._teardown(), loop).result(
                CLOSE_WAIT_S * len(FEET)
            )
        except Exception as error:  # noqa: BLE001 - 关不干净也要让进程能退
            _log(f"关闭设备未完成：{error!r}")
        loop.call_soon_threadsafe(loop.stop)
        if thread is not None:
            thread.join(timeout=CLOSE_WAIT_S)
        with self._lock:
            self._loop = self._thread = None

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        if self._loop is not None:
            return self._loop
        loop = asyncio.new_event_loop()
        ready = threading.Event()

        def run() -> None:
            asyncio.set_event_loop(loop)
            loop.call_soon(ready.set)
            loop.run_forever()
            loop.close()

        # daemon：sidecar 被杀时这个线程不该拦着进程退出（同 `TransportLoop`）。
        thread = threading.Thread(target=run, name="gait-ble-loop", daemon=True)
        thread.start()
        ready.wait(timeout=5.0)
        self._loop, self._thread = loop, thread
        for port in self._ports.values():
            port._loop = loop
        return loop

    # ── 连接编排（在 BLE 循环里跑）──

    async def _connect(self) -> None:
        await self._teardown()
        self._last_error = None
        self._binding_problem = None
        opened: list[_Foot] = []
        try:
            if self.bindings is not None:
                bound = await self._select_bound(self._usable_binding())
                opened = [bound[label] for label in FEET]
                assignment = "binding"
            else:
                selected, assignment = await self._select()
                for label, discovered in zip(FEET, selected, strict=True):
                    opened.append(await self._open_foot(label, discovered))
            for foot in opened:
                foot.applied = await self._ops.configure(foot.device)
            # 两台都配完再**一起**写速率开流 —— 见 `configure_streaming` 的 defer_rate。
            started = await asyncio.gather(
                *(self._ops.start(foot.device, foot.applied) for foot in opened)
            )
            for foot, applied in zip(opened, started, strict=True):
                foot.applied = applied
                if not applied.verified:
                    # CLI 在这里中止；sidecar 不中止，而是记进 provenance 随会话落盘。
                    # 自检界面上的到达率/链路仍然是真实量出来的，数据的可疑之处有据可查。
                    _log(f"{foot.discovered.address} 配置回读不一致：{applied.mismatches}")
        except asyncio.CancelledError:
            for foot in opened:
                await self._close_device(foot)
            raise
        except Exception as error:  # noqa: BLE001 - 连接失败只降级，不崩 sidecar
            self._last_error = f"{type(error).__name__}: {error}"
            _log(f"连接失败：{self._last_error}")
            for foot in opened:
                await self._close_device(foot)
            return
        # 积压线：开流前在队列里排着的样本按上一轮的速率产出，不进到达率（同 v3prime）。
        started_at = self._clock()
        with self._lock:
            for label, foot in zip(FEET, opened, strict=True):
                self._feet[label] = foot
                self._windows[label].clear()
                self._steps[label].reset()
                self._ports[label].bind(foot.tap)
            self._foot_assignment = assignment
            self._streaming = True
        loop = asyncio.get_running_loop()
        self._consumers = [
            loop.create_task(self._consume(label, foot.device, started_at))
            for label, foot in zip(FEET, opened, strict=True)
        ]
        _log(
            "两台已连接并开流："
            + "，".join(f"{label}={foot.discovered.address}" for label, foot in self._feet.items())
            + f"（左右足来源：{assignment}）"
        )

    async def _select(self) -> tuple[list[DiscoveredDevice], str]:
        """扫描并按左右足排好。模块广播有间隔，一次扫不全是常态 —— 重扫 4 次。"""
        needles = {"L": self.left, "R": self.right}
        found: list[DiscoveredDevice] = []
        problem = "未扫描"
        for attempt in range(1, SCAN_ATTEMPTS + 1):
            try:
                found = list(await self._ops.scan(self.scan_timeout))
            except Exception as error:  # noqa: BLE001 - 蓝牙关着等，重试后照实报
                problem = f"扫描失败：{error}"
                _log(f"{problem}（{attempt}/{SCAN_ATTEMPTS}）")
                continue
            chosen = _assign(found, needles)
            if chosen is not None:
                if self.left and self.right:
                    assignment = "explicit_mac"
                elif self.left or self.right:
                    assignment = "partial_mac"
                else:
                    assignment = "scan_order"
                return chosen, assignment
            problem = (
                f"扫描到 {len(found)} 台，未凑齐左右两台"
                f"（过滤 L={self.left!r} R={self.right!r}）：{[d.address for d in found]}"
            )
            _log(f"{problem}（{attempt}/{SCAN_ATTEMPTS}）")
        raise _ConnectError(problem + "。确认模块已按键开机且在范围内。")

    def _usable_binding(self) -> FootBinding:
        """读绑定；不能用来分左右时抛 `_ConnectError` 并记下原因。

        「不能用」包括：没绑、只绑了一只、文件坏了、绑定用的身份种类或推导与当前不符。
        这些情况下**不连接**，也不退回扫描顺序 —— 扫描顺序分出来的左右正是绑定要取代的
        东西（RAY-479 用户拍板：绑定之后左右只看 MAC）。
        """
        assert self.bindings is not None
        try:
            binding = self.bindings.read()
        except BindingError as error:
            self._refuse(f"左右绑定文件读不回来（{error}），需要重新配对。")
        present = {identity for identity in (binding.left, binding.right) if identity}
        verdict = admit_for_session(
            binding, present, current_kind="mac", current_provenance=MAC_PROVENANCE
        )
        if not verdict.admitted:
            self._refuse(" ".join(verdict.problems))
        return binding

    def _refuse(self, problem: str) -> NoReturn:
        self._binding_problem = problem
        raise _ConnectError(problem)

    async def _select_bound(self, binding: FootBinding) -> dict[str, _Foot]:
        """扫描，逐台连上读设备自报 MAC，按绑定认出左右。

        MAC 是唯一依据：扫描顺序、平台地址、信号强弱都不参与。认出的留着（之后配置
        开流），认不出的立刻断开。两只都认出就停，不再去碰剩下的模块。

        绑定的模块凑不齐就抛 —— 读数于是停在「未连接」，自检如实阻断，并由
        `binding_problem` 说出缺的是哪一只、扫到了哪些陌生模块。
        """
        wanted = {"L": binding.left, "R": binding.right}
        found: dict[str, _Foot] = {}
        #: 已经读过身份的地址 → 身份（读不到 MAC 记 None）。重扫时不重复连它们。
        seen: dict[str, DeviceIdentity | None] = {}
        scan_problem: str | None = None
        try:
            for attempt in range(1, SCAN_ATTEMPTS + 1):
                try:
                    discovered = list(await self._ops.scan(self.scan_timeout))
                except Exception as error:  # noqa: BLE001 - 蓝牙关着等，重试后照实报
                    scan_problem = f"扫描失败：{error}"
                    _log(f"{scan_problem}（{attempt}/{SCAN_ATTEMPTS}）")
                    continue
                scan_problem = None
                for candidate in discovered:
                    if candidate.address in seen:
                        continue
                    try:
                        foot = await self._open_foot("?", candidate)
                    except asyncio.CancelledError:
                        raise
                    except Exception as error:  # noqa: BLE001 - 一台连不上不挡另一台
                        _log(f"{candidate.address} 连接失败，稍后重试：{error!r}")
                        continue
                    identity = None if foot.identity_degraded else foot.identity
                    seen[candidate.address] = identity
                    label = next(
                        (
                            side
                            for side in FEET
                            if side not in found and identity is not None
                            and wanted[side] == identity
                        ),
                        None,
                    )
                    if label is None:
                        await self._close_device(foot)
                        continue
                    found[label] = foot
                    if len(found) == len(FEET):
                        return found
                _log(
                    f"按绑定认出 {sorted(found)}，还缺 "
                    f"{[side for side in FEET if side not in found]}（{attempt}/{SCAN_ATTEMPTS}）"
                )
            missing = [side for side in FEET if side not in found]
            parts = [
                f"{FOOT_COLORS[side]}模块 {mask_mac(wanted[side].value if wanted[side] else None)}"
                " 没有找到"
                for side in missing
            ]
            strangers = sorted(
                {
                    mask_mac(identity.value) or "?"
                    for identity in seen.values()
                    if identity is not None and identity not in wanted.values()
                }
            )
            if strangers:
                parts.append(f"扫描到未绑定的模块 {'、'.join(strangers)}")
            if any(identity is None for identity in seen.values()):
                parts.append("有模块读不到自报 MAC")
            if scan_problem:
                parts.append(scan_problem)
            self._binding_problem = "在场模块与左右绑定不符：" + "；".join(parts) + "。"
            raise _ConnectError(self._binding_problem)
        except BaseException:
            for foot in found.values():
                await self._close_device(foot)
            raise

    # ── 配对识别（RAY-479）──

    def identify_for_binding(
        self,
        label: str,
        *,
        exclude: DeviceIdentity | None = None,
        timeout: float = IDENTIFY_WAIT_S,
    ) -> DeviceIdentity:
        """找出**唯一**一台开着的模块，读出它自报的 MAC。不写任何东西。

        `exclude` 是已经绑在另一只脚上的身份：配右脚时左脚的蓝色模块还开着是正常的，
        它不算候选。

        恰好一台才返回；零台、多台、读不到 MAC、连不上都抛 `IdentifyFailure`，文案
        按颜色说清楚该开哪一台 —— **从不在多台里挑一台**：挑错的那一刻起，此后每一场
        会话都左右镜像，而每个指标单看都像真的。

        开始前先断开现有连接：连着的模块不广播，扫描就看不见它。
        """
        if label not in FEET:
            raise ValueError(f"脚标必须是 'L' 或 'R'，收到 {label!r}")
        with self._lock:
            if self._closed:
                raise IdentifyFailure("设备源已关闭，无法配对。", "请重启应用后重试。")
            pending = self._pending
            if pending is not None and not pending.done():
                pending.cancel()
            loop = self._ensure_loop()
            future = asyncio.run_coroutine_threadsafe(self._identify(label, exclude), loop)
            # 占住 `_pending`：识别期间 `refresh()` 不会并发地再起一次连接。
            self._pending = future
        try:
            return future.result(timeout)
        except concurrent.futures.TimeoutError as error:
            future.cancel()
            raise IdentifyFailure(
                f"识别{FOOT_COLORS[label]}模块超时（{timeout:.0f} 秒）。",
                f"请只打开{FOOT_COLORS[label]}模块、靠近电脑后重试。",
            ) from error
        except concurrent.futures.CancelledError as error:
            raise IdentifyFailure(
                f"识别{FOOT_COLORS[label]}模块被中断。", "请重试。"
            ) from error

    async def _identify(self, label: str, exclude: DeviceIdentity | None) -> DeviceIdentity:
        await self._teardown()
        with self._lock:
            self._foot_assignment = None
        color = FOOT_COLORS[label]
        other = FOOT_COLORS["R" if label == "L" else "L"]
        power_only = f"请只打开{color}模块，其余模块先关机，然后重试。"

        union: dict[str, DiscoveredDevice] = {}
        scans = 0
        scan_problem: str | None = None
        for attempt in range(1, SCAN_ATTEMPTS + 1):
            try:
                found = await self._ops.scan(self.scan_timeout)
            except Exception as error:  # noqa: BLE001 - 蓝牙关着等，重试后照实报
                scan_problem = f"扫描失败：{error}"
                _log(f"配对{scan_problem}（{attempt}/{SCAN_ATTEMPTS}）")
                continue
            scans += 1
            for candidate in found:
                union.setdefault(candidate.address, candidate)
            if union and scans >= IDENTIFY_MIN_SCANS:
                break
        if not union:
            if scan_problem and scans == 0:
                raise IdentifyFailure(
                    f"蓝牙扫描失败，无法识别{color}模块（{scan_problem}）。",
                    "请确认电脑蓝牙已打开、应用有蓝牙权限，然后重试。",
                )
            raise IdentifyFailure(
                f"没有发现模块：请打开{color}模块并靠近电脑。",
                f"按一下{color}模块的开机键，确认指示灯亮起后点「重试」。",
            )

        identified: list[DeviceIdentity] = []
        unreadable: list[str] = []
        unreachable: list[str] = []
        excluded = 0
        for candidate in union.values():
            try:
                foot = await self._open_foot(label, candidate)
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 - 如实计入，不崩
                _log(f"配对时 {candidate.address} 连接失败：{error!r}")
                unreachable.append(candidate.address)
                continue
            try:
                if foot.identity_degraded or foot.identity is None:
                    unreadable.append(candidate.address)
                elif exclude is not None and foot.identity == exclude:
                    excluded += 1
                else:
                    identified.append(foot.identity)
            finally:
                await self._close_device(foot)

        total = len(identified) + len(unreadable) + len(unreachable)
        if total > 1:
            raise IdentifyFailure(
                f"发现多个未绑定模块（{total} 台）：无法确定哪一台是{color}模块。",
                power_only,
            )
        if identified:
            _log(f"配对：{label} 足识别到 {identified[0].value}")
            return identified[0]
        if unreadable:
            raise IdentifyFailure(
                f"发现一台模块，但读不到它自报的 MAC，无法确认是{color}模块。",
                f"请把{color}模块关机再开机、靠近电脑后重试。",
            )
        if unreachable:
            raise IdentifyFailure(
                f"发现一台模块，但连接失败，无法确认是{color}模块。",
                f"请把{color}模块靠近电脑后重试；仍不行就关机再开机。",
            )
        # 只剩已绑在另一只脚上的那台。
        assert excluded
        raise IdentifyFailure(
            f"只发现已绑定为{other}的模块，没有发现{color}模块。",
            f"请打开{color}模块并靠近电脑，然后重试。",
        )

    async def _open_foot(self, label: str, discovered: DiscoveredDevice) -> _Foot:
        tap = TapTransport(self._ops.transport(discovered))
        device = WT901Device(tap)
        foot = _Foot(discovered=discovered, tap=tap, device=device)
        try:
            await device.open()
            # 电量先读：它会把速率临时降到 10 Hz，之后的 MAC / 版本回读才来得及回复。
            foot.battery = await self._ops.read_battery(device)
            foot.identity, foot.identity_degraded = await self._ops.resolve_identity(
                device, platform_address=discovered.address
            )
            if foot.identity_degraded:
                _log(f"{label} 足：{foot.identity_degraded}")
            foot.firmware = await self._ops.read_firmware(device)
        except BaseException:
            await self._close_device(foot)
            raise
        return foot

    async def _consume(self, label: str, device: WT901Device, started_at: float) -> None:
        window, steps = self._windows[label], self._steps[label]
        try:
            async for sample in device.samples():
                if sample.t_host < started_at:
                    continue
                window.record(sample.t_host)
                g = sample.gyro
                steps.feed(sample.t_host, math.sqrt(g.x * g.x + g.y * g.y + g.z * g.z))
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - 消费者死了读数归零，不崩
            _log(f"{label} 足样本消费中止：{error!r}")

    async def _teardown(self) -> None:
        consumers, self._consumers = self._consumers, []
        for task in consumers:
            task.cancel()
        if consumers:
            await asyncio.gather(*consumers, return_exceptions=True)
        with self._lock:
            feet = list(self._feet.values())
            self._feet = {}
            self._streaming = False
            for port in self._ports.values():
                port.bind(None)
        for foot in feet:
            await self._close_device(foot)

    async def _close_device(self, foot: _Foot) -> None:
        problem = await self._ops.close(foot.device)
        if problem:
            _log(f"{foot.discovered.address}：{problem}")

    def _all_connected(self) -> bool:
        feet = self._feet
        return (
            self._streaming
            and all(label in feet for label in FEET)
            and all(feet[label].device.is_connected for label in FEET)
        )

    def _connected(self, label: str) -> bool:
        foot = self._feet.get(label)
        return self._streaming and foot is not None and foot.device.is_connected

    # ── DeviceSource 读数 ──

    def read_batteries(self) -> dict[str, Battery | None]:
        """连上时读的那一次（开高速流之前），断开后归 `None`。

        不在高速流期间重读：手册 §6，200 Hz 下寄存器读来不及回复。
        """
        with self._lock:
            return {
                label: self._feet[label].battery if self._connected(label) else None
                for label in FEET
            }

    def arrival_rates(self) -> dict[str, float]:
        now = self._clock()
        with self._lock:
            return {
                label: self._windows[label].rate(now) if self._connected(label) else 0.0
                for label in FEET
            }

    def link_grades(self) -> dict[str, str]:
        return {label: grade_link(rate) for label, rate in self.arrival_rates().items()}

    def step_counts(self) -> dict[str, int]:
        """**仅供显示**，见 `StepCounter`。"""
        return {label: self._steps[label].count for label in FEET}

    def device_readings(self) -> dict[str, dict[str, str]]:
        """左右各一份，**两只都必须在** —— `calib.store.admit_devices` 少一只就抛。

        没连上的脚给一份如实标注的占位：`kind` 是 `platform-address`（不可移植），
        值说明未连接。它在参数库里必然查不到，出厂标定因此判不通过 —— 那正是
        「没连上就不能说标定匹配」的实话。

        **断了链的脚同样给占位**，哪怕上次读到的身份还留着：service 在 `runPreflight`
        与会话元数据里都按「每只脚都在、且读数代表此刻」来用它，一份过期身份会让
        一只已断开的脚在标定准入上看起来是好的。其余读数方法同一口径：两只脚的键
        永远都在，没连上就是电量 `None`、到达率 0、链路 `bad`。
        """
        readings: dict[str, dict[str, str]] = {}
        with self._lock:
            for label in FEET:
                foot = self._feet.get(label)
                if foot is None or foot.identity is None or not self._connected(label):
                    readings[label] = {
                        "kind": "platform-address",
                        "value": f"unconnected-{label}",
                        "provenance": PLATFORM_PROVENANCE,
                        "firmware": "unknown",
                    }
                    continue
                readings[label] = {
                    "kind": foot.identity.kind,
                    "value": foot.identity.value,
                    "provenance": foot.identity.provenance,
                    "firmware": foot.firmware,
                }
        return readings

    def disk_free_bytes(self) -> int:
        """会话根所在卷的余量；根还没建出来就往上找到存在的那一级，再不行看家目录。"""
        candidate = self.session_root
        while candidate is not None and not candidate.exists():
            parent = candidate.parent
            candidate = None if parent == candidate else parent
        try:
            return shutil.disk_usage(candidate or Path.home()).free
        except OSError:
            return 0

    def module_info(self) -> list[dict[str, Any]]:
        """掩码的是**平台地址**，不是设备自报 MAC —— 后者的字节排布尚未经外部证实，
        不该摆上界面让人去对照（见 `device/identity.py`）。"""
        batteries = self.read_batteries()
        with self._lock:
            return [
                {
                    "side": "left" if label == "L" else "right",
                    "maskedAddress": mask_address(
                        self._feet[label].discovered.address if label in self._feet else None
                    ),
                    "batteryPercent": batteries[label].percent if batteries[label] else None,
                }
                for label in FEET
            ]

    def transports(self) -> dict[str, Transport]:
        """两条 `SessionPort`，每次同一实例 —— 包括重连之后。"""
        return dict(self._ports)

    def begin_stream(self) -> None:
        """对流是空操作：流从 `refresh()` 连上起就开着（见模块文档）。

        只把显示用步数清零，好让采集界面从 0 开始数。
        """
        for counter in self._steps.values():
            counter.reset()

    def end_stream(self) -> None:
        """空操作。会话录制的结束由 `SessionPort.disconnect()` 划定；链路保持，
        设备页之后仍需要读数。"""

    def provenance(self) -> dict[str, Any]:
        with self._lock:
            feet = dict(self._feet)
            assignment = self._foot_assignment
        return {
            "source": "ble",
            "hardware": True,
            "foot_assignment": assignment,
            "binding_problem": self._binding_problem,
            "addresses_masked": {
                label: mask_address(feet[label].discovered.address) if label in feet else None
                for label in FEET
            },
            "identity_degraded": {
                label: feet[label].identity_degraded if label in feet else None
                for label in FEET
            },
            "config": {
                label: feet[label].applied.snapshot()
                if label in feet and feet[label].applied is not None
                else None
                for label in FEET
            },
            "step_counts": "display-only",
        }


def _assign(
    found: Sequence[DiscoveredDevice], needles: Mapping[str, str | None]
) -> list[DiscoveredDevice] | None:
    """按过滤子串挑左右足；没给过滤的那只从剩下的里按扫描顺序取。凑不齐返回 None。"""
    chosen: dict[str, DiscoveredDevice] = {}
    for label in FEET:
        needle = needles.get(label)
        if not needle:
            continue
        match = next(
            (
                d
                for d in found
                if needle.lower() in d.address.lower()
                and all(d.address != c.address for c in chosen.values())
            ),
            None,
        )
        if match is None:
            return None
        chosen[label] = match
    rest = [d for d in found if all(d.address != c.address for c in chosen.values())]
    for label in FEET:
        if label not in chosen:
            if not rest:
                return None
            chosen[label] = rest.pop(0)
    return [chosen[label] for label in FEET]


# ── 人工探针 ──────────────────────────────────────────────────────────────


def probe(source: BleDeviceSource, seconds: float, out: Any = sys.stdout) -> int:  # pragma: no cover - 真机
    """连上后每秒打一行 JSON。macOS 的 TCC 不让代理会话碰蓝牙 —— 这个给人在终端里跑。"""
    try:
        state = source.refresh()
        _emit_probe(out, source, state)
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            time.sleep(1.0)
            _emit_probe(out, source, source.state)
        return 0 if source.state == "connected" else 1
    finally:
        source.close()


def _emit_probe(out: Any, source: BleDeviceSource, state: str) -> None:  # pragma: no cover
    batteries = source.read_batteries()
    out.write(
        json.dumps(
            {
                "t": round(time.monotonic(), 3),
                "state": state,
                "error": source.last_error,
                # 原始值一并打出：只打 percent 时「读到原始值 0」与「没读到」都显示成 null，
                # 2026-09-16 首次真机探测就因此看不出成因。
                "batteries": {
                    k: ({"percent": v.percent, "raw": v.raw} if v is not None else None)
                    for k, v in batteries.items()
                },
                "arrival": {k: round(v, 3) for k, v in source.arrival_rates().items()},
                "links": source.link_grades(),
                "steps": source.step_counts(),
                "identities": source.device_readings(),
                "provenance": source.provenance(),
            },
            ensure_ascii=False,
        )
        + "\n"
    )
    out.flush()


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - 进程入口
    parser = argparse.ArgumentParser(prog="python -m gait.app.blesource")
    parser.add_argument("--probe", action="store_true", help="连接并每秒打印读数")
    parser.add_argument("--seconds", type=float, default=10.0)
    args = parser.parse_args(argv)
    if not args.probe:
        parser.print_help()
        return 2
    return probe(BleDeviceSource.from_environment(os.environ), args.seconds)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
