"""V3′ 实验工装。cli 的 `v3prime`（RAY-213，工程模式）。

**不进机构产品流程。** 它把《06 测试与验证方案》v1.1 §5 的 V3′ 流程做成一条
命令：采一趟（对碰段 + 步行段）→ 锚点真值 Δ → 指标偏差 → 按 R1 判据给结论。

    # 现场：两台模块连本机，先对碰后步行
    python -m gait.cli.v3prime live --subject S1 --trial 1 --out out/

    # 复算：从本工具落下的一趟重新分析（不需要硬件）
    python -m gait.cli.v3prime replay --trial-dir out/S1-1

    # 汇总多趟，出三选一结论
    python -m gait.cli.v3prime verdict --trials out/S1-1 out/S1-2 out/S2-1

## 为什么必须在线采，而不能事后拿两份录制文件分析

Δ 的**绝对值**是本实验的被测量，而它只在两条时间轴共钟时才存在。wt901 的录制
文件把 `t` 各自归零到自己的第一段字节，两份文件的零点差是未知常数；离线分析只能
靠对碰序列自身粗对齐，那会把 Δ 的绝对值一并吃掉（`sync.anchor.coarse_alignment`
的文档写明了这个代价，`validate.v3prime.evaluate_trial` 会直接拒绝这种报告）。

在线采集没有这个问题：两台设备的通知回调跑在**同一个进程的同一个事件循环**上，
`ImuSample.t_host` 取自同一个 `time.monotonic()`，天然共钟。所以本工具的 `live`
在内存里直接把两侧样本喂给 `measure_offsets`，`coarse_align` 保持 False。

落盘的原始字节仍然照录（`ThreadedRecordingWriter`），但它们的用途是**复核信号**，
不是复算 Δ —— `replay` 复算时用的是同时落下的 `arrivals.npz`，那里存的是采集当时
的共钟时刻。

## 一趟的结构

对碰段与步行段在同一次连接里连着采，中间不断开：Δ 是恒定量，但"同一次连接"这个
前提不能省 —— 重连会让 BLE 协商出新的连接参数，固有延迟随之改变，那时对碰段量到
的 Δ 就不再是步行段的 Δ。工具因此不提供"分两次采"的入口。

## 时钟分辨率前置检查

与 `linktest` 同一个理由，且在这里更要命：本实验要分辨的是毫秒级的 Δ，而
`time.monotonic()` 在 Windows + Python 3.12 上是 15.6 ms 的台阶。分辨率不足时
测出来的 Δ 是时钟的量化噪声，不是链路的性质 —— 所以开采前先验，不合格直接拒绝
开跑，而不是采完再在报告里加一行小字。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
from wt901 import BleTransport, DiscoveredDevice, WT901Device, scan
from wt901.transport.recording import RecordingTransport

from gait.analysis.events import segment_cycles
from gait.config import AlgoConfig
from gait.contracts import FootLabel
from gait.core.zupt import detect_stance
from gait.device.ble import StreamConfig, configure_streaming
from gait.device.recorder import ThreadedRecordingWriter
from gait.sync.anchor import FootSignal, measure_offsets
from gait.sync.selfcheck import check as selfcheck
from gait.sync.selfcheck import stance_spans
from gait.sync.timebase import build_timebase
from gait.validate.v3prime import (
    NEGLIGIBLE_MEDIAN_S,
    NEGLIGIBLE_P90_S,
    V3PrimeError,
    Verdict,
    evaluate_trial,
)

__all__ = ["analyze_trial", "main"]

#: 主机单调时钟分辨率必须细于采样周期的这个比例。同 `linktest.CLOCK_RESOLUTION_RATIO`。
CLOCK_RESOLUTION_RATIO = 10

#: 一趟里落下的文件名。集中在这里，`live` 与 `replay` 不会各写各的。
ARRIVALS_FILENAME = "arrivals.npz"
TRIAL_FILENAME = "trial.json"


class HarnessError(RuntimeError):
    """采集流程的前提不成立。"""


@dataclass
class FootCapture:
    """一只脚在一趟里的采集结果。全部时刻同源于本进程的 `time.monotonic()`。"""

    foot: str
    device_id: str
    arrival: list[float]
    accel: list[tuple[float, float, float]]
    gyro: list[tuple[float, float, float]]

    def arrays(self) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        return (
            np.asarray(self.arrival, dtype=np.float64),
            np.asarray(self.accel, dtype=np.float64),
            np.asarray(self.gyro, dtype=np.float64),
        )


def host_clock_resolution(samples: int = 200) -> float:
    """实测 `time.monotonic()` 的分辨率，s。取连续不同读数之间的最小差。"""
    deltas: list[float] = []
    previous = time.monotonic()
    for _ in range(samples):
        current = time.monotonic()
        if current != previous:
            deltas.append(current - previous)
            previous = current
    return min(deltas) if deltas else 0.0


def require_adequate_clock(nominal_fs: float, echo=print) -> float:
    """时钟不够细就拒绝开跑。理由见模块文档。"""
    resolution = host_clock_resolution()
    period = 1.0 / nominal_fs
    limit = period / CLOCK_RESOLUTION_RATIO
    echo(
        f"主机时钟分辨率实测 {resolution * 1e3:.4g} ms"
        f"（采样周期 {period * 1e3:.1f} ms，要求细于其 1/{CLOCK_RESOLUTION_RATIO}）"
    )
    if resolution > limit:
        raise HarnessError(
            f"主机单调时钟分辨率 {resolution * 1e3:.3g} ms 粗于判据 {limit * 1e3:.3g} ms。"
            "本实验要分辨毫秒级的跨足偏差，用这样的时钟测出来的是时钟的量化台阶，"
            "不是链路的性质。Windows 上需要 Python ≥ 3.13（3.12 走 GetTickCount64，"
            "约 15.6 ms）。"
        )
    return resolution


def _foot_signal(capture: FootCapture) -> FootSignal:
    """采集结果 → 锚点检测的输入。模值对模块姿态不变，见 `sync.anchor` 模块文档。"""
    arrival, accel, _ = capture.arrays()
    return FootSignal(magnitude=np.linalg.norm(accel, axis=1), arrival=arrival)


def _cycles(capture: FootCapture, foot: FootLabel, nominal_fs: float, cfg: AlgoConfig):
    """一只脚的步态周期。

    `position=None`：双支撑期与步时对称性只依赖事件**时刻**，不依赖轨迹，所以这里
    不跑惯导。代价是 `GaitCycle.stride_length` / `gait_speed` 为 nan —— 本实验不看
    它们（它们是足内量，对跨足偏差本来就免疫）。

    时间轴用 `build_timebase` 的输出而不是原始到达时刻：实测采样率与标称差几百
    ppm，几十秒累积下来就是几十毫秒，与被测效应同量级。
    """
    arrival, accel, gyro = capture.arrays()
    timebase = build_timebase(arrival, nominal_fs, cfg)
    stance = detect_stance(accel, gyro, timebase.report.fs, cfg)
    # `FootLabel` 是 Literal["L", "R"]，不是可实例化的类型 —— 直接传字面量。
    cycles, _edges = segment_cycles(
        foot, timebase.t, accel, gyro, stance.stances, position=None, cfg=cfg
    )
    return cycles, timebase, stance


def analyze_trial(
    label: str,
    left: FootCapture,
    right: FootCapture,
    nominal_fs: float,
    cfg: AlgoConfig | None = None,
) -> dict[str, Any]:
    """一趟的完整评估。`live` 与 `replay` 共用这一条路径。

    分成两问：**Δ 是多少**（锚点，对碰段）与**它把指标带偏多少**（步行段）。
    第二问在事件太少时会失败（受试者没走够），那时仍然返回第一问的结果 ——
    Δ 本身就是 V3′ 最主要的产出，不该因为步行段没采好而整趟作废。
    """
    cfg = cfg or AlgoConfig()
    anchor = measure_offsets(
        _foot_signal(left), _foot_signal(right), nominal_fs, cfg, coarse_align=False
    )
    payload: dict[str, Any] = {
        "label": label,
        "nominal_fs": nominal_fs,
        "anchor": anchor.snapshot(),
        "left_device": left.device_id,
        "right_device": right.device_id,
    }

    try:
        left_cycles, left_tb, left_stance = _cycles(left, "L", nominal_fs, cfg)
        right_cycles, right_tb, right_stance = _cycles(right, "R", nominal_fs, cfg)
        quality = selfcheck(
            stance_spans(left_tb.t, left_stance.stances),
            stance_spans(right_tb.t, right_stance.stances),
            cfg,
        )
        trial = evaluate_trial(
            label,
            anchor,
            left_cycles,
            right_cycles,
            sync_quality=quality.snapshot(),
            delta_selfcheck=quality.offset_estimate,
        )
        payload["trial"] = trial.snapshot()
        payload["sync_quality"] = quality.snapshot()
    except (V3PrimeError, ValueError) as error:
        # 步行段没采好不作废整趟 —— 记下原因，Δ 照样交付。
        payload["trial"] = None
        payload["metrics_error"] = str(error)
    return payload


async def _connect(count: int, mac_filters: list[str] | None, timeout: float, echo):
    """扫描并选出 `count` 台设备，**返回顺序即左右足顺序**（第一台为左足）。

    这个顺序是有后果的：Δ 定义为「左 − 右」，`MetricBias.bias` 的符号、报告里
    每一处「偏差把读数推向哪一边」都建立在它上面。给了 `--mac` 时顺序由参数定；
    没给时只能取扫描顺序，而扫描顺序取决于广播时机，**同两台设备两次跑可能相反**。

    所以没给 `--mac` 时不静静地用扫描顺序：把选中的地址回显出来，并在报告里标
    `foot_assignment: "scan_order"`。|Δ| 判据不受影响（取绝对值），但偏差方向会
    整体反号，而那正是三选一决策要读的东西。

    重扫 4 次：模块广播有间隔，一次扫不全是常态，不是故障。
    """
    found: list[DiscoveredDevice] = []
    for attempt in range(4):
        found = await scan(timeout=timeout)
        if len(found) >= count:
            break
        echo(f"扫描到 {len(found)} 台，重试（{attempt + 1}/4）")
    if mac_filters:
        selected: list[DiscoveredDevice] = []
        for needle in mac_filters:
            matches = [
                d for d in found
                if needle.lower() in d.address.lower()
                and all(d.address != s.address for s in selected)
            ]
            if not matches:
                raise HarnessError(
                    f"未扫描到匹配 {needle!r} 的设备。已发现：{[d.address for d in found]}"
                )
            selected.append(matches[0])
    else:
        selected = found[:count]
        echo(
            "⚠️ 未指定 --mac：左右足按扫描顺序定 —— "
            f"左足={selected[0].address if selected else 'n/a'}、"
            f"右足={selected[1].address if len(selected) > 1 else 'n/a'}。"
            "与实际佩戴不符会让 Δ 与所有偏差整体反号（|Δ| 判据取绝对值，不受影响）。"
            "请核对，或用 --mac 显式指定。"
        )
    if len(selected) < count:
        raise HarnessError(
            f"需要 {count} 台设备，只扫描到 {len(selected)} 台。确认模块已按键开机。"
        )
    return selected


async def _run_live(
    *,
    label: str,
    out_dir: Path,
    nominal_fs: float,
    taps: int,
    tap_seconds: float,
    walk_seconds: float,
    mac_filters: list[str] | None,
    scan_timeout: float,
    cfg: AlgoConfig,
    echo=print,
) -> dict[str, Any]:
    """采一趟：连接 → 对碰段 → 步行段 → 分析。

    两段之间**不断开连接**：重连会重新协商连接参数，固有链路延迟随之改变，那时
    对碰段量到的 Δ 就不是步行段的 Δ 了（模块文档"一趟的结构"）。
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    selected = await _connect(2, mac_filters, scan_timeout, echo)

    devices: list[WT901Device] = []
    writers: list[ThreadedRecordingWriter] = []
    captures: list[FootCapture] = []
    try:
        for index, discovered in enumerate(selected):
            foot = "L" if index == 0 else "R"
            safe = discovered.address.replace(":", "-")
            writer = ThreadedRecordingWriter(
                out_dir / f"raw_{foot}_{safe}.jsonl", device_id=discovered.address, note=label
            )
            device = WT901Device(RecordingTransport(BleTransport(discovered), writer))
            try:
                await device.open()
            except BaseException:
                # 本台没连上：把已连上的都收干净再抛 —— 泄漏的 BLE 连接会让下一次
                # connect 直接失败。
                writer.close()
                for opened in devices:
                    await _close_quietly(opened, echo)
                raise
            writers.append(writer)
            devices.append(device)
            captures.append(
                FootCapture(foot=foot, device_id=device.device_id, arrival=[], accel=[], gyro=[])
            )
            echo(f"{foot} 足已连接：{discovered.name} {discovered.address}")

        for device, capture in zip(devices, captures, strict=True):
            applied = await configure_streaming(device, StreamConfig(rate_hz=nominal_fs))
            if not applied.verified:
                raise HarnessError(
                    f"{capture.device_id} 配置校验失败：{applied.mismatches}。中止本趟。"
                )
        echo(f"两台均已配置 {nominal_fs:.0f} Hz")

        consumers = [
            asyncio.ensure_future(_consume(device, capture))
            for device, capture in zip(devices, captures, strict=True)
        ]
        try:
            echo("")
            echo(f"▶ 对碰段（{tap_seconds:.0f} s）：两模块外壳干脆对碰 {taps} 次，")
            echo("  间隔至少半秒、不要打成节拍器，力道以不削顶为宜。")
            await _countdown(tap_seconds, echo)
            echo("")
            echo(f"▶ 步行段（{walk_seconds:.0f} s）：按 T-01 定时步行（4 m 往返）。")
            await _countdown(walk_seconds, echo)
        finally:
            for task in consumers:
                task.cancel()
            await asyncio.gather(*consumers, return_exceptions=True)
    finally:
        for device in devices:
            await _close_quietly(device, echo)
        for writer in writers:
            writer.close()

    left, right = captures
    echo("")
    echo(f"采集完成：L {len(left.arrival)} 样本 / R {len(right.arrival)} 样本")
    # 采集元数据与样本存在一起：`trial.json` 是派生物，`replay` 会整份重写它，
    # 而"左右足是怎么定的""什么时候采的"这两件事一旦只活在 json 里，复算一次
    # 就没了 —— 偏差方向可不可信正是靠前者判断。
    np.savez(
        out_dir / ARRIVALS_FILENAME,
        **_capture_arrays(left, "left"),
        **_capture_arrays(right, "right"),
        label=np.asarray(label),
        nominal_fs=np.asarray(nominal_fs),
        left_device=np.asarray(left.device_id),
        right_device=np.asarray(right.device_id),
        foot_assignment=np.asarray("explicit_mac" if mac_filters else "scan_order"),
        captured_utc=np.asarray(datetime.now(UTC).isoformat()),
    )
    # 走与 `replay` 完全相同的读取路径，好让两条路的产出逐字段一致 —— 现场看到的
    # 报告与事后复算出来的报告不一致，是最难查的那种不一致。
    return load_trial_dir(out_dir, cfg)


def _capture_arrays(capture: FootCapture, prefix: str) -> dict[str, np.ndarray]:
    arrival, accel, gyro = capture.arrays()
    return {
        f"{prefix}_arrival": arrival,
        f"{prefix}_accel": accel,
        f"{prefix}_gyro": gyro,
    }


async def _consume(device: WT901Device, capture: FootCapture) -> None:
    """把样本收进内存。`t_host` 由 wt901 在通知回调里取，两台同源同钟。"""
    async for sample in device.stream():
        capture.arrival.append(sample.t_host)
        capture.accel.append((sample.accel.x, sample.accel.y, sample.accel.z))
        capture.gyro.append((sample.gyro.x, sample.gyro.y, sample.gyro.z))


async def _countdown(seconds: float, echo) -> None:
    """按秒回报剩余时间。现场需要知道还剩多久，尤其对碰段。"""
    remaining = seconds
    while remaining > 0:
        step = min(5.0, remaining)
        await asyncio.sleep(step)
        remaining -= step
        if remaining > 0:
            echo(f"  剩余 {remaining:.0f} s")


async def _close_quietly(device: WT901Device, echo) -> None:
    try:
        await asyncio.wait_for(device.close(), timeout=10.0)
    except BaseException as error:  # noqa: BLE001 - 关闭失败不该盖住主因
        echo(f"关闭 {device.device_id} 时出错（忽略）：{error!r}")


def load_trial_dir(path: Path, cfg: AlgoConfig | None = None) -> dict[str, Any]:
    """从一趟的 `arrivals.npz` 复算。不需要硬件。

    复算读的是采集当时的**共钟**到达时刻，不是录制文件里各自归零的 `t` ——
    后者算不出 Δ 的绝对值（模块文档"为什么必须在线采"）。
    """
    data = np.load(path / ARRIVALS_FILENAME, allow_pickle=False)
    captures = []
    for prefix, foot, device_key in (("left", "L", "left_device"), ("right", "R", "right_device")):
        captures.append(
            FootCapture(
                foot=foot,
                device_id=str(data[device_key]),
                arrival=data[f"{prefix}_arrival"].tolist(),
                accel=[tuple(row) for row in data[f"{prefix}_accel"]],
                gyro=[tuple(row) for row in data[f"{prefix}_gyro"]],
            )
        )
    payload = analyze_trial(
        str(data["label"]), captures[0], captures[1], float(data["nominal_fs"]), cfg
    )
    for key in ("foot_assignment", "captured_utc"):
        # 缺席不补默认值：一份没记录左右足来源的旧数据，与一份记着 "scan_order"
        # 的数据不是一回事，前者应当显示为"未记录"而不是被猜成后者。
        payload[key] = str(data[key]) if key in data.files else None
    return payload


def _echo_trial(payload: dict[str, Any], echo=print) -> None:
    offset = payload["anchor"]["offset"]
    echo("")
    echo(f"== 趟次 {payload['label']} ==")
    assignment = payload.get("foot_assignment")
    if assignment == "scan_order":
        echo(
            "⚠️ 左右足按扫描顺序定（未用 --mac）：若与实际佩戴相反，"
            "Δ 与所有偏差整体反号。|Δ| 判据不受影响。"
        )
    elif assignment is None:
        echo("左右足来源未记录（早于本字段的数据）：偏差方向请自行核对。")
    echo(f"对碰 {offset['count']} 次，其中降级（削顶/未插值）{offset['degraded_pairs']} 次")
    if offset["count"]:
        echo(
            f"跨足偏差 Δ（左−右，主机时基）：中位 {_ms(offset['median_s'])}"
            f" | |Δ| 90 分位 {_ms(offset['p90_abs_s'])}"
            f" | |Δ| 最大 {_ms(offset['max_abs_s'])}"
            f" | 漂移 {_ms(offset['drift_s_per_min'])}/min"
        )
    trial = payload.get("trial")
    if trial is None:
        echo(f"指标偏差未产出：{payload.get('metrics_error', '未知原因')}")
        return
    cross = trial["cross_check_s"]
    echo(
        "与 RAY-263 配对双支撑差分法互证："
        + (
            f"差分法 Δ={_ms(trial['delta_selfcheck_s'])}，两法之差 {_ms(cross)}"
            if cross is not None
            else "差分法判定 offset 不可估（相位在漂），本趟不构成印证"
        )
    )
    for metric in trial["metrics"]:
        line = (
            f"  {metric['name']}：主机时基 {_num(metric['host'])}"
            f" / 真值校正后 {_num(metric['corrected'])}"
            f" → 偏差 {_num(metric['bias'], sign=True)}"
        )
        echo(line)
        if not metric.get("comparable", True):
            # 不可比不是"没算出来"，是"两次读数不是同一个量"。原因必须跟着走，
            # 否则读报告的人只会看到一个 n/a 然后自己编一个解释。
            echo(f"    ↑ 不可比：{metric.get('note', '未说明')}")


def _num(value: float | None, *, sign: bool = False) -> str:
    """报告里的数值。None（快照里的非有限值）显示为 n/a，不掩饰成 0。"""
    if value is None or not math.isfinite(value):
        return "n/a"
    return f"{value:+.4f}" if sign else f"{value:.4f}"


def _ms(value: float | None) -> str:
    if value is None or not math.isfinite(value):
        return "n/a"
    return f"{value * 1e3:+.2f} ms"


def _verdict(paths: list[Path], cfg: AlgoConfig, echo=print) -> dict[str, Any]:
    """汇总多趟，按 R1 判据给三选一结论。"""
    trials = []
    payloads = []
    for path in paths:
        payload = json.loads((path / TRIAL_FILENAME).read_text(encoding="utf-8"))
        payloads.append(payload)
        deltas = [pair["delta_s"] for pair in payload["anchor"]["pairs"]]
        if not deltas:
            echo(f"跳过 {path}：没有配对到对碰")
            continue
        trials.append((payload["label"], np.asarray(deltas, dtype=np.float64)))

    all_deltas = np.concatenate([d for _, d in trials]) if trials else np.zeros(0)
    verdict = Verdict(deltas=all_deltas, trials=len(trials), taps=int(all_deltas.size))
    echo("")
    echo(f"== V3′ 汇总：{verdict.trials} 趟 / {verdict.taps} 次对碰 ==")
    # 判据从常量取，不在这里重写一遍数字：`validate/v3prime.py` 的模块文档说
    # 「判据只有一处家」，而一个写死在输出串里的 5.50 会让现场读到的门槛与
    # `Verdict.negligible` 真正执行的那个悄悄分家。
    echo(
        f"|Δ| 中位 {verdict.median_abs * 1e3:.2f} ms"
        f"（判据 < {NEGLIGIBLE_MEDIAN_S * 1e3:.2f}）"
        f" | 90 分位 {verdict.p90_abs * 1e3:.2f} ms"
        f"（判据 < {NEGLIGIBLE_P90_S * 1e3:.2f}）"
        f" | 最大 {verdict.max_abs * 1e3:.2f} ms"
    )
    echo(f"结论：{verdict.decision}")
    echo("判据来源：06 测试与验证方案 v1.1 §5 / RAY-213 需求修订 R1（开跑前冻结）")
    return {"verdict": verdict.snapshot(), "trials": [p["label"] for p in payloads]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m gait.cli.v3prime",
        description="V3′ 实验工装（工程模式）：主机侧同步误差量化与三选一决策。",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    live = sub.add_parser("live", help="现场采一趟（两台设备同进程，t_host 共钟）")
    live.add_argument("--subject", required=True, help="受试者代号（脱敏，勿用姓名/档案号）")
    live.add_argument("--trial", required=True, help="本受试者的第几趟")
    live.add_argument("--out", type=Path, required=True, help="趟次输出根目录")
    live.add_argument("--taps", type=int, default=20, help="对碰次数（默认 20）")
    live.add_argument("--tap-seconds", type=float, default=40.0, help="对碰段时长，s")
    live.add_argument("--walk-seconds", type=float, default=180.0, help="步行段时长，s")
    live.add_argument("--mac", action="append", help="按 MAC 片段选设备，第一个为左足")
    live.add_argument("--scan-timeout", type=float, default=10.0)
    live.add_argument("--nominal-fs", type=float, default=200.0)

    replay = sub.add_parser("replay", help="从已采趟次复算（不需要硬件）")
    replay.add_argument("--trial-dir", type=Path, required=True)

    verdict = sub.add_parser("verdict", help="汇总多趟，按 R1 判据给三选一结论")
    verdict.add_argument("--trials", type=Path, nargs="+", required=True)
    verdict.add_argument("--out", type=Path, default=None)

    args = parser.parse_args(argv)
    cfg = AlgoConfig()

    try:
        if args.command == "live":
            label = f"{args.subject}-{args.trial}"
            trial_dir = args.out / label
            require_adequate_clock(args.nominal_fs)
            payload = asyncio.run(
                _run_live(
                    label=label,
                    out_dir=trial_dir,
                    nominal_fs=args.nominal_fs,
                    taps=args.taps,
                    tap_seconds=args.tap_seconds,
                    walk_seconds=args.walk_seconds,
                    mac_filters=args.mac,
                    scan_timeout=args.scan_timeout,
                    cfg=cfg,
                )
            )
            _write(trial_dir / TRIAL_FILENAME, payload)
            _echo_trial(payload)
            print(f"趟次已写入 {trial_dir / TRIAL_FILENAME}")
        elif args.command == "replay":
            payload = load_trial_dir(args.trial_dir, cfg)
            _write(args.trial_dir / TRIAL_FILENAME, payload)
            _echo_trial(payload)
        else:
            summary = _verdict(list(args.trials), cfg)
            if args.out is not None:
                _write(args.out, summary)
                print(f"汇总已写入 {args.out}")
    except (HarnessError, V3PrimeError, ValueError, OSError) as error:
        print(f"V3′ 工装失败：{error}", file=sys.stderr)
        return 2
    return 0


def _write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    raise SystemExit(main())
