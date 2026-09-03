"""直行段/转身段分离与中段步筛选。契约 §1 的 `analysis/segments.py`（F4.2）。

PRD §7.2：4 米往返协议成立的前提是时空参数**不被转身与加减速污染**。

## 一、判据用逐步的航向变化，不用步长

转身步与直行步在好几个量上都分得开。实测（4 米往返，合成）：

| 判据 | 直行 | 转身 | 间隔 |
| --- | --- | --- | --- |
| 逐步航向变化 | 0.000° | 90.0° | **+89.997°** |
| 峰值偏航角速率 | 0.000 rad/s | 6.63 rad/s | +6.625 |
| 步长 | 1.300 m | 0.085 m | −1.215 m |

三个都能分，但**步长不能用来定义转身** —— 那会循环论证：分段的目的正是为了让步长可信，
拿步长去分段等于先假定它可信。一个步长异常的直行步（绊了一下）会被判成转身而消失，
于是"直行步的步长"这个统计量被它自己的定义过滤过了。

航向变化则是独立的证据：它来自角速度，与位置积分无关。

## 二、剔除策略在 4 米协议下是**样本量**问题，不是偏差问题

Issue 点名「单段直行仅 4~6 步/侧，剔除策略敏感性是数据评估核心问题」。实测下来这个
敏感性的真面目是**样本量塌缩**：

| 直行段长度 | 每段步数 | 剔 0 | 剔 1 | 剔 2 |
| --- | --- | --- | --- | --- |
| 4 m | 3.0 | 48 步 | **16 步** | **0 步（全剔光）** |
| 6 m | 4.8 | 58 步 | 34 步 | 11 步 |
| 10 m | 8.0 | 64 步 | 48 步 | 32 步 |

在 PRD 的 4 米协议下，**每端剔 1 步就丢掉三分之二的数据；剔 2 步一步不剩。**

所以 `trim` 必须是可回溯的参数，而且剔光时要**报错而不是返回空**：一个空的步集会让
下游算出 `nan`，而 `nan` 在报告里可能被渲染成"—"，看起来像"这项没测"，而不是"这项被
剔除策略吃掉了"。

**偏差那一面这里回答不了。** 合成数据的每个直行 stride 都是精确的 1.300 m（标准差
0.0000），因为生成器不建模起步加速与到头减速 —— 而那正是剔除首尾步要对付的东西。
剔除的**收益**因此只能由真机数据回答（RAY-230）。本模块把 trim 做成可回溯参数、把
被剔的步存下来，就是为了那一天能直接重算对比。

## 三、分离结果必须可复查（FR-312 语义）

分段边界与被剔除的步都进 `SegmentationReport`，且报告里带上用的是哪个 `trim`。
「可复查」的实际含义是：拿着报告能重算出同一个结果，并且能回答"这一步为什么没进
统计"。只存最终的步集做不到后者。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final

import numpy as np

from gait.contracts import GaitCycle

#: 报告的结构版本。
SEGMENTATION_VERSION: Final[str] = "1.0"

KIND_STRAIGHT: Final[str] = "straight"
KIND_TURN: Final[str] = "turn"

#: 逐步航向变化超过这个角度（deg）即判为转身步。
#:
#: 实测直行步是 0.000°、转身步是 90.0°，中间的空当有 89.997° —— 阈值定在哪里几乎
#: 都一样。取 25° 是因为它同时能容纳真实行走里的小幅航向调整（走廊里绕行、避让），
#: 那些不该被当成转身。
DEFAULT_TURN_DEGREES: Final[float] = 25.0

#: 每个直行段首尾各剔除几步。**在 4 米协议下这个数很贵**，见模块文档 §2。
DEFAULT_TRIM_STEPS: Final[int] = 1

#: 跨足转身一致性的时间容差，秒。0 表示要求两足的转身周期**时间上真的重叠**。
#: 实测 0 与 0.5 s 给出同一结果（共现 0.083），所以默认取更严的那个。
DEFAULT_AGREEMENT_TOLERANCE_S: Final[float] = 0.0


class SegmentationError(ValueError):
    """分段的输入非法，或剔除策略把数据剔光了。"""


@dataclass(frozen=True)
class PathSegment:
    """一段连续的直行或转身。索引指的是 `cycles` 里的下标。"""

    kind: str
    start: int
    #: 不含。
    stop: int
    #: 这一段内的总航向变化，deg。
    heading_change: float
    duration: float

    @property
    def cycles(self) -> int:
        return self.stop - self.start

    def snapshot(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "start": self.start,
            "stop": self.stop,
            "heading_change": self.heading_change,
            "duration": self.duration,
            "cycles": self.cycles,
        }


@dataclass(frozen=True)
class SegmentationReport:
    """分段与筛选的完整结果。**可复查**（FR-312 语义），见模块文档 §3。"""

    segments: list[PathSegment]
    #: 进入统计的周期下标。
    selected: list[int]
    #: 被剔除的周期下标，以及各自的理由。只存最终步集就回答不了"这一步为什么没进统计"。
    dropped: dict[int, str]
    #: 用的是哪个剔除参数。报告里必须带上它，否则重算不出同一个结果。
    trim: int
    turn_degrees: float
    turns: int
    mean_turn_duration: float
    version: str = SEGMENTATION_VERSION

    @property
    def straight_segments(self) -> list[PathSegment]:
        return [item for item in self.segments if item.kind == KIND_STRAIGHT]

    def snapshot(self) -> dict[str, Any]:
        return {
            "segments": [item.snapshot() for item in self.segments],
            "selected": list(self.selected),
            "dropped": {str(key): value for key, value in self.dropped.items()},
            "trim": self.trim,
            "turn_degrees": self.turn_degrees,
            "turns": self.turns,
            "mean_turn_duration": self.mean_turn_duration,
            "version": self.version,
        }


def heading_change_per_cycle(
    cycles: Sequence[GaitCycle], t: np.ndarray, yaw_rate: np.ndarray
) -> np.ndarray:
    """每个周期内的航向变化，deg。

    `yaw_rate` 是导航系的偏航角速率（rad/s）。只有足部大致水平时才能用足部系的 z 轴
    角速度代替它 —— 那个近似在正常行走里成立，在爬楼或蹲下时不成立。调用方给什么就
    积什么，本函数不替它做这个判断。
    """
    times = np.asarray(t, dtype=np.float64)
    rate = np.asarray(yaw_rate, dtype=np.float64)
    if times.shape != rate.shape:
        raise SegmentationError(
            f"t 与 yaw_rate 的形状必须一致：{times.shape} vs {rate.shape}"
        )
    changes = np.zeros(len(cycles))
    for index, cycle in enumerate(cycles):
        lo = int(np.searchsorted(times, cycle.t_ic))
        hi = int(np.searchsorted(times, cycle.t_ic_next))
        if hi <= lo:
            continue
        changes[index] = np.degrees(np.trapezoid(rate[lo:hi], times[lo:hi]))
    return changes


def separate(
    cycles: Sequence[GaitCycle],
    heading_change: np.ndarray,
    *,
    turn_degrees: float = DEFAULT_TURN_DEGREES,
) -> list[PathSegment]:
    """把周期切成交替的直行段与转身段。

    判据是**逐步的航向变化**，不是步长 —— 拿步长分段会循环论证，见模块文档 §1。
    """
    changes = np.asarray(heading_change, dtype=np.float64)
    if changes.size != len(cycles):
        raise SegmentationError(
            f"heading_change 的长度 {changes.size} 与周期数 {len(cycles)} 不一致"
        )
    if not len(cycles):
        return []
    if turn_degrees <= 0:
        raise SegmentationError(f"turn_degrees 必须为正，收到 {turn_degrees}")

    kinds = np.where(np.abs(changes) >= turn_degrees, KIND_TURN, KIND_STRAIGHT)
    return _tile(cycles, kinds, changes)


def _tile(
    cycles: Sequence[GaitCycle], kinds: np.ndarray, changes: np.ndarray
) -> list[PathSegment]:
    """把逐周期的类别铺成交替的段。`separate` 与跨足协调共用同一套铺排。"""
    segments: list[PathSegment] = []
    start = 0
    for index in range(1, len(cycles) + 1):
        if index < len(cycles) and kinds[index] == kinds[start]:
            continue
        segments.append(
            PathSegment(
                kind=str(kinds[start]),
                start=start,
                stop=index,
                heading_change=float(changes[start:index].sum()),
                duration=float(cycles[index - 1].t_ic_next - cycles[start].t_ic),
            )
        )
        start = index
    return segments


def separate_with_agreement(
    left: tuple[Sequence[GaitCycle], np.ndarray],
    right: tuple[Sequence[GaitCycle], np.ndarray],
    *,
    sync_quality: dict[str, Any],
    turn_degrees: float = DEFAULT_TURN_DEGREES,
    tolerance_s: float = DEFAULT_AGREEMENT_TOLERANCE_S,
) -> tuple[list[PathSegment], list[PathSegment]]:
    """两只脚一起分段：**只有两足都判出的转身才算转身。**

    ## 为什么这条规则成立

    转身是**身体**改变行进方向，两只脚都要跟着走；而航向漂移是**逐脚**的
    （RAY-359 实测左脚 12/12 趟都比右脚差）。所以「两足是否同时判出」把这两类分开，
    而**长度**分不开 —— 见下。

    实测（T-230-03，转身真值 **0**，判出的全是误报）：跨足共现率 **0.083**，
    **低于**循环平移 400 次的随机基线 **0.140**；8 个有转身的格里 7 格共现为零。

    ## 为什么不是「转身至少 N 步」

    实测假转身 11/13 格是单步，看上去用长度就能筛掉。**但那是错的判别量。**
    `separate` 的判据是**逐周期**的：`|Δ航向| ≥ turn_degrees` 的单个周期就是转身，
    所以**一步 30° 的转弯按本模块自己的定义就是合法转身**。长度过滤器会把它判成直行。

    更根本的：假转身是单步，因为漂移是**逐步噪声**；而小角度真转身**也是单步**。
    **长度是两类共有的属性。** 合成对照直接验到这一点：`turn_strides=1` 的真转身
    跨足共现 **5/5**，长度过滤器却会把它全杀掉。

    ## 它挡不住什么

    * **两只脚在同一时刻一起判错**时无效。真机上这种情况罕见（共现低于随机），但
      `S1-sport/slow-a` 那种前向解发散的格里仍会漏过几次 —— 那一格另有缺陷。
    * **单足会话**用不了这条规则。调用方缺一只脚时不要调它。
    * 它**继承跨足同步质量的全部约束**（PRD §13）：两只脚的时间轴对不齐，共现判断
      就没有意义，所以 `sync_quality` 是必需的关键字参数，与 `events.double_support`
      同一口径。
    """
    if sync_quality is None:
        raise SegmentationError(
            "跨足转身一致性是**跨足**判断，必须附同步质量标注（PRD §13）。"
            "两只脚的时间轴对不齐时，'同时判出'这句话本身没有意义。"
        )
    if tolerance_s < 0:
        raise SegmentationError(f"tolerance_s 不得为负，收到 {tolerance_s}")

    prepared = []
    for cycles, heading_change in (left, right):
        changes = np.asarray(heading_change, dtype=np.float64)
        if changes.size != len(cycles):
            raise SegmentationError(
                f"heading_change 的长度 {changes.size} 与周期数 {len(cycles)} 不一致"
            )
        if turn_degrees <= 0:
            raise SegmentationError(f"turn_degrees 必须为正，收到 {turn_degrees}")
        kinds = np.where(np.abs(changes) >= turn_degrees, KIND_TURN, KIND_STRAIGHT)
        prepared.append((list(cycles), changes, kinds))

    windows = [
        [
            (float(cycle.t_ic), float(cycle.t_ic_next))
            for cycle, kind in zip(cycles, kinds, strict=True)
            if kind == KIND_TURN
        ]
        for cycles, _changes, kinds in prepared
    ]

    out: list[list[PathSegment]] = []
    for side, (cycles, changes, kinds) in enumerate(prepared):
        other = windows[1 - side]
        confirmed = kinds.copy()
        for index, (cycle, kind) in enumerate(zip(cycles, kinds, strict=True)):
            if kind != KIND_TURN:
                continue
            start, stop = float(cycle.t_ic), float(cycle.t_ic_next)
            if not any(
                start - tolerance_s < end and other_start - tolerance_s < stop
                for other_start, end in other
            ):
                # 另一只脚在同一时刻没有判出转身 —— 降级为直行，相邻直行段随之合并。
                confirmed[index] = KIND_STRAIGHT
        out.append(_tile(cycles, confirmed, changes))
    return out[0], out[1]


def select_middle_steps(
    cycles: Sequence[GaitCycle],
    segments: Sequence[PathSegment],
    *,
    trim: int = DEFAULT_TRIM_STEPS,
    turn_degrees: float = DEFAULT_TURN_DEGREES,
) -> SegmentationReport:
    """剔除转身步与每个直行段首尾各 `trim` 步。

    **剔光时报错，不返回空。** 4 米协议下每段只有约 3 步，`trim=2` 会一步不剩
    （见模块文档 §2 的实测表）。空的步集会让下游算出 `nan`，而 `nan` 在报告里可能被
    渲染成"—"，看起来像"这项没测"，而不是"这项被剔除策略吃掉了"—— 两者对数据评估
    是完全不同的结论。
    """
    if trim < 0:
        raise SegmentationError(f"trim 不得为负，收到 {trim}")

    selected: list[int] = []
    dropped: dict[int, str] = {}
    for segment in segments:
        if segment.kind == KIND_TURN:
            for index in range(segment.start, segment.stop):
                dropped[index] = "turn"
            continue
        count = segment.cycles
        if count <= 2 * trim:
            for index in range(segment.start, segment.stop):
                dropped[index] = f"segment_too_short_for_trim_{trim}"
            continue
        for index in range(segment.start, segment.stop):
            offset = index - segment.start
            if offset < trim:
                dropped[index] = "segment_head"
            elif offset >= count - trim:
                dropped[index] = "segment_tail"
            else:
                selected.append(index)

    if not selected:
        raise SegmentationError(
            f"剔除策略（trim={trim}）把所有步都剔掉了：共 {len(cycles)} 个周期，"
            f"直行段 {len([s for s in segments if s.kind == KIND_STRAIGHT])} 个。"
            "4 米协议下每段只有约 3 步，trim=2 会一步不剩 —— "
            "返回空集会让下游算出 nan，而那看起来像'这项没测'而不是'被剔除策略吃掉了'。"
        )

    turns = [item for item in segments if item.kind == KIND_TURN]
    return SegmentationReport(
        segments=list(segments),
        selected=selected,
        dropped=dropped,
        trim=trim,
        turn_degrees=turn_degrees,
        turns=len(turns),
        mean_turn_duration=(
            float(np.mean([item.duration for item in turns])) if turns else 0.0
        ),
    )


def analyse(
    cycles: Sequence[GaitCycle],
    t: np.ndarray,
    yaw_rate: np.ndarray,
    *,
    trim: int = DEFAULT_TRIM_STEPS,
    turn_degrees: float = DEFAULT_TURN_DEGREES,
) -> SegmentationReport:
    """一步到位：算航向变化、分段、筛中段步。"""
    changes = heading_change_per_cycle(cycles, t, yaw_rate)
    segments = separate(cycles, changes, turn_degrees=turn_degrees)
    return select_middle_steps(
        cycles, segments, trim=trim, turn_degrees=turn_degrees
    )


def selected_cycles(
    cycles: Sequence[GaitCycle], report: SegmentationReport
) -> list[GaitCycle]:
    """按报告取出进入统计的周期。"""
    return [cycles[index] for index in report.selected]
