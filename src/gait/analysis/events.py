"""步态事件分割与时空/速度参数。契约 §1 的 `analysis/events.py`（F4.1）。

PRD §13 的 v1 指标由这里产出：步长、步速、步频、支撑相/摆动相占比、双支撑期占比。

## 一、为什么必须细化 IC/TO，以及细化到什么程度

整体设计 §6.1 说「直接用 ZUPT 区间边界当作 IC/TO 会有 **10~30 ms** 系统性偏差」。
实测下来那个估计**偏小了大约一倍**：

| 步频 | 支撑相占比 | IC 迟到 | TO 提前 |
| --- | --- | --- | --- |
| 90 | 0.60 | +48.2 ms | −48.3 ms |
| 108 | 0.60 | +51.8 ms | −51.7 ms |
| 125 | 0.65 | +55.0 ms | −54.0 ms |

标准差只有 1.4 ms —— 它是**系统性**的，不是噪声。而验收线是 20 ms，所以原始边界差了
2.5 倍，细化不是锦上添花。

（这与 RAY-211 从另一头量到的是同一件事：那里测的是"检出的支撑相比生理支撑相每侧短
约 50 ms"，正好对得上。）

## 二、细化的做法：逐样本外推，而不是乘一个系数

偏差随检测窗口变，很自然会想到"按窗口长度乘个系数修回去"。实测那个系数**在 1.27~1.38
之间漂**（窗口 7~41 个样本），不是常数 —— 拟出来的任何一个值都只是当前这套参数下的
标定，有人调 `zupt_window_samples` 的那天它会悄悄失效。

真正的做法建在一条观察上：**窗口化的检测保守但可靠，而保守正来自那个窗口。** 既然手上
已经有一个被确认的支撑相，边缘细化就不再需要鲁棒性 —— 可以用**逐样本**的判据往两侧推，
推到第一个不静止的样本为止。鲁棒性由窗口化的那一步提供，精度由逐样本的这一步提供，
两步各司其职。

实测细化后的误差：

| 条件 | 原始 IC | 细化 IC | 细化 \\|误差\\| 最大 |
| --- | --- | --- | --- |
| 无噪声 ~ BS-BT91 三倍噪声，三档步频 | +48~55 ms | +0.0~1.8 ms | **≤ 4.0 ms** |

而最有说服力的一条是**它与窗口无关**：

| `zupt_window_samples` | 原始 IC | 细化 IC |
| --- | --- | --- |
| 7 | +22.3 ms | +1.0 ms |
| 15 | +51.8 ms | +1.0 ms |
| 31 | +104.4 ms | +1.0 ms |
| 41 | +129.9 ms | +1.0 ms |

原始偏差跟着窗口从 22 ms 涨到 130 ms，细化后纹丝不动。系数法做不到这一点。

## 三、双支撑期只能由细化后的事件算

RAY-205 实测：直接拿两只脚的 ZUPT 区间取交集，双支撑期占比读到**约 2%**，而生理值是
10~25% —— 低一个数量级。原因就是 §1 那 50 ms：两只脚各削一次，重叠区几乎被削光。

所以 `double_support()` 只接受细化后的事件。`core/dualfoot.py` 的
`check_alternating_stance()` 也输出一个 `double_support_fraction`，那个**只用于粗判
有没有严重异常**，不是指标 —— 两者不要混用。

## 四、跨足指标强制附带同步质量

PRD §13：双支撑期占比是**跨足**指标，「强制附同步质量标注」。所以 `double_support()`
的签名里 `sync_quality` 不是可选参数：跨足指标离开同步质量就没有意义 —— 一个 80 ms 的
跨足偏差会让双支撑期占比整体挪 8 个百分点，而读数本身看不出任何异常。

## 五、步宽不在这里，因为它不可测

两条足部轨迹各自从自己的原点起算，初始足间偏置是未知量，而步宽恰好等于那个偏置加上
航向发散（RAY-260 的证明）。PRD §13 的 v1 指标清单里没有步宽，本模块因此不产出它。
若将来有人要加回来，先读 RAY-260。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from itertools import pairwise
from typing import Any, Final

import numpy as np

from gait.config import AlgoConfig
from gait.contracts import FootLabel, GaitCycle
from gait.core.ins import GRAVITY_STANDARD

#: 事件报告的结构版本。
EVENTS_VERSION: Final[str] = "1.0"

#: 一个 stride 里有几步。步频按步算，stride 按同一只脚的两次触地算。
STEPS_PER_STRIDE: Final[int] = 2


class EventError(ValueError):
    """事件分割的输入非法。"""


@dataclass(frozen=True)
class StanceEdges:
    """一个细化之后的支撑相。索引是**样本下标**，闭开区间 `[ic, to)`。"""

    #: 初始触地的样本下标。
    ic: int
    #: 足尖离地的样本下标（不含）。
    to: int
    #: 细化把边缘各推了多少个样本。它是"原始检测有多保守"的直接读数。
    expanded_start: int
    expanded_stop: int

    @property
    def samples(self) -> int:
        return self.to - self.ic


def refine_stance_edges(
    acc: np.ndarray,
    gyr: np.ndarray,
    stances: Sequence[tuple[int, int]],
    cfg: AlgoConfig | None = None,
) -> list[StanceEdges]:
    """把 ZUPT 区间的边缘推到真实的静止边界。

    判据是**逐样本**的（没有窗口），因为鲁棒性已经由窗口化的检测提供了 —— 这里的
    每个区间都是被确认过的支撑相，往外推一两个样本不会把整段判错。理由与实测见模块
    文档 §2。

    外推有上限（`1.5 × zupt_window_samples`）：没有上限的话，一个真正长时间的静止段
    （受试者站着不动）会让相邻两个"支撑相"连成一片。
    """
    cfg = cfg or AlgoConfig()
    acc = np.asarray(acc, dtype=np.float64)
    gyr = np.asarray(gyr, dtype=np.float64)
    if acc.ndim != 2 or acc.shape[1] != 3:
        raise EventError(f"acc 应为 (n,3)，收到 shape={acc.shape}")
    if gyr.shape != acc.shape:
        raise EventError(f"gyr 与 acc 的形状必须一致：{gyr.shape} vs {acc.shape}")

    # 逐样本的静止判据。阈值沿用 `core/zupt.py` 的粗筛阈值 —— 那一层本来就是
    # 逐样本的，只是在那里被窗口统计盖住了。
    quiet = (
        np.abs(np.linalg.norm(acc, axis=1) - GRAVITY_STANDARD) < cfg.zupt_acc_threshold
    ) & (np.linalg.norm(gyr, axis=1) < cfg.zupt_gyr_threshold)

    limit = max(round(1.5 * cfg.zupt_window_samples), 1)
    total = acc.shape[0]
    refined: list[StanceEdges] = []
    for start, stop in stances:
        if not 0 <= start < stop <= total:
            raise EventError(f"支撑相区间越界：[{start}, {stop}) 不在 [0, {total}] 内")
        new_start = start
        while start - new_start < limit and new_start - 1 >= 0 and quiet[new_start - 1]:
            new_start -= 1
        new_stop = stop
        while new_stop - stop < limit and new_stop < total and quiet[new_stop]:
            new_stop += 1
        refined.append(
            StanceEdges(
                ic=new_start,
                to=new_stop,
                expanded_start=start - new_start,
                expanded_stop=new_stop - stop,
            )
        )
    return refined


@dataclass(frozen=True)
class DoubleSupport:
    """双支撑期。**跨足指标，强制附同步质量**（PRD §13）。"""

    #: 每个双支撑相位的时长，s。
    phases: np.ndarray
    mean: float
    #: 占一个 step 时长的比例。生理值 10~25%（整体设计 §6.2）。
    fraction: float
    #: 同步质量标注。**不是可选的** —— 见模块文档 §4。
    sync_quality: dict[str, Any]

    def snapshot(self) -> dict[str, Any]:
        return {
            "phases": [float(value) for value in self.phases],
            "mean": self.mean,
            "fraction": self.fraction,
            "sync_quality": dict(self.sync_quality),
            "version": EVENTS_VERSION,
        }


def _cycles_from_edges(
    foot: FootLabel,
    t: np.ndarray,
    position: np.ndarray | None,
    edges: Sequence[StanceEdges],
) -> list[GaitCycle]:
    cycles: list[GaitCycle] = []
    for index, (current, following) in enumerate(pairwise(edges)):
        t_ic = float(t[current.ic])
        t_to = float(t[min(current.to, t.size - 1)])
        t_ic_next = float(t[following.ic])
        if not t_ic < t_to < t_ic_next:
            # 事件时刻必须严格递增，否则 `GaitCycle` 自己会拒绝。出现这种情况说明
            # 上游把两个支撑相连成了一片 —— 跳过而不是构造一条非法的周期。
            continue
        stride_time = t_ic_next - t_ic
        stance_time = t_to - t_ic
        swing_time = t_ic_next - t_to
        if position is None:
            stride_length = float("nan")
        else:
            start = position[current.ic, :2]
            end = position[following.ic, :2]
            stride_length = float(np.linalg.norm(end - start))
        speed = stride_length / stride_time if stride_time > 0 else float("nan")
        cycles.append(
            GaitCycle(
                foot=foot,
                idx=index,
                t_ic=t_ic,
                t_to=t_to,
                t_ic_next=t_ic_next,
                stride_length=stride_length,
                stride_time=stride_time,
                gait_speed=speed,
                stance_time=stance_time,
                swing_time=swing_time,
                stance_ratio=100.0 * stance_time / stride_time,
                toe_clearance=float("nan"),
                strike_angle=float("nan"),
                valid=True,
                confidence="normal",
            )
        )
    return cycles


def segment_cycles(
    foot: FootLabel,
    t: np.ndarray,
    acc: np.ndarray,
    gyr: np.ndarray,
    stances: Sequence[tuple[int, int]],
    *,
    position: np.ndarray | None = None,
    cfg: AlgoConfig | None = None,
) -> tuple[list[GaitCycle], list[StanceEdges]]:
    """从零速区间切出步态周期。返回 `(周期, 细化后的支撑相)`。

    `position` 是导航系位置（`NavResult.p`）。没有它也能算全部**时间**参数 ——
    步长与步速会是 `nan`，而不是一个编造的数。区分这两者很重要：一次只做事件分割的
    调用不该被迫先跑一遍惯导。
    """
    times = np.asarray(t, dtype=np.float64)
    if times.ndim != 1:
        raise EventError(f"t 应为一维，收到 shape={times.shape}")
    if times.size != np.asarray(acc).shape[0]:
        raise EventError("t 与 acc 的长度不一致")
    if position is not None:
        position = np.asarray(position, dtype=np.float64)
        if position.shape[0] != times.size:
            raise EventError("position 与 t 的长度不一致")

    edges = refine_stance_edges(acc, gyr, stances, cfg)
    if len(edges) < 2:
        return [], edges
    return _cycles_from_edges(foot, times, position, edges), edges


def double_support(
    left: Sequence[GaitCycle],
    right: Sequence[GaitCycle],
    *,
    sync_quality: dict[str, Any],
) -> DoubleSupport:
    """双支撑期。**只接受细化后的事件**，理由见模块文档 §3。

    `sync_quality` 是**必需的关键字参数**，不是可选装饰：跨足指标离开同步质量就没有
    意义 —— 一个 80 ms 的跨足偏差会让这个占比整体挪 8 个百分点，而读数本身看不出任何
    异常（PRD §13「强制附同步质量标注」）。
    """
    if sync_quality is None:
        raise EventError(
            "双支撑期是跨足指标，必须附同步质量标注（PRD §13）。"
            "没有它，读数无法与另一次采集比较。"
        )
    if not left or not right:
        raise EventError("双支撑期需要两只脚都有步态周期")

    intervals = sorted(
        [(cycle.t_ic, cycle.t_to, "L") for cycle in left]
        + [(cycle.t_ic, cycle.t_to, "R") for cycle in right]
    )
    phases: list[float] = []
    for (_, to_first, foot_first), (ic_second, _, foot_second) in pairwise(intervals):
        if foot_first == foot_second:
            # 同一只脚连着两个支撑相 —— 另一只脚在这中间没有被检出。跳过而不是凑数。
            continue
        phases.append(to_first - ic_second)

    values = np.array(phases, dtype=np.float64)
    step_times = np.array(
        [cycle.stride_time / STEPS_PER_STRIDE for cycle in [*left, *right]]
    )
    step_time = float(np.median(step_times)) if step_times.size else float("nan")
    mean = float(values.mean()) if values.size else float("nan")
    return DoubleSupport(
        phases=values,
        mean=mean,
        fraction=mean / step_time if step_time > 0 else float("nan"),
        sync_quality=dict(sync_quality),
    )


@dataclass(frozen=True)
class SpatioTemporal:
    """一只脚的时空/速度参数汇总。PRD §13 的 v1 指标。"""

    foot: FootLabel
    cycles: int
    #: 步频，步/分。按**步**算而不是按 stride 算 —— 临床习惯如此，且 PRD 用的是"步频"。
    cadence: float
    stride_length: float
    gait_speed: float
    stance_ratio: float
    swing_ratio: float
    stride_time: float

    def snapshot(self) -> dict[str, Any]:
        return {
            "foot": self.foot,
            "cycles": self.cycles,
            "cadence": self.cadence,
            "stride_length": self.stride_length,
            "gait_speed": self.gait_speed,
            "stance_ratio": self.stance_ratio,
            "swing_ratio": self.swing_ratio,
            "stride_time": self.stride_time,
            "version": EVENTS_VERSION,
        }


def summarize(foot: FootLabel, cycles: Sequence[GaitCycle]) -> SpatioTemporal:
    """把逐周期的值汇总成一只脚的参数。

    用**中位数**而不是均值：一次绊步或一个被误分的周期会把均值拉走，而步态参数的
    临床解读建立在"典型的一步"上，不是"平均的一步"。
    """
    if not cycles:
        raise EventError("没有步态周期，时空参数无从谈起")
    stride_time = float(np.median([cycle.stride_time for cycle in cycles]))
    lengths = np.array([cycle.stride_length for cycle in cycles])
    speeds = np.array([cycle.gait_speed for cycle in cycles])
    stance = float(np.median([cycle.stance_ratio for cycle in cycles]))
    return SpatioTemporal(
        foot=foot,
        cycles=len(cycles),
        cadence=60.0 * STEPS_PER_STRIDE / stride_time if stride_time > 0 else float("nan"),
        stride_length=float(np.nanmedian(lengths)) if lengths.size else float("nan"),
        gait_speed=float(np.nanmedian(speeds)) if speeds.size else float("nan"),
        stance_ratio=stance,
        swing_ratio=100.0 - stance,
        stride_time=stride_time,
    )
