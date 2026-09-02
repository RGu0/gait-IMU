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
from wt901 import BleTransport, DiscoveredDevice, ReturnRate, WT901Device, scan
from wt901.transport.recording import RecordingTransport

from gait.analysis.events import (
    StanceEdges,
    detect_stance_intervals,
    segment_cycles,
)
from gait.config import AlgoConfig
from gait.contracts import FootLabel
from gait.core.zupt import detect_stance
from gait.device.ble import StreamConfig, configure_streaming, start_streaming
from gait.device.recorder import ThreadedRecordingWriter
from gait.sync.anchor import FootSignal, measure_offsets
from gait.sync.integrity import assess
from gait.sync.selfcheck import check as selfcheck
from gait.sync.selfcheck import drop_still_lead, stance_spans
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


def _stream_config(nominal_fs: float) -> StreamConfig:
    """标称采样率 → 下发给器件的 `StreamConfig`。

    **这是一个函数而不是 `_run_live` 里的一行，为的是让它可测。** 原先那一行写的是
    `StreamConfig(rate_hz=nominal_fs)` —— 一个不存在的字段名（真名是 `rate`，取
    `ReturnRate` 的值）。它藏在只有真硬件才走得到的 `_run_live` 里，于是没有任何
    测试碰得到它，错误一直等到上机才暴露。抽成函数之后，构造这件事在 CI 里就被
    执行到了。

    器件只支持离散的几档。不支持的值**当场失败**，不去找最近的一档：采集会照着
    一个与分析假设不同的速率跑完，而 `--nominal-fs` 正是分析用来回推包内时刻的
    那个数 —— 两者不一致不会报错，只会让整趟数据的时间轴系统性地错。
    """
    table = {
        10.0: ReturnRate.HZ_10,
        20.0: ReturnRate.HZ_20,
        50.0: ReturnRate.HZ_50,
        100.0: ReturnRate.HZ_100,
        200.0: ReturnRate.HZ_200,
    }
    rate = table.get(float(nominal_fs))
    if rate is None:
        raise HarnessError(
            f"器件不支持 {nominal_fs} Hz。可选：{sorted(table)}。"
            "本工装不替你取最近的一档 —— 采集速率与 --nominal-fs 不一致时"
            "没有任何东西会报错，只会让整趟的时间轴系统性地错。"
        )
    return StreamConfig(rate=int(rate))


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

    ## 静止前导必须在**细化之前**剔掉

    RAY-202 的初始对准需要一段静止前导，而 ZUPT 会把它检成一个很长的支撑相。它不是
    一步：留着它，`analysis/events.py::double_support()` 会把它当作一个寻常的双支撑
    相位，以一个**秒级**读数混进均值（典型相位只有约 110 ms）。实测双支撑期占比因此
    从 20.5% 读成 30.5% —— **污染 7~10 个百分点，而生理带宽本身才 10~25%**（RAY-296）。

    剔的时机是要紧的。判据是"支撑相比典型值长得离谱"（`selfcheck_still_lead_factor`，
    默认 2.5 倍），而 `refine_stance_edges` 把两端各推出约 50 ms —— 它把**典型支撑相
    拉长的比例比前导大**，于是比值被压低。实测同一份数据：

    | 边界 | 前导 | 典型 | 比值 |
    | --- | --- | --- | --- |
    | ZUPT（细化前） | 1615 ms | 565 ms | **2.86** ✓ |
    | 细化后 | 1670 ms | 670 ms | **2.49** ✗ 差一点点就跌破 2.5 |

    也就是说，同一条判据在细化后就**不再认得出前导**。所以这里在 ZUPT 边界上剔 ——
    那也正是 `sync/selfcheck.py` 与 `tests/test_events.py::dual_cycles()` 用它的地方。

    返回的是**剔过前导的**支撑相索引，好让 `analyze_trial()` 的两条产出路径
    （selfcheck 与指标偏差）确定地看到同一批步。此前它们各走各的前提：selfcheck 那
    一路干净（`check()` 自己会剔），指标那一路带着前导 —— 同一次采集两种口径。
    """
    arrival, accel, gyro = capture.arrays()
    timebase = build_timebase(arrival, nominal_fs, cfg)
    stance = detect_stance(accel, gyro, timebase.report.fs, cfg)
    kept = len(drop_still_lead(stance_spans(timebase.t, stance.stances), cfg))
    stances = stance.stances[len(stance.stances) - kept :]
    # `FootLabel` 是 Literal["L", "R"]，不是可实例化的类型 —— 直接传字面量。
    # 支撑相区间走 `detect_stance_intervals`，不走零速区间的边缘细化。
    #
    # `stance.stances` 是**零速时刻**（一周期一个，跨度 15~58 ms）。V3′ 报的
    # `double_support_fraction` 此前一直是负的（实测 −0.62~−0.92），因为用两个近似
    # 零宽的区间算重叠，得到的必然是负一个 step 时长 —— 那是零宽区间的算术，不是
    # 双支撑期读数（RAY-325 `stance-interval-detection`）。
    #
    # 剔前导仍然在 ZUPT 边界上做（见上），因为那一步依赖"前导比典型支撑相长得多"
    # 这个比值，而两种口径的典型支撑相差一个数量级。
    edges = detect_stance_intervals(accel, timebase.report.fs, stance, cfg)
    # 前导按**区间自己的**尺度剔。`drop_still_lead` 的判据是"比典型支撑相长 2.5 倍"，
    # 它自己算中位数，所以对两种口径都成立 —— 但**不能**拿零速区间那边剩下的个数去切
    # 这一边：两条路的区间数不一样（真机实测 35~42 vs 34~37），个数对不上，切掉的就
    # 不是前导那一段。
    kept_edges = _drop_lead_edges(timebase.t, edges, cfg)
    cycles, _edges = segment_cycles(
        foot, timebase.t, accel, gyro, stances, position=None, cfg=cfg,
        stance_edges=kept_edges if len(kept_edges) >= 2 else None,
    )
    return cycles, timebase, stances


def _drop_lead_edges(
    t: np.ndarray, edges: list[StanceEdges], cfg: AlgoConfig
) -> list[StanceEdges]:
    """按支撑相**区间自己的**时长剔起步前导，判据复用 `drop_still_lead`。"""
    if not edges:
        return edges
    spans = [
        (float(t[edge.ic]), float(t[min(edge.to, t.size - 1)])) for edge in edges
    ]
    return edges[len(edges) - len(drop_still_lead(spans, cfg)) :]


def _slice_capture(capture: FootCapture, window: tuple[float, float]) -> FootCapture:
    """按主机时刻把捕获裁到 `window`。

    只在**时基不稳**时用于重拟合（见 `analyze_trial`）。前提成立时不该走这条路 ——
    更长的拟合区间给出更好的 fs 估计，无故裁短是把精度让掉。
    """
    arrival = np.asarray(capture.arrival, dtype=np.float64)
    keep = np.flatnonzero((arrival >= window[0]) & (arrival <= window[1]))
    if keep.size == 0:
        return capture
    start, stop = int(keep[0]), int(keep[-1]) + 1
    return FootCapture(
        foot=capture.foot,
        device_id=capture.device_id,
        arrival=list(capture.arrival[start:stop]),
        accel=list(capture.accel[start:stop]),
        gyro=list(capture.gyro[start:stop]),
    )


def analyze_trial(
    label: str,
    left: FootCapture,
    right: FootCapture,
    nominal_fs: float,
    cfg: AlgoConfig | None = None,
    tap_window: tuple[float, float] | None = None,
) -> dict[str, Any]:
    """一趟的完整评估。`live` 与 `replay` 共用这一条路径。

    分成两问：**Δ 是多少**（锚点，对碰段）与**它把指标带偏多少**（步行段）。
    第二问在事件太少时会失败（受试者没走够），那时仍然返回第一问的结果 ——
    Δ 本身就是 V3′ 最主要的产出，不该因为步行段没采好而整趟作废。
    """
    cfg = cfg or AlgoConfig()
    # 只在对碰段取锚点：步行段的足跟着地同样超阈值，不限窗会把左右两次不相关的
    # 着地配成一对（实测假 Δ = −223 ms，而同趟对碰段内真 Δ 全在 ±8 ms）。
    anchor = measure_offsets(
        _foot_signal(left),
        _foot_signal(right),
        nominal_fs,
        cfg,
        coarse_align=False,
        window=tap_window,
    )
    # 时基不稳就不该拿这一趟的 Δ 去支撑任何结论：`t_host = offset + index/fs`，
    # fs 错几个百分点，几万个样本累积下来就是秒级的时刻误差 —— 那时"跨足偏差"
    # 量到的是两条各自跑偏的时间轴之差，不是链路的固有延迟差。
    #
    # 判据直接用 `SyncReport.stable`（RAY-209 的验收标准：分窗采样率相对离散度
    # < 0.1%），不另发明阈值。仍然把锚点结果留在报告里 —— 它是诊断链路的材料，
    # 只是不进判定。
    # 数据完整性与时基稳定性一起判：前者说明"丢了多少"，后者说明"时间轴还能不能
    # 用一条直线描述"。两者都过才算这一趟可信。
    #
    # 用 `integrity.assess` 的现成分级（RAY-210），不另发明阈值 —— `unusable` 是
    # 那个模块自己定义的"不可用"。实测教训：模块静止时缺失率 0.1%，而受试者
    # 手持对碰并走动时掉到 3.6%~10.2%（等级 unusable），**同一对硬件、相隔几分钟**。
    # 所以链路必须在运动条件下验，静态达标说明不了问题。
    # 完整性用**实测**采样率作分母，不用标称。器件晶振比标称低约 0.75%，
    # 按标称算的话一条完美链路的逐秒到达率读作 0.988，永远低于欠采门槛
    # （RAY-200 的 `bench-runs/README.md` 记过这一条，`cli/linktest.py` 已按此办；
    # 本函数此前漏了，实测使 overall_rate 从 0.9995 被读成 0.9918）。
    left_arrival = np.asarray(left.arrival, dtype=np.float64)
    right_arrival = np.asarray(right.arrival, dtype=np.float64)
    left_integrity = assess(left_arrival, anchor.left_sync.fs, cfg)
    right_integrity = assess(right_arrival, anchor.right_sync.fs, cfg)

    # 时基不稳时，改用**对碰窗口**重拟合，而不是整趟作废。
    #
    # `t_host = offset + index / fs` 是一个**局部线性模型**。拿一个在 940 s 上拟合的
    # 模型去内插一个 40 s 窗口，只有在 fs 于全程恒定时才成立 —— 而 `stable=False`
    # 说的正是这个前提被推翻了。此时在**使用区间**上重拟合是标准做法。
    #
    # 实测（RAY-230 批次，两趟被旧闸门整趟拒收）：
    #
    #     趟次        全趟时基            仅对碰窗口拟合
    #     S1-sport    +202.74 ms          +5.70 ms
    #     S1-flat     −29.27 ms           −2.93 ms
    #     （合格趟）   −1.12 ms            −0.93 ms   ← 两者一致
    #
    # 最后一行是关键：**前提成立时它是恒等变换**，前提不成立时才是修正。
    # 若非如此，这就成了"换把尺子去凑一个好看的数"。
    #
    # 这也更正了本分支早先提交里的说法（"裁短会让每足时基重新拟合，换的是尺子"）
    # —— 那条在整段数据均匀时成立，在测量窗**之外**有损伤时不成立。
    timebase_scope = "full"
    if tap_window is not None and not (anchor.left_sync.stable and anchor.right_sync.stable):
        windowed = measure_offsets(
            _foot_signal(_slice_capture(left, tap_window)),
            _foot_signal(_slice_capture(right, tap_window)),
            nominal_fs,
            cfg,
            coarse_align=False,
        )
        if windowed.pairs:
            anchor = windowed
            timebase_scope = "tap_window"

    # 完整性按**测量区间**判，不按整趟。Δ 只在对碰段测，步行段的空洞伤不到它。
    # 实测：S1-sport 右足整趟 unusable（丢 207、18 处空洞），而对碰窗口内
    # grade=normal、丢 0、0 空洞。整趟二值会把这样一趟整个否掉。
    if tap_window is not None:
        in_tap_l = left_arrival[(left_arrival >= tap_window[0]) & (left_arrival <= tap_window[1])]
        in_tap_r = right_arrival[
            (right_arrival >= tap_window[0]) & (right_arrival <= tap_window[1])
        ]
        delta_left = assess(in_tap_l, anchor.left_sync.fs, cfg) if in_tap_l.size else left_integrity
        delta_right = (
            assess(in_tap_r, anchor.right_sync.fs, cfg) if in_tap_r.size else right_integrity
        )
    else:
        delta_left, delta_right = left_integrity, right_integrity

    usable = delta_left.grade != "unusable" and delta_right.grade != "unusable"
    trustworthy = usable and bool(anchor.pairs)
    payload: dict[str, Any] = {
        "label": label,
        "nominal_fs": nominal_fs,
        "anchor": anchor.snapshot(),
        "left_device": left.device_id,
        "right_device": right.device_id,
        "timebase_trustworthy": trustworthy,
        "timebase_scope": timebase_scope,
        "delta_region_integrity": {
            "left": delta_left.snapshot(),
            "right": delta_right.snapshot(),
        },
        "tap_window": list(tap_window) if tap_window else None,
        "integrity": {
            "left": left_integrity.snapshot(),
            "right": right_integrity.snapshot(),
        },
    }
    if timebase_scope == "tap_window":
        payload["timebase_note"] = (
            "整趟时基不稳（左 stable="
            f"{anchor.left_sync.stable} 右={anchor.right_sync.stable} 为重拟合后的值），"
            "已改用**对碰窗口**重新拟合时基。`t_host = offset + index/fs` 是局部线性模型，"
            "拿 940 s 的拟合去内插 40 s 窗口，只在 fs 全程恒定时成立。"
            "前提成立时本路径不触发（实测在时基稳的趟次上两种拟合给出同一个 Δ）。"
        )
    if not trustworthy:
        payload["timebase_note"] = (
            f"这一趟不可信：左足数据完整性 {left_integrity.grade}"
            f"（丢失 {left_integrity.lost_samples}，{len(left_integrity.gaps)} 处空洞），"
            f"右足 {right_integrity.grade}"
            f"（丢失 {right_integrity.lost_samples}，{len(right_integrity.gaps)} 处空洞）；"
            f"时基稳定性 左={anchor.left_sync.stable} 右={anchor.right_sync.stable}。"
            "丢包会让实测采样率偏低（RAY-210：1% 丢包 ≈ −1% 采样率），时间轴随之跑偏，"
            "此时的 Δ 量的是两条各自跑偏的轴之差，不是链路固有延迟差。"
            "Δ 只可用于诊断，不得计入 V3′ 判定。"
        )

    if not trustworthy:
        payload["trial"] = None
        payload["metrics_error"] = "时基不可信，未计算指标偏差（见 timebase_note）"
        return payload

    try:
        left_cycles, left_tb, left_stances = _cycles(left, "L", nominal_fs, cfg)
        right_cycles, right_tb, right_stances = _cycles(right, "R", nominal_fs, cfg)
        quality = selfcheck(
            stance_spans(left_tb.t, left_stances),
            stance_spans(right_tb.t, right_stances),
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
    settle_seconds: float,
    mac_filters: list[str] | None,
    scan_timeout: float,
    cfg: AlgoConfig,
    echo=print,
) -> dict[str, Any]:
    """采一趟：连接 → **稳定期** → 对碰段 → 步行段 → 分析。

    两段之间**不断开连接**：重连会重新协商连接参数，固有链路延迟随之改变，那时
    对碰段量到的 Δ 就不是步行段的 Δ 了（模块文档"一趟的结构"）。

    ## 为什么开头要丢掉一段

    两台设备是**顺序**建链、顺序配置的：第一台写完速率寄存器就开始满速推流，
    而第二台此时还在建链与寄存器读写。**第二台在这个阶段掉到 160~184 样本/秒**
    （稳态 200）。实测（T-213-02，7/7 复现）：劣化跟随**连接顺序**而非器件 ——
    对调 `--mac` 顺序后它整个跳到另一台上；静置与行走一致，与运动无关。

    要紧的是这段样本**在消费者启动之前就已产生**：设备从连上那一刻就按上一轮
    固化的速率产出（所以积压也是 200 Hz，按速率看不出异常），排在队列里等消费。
    不划 `started` 这条线，它们会连同劣化一起被收进来 —— 实测每趟的 arrival
    跨度因此比名义时长多约 5.3 s，而恢复稳定所需的最小裁切量正是 5~6 s，两者吻合。

    后果不是"少几个样本"：`build_timebase` 分窗拟合，**一个坏窗**就能让
    `fs_window_spread` 从 1e-4 涨到 2.5e-2（判据 < 1e-3），整趟判 `stable=False`；
    而对碰紧贴这一段，Δ 中位被从真值约 0 推到 +24~+50 ms —— 判据只有 5.5 ms。

    **主要的那道闸是 `started`**（丢弃消费者启动前的积压）。按 arrival[0] ≈
    `started` − 5.3 s 推算，它之后的残留约 0.7 s，`settle_seconds` 是给这点残留
    留的余量，不是主力。

    两者都在**消费者侧**丢弃：`arrivals.npz` 因此只含干净数据，时基、`stable`、
    锚点全都自然地在剔除之后评估，下游无需改动。
    **原始字节仍由 `RecordingTransport` 完整落进 `raw_*.jsonl`**，劣化段可审计。
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    loop = asyncio.get_running_loop()
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

        stream_config = _stream_config(nominal_fs)
        # 先把两台的**非速率**配置全部下完，最后再一起开流。
        #
        # 写速率寄存器就是开流。逐台走完整配置时，第一台写完速率即满速推流，
        # 而第二台还要走完自己那 3~5 秒的配置（四次写事务各 0.7 s，加四次回读）——
        # 第一台独自推流的这几秒，正是第二台开流后过渡期的成因：实测第 2~6 秒
        # 掉到 160~184 样本/秒（稳态 200），7/7 复现，且跟随连接顺序而非器件
        # （T-213-02）。
        applied_configs = []
        for device, capture in zip(devices, captures, strict=True):
            applied = await configure_streaming(device, stream_config, defer_rate=True)
            if not applied.verified:
                raise HarnessError(
                    f"{capture.device_id} 配置校验失败：{applied.mismatches}。中止本趟。"
                )
            applied_configs.append(applied)

        # 两次速率写入尽量靠拢：间隔从 3~5 s 降到一次 BLE 写的往返。
        # 只并发**速率写入**，建链仍是顺序的 —— CoreBluetooth 对并发 connect 的
        # 行为未验，一次只改一个变量。
        started = await asyncio.gather(
            *(
                start_streaming(device, stream_config, applied)
                for device, applied in zip(devices, applied_configs, strict=True)
            )
        )
        for applied, capture in zip(started, captures, strict=True):
            if not applied.verified:
                raise HarnessError(
                    f"{capture.device_id} 速率校验失败：{applied.mismatches}。中止本趟。"
                )
        echo(f"两台均已配置 {nominal_fs:.0f} Hz")

        # 采集起点必须在消费者启动**之前**取：连接与配置期间设备已在产出样本
        # （按上一轮固化的速率），它们积压在 wt901 的队列里等着被读。不划这条线
        # 就会把配置阶段的残留当成采集数据 —— 实测后果是 arrival 跨度比名义时长
        # 多出 5.5 s，回归据此把实测采样率读成 191/178 Hz（真值 198），残差 p95
        # 涨到 0.5~1.3 s，整趟被判 unusable。链路本身当时是好的。
        #
        # `loop.time()` 与 `ImuSample.t_host` 同源：CPython 的事件循环时钟就是
        # `time.monotonic`（`cli/linktest.py` 依赖的也是这一点）。
        started = loop.time()
        # 稳定期内消费者照常跑（要把队列抽空），但样本一律丢弃。
        capture_from = started + settle_seconds
        consumers = [
            asyncio.ensure_future(_consume(device, capture, capture_from))
            for device, capture in zip(devices, captures, strict=True)
        ]
        try:
            if settle_seconds > 0:
                echo("")
                echo(f"▶ 稳定期（{settle_seconds:.0f} s）：两台都不要动，这一段不进分析。")
                await _countdown(settle_seconds, echo)
            echo("")
            echo(f"▶ 对碰段（{tap_seconds:.0f} s）：两模块外壳干脆对碰 {taps} 次，")
            echo("  间隔至少半秒、不要打成节拍器，力道以不削顶为宜。")
            tap_start = loop.time()
            await _countdown(tap_seconds, echo)
            tap_stop = loop.time()
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
        tap_window=np.asarray([tap_start, tap_stop]),
        settle_seconds=np.asarray(settle_seconds),
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


async def _consume(device: WT901Device, capture: FootCapture, started: float) -> None:
    """把样本收进内存。`t_host` 由 wt901 在通知回调里取，两台同源同钟。

    早于 `started` 的样本一律丢弃：那是连接与配置阶段积压在队列里的残留，混进来
    会让时基回归得到一个明显偏低的采样率（见 `_run_live` 里划定 `started` 的注释）。
    """
    async for sample in device.samples():
        if sample.t_host < started:
            continue
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
    window = None
    if "tap_window" in data.files:
        lo, hi = (float(v) for v in data["tap_window"])
        window = (lo, hi)
    payload = analyze_trial(
        str(data["label"]), captures[0], captures[1], float(data["nominal_fs"]), cfg,
        tap_window=window,
    )
    payload["settle_seconds"] = (
        float(data["settle_seconds"]) if "settle_seconds" in data.files else None
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
    if payload.get("timebase_trustworthy") is False:
        echo("")
        echo("❌ 这一趟不计入 V3′ 判定：" + payload.get("timebase_note", ""))
        for side, key in (("左", "left_sync"), ("右", "right_sync")):
            s = payload["anchor"][key]
            echo(
                f"   {side}足时基：实测 {s['fs']:.2f} Hz（{s['fs_deviation_ppm']:+.0f} ppm）"
                f" stable={s['stable']}"
                f" 残差 p95={s['residual_p95'] * 1e3:.1f} ms"
                f" max={s['residual_max'] * 1e3:.1f} ms"
                f"（BLE 抖动本该是几毫秒量级）"
            )
        echo("")
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
    skipped: list[str] = []
    for path in paths:
        payload = json.loads((path / TRIAL_FILENAME).read_text(encoding="utf-8"))
        payloads.append(payload)
        if payload.get("timebase_trustworthy") is False:
            # 静默丢弃会让"5 人 × 3 趟"这句话变成谎话 —— 被排除的趟次必须
            # 出现在汇总里，否则样本量看起来永远是满的。
            skipped.append(f"{payload['label']}（时基不稳）")
            continue
        deltas = [pair["delta_s"] for pair in payload["anchor"]["pairs"]]
        if not deltas:
            skipped.append(f"{payload['label']}（无配对对碰）")
            continue
        trials.append((payload["label"], np.asarray(deltas, dtype=np.float64)))
    if skipped:
        echo(f"已排除 {len(skipped)} 趟：{'、'.join(skipped)}")

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
    live.add_argument(
        "--settle-seconds",
        type=float,
        default=3.0,
        help=(
            "消费者起点之后再多丢弃的秒数。这一段不进 arrivals.npz（原始字节仍落盘）。"
            "劣化的主体在**消费者起点之前**的积压里，已由 started 挡掉；实测残留约 0.7 s，"
            "默认 3 s 给约 4 倍余量。设为 0 则只靠 started。"
        ),
    )
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
                    settle_seconds=args.settle_seconds,
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
