"""变异性、疲劳衰减、对称性与转身指标。契约 §1 的 `analysis/variability.py`（F4.3）。

PRD §7.4、§13：定时步行测试的差异化指标集。

## 一、CV 的不确定度，以及 16 步这个门槛是量出来的

验收标准要求「60 s 配置下 CV 正常输出且 `grade` 反映样本量少」。要让 `grade` 有意义，
先得知道"少"到什么程度会让 CV 失去意义。

CV 的估计误差有解析式 `σ_CV ≈ CV/√(2N)`。蒙特卡洛（20000 次重抽，真实 CV = 3%）确认
了它，并给出了实际的散布：

| N 步 | 估计均值 | 标准差 | p5~p95 宽度 | 相对不确定度 |
| --- | --- | --- | --- | --- |
| 6 | 2.86% | 0.93% | 3.03% | **32.5%** |
| 10 | 2.92% | 0.70% | 2.29% | **23.8%** |
| 16 | 2.96% | 0.55% | 1.79% | 18.5% |
| 40 | 2.98% | 0.34% | 1.11% | 11.3% |
| 160 | 2.99% | 0.17% | 0.55% | 5.6% |

但"相对不确定度多少算大"仍然是个主观判断。所以门槛不定在不确定度上，定在**能不能分辨
一倍的差异**上 —— 那是个有临床含义的问题：

| N 步 | CV=3% 的 p95 | CV=6% 的 p5 | 分得开吗 |
| --- | --- | --- | --- |
| 6 | 4.47% | 2.89% | **否** |
| 10 | 4.12% | 3.60% | **否** |
| 16 | 3.87% | 4.18% | 是 |
| 25 | 3.69% | 4.57% | 是 |

**16 步是分界。** 少于 16 步时，3% 与 6% 的 CV（一倍之差，临床上截然不同）的 90% 区间
互相重叠 —— 报出来的数分不出这两种情况。

而 RAY-215 实测：4 米往返 60 s、每端剔 1 步之后只剩 **10~16 步**。也就是说 **PRD 的
60 s 配置恰好落在这条分界线上或之下**。这不是缺陷，是 AC-15 早就预料到的情形 ——
指标照常输出，由 `grade` 说明它能支撑什么结论。

## 二、疲劳衰减只在 180 s 配置输出，且是硬拒绝

PRD §7.4 写明这一条。理由在 §1 的数上：疲劳衰减比的是前 1/3 与后 1/3 的步速，60 s
配置下每一份只有三四步，两个三四步的均值之差几乎全是噪声。

所以传入非 180 s 的配置时**抛错，不返回 `None`** —— `None` 会被下游渲染成"—"，与
"这项测了但没有变化"看起来一样，而两者是完全不同的结论。

## 三、跨足的时序成分强制附同步质量

与 `analysis/events.py` 的双支撑期同一条规则（PRD §13）。对称性指数里，**步长**成分
是各足自算的、不受跨足同步影响；**支撑相时长**成分同理。但只要把两足的时序放到同一条
轴上比（例如步时对称性），同步偏差就直接进入结果，所以那一类必须带标注。

本模块把这件事做在类型上：`symmetry()` 返回的每一项都带 `cross_foot` 标志，跨足的那些
在没有 `sync_quality` 时构造不出来。

## 四、CV 要在**分段之后**算

RAY-215 实测：不分离时步长 CV 读到 **71.0%**（转身步的步长只有 0.085 m），分离后是
0.0%。中位数对转身免疫，离散度完全不免疫 —— 本模块的输入必须是已经筛过的中段步。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from itertools import pairwise
from typing import Any, Final

import numpy as np

from gait.contracts import GaitCycle

#: 报告的结构版本。
VARIABILITY_VERSION: Final[str] = "1.0"

#: CV 可用所需的最少步数。**量出来的，不是拍的** —— 见模块文档 §1。
#:
#: 少于这个数时，3% 与 6% 的 CV（一倍之差）的 90% 区间互相重叠，报出来的数分不出这
#: 两种情况。
MIN_STEPS_FOR_CV: Final[int] = 16

#: 疲劳衰减只在这个时长的配置下输出（PRD §7.4）。
FATIGUE_PROTOCOL_SECONDS: Final[int] = 180

GRADE_NORMAL: Final[str] = "normal"
GRADE_DEGRADED: Final[str] = "degraded"
GRADE_INSUFFICIENT: Final[str] = "insufficient"


class VariabilityError(ValueError):
    """变异性指标的输入非法，或该指标在当前配置下不该输出。"""


@dataclass(frozen=True)
class Variability:
    """一个 CV 指标。**值与样本量一起走** —— PRD §7.4 要求随值输出 `n_steps`。"""

    name: str
    value: float
    n_steps: int
    #: CV 自身的标准误，`CV/√(2N)`。蒙特卡洛确认过（见模块文档 §1）。
    standard_error: float
    grade: str

    @property
    def relative_uncertainty(self) -> float:
        """标准误占 CV 的比例。N=10 时约 24%。"""
        return self.standard_error / self.value if self.value > 0 else float("nan")

    def snapshot(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "value": self.value,
            "n_steps": self.n_steps,
            "standard_error": self.standard_error,
            "relative_uncertainty": self.relative_uncertainty,
            "grade": self.grade,
        }


def _grade_for(n_steps: int) -> str:
    if n_steps < 3:
        return GRADE_INSUFFICIENT
    if n_steps < MIN_STEPS_FOR_CV:
        return GRADE_DEGRADED
    return GRADE_NORMAL


def coefficient_of_variation(name: str, values: Sequence[float]) -> Variability:
    """变异系数。**输入必须是已经筛过的中段步**（见模块文档 §4）。

    少于 3 个样本时不给数字：两个样本的"标准差"只是它们之差的一半，那不是变异性。
    """
    array = np.asarray([value for value in values if np.isfinite(value)], dtype=np.float64)
    n_steps = int(array.size)
    if n_steps < 3:
        return Variability(
            name=name,
            value=float("nan"),
            n_steps=n_steps,
            standard_error=float("nan"),
            grade=GRADE_INSUFFICIENT,
        )
    mean = float(array.mean())
    if mean <= 0:
        raise VariabilityError(
            f"{name} 的均值为 {mean}，CV 无从谈起 —— 变异系数是相对量。"
        )
    value = float(array.std(ddof=1) / mean)
    return Variability(
        name=name,
        value=value,
        n_steps=n_steps,
        standard_error=value / np.sqrt(2 * n_steps),
        grade=_grade_for(n_steps),
    )


@dataclass(frozen=True)
class FatigueDecline:
    """疲劳衰减：后 1/3 与前 1/3 的步速差。**仅 180 s 配置输出**（PRD §7.4）。"""

    first_third_speed: float
    last_third_speed: float
    #: 后 1/3 相对前 1/3 的变化率。负值表示变慢。
    decline: float
    n_first: int
    n_last: int
    grade: str

    def snapshot(self) -> dict[str, Any]:
        return {
            "first_third_speed": self.first_third_speed,
            "last_third_speed": self.last_third_speed,
            "decline": self.decline,
            "n_first": self.n_first,
            "n_last": self.n_last,
            "grade": self.grade,
        }


def fatigue_decline(
    cycles: Sequence[GaitCycle], *, protocol_seconds: int
) -> FatigueDecline:
    """疲劳衰减。**非 180 s 配置直接抛错。**

    抛错而不是返回 `None`：`None` 会被下游渲染成"—"，与"这项测了但没有变化"看起来
    一样，而两者是完全不同的结论。60 s 配置下前后各只有三四步，两个三四步的均值之差
    几乎全是噪声（见模块文档 §2）。
    """
    if protocol_seconds != FATIGUE_PROTOCOL_SECONDS:
        raise VariabilityError(
            f"疲劳衰减只在 {FATIGUE_PROTOCOL_SECONDS} s 配置下输出，当前是 "
            f"{protocol_seconds} s（PRD §7.4）。"
            "短配置下前后各只有三四步，两个均值之差几乎全是噪声 —— "
            "不返回 None，因为 None 会被渲染成'—'，看起来像'测了但没变化'。"
        )
    speeds = np.asarray(
        [cycle.gait_speed for cycle in cycles if np.isfinite(cycle.gait_speed)],
        dtype=np.float64,
    )
    if speeds.size < 6:
        raise VariabilityError(
            f"疲劳衰减需要至少 6 个有效周期（前后各 2 个），收到 {speeds.size} 个"
        )
    third = speeds.size // 3
    first = speeds[:third]
    last = speeds[-third:]
    baseline = float(first.mean())
    if baseline <= 0:
        raise VariabilityError("前 1/3 的平均步速非正，衰减率无从谈起")
    return FatigueDecline(
        first_third_speed=baseline,
        last_third_speed=float(last.mean()),
        decline=float((last.mean() - baseline) / baseline),
        n_first=int(first.size),
        n_last=int(last.size),
        grade=_grade_for(int(speeds.size)),
    )


@dataclass(frozen=True)
class SymmetryIndex:
    """一项对称性指数。

    `SI = |L − R| / (0.5·(L + R))`，取绝对值 —— 对称性问的是"差多少"，不是"哪边大"。
    哪边大由 `left` / `right` 两个原值回答。
    """

    name: str
    left: float
    right: float
    index: float
    #: 这一项是不是跨足时序量。为真时 `sync_quality` 必须非空。
    cross_foot: bool
    sync_quality: dict[str, Any] | None
    n_left: int
    n_right: int

    def snapshot(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "left": self.left,
            "right": self.right,
            "index": self.index,
            "cross_foot": self.cross_foot,
            "sync_quality": dict(self.sync_quality) if self.sync_quality else None,
            "n_left": self.n_left,
            "n_right": self.n_right,
        }


def _symmetry(
    name: str,
    left: Sequence[float],
    right: Sequence[float],
    *,
    cross_foot: bool,
    sync_quality: dict[str, Any] | None,
) -> SymmetryIndex:
    if cross_foot and not sync_quality:
        raise VariabilityError(
            f"{name} 是跨足时序指标，必须附同步质量标注（PRD §13）。"
            "同步偏差直接进入这个数，而读数本身看不出任何异常。"
        )
    left_values = np.asarray([v for v in left if np.isfinite(v)], dtype=np.float64)
    right_values = np.asarray([v for v in right if np.isfinite(v)], dtype=np.float64)
    if not left_values.size or not right_values.size:
        raise VariabilityError(f"{name} 的对称性需要两只脚都有有效值")
    left_mean = float(np.median(left_values))
    right_mean = float(np.median(right_values))
    total = left_mean + right_mean
    if total <= 0:
        raise VariabilityError(f"{name} 的左右之和非正，对称性指数无从谈起")
    return SymmetryIndex(
        name=name,
        left=left_mean,
        right=right_mean,
        index=float(abs(left_mean - right_mean) / (0.5 * total)),
        cross_foot=cross_foot,
        sync_quality=dict(sync_quality) if sync_quality else None,
        n_left=int(left_values.size),
        n_right=int(right_values.size),
    )


def symmetry(
    left: Sequence[GaitCycle],
    right: Sequence[GaitCycle],
    *,
    sync_quality: dict[str, Any] | None = None,
) -> list[SymmetryIndex]:
    """步长与支撑相的对称性指数。

    **步长与支撑相时长都是足内量** —— 每只脚自己算自己的，跨足同步偏差够不着它们，
    所以这两项不需要 `sync_quality`。这一点值得说清楚，因为"对称性"听起来像跨足量：
    它比较的是两只脚，但比较的**两个输入各自是足内的**。

    真正的跨足时序量（步时对称性、双支撑期左右差）会进入 `analysis/events.py` 的
    `double_support` 与后续的时序指标，那些必须带标注。这里给出 `sync_quality` 时会
    原样带上，好让报告层不必再去拼。
    """
    if not left or not right:
        raise VariabilityError("对称性需要两只脚都有步态周期")
    return [
        _symmetry(
            "stride_length",
            [cycle.stride_length for cycle in left],
            [cycle.stride_length for cycle in right],
            cross_foot=False,
            sync_quality=sync_quality,
        ),
        _symmetry(
            "stance_time",
            [cycle.stance_time for cycle in left],
            [cycle.stance_time for cycle in right],
            cross_foot=False,
            sync_quality=sync_quality,
        ),
    ]


def step_time_symmetry(
    left: Sequence[GaitCycle],
    right: Sequence[GaitCycle],
    *,
    sync_quality: dict[str, Any],
) -> SymmetryIndex:
    """步时对称性 —— **真正的跨足时序量**，`sync_quality` 必填。

    它比的是"左脚触地到右脚触地"与"右脚触地到左脚触地"，两个量各跨一次足。一个恒定
    的跨足偏差 Δ 让前者加 Δ、后者减 Δ —— 与 RAY-211 里那个配对双支撑差是同一个机制，
    所以它对同步偏差**极其敏感**，正因如此它才必须带标注。
    """
    if not sync_quality:
        raise VariabilityError(
            "步时对称性是跨足时序量，必须附同步质量标注（PRD §13）。"
            "一个恒定的跨足偏差会让它整体偏移，而读数本身看不出任何异常。"
        )
    events = sorted(
        [(cycle.t_ic, "L") for cycle in left] + [(cycle.t_ic, "R") for cycle in right]
    )
    left_to_right: list[float] = []
    right_to_left: list[float] = []
    for (first, foot_first), (second, foot_second) in pairwise(events):
        if foot_first == foot_second:
            continue
        (left_to_right if foot_first == "L" else right_to_left).append(second - first)
    if not left_to_right or not right_to_left:
        raise VariabilityError("步时对称性需要左右交替的触地序列")
    return _symmetry(
        "step_time",
        left_to_right,
        right_to_left,
        cross_foot=True,
        sync_quality=sync_quality,
    )


@dataclass(frozen=True)
class VariabilityReport:
    """一次会话的变异性指标集。"""

    stride_length_cv: Variability
    stride_time_cv: Variability
    symmetry: list[SymmetryIndex]
    turns: int
    mean_turn_duration: float
    fatigue: FatigueDecline | None
    version: str = VARIABILITY_VERSION

    @property
    def grade(self) -> str:
        """整体等级取最差的那一项 —— 一个可用的指标救不了一个不可用的。"""
        order = {GRADE_NORMAL: 0, GRADE_DEGRADED: 1, GRADE_INSUFFICIENT: 2}
        worst = max(
            (self.stride_length_cv.grade, self.stride_time_cv.grade),
            key=lambda item: order[item],
        )
        return worst

    def snapshot(self) -> dict[str, Any]:
        return {
            "stride_length_cv": self.stride_length_cv.snapshot(),
            "stride_time_cv": self.stride_time_cv.snapshot(),
            "symmetry": [item.snapshot() for item in self.symmetry],
            "turns": self.turns,
            "mean_turn_duration": self.mean_turn_duration,
            "fatigue": self.fatigue.snapshot() if self.fatigue else None,
            "grade": self.grade,
            "version": self.version,
        }


def analyse(
    left: Sequence[GaitCycle],
    right: Sequence[GaitCycle],
    *,
    turns: int = 0,
    mean_turn_duration: float = 0.0,
    protocol_seconds: int | None = None,
    sync_quality: dict[str, Any] | None = None,
) -> VariabilityReport:
    """汇总一次会话的变异性指标。

    **输入必须是已经过 `analysis/segments` 筛选的中段步。** 不筛的话 CV 会被转身步
    带到 71%（RAY-215 实测），而那个数看起来完全像一个"变异性极高"的病理步态。

    `protocol_seconds` 不是 180 时不算疲劳衰减，且**不报错** —— 这里是汇总入口，
    "这项不适用"是正常情况；单独调 `fatigue_decline()` 才抛错。
    """
    combined = [*left, *right]
    if not combined:
        raise VariabilityError("没有步态周期，变异性指标无从谈起")

    fatigue: FatigueDecline | None = None
    if protocol_seconds == FATIGUE_PROTOCOL_SECONDS:
        try:
            fatigue = fatigue_decline(combined, protocol_seconds=protocol_seconds)
        except VariabilityError:
            fatigue = None

    return VariabilityReport(
        stride_length_cv=coefficient_of_variation(
            "stride_length", [cycle.stride_length for cycle in combined]
        ),
        stride_time_cv=coefficient_of_variation(
            "stride_time", [cycle.stride_time for cycle in combined]
        ),
        symmetry=symmetry(left, right, sync_quality=sync_quality),
        turns=turns,
        mean_turn_duration=mean_turn_duration,
        fatigue=fatigue,
    )
