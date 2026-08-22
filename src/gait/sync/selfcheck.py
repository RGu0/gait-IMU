"""同步质量自检。契约 §1 的 `sync/selfcheck.py`（F3.4）。

PRD §8：同步失效**没有直接的在线信号** —— 两台设备各自的时基都自洽，错的是它们之间的
关系，而那个关系没有基准可比。所以只能反过来问：**如果同步是对的，步态在生理上说得
通吗？** 不说得通就标注可疑。

PRD §8 给了两条判据。实测下来两条都不能照字面用，而真正管用的那个量 PRD 没提。以下
三节是量出来的，不是设计出来的。

## 一、「左右步周期差 < 10%」对 offset 完全免疫

一个恒定 offset 把一只脚的所有事件整体平移，**它自己的 stride 周期一点不变**。实测：
offset 从 0 加到 200 ms，左右 stride 周期差恒为 3.24%，一动不动。

这条判据不是没用 —— 它抓的是**左右节律不对称**（拖步、疼痛回避）。但它抓不到同步
偏差，不能当同步自检的主判据。本模块保留它，但摆在正确的位置上。

## 二、「双支撑期应为正」会在正常快走上误报

双支撑期由**支撑相边界**算出，而 ZUPT 检出的边界不是生理边界：它只标真正静止的那
一段，触地与离地的过渡都被削掉。实测这个削减量**单侧约 50 ms**，且相当稳定：

| 步频 | 支撑相占比 | 真实双支撑 | ZUPT 测得 |
| --- | --- | --- | --- |
| 90 | 0.60 | 0.1333 | 0.0365 |
| 108 | 0.60 | 0.1111 | 0.0092 |
| 125 | 0.60 | 0.0960 | **−0.0150** |
| 140 | 0.60 | 0.0857 | **−0.0243** |

步频 125 步/分是一个寻常的快走速度，而在那里测得的双支撑期**已经是负的** —— 数据
完全正常、同步完全正确。照字面判「应为正」会把这些会话全部标成同步可疑。

所以本模块不判它的符号，而是把它作为**观测量**报出去，并给出扣除削减量之后的估计。

## 三、真正对 offset 敏感的量：配对双支撑差

把双支撑相位按「先离地的是哪只脚」分成两类。一个恒定 offset 让其中一类变长 Δ、另一类
变短 Δ，所以：

* 两类的**均值之差 = 2Δ** —— 精确线性，实测斜率恰为 2；
* 两类合起来的**均值几乎不变**：每个左前相位变短 Δ、每个右前相位变长 Δ，所以均值
  只随两类**个数之差**变化，变化量是 `Δ·(n_右前 − n_左前) / N`。实测 39 个相位、
  两类差一个时，80 ms 的 offset 只让均值动 2.05 ms —— 比配对差迟钝约 78 倍。这正是
  双支撑期均值检不出 offset 的原因；
* 上一节那个 50 ms 的削减量是**共模**的，在差分里精确抵消 —— 所以这个量与步频、
  与支撑相占比都无关。实测 Δ=0 时本底散布 < 4 ms（跨三档步频、两档占比）。

## 四、它与真实不对称的混淆，以及唯一的解法

一个坏消息：**左右支撑相时长不等，会产生与 offset 完全相同的读数。** 实测右足支撑相
占比 0.65（左 0.60）在零 offset 下给出 −55.6 ms 的配对差 —— 与 Δ = 27.8 ms 的同步偏差
无法区分。而病理步态本来就不对称，那正是本系统最需要工作的人群。

解法来自 offset 的定义本身：**它够不着足内的量。** 平移一只脚的全部事件，那只脚自己的
支撑相时长完全不变；而真实的不对称改变的正是足内时长。所以先用足内时长差预测配对差
应该是多少，**残差**才是同步偏差：

| 右足支撑占比 | 注入 Δ | 配对差 | 足内时长差 | 残差 | 应为 −2Δ | 误差 |
| --- | --- | --- | --- | --- | --- | --- |
| 0.60 | 0 | −0.1 | 0.0 | −0.1 | 0 | −0.1 |
| 0.60 | 30 ms | −60.1 | 0.0 | −60.1 | −60 | −0.1 |
| 0.65 | 30 ms | −115.6 | −50.0 | −65.6 | −60 | −5.6 |
| 0.72 | 60 ms | −253.6 | −122.5 | −131.1 | −120 | −11.1 |

即使在 0.72 对 0.60 这种严重不对称下，残差误差也只有 11 ms（等效 5.5 ms 的假 offset），
远在 PRD §8 容许的 ±10~30 ms 跨足不确定度之内。

## 五、什么时候必须拒绝给出估计

左右**步频不同**时，两只脚的相位关系随时间连续漂移 —— 此时根本不存在"一个恒定
offset"这种东西。测出来的那个数是窗口内的**平均**相位差，取决于起始相位与窗口长度，
本质上是任意的。

一开始我用「stride 周期差 > 10%」当闸门（PRD §8 的那个数）。**它不够。** 实测右足
stride 只长 2% 时，周期差是 2.23%、稳稳在闸门之内，而估计出来的 offset 是 110 ms ——
一个完全编造的数字，还刚好大到能触发告警。

正确的问法不是"两只脚的周期差多少"，而是"**这个 offset 在整段里稳不稳**"。前后半程
各估一次，比较两者：

| 情形 | 前半 | 后半 | 不一致 |
| --- | --- | --- | --- |
| 注入 30 ms | 30.1 | 30.1 | **0.0** |
| 注入 120 ms | 120.1 | 120.1 | **0.0** |
| 不对称步态（占比 0.72 对 0.60） | 5.6 | 5.6 | **0.0** |
| 右 stride 长 2% | 110.6 | 327.2 | 216.7 |
| 右 stride 长 8% | 96.9 | 131.9 | 35.0 |
| 右 stride 长 20% | 57.8 | −4.1 | 61.8 |

真恒定的一律读 0.0，漂移的最小也有 35.0 —— 中间的空当足够宽。所以可估性由这个一致性
判据决定；stride 周期差降为**报告出去的不对称指标**（PRD §8 要的那个），不再当闸门。

## 不拦截

PRD §8：「不拦截，进 `sync_quality`」。本模块只标注，且标注进的是会话与跨足指标 ——
单足指标不受跨足同步影响，标它是误伤。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import pairwise
from typing import Any, Final

import numpy as np

from gait.config import AlgoConfig

#: `sync_quality` 的结构版本。它进 `SessionMeta.sync_quality`（PRD §13）。
SYNC_QUALITY_VERSION: Final[str] = "1.0"

#: PRD §8 要求的埋点名。上报通道属 RAY-227，本模块只负责触发。
TELEMETRY_EVENT: Final[str] = "sync_selfcheck_flagged"

#: 标注可疑的理由。它们进 `SyncQuality.reasons`，也进埋点载荷。
REASON_OFFSET: Final[str] = "cross_foot_offset"
REASON_CADENCE: Final[str] = "cadence_mismatch"
REASON_DRIFTING: Final[str] = "offset_not_constant"
REASON_TOO_FEW_PHASES: Final[str] = "too_few_double_support_phases"


class SelfCheckError(ValueError):
    """自检的输入非法。"""


@dataclass(frozen=True)
class DoubleSupport:
    """双支撑期的观测量。

    **这里的每一个数都是 ZUPT 边界下的读数，不是生理值。** ZUPT 单侧削掉约 50 ms，
    所以 `mean` 系统性地比生理双支撑期小约 100 ms，快走时甚至为负。模块文档 §2 有
    实测表。要拿它当生理指标用，得先扣掉削减量 —— 那属 RAY-216，不在这里。
    """

    #: 左足先离地的那一类相位的均值，s。
    left_leading: float
    #: 右足先离地的那一类相位的均值，s。
    right_leading: float
    #: 两类合起来的均值，s。**它几乎看不见 offset**：一类变长多少，另一类就变短多少，
    #: 残余只来自两类个数之差（`Δ·(n_右前 − n_左前) / N`）。实测比配对差迟钝约 78 倍。
    mean: float
    #: 占一个 step 时长的比例。负值不代表异常，见 §2。
    fraction: float
    #: 参与统计的相位个数（两类合计）。
    phases: int
    #: 其中读数为负的个数。**它是观测，不是告警** —— 正常快走就会出现。
    negative_phases: int

    @property
    def leading_difference(self) -> float:
        """两类均值之差，s。恒定 offset 下它等于 2Δ。"""
        return self.left_leading - self.right_leading


@dataclass(frozen=True)
class SyncQuality:
    """同步质量自检的结论。进 `SessionMeta.sync_quality`（PRD §13）。"""

    #: 估计的跨足时间偏差，s。左足事件相对右足**早**多少为正。
    #:
    #: `None` 表示**不可估计**，不是"零偏差"。左右步频不同时两足相位持续漂移，
    #: 根本不存在一个恒定 offset —— 那时给数字是编造。见模块文档 §5。
    offset_estimate: float | None
    #: 左右 stride 周期之差占均值的比例。**它对 offset 免疫**，抓的是节律不对称。
    stride_period_difference: float
    #: 前后半程两次估计之差的绝对值，s。它回答的是「这个 offset 稳不稳」。
    #:
    #: 真恒定的 offset（含严重不对称步态）实测一律读 0.0；相位在漂的最小读 35 ms。
    #: 可估性由它决定，不由 stride 周期差决定 —— 见模块文档 §5。
    offset_consistency: float
    #: 左右足**自己**的支撑相时长之差，s。offset 够不着它，所以它是不对称的干净读数。
    within_foot_stance_difference: float
    double_support: DoubleSupport

    #: 估计是否成立。为假时 `offset_estimate` 必为 `None`。
    determinate: bool
    #: 是否标注「同步可疑」。**标注不拦截**（PRD §8）。
    flagged: bool
    reasons: list[str] = field(default_factory=list)
    version: str = SYNC_QUALITY_VERSION

    @property
    def telemetry(self) -> dict[str, Any] | None:
        """`sync_selfcheck_flagged` 的载荷；未标注时为 `None`。

        载荷里带上估计值与两个免疫量，是为了让上报的数据能**事后区分**是哪一类问题：
        只报一个布尔值的话，真同步故障与严重不对称步态在后台看起来一模一样。
        """
        if not self.flagged:
            return None
        return {
            "event": TELEMETRY_EVENT,
            "reasons": list(self.reasons),
            "offset_estimate": self.offset_estimate,
            "stride_period_difference": self.stride_period_difference,
            "offset_consistency": self.offset_consistency,
            "within_foot_stance_difference": self.within_foot_stance_difference,
            "double_support_phases": self.double_support.phases,
            "version": self.version,
        }

    def snapshot(self) -> dict[str, Any]:
        """写入 `SessionMeta.sync_quality` 的普通字典。"""
        return {
            "offset_estimate": self.offset_estimate,
            "stride_period_difference": self.stride_period_difference,
            "offset_consistency": self.offset_consistency,
            "within_foot_stance_difference": self.within_foot_stance_difference,
            "double_support": {
                "left_leading": self.double_support.left_leading,
                "right_leading": self.double_support.right_leading,
                "mean": self.double_support.mean,
                "fraction": self.double_support.fraction,
                "phases": self.double_support.phases,
                "negative_phases": self.double_support.negative_phases,
            },
            "determinate": self.determinate,
            "flagged": self.flagged,
            "reasons": list(self.reasons),
            "version": self.version,
        }


Span = tuple[float, float]


def stance_spans(t: np.ndarray, stances: list[tuple[int, int]]) -> list[Span]:
    """把 `StanceDetection.stances` 的索引区间换成时刻区间。

    索引对不齐两只脚（各自的段长不同），时刻才对得齐 —— 而本模块要比的全是**时刻**。
    """
    times = np.asarray(t, dtype=np.float64)
    if times.ndim != 1:
        raise SelfCheckError(f"t 应为一维，收到 shape={times.shape}")
    spans: list[Span] = []
    for start, stop in stances:
        if not 0 <= start < stop <= times.size:
            raise SelfCheckError(f"支撑相区间越界：[{start}, {stop}) 不在 [0, {times.size}] 内")
        spans.append((float(times[start]), float(times[stop - 1])))
    return spans


def drop_still_lead(spans: list[Span], cfg: AlgoConfig | None = None) -> list[Span]:
    """去掉起步前的静止前导。

    RAY-202 的初始对准需要一段静止前导，而 ZUPT 会把它检成**一个很长的支撑相**。
    它不是一步：留着它，它的"触地时刻"会把整个左右配对错开一位，配对差随即失去意义。

    判据是"比典型支撑相长得离谱"（默认 2.5 倍），用中位数作典型值 —— 前导本身也在
    样本里，用均值会被它自己拉高。
    """
    cfg = cfg or AlgoConfig()
    if not spans:
        return []
    typical = float(np.median([stop - start for start, stop in spans]))
    remaining = list(spans)
    while remaining and (remaining[0][1] - remaining[0][0]) > cfg.selfcheck_still_lead_factor * typical:
        remaining = remaining[1:]
    return remaining


def stride_periods(spans: list[Span]) -> np.ndarray:
    """同一只脚相邻两次触地之间的时长，s。"""
    if len(spans) < 2:
        return np.zeros(0)
    return np.diff([start for start, _ in spans])


def double_support(left: list[Span], right: list[Span], step_time: float) -> DoubleSupport:
    """双支撑相位，按「先离地的是哪只脚」分两类。

    `b0 - a1`：前一足的离地时刻减后一足的触地时刻。正值表示两足同时着地（重叠），
    负值表示中间有腾空。**负值在 ZUPT 边界下是常态**，见模块文档 §2。
    """
    tagged = sorted(
        [(start, stop, "L") for start, stop in left] + [(start, stop, "R") for start, stop in right]
    )
    lead_left: list[float] = []
    lead_right: list[float] = []
    for (_, stop0, foot0), (start1, _, foot1) in pairwise(tagged):
        if foot0 == foot1:
            # 同一只脚连着两个支撑相 —— 另一只脚在这中间没有被检出。跳过而不是
            # 拿它凑数：配对差的前提是两类相位来自同一个交替序列。
            continue
        (lead_left if foot0 == "L" else lead_right).append(stop0 - start1)

    both = np.array(lead_left + lead_right)
    return DoubleSupport(
        left_leading=float(np.mean(lead_left)) if lead_left else float("nan"),
        right_leading=float(np.mean(lead_right)) if lead_right else float("nan"),
        mean=float(both.mean()) if both.size else float("nan"),
        fraction=float(both.mean() / step_time) if both.size and step_time > 0 else float("nan"),
        phases=int(both.size),
        negative_phases=int((both < 0).sum()),
    )


def _raw_offset(left: list[Span], right: list[Span]) -> float | None:
    """一段区间上的 offset 估计，s。样本不足时返回 `None`。

    残差 = 配对双支撑差 − 足内支撑相时长差，等于 2Δ；除以 2 得到 Δ。足内那一项是
    修正真实不对称用的 —— offset 够不着足内的量，见模块文档 §4。
    """
    if len(left) < 2 or len(right) < 2:
        return None
    periods = np.concatenate((stride_periods(left), stride_periods(right)))
    if periods.size == 0:
        return None
    period = float(np.median(periods))
    support = double_support(left, right, step_time=0.5 * period)
    if not support.phases:
        return None
    within = float(np.median([stop - start for start, stop in left])) - float(
        np.median([stop - start for start, stop in right])
    )
    return 0.5 * (support.leading_difference - within)


def _consistency(left: list[Span], right: list[Span]) -> float:
    """前后半程两次估计之差的绝对值，s；估不出来时返回 `inf`。

    `inf` 而不是 0：估不出来是"不知道稳不稳"，而 0 会被当成"很稳"。闸门必须朝
    保守的方向失败。
    """
    first = _raw_offset(left[: len(left) // 2], right[: len(right) // 2])
    second = _raw_offset(left[len(left) // 2 :], right[len(right) // 2 :])
    if first is None or second is None:
        return float("inf")
    return abs(first - second)


def check(left: list[Span], right: list[Span], cfg: AlgoConfig | None = None) -> SyncQuality:
    """自检。`left` / `right` 是两足的支撑相时刻区间，同一条时间轴。

    顺序是有讲究的：**先判 offset 稳不稳，再报它的值**。相位在漂时不存在恒定 offset，
    此时给出的任何数字都是编造（模块文档 §5：右足 stride 长 2% 就能编出 110 ms）。
    """
    cfg = cfg or AlgoConfig()
    left = drop_still_lead(left, cfg)
    right = drop_still_lead(right, cfg)
    if len(left) < 2 or len(right) < 2:
        raise SelfCheckError(
            f"两足各需至少 2 个支撑相才能谈周期，去掉静止前导后收到 {len(left)} 与 {len(right)}"
        )

    median_left = float(np.median(stride_periods(left)))
    median_right = float(np.median(stride_periods(right)))
    mean_period = 0.5 * (median_left + median_right)
    period_difference = abs(median_left - median_right) / mean_period if mean_period > 0 else 0.0

    support = double_support(left, right, step_time=0.5 * mean_period)
    within = float(np.median([stop - start for start, stop in left])) - float(
        np.median([stop - start for start, stop in right])
    )
    consistency = _consistency(left, right)

    reasons: list[str] = []
    determinate = True
    offset: float | None = None

    if support.phases < cfg.selfcheck_min_phases:
        determinate = False
        reasons.append(REASON_TOO_FEW_PHASES)
    elif consistency > cfg.selfcheck_offset_consistency_s:
        # 相位在漂 —— 没有恒定 offset 可估。
        determinate = False
        reasons.append(REASON_DRIFTING)
    else:
        offset = _raw_offset(left, right)
        if offset is not None and abs(offset) > cfg.selfcheck_offset_warn_s:
            reasons.append(REASON_OFFSET)

    # 节律不对称单独标注。它**不影响可估性** —— 一个恒定 offset 在不对称步态上照样
    # 是恒定的，第 §4 节的足内修正已经把不对称扣掉了。标它是因为 PRD §8 要它。
    if period_difference > cfg.selfcheck_stride_period_tolerance:
        reasons.append(REASON_CADENCE)

    return SyncQuality(
        offset_estimate=offset,
        stride_period_difference=period_difference,
        offset_consistency=consistency,
        within_foot_stance_difference=within,
        double_support=support,
        determinate=determinate,
        flagged=bool(reasons),
        reasons=reasons,
    )
