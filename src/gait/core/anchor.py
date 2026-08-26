"""零速段位置锚定。整体设计 §5.8 第 2 条。

## 用的是哪一条信息

ZUPT 观测说的是"支撑相里速度为零"。支撑相里真正成立的事实比这强：**足部不动**，
也就是整个支撑相里位置是同一个常数。滤波器拿不到后一条 —— 它在样本 k 上只能看到
"此刻速度应为零"，而位置的常数性是一条**跨样本**的约束，因果滤波没有表达它的位置。

后果是 RAY-261 量到的那 4 cm：ZUPT 更新在把速度拉回零的同时，通过协方差交叉项也在
微调位置，一个支撑相下来位置会蠕动约 4 cm。这个绝对值在走路和低速档几乎一样，
占 1.30 m 步长是 3%，占 0.35 m 步长是 11%。

本模块把每个支撑相的位置当作一个**伪路标**（整体设计 §5.8 的说法），要求相内所有样本
共享同一个位置，摆动相则在相邻两个路标之间线性分摊修正量。这就是那句"轻量图优化"：
图是一条链，约束是"同一相内位置相等"，而链式结构让它有闭式解，不需要迭代求解器。

## 与 RAY-261「决策 2」的边界

RAY-261 的候选出路里也有"抑制支撑相内位置蠕动"，但那一条写的是**在 ESKF 里加一条
观测**，会改变本地基础链的数值，属未决的决策 2、且明确"应另开 Issue"。

本模块**不是**那个改动：它是精算链上的后处理，只在 `cloud/recompute.py` 里被调用，
`eskf.run_ins` 一行未改。基础链的输出逐位不变 —— 端云同构说的是"共用同一内核"，
不是"两条链输出必须相同"（若相同，云端重算就没有存在的理由）。

## 速度不跟着改

只修位置，不修速度。位置修正后 `v` 与 `dp/dt` 会有微小的不一致，这是清楚知道并接受的：
下游的步长与步速都从**位置**算（`analysis/events.py` 的 `stride_length` 取相邻触地的
位移，`gait_speed = stride_length / stride_time`），`v` 不参与任何报告指标。

由位置差分反推速度会把每一相边界上的修正量变成一个尖峰，那是用一个真问题换一个假问题。
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import pairwise

import numpy as np

from gait.contracts import NavResult


class AnchorError(ValueError):
    """锚定输入非法。"""


@dataclass(frozen=True)
class AnchorReport:
    """锚定改了多少。`max_creep_removed` 直接对应 RAY-261 量到的那 4 cm。"""

    #: 支撑相个数。
    stances: int
    #: 锚定前，单个支撑相内位置漂移量的中位数，m。
    median_creep_before: float
    #: 锚定前的最大相内漂移量，m。
    max_creep_before: float
    #: 锚定引入的位置修正的最大模长，m。
    max_position_shift: float
    #: 轨迹总位移的变化量，m。远大于零说明锚定改变的不只是相内蠕动，值得看一眼。
    total_displacement_change: float

    def snapshot(self) -> dict[str, float | int]:
        return {
            "stances": self.stances,
            "median_creep_before": self.median_creep_before,
            "max_creep_before": self.max_creep_before,
            "max_position_shift": self.max_position_shift,
            "total_displacement_change": self.total_displacement_change,
        }


@dataclass(frozen=True)
class AnchorResult:
    navigation: NavResult
    report: AnchorReport


def _creep(position: np.ndarray, stances: list[tuple[int, int]]) -> np.ndarray:
    """每个支撑相内首尾位置差的模长。"""
    if not stances:
        return np.zeros(0)
    return np.array(
        [float(np.linalg.norm(position[end - 1] - position[start])) for start, end in stances]
    )


def anchor_stance_positions(navigation: NavResult) -> AnchorResult:
    """把每个支撑相的位置钉到该相的均值，摆动相线性分摊修正。

    ## 为什么是均值而不是首样本

    取首样本等于宣称"相内的漂移全部发生在进入支撑相之后"，取末样本则是反过来。两者都
    是无依据的偏袒，且会在轨迹上引入一个与步频同频的系统偏置。均值不偏袒任何一端，
    且在"漂移近似线性"这个（实测成立的）前提下就是最小二乘解。

    ## 边界

    第一个支撑相之前与最后一个支撑相之后的样本，修正量取最近那一相的修正量并保持常数。
    外推一个线性趋势会在没有观测支撑的区间上放大误差，而那两段通常是站立引导段与收尾段，
    本来就不进指标。
    """
    if not isinstance(navigation, NavResult):
        raise AnchorError(f"navigation 必须是 NavResult，收到 {type(navigation).__name__}")

    position = np.asarray(navigation.p, dtype=np.float64)
    stances = [(int(start), int(end)) for start, end in navigation.stances]
    creep_before = _creep(position, stances)

    if not stances:
        # 没有支撑相就没有伪路标。返回原轨迹而不是报错：一段纯摆动的数据是合法输入，
        # 只是这一步无事可做。
        return AnchorResult(navigation, AnchorReport(0, 0.0, 0.0, 0.0, 0.0))

    n = len(position)
    correction = np.zeros((n, 3))

    # 每个相的修正量：相内每个样本各自到该相锚点的差。
    for start, end in stances:
        correction[start:end] = position[start:end].mean(axis=0) - position[start:end]

    # 摆动相：在相邻两个支撑相的边界修正量之间线性插值。修正量而不是位置本身做插值 ——
    # 位置在摆动相里本来就该变化，被插值抹平的必须只有"误差"这一部分。
    for (_, end_prev), (start_next, _) in pairwise(stances):
        gap = start_next - end_prev
        if gap <= 0:
            continue
        left = correction[end_prev - 1]
        right = correction[start_next]
        weights = np.arange(1, gap + 1, dtype=np.float64) / (gap + 1)
        correction[end_prev:start_next] = left + weights[:, None] * (right - left)

    # 两端保持常数，见 docstring。
    correction[: stances[0][0]] = correction[stances[0][0]]
    correction[stances[-1][1] :] = correction[stances[-1][1] - 1]

    anchored = position + correction
    before = float(np.linalg.norm(position[-1] - position[0]))
    after = float(np.linalg.norm(anchored[-1] - anchored[0]))

    navigation_out = NavResult(
        t=navigation.t,
        q=navigation.q,
        v=navigation.v,
        p=anchored,
        bg=navigation.bg,
        ba=navigation.ba,
        zupt=navigation.zupt,
        stances=list(navigation.stances),
        degraded=navigation.degraded,
        score=navigation.score,
    )
    report = AnchorReport(
        stances=len(stances),
        median_creep_before=float(np.median(creep_before)),
        max_creep_before=float(np.max(creep_before)),
        max_position_shift=float(np.max(np.linalg.norm(correction, axis=1))),
        total_displacement_change=abs(after - before),
    )
    return AnchorResult(navigation_out, report)
