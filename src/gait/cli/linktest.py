"""200 Hz 双设备链路压测。cli 的 `linktest`（RAY-199 的最小实现，服务 RAY-200）。

PRD §17.1 验证点 V2：≤5 m 近距下 30 min 缺失率 < 0.5%。本工具回答的正是这个
判据，并把「无持续性欠采」操作化为一个可复核的数（见 `sustained_windows`）。

一轮压测 = 扫描 → 双连接 → 电量（高速流开启**前**读）→ 固定时序配置下发并校验
→ Notify 回调第一动作落盘原始字节 → 持续采集 → 结束后降速、再读电量 → 逐秒
到达率统计 → 报告。统计不在这里重新发明：`gait.sync.integrity.assess` 是 PRD
§6.1「到达率逐秒监控 / 空洞切分」的唯一实现，压测与正式采集用同一把尺子。

## 电量为什么在配置**之前**读，结束后为什么先降速

手册 §6：200 Hz 下寄存器读指令来不及回复。且设备把配置保存在 flash 里 ——
上一轮压测留下的 200 Hz 会让本轮一连上就是高速流。所以连接后第一件事是把
速率**临时**降到 10 Hz（不落 flash），把电量读出来，再走正式配置；结束时同样
先降速再读电量。

## 断连不重连

对压测而言断连是**结果**，不是要掩盖的故障：一轮里发生断连，这一轮就该作为
「不达标」的数据点被记录（PRD §6.1 的正式采集同样是断连即安全停止）。所以
`auto_reconnect=False`，断连时刻记进报告，该设备停止采集。

## 回放模式

`--replay a.jsonl b.jsonl` 用录制文件代替真机（wt901 的 ReplayTransport），
跳过配置与电量（回放不会应答寄存器读）。它验证的是采集与统计通路本身 ——
没有硬件时先证明工具是对的，真机压测才谈得上可信。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import platform
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
from wt901 import (
    Battery,
    DiscoveredDevice,
    ReturnRate,
    TransportTimeoutError,
    WT901Device,
    WT901Error,
    scan,
)
from wt901.protocol.registers import Register
from wt901.transport.ble import BleTransport
from wt901.transport.replay import ReplayTransport

from gait.device.ble import AppliedConfig, StreamConfig, configure_streaming
from gait.device.recorder import RecordingTransport, open_recording_writer
from gait.sync.integrity import IntegrityReport, assess

__all__ = ["BenchEnvironment", "DeviceRun", "main", "run_bench"]

#: V2 判据：平均缺失率 < 0.5%。
LOSS_RATE_CRITERION = 0.005
#: 「无持续性欠采」的操作化：任何 30 s 滑动窗内逐秒到达率均值 < 0.99 记为一个
#: 持续欠采窗口。0.99 取自实测抖动下限（integrity.py 记录无丢包时逐秒最低 0.94，
#: 但那是单秒毛刺；30 s 均值仍低于 0.99 只能来自真实的速率不足）。
SUSTAINED_WINDOW_S = 30
SUSTAINED_RATE_FLOOR = 0.99

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
    integrity: IntegrityReport | None = None
    sustained_windows: list[int] = field(default_factory=list)

    @property
    def loss_rate(self) -> float | None:
        if self.integrity is None:
            return None
        lost = self.integrity.lost_samples
        total = self.integrity.received + lost
        return lost / total if total else 0.0

    def snapshot(self) -> dict[str, Any]:
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
            "loss_rate": self.loss_rate,
            "sustained_undersampling_windows": self.sustained_windows,
            "integrity": self.integrity.snapshot() if self.integrity else None,
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


async def _read_battery_at_low_rate(device: WT901Device) -> Battery | None:
    """把速率临时降到 10 Hz 后读电量。读不到返回 ``None`` 而不是中止一轮。"""
    try:
        await device.registers.write(
            Register.RRATE, int(ReturnRate.HZ_10), persist=False, remember=False
        )
        return await device.telemetry.read_battery()
    except (TransportTimeoutError, WT901Error):
        return None


async def _consume(
    device: WT901Device, run: DeviceRun, stop_at: float, clock
) -> None:
    """把样本的 ``t_host`` 收进 ``run.arrivals``，直到时间到或断连。"""
    iterator = device.samples()
    while True:
        remaining = stop_at - clock()
        if remaining <= 0:
            return
        try:
            sample = await asyncio.wait_for(
                anext(iterator), timeout=min(remaining, 5.0)
            )
        except TimeoutError:
            if not device.is_connected:
                run.disconnected_at = clock()
                return
            continue
        except StopAsyncIteration:
            return
        run.arrivals.append(sample.t_host)


async def _progress(
    runs: list[DeviceRun], started: float, stop_at: float, clock, echo
) -> None:
    """每 10 s 报一次各设备近 10 s 的到达率。看的是「还活着吗」，不是终值。"""
    last_counts = [0] * len(runs)
    last_tick = started
    while clock() < stop_at:
        await asyncio.sleep(min(10.0, max(stop_at - clock(), 0.01)))
        now = clock()
        window = max(now - last_tick, 1e-9)
        last_tick = now
        parts = []
        for i, run in enumerate(runs):
            count = len(run.arrivals)
            rate = (count - last_counts[i]) / window
            last_counts[i] = count
            state = f"{rate:.1f} Hz" if run.disconnected_at is None else "断连"
            parts.append(f"{run.device_id}: {state} ({count} 样本)")
        echo(f"[{now - started:6.0f}s] " + " | ".join(parts))


def _finalize(run: DeviceRun, nominal_fs: float) -> None:
    if len(run.arrivals) >= 2:
        arrival = np.asarray(run.arrivals, dtype=np.float64)
        run.integrity = assess(arrival, nominal_fs)
        run.sustained_windows = sustained_undersampling(
            run.integrity.per_second_rate
        )


def _verdict(runs: list[DeviceRun]) -> dict[str, Any]:
    """对照 V2 判据。任何一台设备不达标即整轮不达标。"""
    problems: list[str] = []
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
        if run.stats.get("dropped_samples", 0):
            problems.append(
                f"{run.device_id}: 主机侧消费队列溢出 "
                f"{run.stats['dropped_samples']} 样本，测量自身不可信"
            )
    return {"pass": not problems, "problems": problems}


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
    clock = loop.time
    started_utc = datetime.now(UTC).isoformat()

    devices: list[WT901Device] = []
    runs: list[DeviceRun] = []
    writers = []
    replay_transports: list[ReplayTransport] = []

    if replay_files:
        for path in replay_files:
            transport = ReplayTransport.from_file(path, speed=replay_speed)
            replay_transports.append(transport)
            device = WT901Device(transport)
            await device.open()
            devices.append(device)
            runs.append(DeviceRun(device_id=device.device_id))
    else:
        found = await scan(scan_timeout)
        if mac_filters:
            selected: list[DiscoveredDevice] = []
            for needle in mac_filters:
                matches = [
                    d for d in found if needle.lower() in d.address.lower()
                ]
                if not matches:
                    raise SystemExit(
                        f"扫描到 {len(found)} 台 WT 设备，无一匹配 {needle!r}。"
                        f"已发现：{[d.address for d in found]}"
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
        for index, discovered in enumerate(selected):
            safe = discovered.address.replace(":", "-")
            recording_path = out_dir / f"raw_{index}_{safe}.jsonl"
            writer = open_recording_writer(
                recording_path, device_id=discovered.address, note=env.label
            )
            writers.append(writer)
            transport = RecordingTransport(
                BleTransport(discovered), writer
            )
            device = WT901Device(transport)
            await device.open()
            devices.append(device)
            run = DeviceRun(device_id=device.device_id)
            run.recording_path = str(recording_path)
            runs.append(run)
            echo(f"已连接 {discovered.name} {discovered.address}")

    try:
        if not replay_files:
            # 电量在高速流开启前读（PRD §6.1 自检顺序）；随后正式配置并校验。
            for device, run in zip(devices, runs):
                run.battery_before = await _read_battery_at_low_rate(device)
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

        started = clock()
        stop_at = started + duration
        consumers = [
            asyncio.ensure_future(_consume(device, run, stop_at, clock))
            for device, run in zip(devices, runs)
        ]
        reporter = asyncio.ensure_future(
            _progress(runs, started, stop_at, clock, echo)
        )
        if replay_transports:
            # 回放喂完就结束，不必等满 duration；但要等消费者把队列排空，
            # 否则最后一批样本会被裁掉，全速回放下尤其明显。
            await asyncio.gather(
                *(transport.wait_exhausted() for transport in replay_transports)
            )
            drain_deadline = clock() + 5.0
            while (
                any(device.pending_samples for device in devices)
                and clock() < drain_deadline
            ):
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.05)
            for task in consumers:
                task.cancel()
        await asyncio.gather(*consumers, return_exceptions=True)
        reporter.cancel()

        if not replay_files:
            for device, run in zip(devices, runs):
                if run.disconnected_at is None:
                    run.battery_after = await _read_battery_at_low_rate(device)
                    echo(
                        f"{run.device_id} 电量（后）："
                        f"{_battery_snapshot(run.battery_after)}"
                    )
    finally:
        for device, run in zip(devices, runs):
            stats = device.stats
            run.stats = {
                "frames": stats.frames,
                "samples": stats.samples,
                "dropped_samples": stats.dropped_samples,
                "resync_count": stats.resync_count,
                "dropped_bytes": stats.dropped_bytes,
                "reconnects": stats.reconnects,
            }
            await device.close()
        for writer in writers:
            writer.close()

    for run in runs:
        _finalize(run, nominal_fs)
    verdict = _verdict(runs)

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
        "environment": env.snapshot(),
        "replay": [str(p) for p in replay_files] if replay_files else None,
        "devices": [run.snapshot() for run in runs],
        "verdict": verdict,
    }
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
        np.savetxt(
            out_dir / f"arrival_{index}.csv",
            np.asarray(run.arrivals, dtype=np.float64),
            fmt="%.6f",
        )
    (out_dir / "report.md").write_text(_markdown(report), encoding="utf-8")
    echo(f"报告已写入 {out_dir}")
    echo("判定：" + ("达标" if verdict["pass"] else f"不达标 {verdict['problems']}"))
    return report


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
    ]
    if env["note"]:
        lines.append(f"- 备注：{env['note']}")
    if report["replay"]:
        lines.append(f"- **回放模式**（无真机）：{report['replay']}")
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
            lines.append(f"| {dev['device_id']} | {dev['samples']} | — | 样本不足 | | | | | | |")
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
        "--replay-speed",
        default="1.0",
        help="回放速度倍率；full = 全速不等待",
    )
    args = parser.parse_args(argv)

    config_kwargs: dict[str, int] = {"rate": int(_RATE_BY_HZ[args.rate])}
    if args.bandwidth is not None:
        config_kwargs["bandwidth"] = args.bandwidth
    if args.nine_axis:
        from gait.device.ble import ALGORITHM_NINE_AXIS

        config_kwargs["algorithm"] = ALGORITHM_NINE_AXIS
    config = StreamConfig(**config_kwargs)

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
