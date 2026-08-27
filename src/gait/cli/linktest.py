"""200 Hz 双设备链路压测。cli 的 `linktest`（RAY-199 的最小实现，服务 RAY-200）。

PRD §17.1 验证点 V2：≤5 m 近距下 30 min 缺失率 < 0.5%。本工具回答的正是这个
判据，并把「无持续性欠采」量化成一个可复核的数（见 `worst_window_loss`）——
但**不替 PRD 定阈值**：pass/fail 只由已明文的「平均缺失率 < 0.5%」决定。

一轮压测 = 扫描 → 双连接 → 电量（高速流开启**前**读）→ 固定时序配置下发并校验
→ Notify 回调第一动作落盘原始字节 → 持续采集 → 结束后降速、再读电量 → 逐秒
到达率统计 → 报告。统计不在这里重新发明：`gait.sync.integrity.assess` 是 PRD
§6.1「到达率逐秒监控 / 空洞切分」的唯一实现，压测与正式采集用同一把尺子。

## 采集只收 `t_host >= started` 的样本

设备从连接那一刻就开始产出样本（上一轮固化的速率），电量与配置阶段的残留
会带着更早的时刻混进统计 —— 配置期间设备只有 10 Hz，几十个残留样本足以让
`assess` 在开头看到成片的假空洞，把一条完美链路判成不达标。`t_host` 与本模块
的 `loop.time()` 同源（CPython 的事件循环时钟就是 `time.monotonic`），直接按
时刻过滤。

## 断连不重连

对压测而言断连是**结果**，不是要掩盖的故障：一轮里发生断连，这一轮就该作为
「不达标」的数据点被记录（PRD §6.1 的正式采集同样是断连即安全停止）。所以
`auto_reconnect=False`，断连时刻由看门狗记进报告。

## 主机时钟分辨率是这个实验的前提，必须先验

整个 V2 判据建立在**主机接收时刻**上：wt901 用 `time.monotonic()` 给每个样本
打 `t_host`，到达率、空洞、缺失率全是从这些时刻算出来的。如果主机的单调时钟
分辨率粗于采样周期，这些时刻就被量化成台阶，**测出来的丢包是时钟的假象，不是
链路的性质**。

这不是理论风险：`time.monotonic()` 在 **Windows + Python 3.12** 上走
`GetTickCount64()`，分辨率约 **15.6 ms** —— 而 200 Hz 的采样周期是 5 ms。
（CPython 3.13 才改用 `QueryPerformanceCounter`。）本项目的正式交付平台正是
Windows，所以这条必须在跑之前查，且查不过要进判定 —— 一份用 15.6 ms 时钟量
出来的「缺失率 18%」会把 go/no-go 引向完全错误的方向。

判据取 `分辨率 ≤ 采样周期 / 10`。

## 回放模式

`--replay a.jsonl b.jsonl` 用录制文件代替真机（wt901 的 ReplayTransport），
跳过配置与电量（回放不会应答寄存器读）。它验证的是采集通路的**接线**：
字节 → 帧 → 样本 → 到达时刻 → 报告文件。

**回放不验证时序指标。** 回放的到达时刻由事件循环的 `sleep` 精度决定，而不是
录制里记的时刻：非 1.0 倍速会直接压缩/拉伸时间轴，即使 1.0 倍速，在定时器
粒度粗的宿主上（同样是 Windows）也复现不出 20 ms 的节拍。所以报告对回放一律
标 `timing_valid: false`（1.0 倍速且时钟够细时才为 true），测量链路本身的
正确性由 `tests/test_linktest.py` 用**确定性到达时刻数组**验证。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import platform
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
from wt901 import (
    AlgorithmMode,
    Battery,
    BleTransport,
    DiscoveredDevice,
    ReturnRate,
    WT901Device,
    scan,
)
from wt901.protocol.frames import FrameDecoder, FrameFlag
from wt901.recording import read_recording
from wt901.transport.recording import RecordingTransport
from wt901.transport.replay import ReplayTransport

from gait.device.ble import (
    AppliedConfig,
    StreamConfig,
    configure_streaming,
    read_battery_at_low_rate,
)
from gait.device.recorder import ThreadedRecordingWriter
from gait.sync.integrity import IntegrityReport, assess, estimate_period

__all__ = ["BenchEnvironment", "DeviceRun", "main", "run_bench", "worst_window_loss"]

#: V2 判据：平均缺失率 < 0.5%。
LOSS_RATE_CRITERION = 0.005
#: 「无持续性欠采」看多长的窗口，秒。见 `worst_window_loss`。
#:
#: **阈值刻意不在代码里定。** PRD §17.1 只写了「无持续性欠采」，没有定量口径；
#: RAY-200 的实测分布已具备定它的条件，但那属 requirement 变更，不由工具发明。
#: 所以本模块只把「最差 30 s 窗缺失率」报出来，pass/fail 仅由「平均缺失率 <
#: 0.5%」这一条已明文的判据决定。
SUSTAINED_WINDOW_S = 30

#: 主机单调时钟分辨率必须细于采样周期的这个比例，测量才有意义（见模块 docstring）。
#: 取 10：量化误差 ≤ 半个周期的 1/5，不足以在残差上造出 3 样本（PRD 的空洞阈值）
#: 的台阶。Windows + Python 3.12 的 15.6 ms 在 200 Hz 下差了 31 倍，会被拦住。
CLOCK_RESOLUTION_RATIO = 10


#: 关闭一台设备最多等多久，秒。见 `_close_quietly`。
CLOSE_TIMEOUT = 10.0


async def _close_quietly(device: WT901Device, echo) -> None:
    """关闭一台设备，**永远不会挂住，也永远不抛**。

    必须带超时。真机实测两次：对一台**已经断连**（或连接失败）的 peripheral 调
    bleak 的 disconnect，CoreBluetooth 不会再回调，``await`` 就永远等下去。

    - RAY-200 round-2：卡在采集**完成之后、写报告之前**，30 分钟数据被扣在进程里。
    - RAY-200 round-3：卡在**连接失败的清理路径**里 —— 第二台连不上，清理第一台
      时挂住，整个重试循环停摆，一次采集都没开始。

    第一次只修了前者，没有把所有 ``close()`` 调用点一起收口，于是第二处又栽了
    一遍。清理路径的失败不该有资格拖住主流程 —— 这个函数就是那个收口。
    """
    try:
        await asyncio.wait_for(device.close(), timeout=CLOSE_TIMEOUT)
    except TimeoutError:
        echo(
            f"关闭 {device.device_id} 超时（{CLOSE_TIMEOUT:.0f}s）：设备可能已断连，"
            "蓝牙栈不再回调。继续。"
        )
    except Exception as error:  # noqa: BLE001 - 一台关不掉不能拦住其余的清理
        echo(f"关闭 {device.device_id} 时出错（忽略）：{error!r}")


def host_clock_resolution() -> float:
    """`t_host` 所用时钟的分辨率，秒。

    wt901 用 `time.monotonic()` 打 `t_host`（device.py），所以要查的是它，
    不是 `perf_counter` —— 两者在 Windows 上曾经是不同的实现。
    """
    return time.get_clock_info("monotonic").resolution

_RATE_BY_HZ = {
    10: ReturnRate.HZ_10,
    20: ReturnRate.HZ_20,
    50: ReturnRate.HZ_50,
    100: ReturnRate.HZ_100,
    200: ReturnRate.HZ_200,
}


@dataclass(frozen=True, slots=True)
class BenchEnvironment:
    """一轮压测的环境记录。判据要求 ≤5 m、建议一轮模拟人体遮挡 —— 这些条件

    不进数据就无法复核，所以是显式字段而不是自由文本。
    """

    label: str
    distance_m: float | None
    occlusion: str
    note: str

    def snapshot(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "distance_m": self.distance_m,
            "occlusion": self.occlusion,
            "note": self.note,
            "platform": platform.platform(),
            "host_bluetooth": "本机内置蓝牙（正式交付平台为 Windows + 指定适配器，"
            "本轮结论是第一个数据点而非最终结论）",
        }


@dataclass
class DeviceRun:
    """一台设备在一轮里的全部产出。"""

    device_id: str
    arrivals: list[float] = field(default_factory=list)
    battery_before: Battery | None = None
    battery_after: Battery | None = None
    applied_config: AppliedConfig | None = None
    disconnected_at: float | None = None
    stats: dict[str, int] = field(default_factory=dict)
    recording_path: str | None = None
    recording_error: str | None = None
    integrity: IntegrityReport | None = None
    worst_window_loss: float | None = None
    """最差 30 s 窗的缺失率。只报数，不参与判定 —— 阈值属 PRD §17.1。"""
    measured_fs: float | None = None
    """器件实发速率，由到达时刻估计。判据的分母，见 `_finalize`。"""
    arrival_array: np.ndarray | None = None

    @property
    def loss_rate(self) -> float | None:
        if self.integrity is None:
            return None
        lost = self.integrity.lost_samples
        total = self.integrity.received + lost
        return lost / total if total else 0.0

    def snapshot(self) -> dict[str, Any]:
        # 逐秒数组不进 JSON —— 同一份数据在 per_second_<n>.csv 里，重复一份
        # 只会把评审要读的报告撑到几千行。
        integrity = self.integrity.snapshot() if self.integrity else None
        if integrity is not None:
            integrity.pop("per_second_rate")
            integrity.pop("per_second_loss")
        return {
            "device_id": self.device_id,
            "samples": len(self.arrivals),
            "battery_before": _battery_snapshot(self.battery_before),
            "battery_after": _battery_snapshot(self.battery_after),
            "applied_config": (
                self.applied_config.snapshot() if self.applied_config else None
            ),
            "disconnected_at": self.disconnected_at,
            "measured_fs": self.measured_fs,
            "device_stats": self.stats,
            "recording": self.recording_path,
            "recording_error": self.recording_error,
            "loss_rate": self.loss_rate,
            "worst_window_loss": self.worst_window_loss,
            "integrity": integrity,
        }


def _battery_snapshot(battery: Battery | None) -> dict[str, Any] | None:
    if battery is None:
        return None
    return {"raw": battery.raw, "percent": battery.percent}


def worst_window_loss(
    per_second_loss: np.ndarray,
    measured_fs: float,
    *,
    window: int = SUSTAINED_WINDOW_S,
) -> float:
    """最差的一个 ``window`` 秒窗口里丢了多少（占该窗应收的比例）。

    这是 PRD §17.1「无持续性欠采」该看的量：**整轮均值会把一段集中的坏时期
    洗掉**，而这个数专门把它捞出来。实测四轮（RAY-200）：

    | 工况 | 整轮缺失 | 最差 30 s 窗 |
    | --- | --- | --- |
    | ≤1 m 桌面 | 0.000% / 0.002% | 0.00% / 0.07% |
    | 2 m 贴地 + 全程躯干遮挡 | 0.064% / 0.118% | 1.60% / 1.14% |
    | 3 m 贴地 | 3.45% / 2.27% | 23.8% / 25.3% |
    | 5 m 桌面 | 3.90% / 5.08% | 24.9% / 37.7% |

    数量级分开，且**不含抖动**。

    ## 为什么不建在逐秒到达率上（被替换掉的那版就是）

    上一版实现是「30 s 窗内逐秒**到达率**均值 < 0.99」。它建在
    `integrity.py` 明确警告过「不能用来分级」的量上，被真机两次证伪：

    1. **round-1（零丢包）**：534/1799 秒的逐秒率低于 0.99，而**一个样本都没丢**
       —— 那是 BLE 通知成簇造成的读数波动，基线本来就贴着 0.99 晃。
    2. **round-4**：报出 29 个「持续欠采窗口」，追下去实际是**一次 0.4 秒的瞬断**
       （单秒丢 82 个）被 30 秒窗口摊开的。区间中位数 1.0092，健康。

    改成中位数并不能修好它 —— 只是把误报换到另一台设备上（0→16）。问题不在
    均值还是中位，在于底下那个量本身含抖动。丢失不含抖动，所以建在丢失上。

    ## 阈值不在这里定

    本函数只返回数，**不判 pass/fail**：把它变成判据需要一个阈值，而那属于
    PRD §17.1 的措辞（「无持续性欠采」目前没有定量口径）。RAY-200 的实测分布
    已经具备定它的条件，但那是 requirement 变更，不由工具发明。
    """
    losses = np.asarray(per_second_loss, dtype=np.float64)
    if losses.size == 0 or measured_fs <= 0:
        return 0.0
    span = min(window, losses.size)
    summed = np.convolve(losses, np.ones(span), mode="valid")
    return float(summed.max() / (span * measured_fs))


async def _consume(device: WT901Device, run: DeviceRun, started: float) -> None:
    """把样本的 ``t_host`` 收进 ``run.arrivals``，直到被取消或流结束。

    没有逐样本超时：给 ``anext`` 套 ``wait_for`` 会在超时取消时终结整个
    async 生成器，之后静默停采（PEP 525 语义）—— 链路一次 >5 s 的停顿就会让
    剩下的半小时颗粒无收。停顿与断连由看门狗在外部观察，这里只管收。
    """
    async for sample in device.samples():
        if sample.t_host < started:
            continue  # 电量/配置阶段的残留样本，见模块 docstring。
        run.arrivals.append(sample.t_host)


async def _watch(
    pairs: list[tuple[WT901Device, DeviceRun]],
    started: float,
    stop_at: float,
    echo,
) -> None:
    """看门狗 + 进度：每 10 s 报一次近窗到达率，断连时刻记进 run。"""
    loop = asyncio.get_running_loop()
    last_counts = [0] * len(pairs)
    last_tick = started
    while loop.time() < stop_at:
        await asyncio.sleep(min(10.0, max(stop_at - loop.time(), 0.01)))
        now = loop.time()
        window = max(now - last_tick, 1e-9)
        last_tick = now
        parts = []
        for i, (device, run) in enumerate(pairs):
            if run.disconnected_at is None and not device.is_connected:
                run.disconnected_at = now
            count = len(run.arrivals)
            rate = (count - last_counts[i]) / window
            last_counts[i] = count
            state = f"{rate:.1f} Hz" if run.disconnected_at is None else "断连"
            parts.append(f"{run.device_id}: {state} ({count} 样本)")
        echo(f"[{now - started:6.0f}s] " + " | ".join(parts))


def _finalize(run: DeviceRun, nominal_fs: float) -> None:
    if len(run.arrivals) < 2:
        return
    arrival = np.asarray(run.arrivals, dtype=np.float64)
    if arrival[-1] - arrival[0] <= 0:
        # 全部样本共享同一个时刻：时间轴上没有跨度，到达率无从谈起。发生在
        # 全速回放，也发生在时钟分辨率粗到装不下整轮的场合。宁可留 None
        # 让判定报「样本不足」，也不要产出一份看着正常的数字。
        return
    run.arrival_array = arrival
    # 判据按**器件实发速率**算，不按标称：器件晶振比标称低约 1%（实测 197.8 Hz
    # vs 标称 200），光这一项就吃掉 0.5% 判据的全部预算，按标称算的话一条完美
    # 链路也永远不达标。见 `integrity.estimate_period` 的说明与其局限。
    run.measured_fs = 1.0 / estimate_period(arrival, nominal_fs)
    run.integrity = assess(arrival, run.measured_fs)
    run.worst_window_loss = worst_window_loss(
        run.integrity.per_second_loss, run.measured_fs
    )


def _verdict(
    runs: list[DeviceRun],
    *,
    timing_valid: bool,
    nominal_fs: float,
    clock_resolution: float | None,
) -> dict[str, Any]:
    """对照 V2 判据。任何一台设备不达标即整轮不达标。

    时钟分辨率不足时**先于**任何链路结论报出来：那种情况下缺失率量的是时钟，
    不是链路（见模块 docstring）。

    ``clock_resolution`` 为 ``None`` 表示**没有检出量化痕迹**（见
    `_observed_resolution`）—— 那本身就是时钟够细的证据，按合格处理。
    """
    problems: list[str] = []
    period = 1.0 / nominal_fs
    clock_adequate = (
        clock_resolution is None or clock_resolution * CLOCK_RESOLUTION_RATIO <= period
    )
    if not clock_adequate:
        assert clock_resolution is not None
        problems.append(
            f"主机单调时钟分辨率 {clock_resolution * 1e3:.3f} ms 粗于采样周期 "
            f"{period * 1e3:.1f} ms 的 1/{CLOCK_RESOLUTION_RATIO}，"
            "到达时刻被量化，本轮缺失率量的是时钟不是链路 —— 结论不可用"
        )
    for run in runs:
        if run.integrity is None:
            problems.append(f"{run.device_id}: 样本不足，无法评估")
            continue
        loss = run.loss_rate or 0.0
        if loss >= LOSS_RATE_CRITERION:
            problems.append(
                f"{run.device_id}: 缺失率 {loss:.3%} ≥ {LOSS_RATE_CRITERION:.1%}"
            )
        if run.disconnected_at is not None:
            problems.append(f"{run.device_id}: 采集中断连")
        if run.recording_error is not None:
            problems.append(
                f"{run.device_id}: 录制写盘失败（{run.recording_error}），"
                "原始字节不完整"
            )
        if run.stats.get("dropped_samples", 0):
            problems.append(
                f"{run.device_id}: 主机侧消费队列溢出 "
                f"{run.stats['dropped_samples']} 样本，测量自身不可信"
            )
    return {
        "pass": not problems,
        "problems": problems,
        "timing_valid": timing_valid and clock_adequate,
        "clock_adequate": clock_adequate,
    }


async def _scan_for(
    device_count: int, scan_timeout: float, attempts: int, echo
) -> list[DiscoveredDevice]:
    """扫到 ``device_count`` 台为止，最多试 ``attempts`` 次。

    真机实测（2026-08-24，两台 WT901BLE67 并排放在桌上、RSSI −32/−36）：单次
    扫描窗口经常**只看到其中一台，且每次是不同的那台** —— 模块 1 分钟未连接
    就进休眠，广播间隔变长，两台的广播窗口未必落在同一次扫描里。

    这不该由操作者用重跑来兜：一轮 30 分钟的实验，工位已经摆好，因为一次扫描
    抖动就退出去让人重来是把工具的缺陷转嫁给人。重试三四次即可。
    """
    found: list[DiscoveredDevice] = []
    for attempt in range(1, attempts + 1):
        found = await scan(scan_timeout)
        if len(found) >= device_count:
            return found
        echo(
            f"扫描到 {len(found)}/{device_count} 台"
            f"（第 {attempt}/{attempts} 次）：{[d.name for d in found]}"
        )
    return found


async def _connect_live(
    device_count: int,
    mac_filters: list[str] | None,
    scan_timeout: float,
    out_dir: Path,
    label: str,
    echo,
    scan_attempts: int = 4,
) -> list[tuple[WT901Device, DeviceRun, ThreadedRecordingWriter]]:
    found = await _scan_for(device_count, scan_timeout, scan_attempts, echo)
    if mac_filters:
        selected: list[DiscoveredDevice] = []
        for needle in mac_filters:
            matches = [
                d
                for d in found
                if needle.lower() in d.address.lower()
                and all(d.address != s.address for s in selected)
            ]
            if not matches:
                raise SystemExit(
                    f"扫描到 {len(found)} 台 WT 设备，无一（未被选过的）匹配 "
                    f"{needle!r}。已发现：{[d.address for d in found]}"
                )
            selected.append(matches[0])
    else:
        selected = found[:device_count]
    if len(selected) < device_count:
        raise SystemExit(
            f"需要 {device_count} 台设备，只扫描到 {len(selected)} 台："
            f"{[(d.name, d.address) for d in found]}。"
            "确认模块已按键开机（1 分钟未连接会休眠但仍广播）。"
        )

    connected: list[tuple[WT901Device, DeviceRun, ThreadedRecordingWriter]] = []
    for index, discovered in enumerate(selected):
        safe = discovered.address.replace(":", "-")
        recording_path = out_dir / f"raw_{index}_{safe}.jsonl"
        writer = ThreadedRecordingWriter(
            recording_path, device_id=discovered.address, note=label
        )
        device = WT901Device(RecordingTransport(BleTransport(discovered), writer))
        try:
            await device.open()
        except BaseException:
            # 本台没连上：把它和**已连上的前几台**都收干净再抛 —— 泄漏的 BLE
            # 连接会让下一次 connect 直接失败（wt901 ble.py 的警告）。
            writer.close()
            for opened, _, _ in connected:
                await _close_quietly(opened, echo)
            raise
        run = DeviceRun(device_id=device.device_id)
        run.recording_path = str(recording_path)
        connected.append((device, run, writer))
        echo(f"已连接 {discovered.name} {discovered.address}")
    return connected


async def run_bench(
    *,
    duration: float,
    out_dir: Path,
    env: BenchEnvironment,
    config: StreamConfig,
    nominal_fs: float,
    device_count: int = 2,
    mac_filters: list[str] | None = None,
    scan_timeout: float = 10.0,
    replay_files: list[Path] | None = None,
    replay_speed: float | None = 1.0,
    echo=print,
) -> dict[str, Any]:
    """跑一轮压测，返回并写出报告。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    loop = asyncio.get_running_loop()
    started_utc = datetime.now(UTC).isoformat()

    devices: list[WT901Device] = []
    runs: list[DeviceRun] = []
    writers: list[ThreadedRecordingWriter] = []
    replay_transports: list[ReplayTransport] = []

    try:
        if replay_files:
            for path in replay_files:
                transport = ReplayTransport.from_file(path, speed=replay_speed)
                replay_transports.append(transport)
                device = WT901Device(transport)
                await device.open()
                devices.append(device)
                runs.append(DeviceRun(device_id=device.device_id))
        else:
            for device, run, writer in await _connect_live(
                device_count, mac_filters, scan_timeout, out_dir, env.label, echo
            ):
                devices.append(device)
                runs.append(run)
                writers.append(writer)

            # 电量在高速流开启前读（PRD §6.1 自检顺序）；随后正式配置并校验。
            for device, run in zip(devices, runs):
                run.battery_before = await read_battery_at_low_rate(device)
                echo(
                    f"{run.device_id} 电量（前）："
                    f"{_battery_snapshot(run.battery_before)}"
                )
                run.applied_config = await configure_streaming(device, config)
                if not run.applied_config.verified:
                    raise SystemExit(
                        f"{run.device_id} 配置校验失败："
                        f"{run.applied_config.mismatches}。中止本轮。"
                    )
                echo(f"{run.device_id} 配置已下发并校验")

        started = loop.time()
        stop_at = started + duration
        consumers = [
            asyncio.ensure_future(_consume(device, run, started))
            for device, run in zip(devices, runs)
        ]
        watchdog = asyncio.ensure_future(
            _watch(list(zip(devices, runs)), started, stop_at, echo)
        )
        try:
            if replay_transports:
                # 回放喂完就结束，不必等满 duration；等消费者把队列排空再收，
                # 否则最后一批样本会被裁掉，全速回放下尤其明显。
                await asyncio.gather(
                    *(t.wait_exhausted() for t in replay_transports)
                )
                drain_deadline = loop.time() + 5.0
                while (
                    any(device.pending_samples for device in devices)
                    and loop.time() < drain_deadline
                ):
                    await asyncio.sleep(0.01)
                await asyncio.sleep(0.05)
            else:
                await asyncio.sleep(duration)
        finally:
            for task in consumers:
                task.cancel()
            await asyncio.gather(*consumers, return_exceptions=True)
            watchdog.cancel()

        if not replay_files:
            for device, run in zip(devices, runs):
                if run.disconnected_at is None and device.is_connected:
                    run.battery_after = await read_battery_at_low_rate(device)
                    echo(
                        f"{run.device_id} 电量（后）："
                        f"{_battery_snapshot(run.battery_after)}"
                    )
    finally:
        for device, run in zip(devices, runs):
            run.stats = asdict(device.stats)
            await _close_quietly(device, echo)
        for writer in writers:
            writer.close()  # 幂等；只兜 device.close 没走到的路径。

    for run, writer in zip(runs, writers):
        if writer.error is not None:
            run.recording_error = repr(writer.error)
    for run in runs:
        _finalize(run, nominal_fs)
    # 回放的到达时刻由 sleep 精度决定，不是录制里记的时刻 —— 一律不认时序指标。
    return _emit_report(
        runs=runs,
        out_dir=out_dir,
        env=env,
        nominal_fs=nominal_fs,
        duration=duration,
        started_utc=started_utc,
        # 回放的到达时刻由 sleep 精度决定，不是录制里记的时刻 —— 不认时序指标。
        timing_valid=not replay_files,
        source="replay" if replay_files else "live",
        sources=[str(p) for p in replay_files] if replay_files else None,
        replay_speed=(
            ("full" if replay_speed is None else replay_speed)
            if replay_files
            else None
        ),
        echo=echo,
    )


def _emit_report(
    *,
    runs: list[DeviceRun],
    out_dir: Path,
    env: BenchEnvironment,
    nominal_fs: float,
    duration: float,
    started_utc: str,
    timing_valid: bool,
    source: str,
    sources: list[str] | None,
    replay_speed: float | str | None,
    echo,
    extra: dict[str, Any] | None = None,
    clock_resolution: float | None = None,
    clock_source: str = "host",
) -> dict[str, Any]:
    """统计、判定、写出报告。现场采集与离线分析共用这一条出口。

    共用不是为了省代码，是为了让两条路径的报告**逐字段同构** —— 一份离线补算
    出来的报告若与现场报告长得不一样，评审时就无法直接比对。
    """
    for run in runs:
        _finalize(run, nominal_fs)
    # 现场采集查本机时钟；离线分析用从录制时刻量出的粒度（None = 未检出量化）。
    if clock_source == "host":
        clock_resolution = host_clock_resolution()
    verdict = _verdict(
        runs,
        timing_valid=timing_valid,
        nominal_fs=nominal_fs,
        clock_resolution=clock_resolution,
    )

    report = {
        "issue": "RAY-200",
        "criterion": {
            "loss_rate": LOSS_RATE_CRITERION,
            "sustained_window_s": SUSTAINED_WINDOW_S,
        },
        "started_utc": started_utc,
        "duration_requested_s": duration,
        "nominal_fs": nominal_fs,
        "host_clock": {
            "monotonic_resolution_s": clock_resolution,
            "sample_period_s": 1.0 / nominal_fs,
            "required_ratio": CLOCK_RESOLUTION_RATIO,
            "adequate": verdict["clock_adequate"],
            "source": clock_source,
        },
        "environment": env.snapshot(),
        "source": source,
        "replay": sources if source == "replay" else None,
        "analyzed": sources if source == "analysis" else None,
        "replay_speed": replay_speed,
        "devices": [run.snapshot() for run in runs],
        "verdict": verdict,
        **(extra or {}),
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    for index, run in enumerate(runs):
        if run.integrity is None:
            continue
        lines = ["second,arrival_rate,lost_samples"]
        for sec, (rate, loss) in enumerate(
            zip(run.integrity.per_second_rate, run.integrity.per_second_loss)
        ):
            lines.append(f"{sec},{rate:.4f},{int(loss)}")
        (out_dir / f"per_second_{index}.csv").write_text(
            "\n".join(lines) + "\n", encoding="utf-8"
        )
        np.savetxt(out_dir / f"arrival_{index}.csv", run.arrival_array, fmt="%.6f")
    (out_dir / "report.md").write_text(_markdown(report), encoding="utf-8")
    echo(f"报告已写入 {out_dir}")
    if source == "replay":
        echo("警告：回放模式，时序指标无效 —— 只证明采集通路接线正确。")
    if not verdict["clock_adequate"]:
        echo(
            f"警告：主机时钟分辨率 {clock_resolution * 1e3:.4g} ms 不足以测 "
            f"{nominal_fs:.0f} Hz，本轮到达率是时钟的假象，结论不可用。"
        )
    echo("判定：" + ("达标" if verdict["pass"] else f"不达标 {verdict['problems']}"))
    return report


#: 配置/电量自检阶段一定发生在录制的最前面这段时间内，s。见 `_streaming_span`。
SETUP_WINDOW_S = 60.0


def _observed_resolution(stamps: list[float]) -> float | None:
    """从录制的时刻里量出打点时钟的粒度，s。样本不足时返回 ``None``。

    ## 为什么离线分析不能查「当前这台机器」的时钟

    `host_clock_resolution()` 问的是**正在跑分析的这台机器**，而录制里的时刻是
    **当初采集那台机器**打的。拿一份 macOS 采集的录制到 Windows 上分析，会因为
    Windows 的 15.6 ms 时钟被误判作废；反过来，一份 Windows 采集的（真的被
    15.6 ms 量化过的）数据拿到 Mac 上分析，反而会被错误放行。相关的是采集时的
    时钟 —— 而它就写在数据里。

    ## 判法：查**量化**，不是查最小间隔

    分辨率为 `q` 的时钟，所有时刻都是 `q` 的整数倍，因此任意两个时刻之差也是
    `q` 的整数倍。所以判据是「全部间隔是否都落在某个公共步长的整数倍上」。

    一开始用的是「最小正间隔」，那是错的：它只在包**成簇**时才小。真机 macOS
    录制量到 0.066 ms 正是因为 BLE 会把两个通知挤进同一个连接事件；而一段没有
    成簇的录制，最小间隔就等于包距（几十 ms），会被误判成粗时钟。

    量化检测没有这个毛病：包距 30 ms 上下浮动的录制，间隔彼此不成整数倍关系；
    而被 15.625 ms 量化过的录制，每个间隔都是它的整数倍。

    返回 ``None`` 表示**测不出量化痕迹** —— 那说明时钟至少细到能分辨这些间隔的
    差异，没有证据表明它粗，调用方按「无异常」处理。要它给出确切分辨率是做不到
    的：细时钟本来就不留量化痕迹。
    """
    unique = np.unique(np.asarray(stamps, dtype=np.float64))
    if unique.size < 3:
        return None
    diffs = np.diff(unique)
    positive = diffs[diffs > 0]
    if positive.size < 2:
        return None
    step = float(positive.min())
    if step <= 0:
        return None
    ratios = positive / step
    if float(np.max(np.abs(ratios - np.round(ratios)))) < 0.01:
        return step
    return None


def _streaming_span(
    counts: list[int], stamps: list[float] | None = None
) -> tuple[int, int]:
    """录制里真正处于高速流的那一段（首尾包索引，闭区间）。

    一份录制不只有采集：开头有电量读与配置下发，结尾有电量复读，这些阶段设备
    还停在 10 Hz。现场路径靠 `started` 把它们挡在统计之外（`_consume` 过滤
    `t_host < started`），录制文件里却没有那个标记 —— 全吃进去的话，一段 10 Hz
    在 200 Hz 的尺子下就是 95% 的「丢包」。真机 round-2 的第一次补算正是这样：
    卡死期间设备以 10 Hz 挂了 11 分钟，缺失率被报成 37.8%。

    判据用**每包帧数**，因为它直接对应物理差别：200 Hz 打包传输每次通知带 8 帧，
    10 Hz 每次带 1 帧。取全片中位数的一半作阈值。

    **只裁首尾，绝不裁中间。** 中间的低速段是真实的链路劣化（正是本实验要测的
    东西），裁掉它等于把结论修饰成想要的样子。

    ## 开头为什么不能只裁「第一个流式包之前」

    设备把 200 Hz 固化在 flash 里，所以**一连上就在高速流** —— 录制的第一个包
    往往已经是 8 帧。真正的顺序是：高速流残留 → 降到 10 Hz 读电量 → 下发配置 →
    正式采集。只裁到「第一个流式包」会把前两段一起留下，那段 10 Hz 就成了开头
    的一个假空洞。实测（round-1 离线复算）：458 个丢失里 450 个挤在前 60 s，
    而现场报告在同一份数据上只丢 4 个。

    所以起点取「落在开头 `SETUP_WINDOW_S` 内的**最后一个**非流式包之后」。
    配置阶段必然在最前面的一分钟内完成，这个窗口之外的低速段一律保留。
    """
    if not counts:
        return 0, -1
    positive = [c for c in counts if c > 0]
    if not positive:
        return 0, len(counts) - 1
    threshold = max(2.0, float(np.median(positive)) / 2.0)
    streaming = [i for i, c in enumerate(counts) if c >= threshold]
    if not streaming:
        return 0, len(counts) - 1

    first, last = streaming[0], streaming[-1]
    if stamps:
        origin = stamps[0]
        for index, count in enumerate(counts):
            if index > last or stamps[index] - origin > SETUP_WINDOW_S:
                break
            if count < threshold:
                first = index + 1
    return min(first, last), last


def analyze_recordings(
    *,
    paths: list[Path],
    out_dir: Path,
    env: BenchEnvironment,
    nominal_fs: float,
    echo=print,
) -> dict[str, Any]:
    """从**录制文件里记的到达时刻**补算一份报告。

    这条路径存在的理由：`ThreadedRecordingWriter` 落盘时写的 ``t`` 就是 BLE
    回调发生的时刻（与 `ImuSample.t_host` 同源同钟），所以一轮采集即使中途被
    打断、没能走到写报告那一步，**字节和真实时序都还在盘上**。一轮 30 分钟的
    实验加上摆工位的时间，不该因为进程没活到最后就整轮作废。

    与 ``--replay`` 的关键区别：回放是把字节重新喂进设备层，`t_host` 在**回放
    时刻**重新打点，时序信息就此丢失（所以回放一律 ``timing_valid: false``）。
    这里直接采信录制里的 ``t``，因此时序指标**有效**。

    一段字节里的多帧共享该段的到达时刻 —— 现场采集也是如此（同一次通知里的
    样本在 `t_host` 上只差几微秒），不是近似。
    """
    runs: list[DeviceRun] = []
    trims: list[dict[str, float]] = []
    resolutions: list[float] = []
    for path in paths:
        recording = read_recording(path)
        decoder = FrameDecoder()
        counts: list[int] = []
        stamps: list[float] = []
        for chunk in recording.chunks:
            frames = [f for f in decoder.feed(chunk.data) if f.flag is FrameFlag.DATA]
            counts.append(len(frames))
            stamps.append(chunk.t)

        observed = _observed_resolution(stamps)
        if observed is not None:
            resolutions.append(observed)
        first, last = _streaming_span(counts, stamps)
        arrivals = [
            stamps[i] for i in range(first, last + 1) for _ in range(counts[i])
        ]
        trimmed = {
            "leading_s": round(stamps[first] - stamps[0], 3) if stamps else 0.0,
            "trailing_s": round(stamps[-1] - stamps[last], 3) if stamps else 0.0,
        }
        trims.append(trimmed)

        run = DeviceRun(device_id=recording.device_id)
        run.arrivals = arrivals
        run.recording_path = str(path)
        run.stats = {
            "frames": len(arrivals),
            "resync_count": decoder.resync_count,
            "dropped_bytes": decoder.dropped_bytes,
        }
        runs.append(run)
        echo(
            f"{recording.device_id}: {len(arrivals)} 样本，来自 {path.name}"
            f"（裁掉首 {trimmed['leading_s']:.1f}s / 末 {trimmed['trailing_s']:.1f}s 的非流式段）"
        )

    return _emit_report(
        runs=runs,
        out_dir=out_dir,
        env=env,
        nominal_fs=nominal_fs,
        duration=max(
            (run.arrivals[-1] - run.arrivals[0] for run in runs if len(run.arrivals) > 1),
            default=0.0,
        ),
        started_utc=datetime.now(UTC).isoformat(),
        timing_valid=True,  # 录制里的 t 是真实到达时刻
        source="analysis",
        sources=[str(p) for p in paths],
        replay_speed=None,
        extra={"trimmed": trims},
        # 时钟粒度取自**录制里的时刻**，不是正在跑分析的这台机器 —— 相关的是
        # 采集时的时钟。多份录制取最粗的那个（最保守）。
        clock_resolution=max(resolutions) if resolutions else None,
        clock_source="recording",
        echo=echo,
    )


def _clock_line(clock: dict[str, Any]) -> str:
    """报告里那行时钟说明。分辨率可能为 ``None`` —— 见 `_observed_resolution`。"""
    period_ms = clock["sample_period_s"] * 1e3
    where = "采集录制的宿主" if clock["source"] == "recording" else "本机"
    resolution = clock["monotonic_resolution_s"]
    if resolution is None:
        measured = "未检出量化痕迹（说明足够细）"
    else:
        measured = f"{resolution * 1e3:.4g} ms"
    suffix = "" if clock["adequate"] else " — ❌ 不足"
    return (
        f"- {where}时钟粒度：{measured}"
        f"（采样周期 {period_ms:.1f} ms，要求细于其 1/{clock['required_ratio']}）"
        f"{suffix}"
    )


def _markdown(report: dict[str, Any]) -> str:
    env = report["environment"]
    lines = [
        f"# RAY-200 链路压测报告 — {env['label']}",
        "",
        f"- 开始（UTC）：{report['started_utc']}",
        f"- 时长（请求）：{report['duration_requested_s']:.0f} s",
        f"- 标称速率：{report['nominal_fs']:.0f} Hz",
        (
            f"- 距离：{'未记录' if env['distance_m'] is None else f'{env['distance_m']} m'}；"
            f"遮挡：{env['occlusion']}"
        ),
        f"- 平台：{env['platform']}",
        f"- 链路：{env['host_bluetooth']}",
        _clock_line(report["host_clock"]),
    ]
    if env["note"]:
        lines.append(f"- 备注：{env['note']}")
    if report["replay"]:
        lines.append(
            f"- **回放模式**（无真机，{report['replay_speed']}×）：{report['replay']}"
        )
        lines.append(
            "- ⚠️ **时序指标无效**：回放的到达时刻由事件循环的 sleep 精度决定，"
            "不是录制里记的时刻。本报告只证明采集通路接线正确，不构成链路结论。"
        )
    if report.get("analyzed"):
        lines.append(f"- **离线补算**：{report['analyzed']}")
        lines.append(
            "- 时序指标**有效**：到达时刻取自录制文件里落盘时记下的 BLE 回调"
            "时刻（与现场 `t_host` 同源同钟），不是回放时重新打的点。"
        )
    if not report["verdict"]["clock_adequate"]:
        lines.append(
            "- ⚠️ **主机时钟分辨率不足**：到达时刻被量化，缺失率量的是时钟不是"
            "链路。本轮结论不可用（Windows + Python 3.12 的 `time.monotonic()` "
            "是 15.6 ms，需 Python ≥ 3.13 或换宿主）。"
        )
    lines += ["", "## 判据（PRD §17.1 V2）", ""]
    lines.append(
        f"pass/fail 只看**平均缺失率 < {report['criterion']['loss_rate']:.1%}**"
        f"（分母为器件实测速率）。「无持续性欠采」以"
        f"**最差 {report['criterion']['sustained_window_s']} s 窗缺失率**量化并列在下表，"
        "但 PRD §17.1 尚未给出定量口径，故不参与判定。"
    )
    lines += ["", "## 各设备", ""]
    lines.append(
        "| 设备 | 样本 | 时长 s | 缺失率 | 最差 30s 窗 | 最差秒丢失 | 空洞数 | "
        "resync | 队列溢出 | 电量前→后 |"
    )
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    for dev in report["devices"]:
        integ = dev["integrity"]
        if integ is None:
            lines.append(
                f"| {dev['device_id']} | {dev['samples']} | — | 样本不足 | | | | | | |"
            )
            continue
        loss = dev["loss_rate"]
        b0, b1 = dev["battery_before"], dev["battery_after"]
        battery = (
            f"{b0['percent'] if b0 else '—'}%→{b1['percent'] if b1 else '—'}%"
            if (b0 or b1)
            else "—"
        )
        lines.append(
            f"| {dev['device_id']} | {integ['received']} | {integ['duration']:.1f} "
            f"| {loss:.3%} | {dev['worst_window_loss']:.2%} "
            f"| {integ['worst_second_loss']} | {len(integ['gaps'])} "
            f"| {dev['device_stats'].get('resync_count', '—')} "
            f"| {dev['device_stats'].get('dropped_samples', '—')} | {battery} |"
        )
    verdict = report["verdict"]
    lines += ["", "## 判定", ""]
    lines.append("**达标**" if verdict["pass"] else "**不达标**")
    for problem in verdict["problems"]:
        lines.append(f"- {problem}")
    lines += [
        "",
        "逐秒到达率曲线见 `per_second_<n>.csv`；原始字节录制见 `raw_*.jsonl`（可回放）。",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    # 一轮跑 30 分钟，操作者靠逐 10 s 的到达率判断「还活着吗」。stdout 一旦是
    # 管道（`| tee round-1.log`，无人值守时的标准用法）Python 就转成块缓冲，
    # 进度整段憋在缓冲区里 —— 真机第一轮就是这样：设备连上了、数据在落盘，
    # 终端却一片安静，看起来和死机一模一样。
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(line_buffering=True)

    parser = argparse.ArgumentParser(
        prog="gait-linktest",
        description="200 Hz 双设备 BLE 链路压测（RAY-200 / PRD V2）",
    )
    parser.add_argument("--duration", type=float, default=1800.0, help="秒，默认 1800")
    parser.add_argument("--devices", type=int, default=2)
    parser.add_argument(
        "--mac", action="append", default=None, help="按地址子串选定设备，可重复"
    )
    parser.add_argument("--scan-timeout", type=float, default=10.0)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--label", default="round-1")
    parser.add_argument("--distance-m", type=float, default=None)
    parser.add_argument("--occlusion", default="无")
    parser.add_argument("--note", default="")
    parser.add_argument(
        "--rate", type=int, default=200, choices=sorted(_RATE_BY_HZ)
    )
    parser.add_argument(
        "--bandwidth",
        type=lambda s: int(s, 0),
        default=None,
        help="带宽寄存器编码，默认 0x03（42 Hz）",
    )
    parser.add_argument("--nine-axis", action="store_true")
    parser.add_argument(
        "--replay", type=Path, action="append", default=None, help="录制文件，可重复"
    )
    parser.add_argument(
        "--analyze",
        type=Path,
        action="append",
        default=None,
        help="从录制文件里记的到达时刻离线补算报告（可重复）。用于抢救被打断的"
        "一轮：字节与真实时序都还在盘上。与 --replay 不同，时序指标有效。",
    )
    parser.add_argument(
        "--replay-speed",
        default="1.0",
        help="回放速度倍率；full = 全速不等待（时序指标随之无效）",
    )
    args = parser.parse_args(argv)

    default_bandwidth = StreamConfig().bandwidth
    config = StreamConfig(
        rate=int(_RATE_BY_HZ[args.rate]),
        bandwidth=default_bandwidth if args.bandwidth is None else args.bandwidth,
        algorithm=AlgorithmMode.NINE_AXIS if args.nine_axis else AlgorithmMode.SIX_AXIS,
    )

    out_dir = args.out or Path(
        f"linktest-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{args.label}"
    )
    env = BenchEnvironment(
        label=args.label,
        distance_m=args.distance_m,
        occlusion=args.occlusion,
        note=args.note,
    )
    replay_speed: float | None
    replay_speed = None if args.replay_speed == "full" else float(args.replay_speed)

    if args.analyze:
        report = analyze_recordings(
            paths=args.analyze,
            out_dir=out_dir,
            env=env,
            nominal_fs=float(args.rate),
        )
        return 0 if report["verdict"]["pass"] else 1

    report = asyncio.run(
        run_bench(
            duration=args.duration,
            out_dir=out_dir,
            env=env,
            config=config,
            nominal_fs=float(args.rate),
            device_count=args.devices,
            mac_filters=args.mac,
            scan_timeout=args.scan_timeout,
            replay_files=args.replay,
            replay_speed=replay_speed,
        )
    )
    return 0 if report["verdict"]["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
