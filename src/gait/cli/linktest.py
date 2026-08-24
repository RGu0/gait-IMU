"""200 Hz 双设备链路压测。cli 的 `linktest`（RAY-199 的最小实现，服务 RAY-200）。

PRD §17.1 验证点 V2：≤5 m 近距下 30 min 缺失率 < 0.5%。本工具回答的正是这个
判据，并把「无持续性欠采」操作化为一个可复核的数（见 `sustained_undersampling`）。

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
    ALGORITHM_NINE_AXIS,
    ALGORITHM_SIX_AXIS,
    AppliedConfig,
    StreamConfig,
    configure_streaming,
    read_battery_at_low_rate,
)
from gait.device.recorder import ThreadedRecordingWriter
from gait.sync.integrity import IntegrityReport, assess

__all__ = ["BenchEnvironment", "DeviceRun", "main", "run_bench"]

#: V2 判据：平均缺失率 < 0.5%。
LOSS_RATE_CRITERION = 0.005
#: 「无持续性欠采」的操作化：任何 30 s 滑动窗内逐秒到达率均值 < 0.99 记为一个
#: 持续欠采窗口。0.99 取自实测抖动下限（integrity.py 记录无丢包时单秒最低读到
#: 0.94，但那是毛刺；30 s 均值仍低于 0.99 只能来自真实的速率不足）。与
#: `AlgoConfig.integrity_rate_warn`（0.98，逐秒分级用）不是同一个量 —— 那是单秒
#: 阈值，这是窗口均值阈值；两者都等 RAY-200 实测分布定稿后一起校准。
SUSTAINED_WINDOW_S = 30
SUSTAINED_RATE_FLOOR = 0.99

#: 主机单调时钟分辨率必须细于采样周期的这个比例，测量才有意义（见模块 docstring）。
#: 取 10：量化误差 ≤ 半个周期的 1/5，不足以在残差上造出 3 样本（PRD 的空洞阈值）
#: 的台阶。Windows + Python 3.12 的 15.6 ms 在 200 Hz 下差了 31 倍，会被拦住。
CLOCK_RESOLUTION_RATIO = 10


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
    sustained_windows: list[int] = field(default_factory=list)
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
            "device_stats": self.stats,
            "recording": self.recording_path,
            "recording_error": self.recording_error,
            "loss_rate": self.loss_rate,
            "sustained_undersampling_windows": self.sustained_windows,
            "integrity": integrity,
        }


def _battery_snapshot(battery: Battery | None) -> dict[str, Any] | None:
    if battery is None:
        return None
    return {"raw": battery.raw, "percent": battery.percent}


def sustained_undersampling(
    per_second_rate: np.ndarray,
    *,
    window: int = SUSTAINED_WINDOW_S,
    floor: float = SUSTAINED_RATE_FLOOR,
) -> list[int]:
    """逐秒到达率里均值低于 ``floor`` 的滑动窗口起始秒。

    整轮不足一个窗口时退化为整轮均值 —— 短采集（自测、回放）也要有判定，
    而不是静默返回「没有窗口所以没有欠采」。
    """
    rates = np.asarray(per_second_rate, dtype=np.float64)
    if rates.size == 0:
        return []
    if rates.size < window:
        return [0] if float(rates.mean()) < floor else []
    kernel = np.ones(window) / window
    means = np.convolve(rates, kernel, mode="valid")
    return [int(i) for i in np.flatnonzero(means < floor)]


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
    run.integrity = assess(arrival, nominal_fs)
    run.sustained_windows = sustained_undersampling(run.integrity.per_second_rate)


def _verdict(
    runs: list[DeviceRun],
    *,
    timing_valid: bool,
    nominal_fs: float,
    clock_resolution: float,
) -> dict[str, Any]:
    """对照 V2 判据。任何一台设备不达标即整轮不达标。

    时钟分辨率不足时**先于**任何链路结论报出来：那种情况下缺失率量的是时钟，
    不是链路（见模块 docstring）。
    """
    problems: list[str] = []
    period = 1.0 / nominal_fs
    clock_adequate = clock_resolution * CLOCK_RESOLUTION_RATIO <= period
    if not clock_adequate:
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
        if run.sustained_windows:
            problems.append(
                f"{run.device_id}: {len(run.sustained_windows)} 个持续欠采窗口"
                f"（首个起于第 {run.sustained_windows[0]} 秒）"
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
                try:
                    await opened.close()
                except Exception as cleanup_error:  # noqa: BLE001 - 清理路径
                    echo(f"清理 {opened.device_id} 时出错（忽略）：{cleanup_error!r}")
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
            try:
                await device.close()  # RecordingTransport.disconnect 会关 writer。
            except Exception as close_error:  # noqa: BLE001 - 一台关不掉不能拦住其余的清理
                echo(f"关闭 {device.device_id} 时出错（忽略）：{close_error!r}")
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
) -> dict[str, Any]:
    """统计、判定、写出报告。现场采集与离线分析共用这一条出口。

    共用不是为了省代码，是为了让两条路径的报告**逐字段同构** —— 一份离线补算
    出来的报告若与现场报告长得不一样，评审时就无法直接比对。
    """
    for run in runs:
        _finalize(run, nominal_fs)
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
            "sustained_rate_floor": SUSTAINED_RATE_FLOOR,
        },
        "started_utc": started_utc,
        "duration_requested_s": duration,
        "nominal_fs": nominal_fs,
        "host_clock": {
            "monotonic_resolution_s": clock_resolution,
            "sample_period_s": 1.0 / nominal_fs,
            "required_ratio": CLOCK_RESOLUTION_RATIO,
            "adequate": verdict["clock_adequate"],
        },
        "environment": env.snapshot(),
        "source": source,
        "replay": sources if source == "replay" else None,
        "analyzed": sources if source == "analysis" else None,
        "replay_speed": replay_speed,
        "devices": [run.snapshot() for run in runs],
        "verdict": verdict,
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
    for path in paths:
        recording = read_recording(path)
        decoder = FrameDecoder()
        arrivals: list[float] = []
        for chunk in recording.chunks:
            for frame in decoder.feed(chunk.data):
                if frame.flag is FrameFlag.DATA:
                    arrivals.append(chunk.t)
        run = DeviceRun(device_id=recording.device_id)
        run.arrivals = arrivals
        run.recording_path = str(path)
        run.stats = {
            "frames": len(arrivals),
            "resync_count": decoder.resync_count,
            "dropped_bytes": decoder.dropped_bytes,
        }
        runs.append(run)
        echo(f"{recording.device_id}: {len(arrivals)} 样本，来自 {path.name}")

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
        echo=echo,
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
        (
            f"- 主机单调时钟分辨率："
            f"{report['host_clock']['monotonic_resolution_s'] * 1e3:.4g} ms"
            f"（采样周期 {report['host_clock']['sample_period_s'] * 1e3:.1f} ms，"
            f"要求细于其 1/{report['host_clock']['required_ratio']}）"
            f"{'' if report['host_clock']['adequate'] else ' — ❌ 不足'}"
        ),
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
        f"平均缺失率 < {report['criterion']['loss_rate']:.1%}，且无持续性欠采"
        f"（任何 {report['criterion']['sustained_window_s']} s 窗内逐秒到达率"
        f"均值 ≥ {report['criterion']['sustained_rate_floor']:.0%}）。"
    )
    lines += ["", "## 各设备", ""]
    lines.append(
        "| 设备 | 样本 | 时长 s | 缺失率 | 最差秒丢失 | 空洞数 | 持续欠采窗 | "
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
            f"| {loss:.3%} | {integ['worst_second_loss']} | {len(integ['gaps'])} "
            f"| {len(dev['sustained_undersampling_windows'])} "
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
        algorithm=ALGORITHM_NINE_AXIS if args.nine_axis else ALGORITHM_SIX_AXIS,
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
