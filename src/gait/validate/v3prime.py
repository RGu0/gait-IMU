"""V3′ 分析管线：主机侧同步误差量化与三选一决策的可执行部分。

契约 §1 的 `validate/`（L8）—— 与 `synthetic.py` 同一个定位：它不是产品流程的
一部分，而是**验证点的可执行部分**，可以被 CLI 调用。放在 L8 而不是 `sync/`，
因为它要读 `analysis/` 的跨足指标（双支撑期、步时对称性），而 `sync/` 在
`analysis/` 之下，反向依赖会破坏契约 §0 的第 2 条。

## 它回答的问题

《06 测试与验证方案》§5 的 V3′：主机侧接收时刻同步（`sync/timebase.py`）的实际
误差有多大？它把双支撑期与步时对称性带偏多少？据此在三条路里选一条。

真值来自物理对碰锚点（`sync/anchor.py`，RAY-212）：两个模块外壳对碰是同一个物理
事件，两侧冲击峰在主机时基下的时刻差 Δ 就是主机侧方案测不到的那一项。

## 判据只有一处家

《06》v1.1 §5 与 RAY-213 需求修订 R1 定下的门槛写在 `NEGLIGIBLE_MEDIAN_S` 与
`NEGLIGIBLE_P90_S`，`Verdict.negligible` 是它唯一的执行点。判据**开跑前定死、
跑完不得修改**（06 §5 的冻结声明）—— 把它写成两个具名常量而不是散在判断里，
是为了让"有没有人在跑完之后动过判据"这件事在 git 历史里一眼可查。

## 为什么校正是"平移一只脚的全部事件"

Δ 是**恒定**偏差：两台设备各自的固有链路延迟之差不随时间变化（随时间变化的那
部分是晶振差，由 `SyncReport.fs` 各自吸收）。所以真值时基下的事件时刻 =
主机时基下的时刻，左足整体减去 Δ。

这也解释了为什么不必重跑惯导：双支撑期与步时对称性**只依赖事件时刻**，不依赖
轨迹。平移事件即可，代价是 O(n) 而不是重算一遍 ESKF。

## 拒绝对粗对齐过的锚点报告下判据

`AnchorReport.alignment_applied_s` 非 None 意味着两条时间轴的零点差是被对碰序列
自身估出来的（离线录制各自归零时的救济），此时 **Δ 的均值按构造在零附近，不携带
绝对偏移的信息**。拿这样的 Δ 去校正指标，等于用一个被自己定义为零的量去量偏差 ——
读数会漂亮得毫无意义。`evaluate_trial` 因此直接拒绝，而不是给一个看起来正常的数。

要得到绝对 Δ 只有两条路：现场在线采集（同进程双设备，`t_host` 天然共钟），或
离线录制时持久化各文件的 epoch。前者是 `cli/v3prime.py --live` 走的路。
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Any, Final

import numpy as np

from gait.analysis.events import double_support
from gait.analysis.variability import step_time_symmetry
from gait.contracts import GaitCycle
from gait.sync.anchor import AnchorReport

#: V3′ 判据（《06 测试与验证方案》v1.1 §5，RAY-213 需求修订 R1）。
#:
#: 由 RAY-264 的实测推得：步时对称性指数 `SI ≈ 2Δ/步时`，步时 0.555 s 时每 10 ms
#: 偏差贡献约 3.6%；文献以 SI > 10% 为有临床意义的不对称；取"同步引起的本底
#: < 2%"（临床门槛的 1/5）得 5.5 ms。
NEGLIGIBLE_MEDIAN_S: Final[float] = 0.0055
#: 90 分位另设 10 ms（本底 3.6%，不足临床门槛一半），约束分布尾部 —— 中位合格
#: 而尾部散开的会话，个别受试者依然会被读成不对称。
NEGLIGIBLE_P90_S: Final[float] = 0.010

#: 本报告的结构版本。V3′ 的结论会被 PRD §8 引用，读的人要知道它按哪版判据算的。
V3PRIME_REPORT_VERSION: Final[str] = "1.0"

__all__ = [
    "NEGLIGIBLE_MEDIAN_S",
    "NEGLIGIBLE_P90_S",
    "V3PRIME_REPORT_VERSION",
    "MetricBias",
    "PairedSupport",
    "TrialResult",
    "V3PrimeError",
    "Verdict",
    "evaluate_trial",
    "paired_double_support",
    "shift_cycles",
    "summarize",
]


class V3PrimeError(ValueError):
    """V3′ 分析的输入不满足前提。"""


@dataclass(frozen=True)
class PairedSupport:
    """配对双支撑差，连同它的**相位结构**。

    结构（两类相位的个数、被剔除的包含型个数）与数值一起返回，是因为 V3′ 要把
    同一份数据在两条时间轴上各算一遍：只有结构相同，两次读数才谈得上相减。
    """

    difference: float
    left_phases: int
    right_phases: int
    contained: int

    @property
    def structure(self) -> tuple[int, int, int]:
        return (self.left_phases, self.right_phases, self.contained)


@dataclass(frozen=True)
class MetricBias:
    """一个跨足指标在"按主机时基"与"按锚点真值校正后"两种读法下的读数。

    `bias = host − corrected`：**主机时基把这个指标带偏了多少**。符号有意义 ——
    它说明偏差把读数推高还是压低，而三选一决策要看的正是"读数会被系统性地推向
    哪一边"。
    """

    name: str
    host: float
    corrected: float
    #: 两次读数是否可比。False 表示校正改变了指标的**内部结构**（相位分类翻转、
    #: 事件配对变化），此时相减得到的不是偏差而是两个不同量之差 —— `bias` 因此
    #: 返回 nan。给一个错数比给 nan 危险得多：nan 会被追问，错数会被引用。
    comparable: bool = True
    #: 不可比的原因，供报告直接引用。
    note: str | None = None

    @property
    def bias(self) -> float:
        if not self.comparable:
            return float("nan")
        return self.host - self.corrected

    def snapshot(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "host": _finite(self.host),
            "corrected": _finite(self.corrected),
            "bias": _finite(self.bias),
            "comparable": self.comparable,
            "note": self.note,
        }


@dataclass(frozen=True)
class TrialResult:
    """一趟（一个受试者的一次采集）的产出。"""

    label: str
    #: 锚点真值：本趟的 Δ 分布。
    anchor: AnchorReport
    #: 用于校正的 Δ，s。取中位而不是均值：BLE 长尾会把均值拖走（同 06 §5 的
    #: 判据口径）。
    delta_truth: float
    #: RAY-263 配对双支撑差分法估出的 Δ，s。None 表示该法判定 offset 不可估
    #: （相位在漂），此时它不构成印证也不构成反驳。
    delta_selfcheck: float | None
    #: 跨足指标在两种时基下的读数。
    metrics: tuple[MetricBias, ...]

    @property
    def cross_check(self) -> float | None:
        """两法估计之差，s。验收标准 3 要的就是这个数。"""
        if self.delta_selfcheck is None:
            return None
        return self.delta_truth - self.delta_selfcheck

    def snapshot(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "delta_truth_s": _finite(self.delta_truth),
            "delta_selfcheck_s": _finite(self.delta_selfcheck),
            "cross_check_s": _finite(self.cross_check),
            "taps": len(self.anchor.pairs),
            "anchor_offset": self.anchor.snapshot()["offset"],
            "metrics": [metric.snapshot() for metric in self.metrics],
        }


@dataclass(frozen=True)
class Verdict:
    """汇总判定。判据见模块文档"判据只有一处家"。"""

    #: 参与判定的逐次对碰 Δ（跨全部趟次），s。
    deltas: np.ndarray
    trials: int
    taps: int

    @property
    def median_abs(self) -> float:
        return float(np.median(np.abs(self.deltas))) if self.deltas.size else float("nan")

    @property
    def p90_abs(self) -> float:
        return float(np.percentile(np.abs(self.deltas), 90)) if self.deltas.size else float("nan")

    @property
    def max_abs(self) -> float:
        return float(np.abs(self.deltas).max()) if self.deltas.size else float("nan")

    @property
    def negligible(self) -> bool | None:
        """R1 判据的唯一执行点。样本为空时返回 None —— "没数据"不是"合格"。"""
        if not self.deltas.size:
            return None
        return self.median_abs < NEGLIGIBLE_MEDIAN_S and self.p90_abs < NEGLIGIBLE_P90_S

    @property
    def decision(self) -> str:
        """三选一里本管线**能**定的那一半。

        固件是否商务可得不是数据能回答的问题（06 §5 的后两行都以它为条件），
        所以不可忽略时这里只报"需在固件与降级之间取舍"，把选择留给人。
        """
        verdict = self.negligible
        if verdict is None:
            return "无数据：未采集到任何有效对碰"
        if verdict:
            return "可忽略 → 维持现状（PRD §8 按现设计交付，跨足指标正常输出）"
        return (
            "不可忽略 → 在「走定制固件」与「跨足时序指标不交付」之间取舍，"
            "取决于厂商 seq+timestamp 固件商务是否可得（06 §5，非数据可答）"
        )

    def snapshot(self) -> dict[str, Any]:
        return {
            "version": V3PRIME_REPORT_VERSION,
            "trials": self.trials,
            "taps": self.taps,
            "median_abs_s": _finite(self.median_abs),
            "p90_abs_s": _finite(self.p90_abs),
            "max_abs_s": _finite(self.max_abs),
            "criterion": {
                "median_abs_s": NEGLIGIBLE_MEDIAN_S,
                "p90_abs_s": NEGLIGIBLE_P90_S,
                "source": "06 测试与验证方案 v1.1 §5 / RAY-213 R1",
            },
            "negligible": self.negligible,
            "decision": self.decision,
        }


def _finite(value: float | None) -> float | None:
    """非有限值以 None 落盘：`json.dumps` 会把 nan 写成裸 `NaN`，那不是合法 JSON。"""
    if value is None:
        return None
    return value if math.isfinite(value) else None


def shift_cycles(cycles: Sequence[GaitCycle], delta: float) -> list[GaitCycle]:
    """把一只脚的全部事件时刻平移 `-delta`。

    平移的是 `t_ic` / `t_to` / `t_ic_next` 三个**时刻**；`stride_time`、
    `stance_time` 这些**时长**一个都不动 —— 恒定平移够不着足内的量（RAY-263
    模块文档 §4 的同一条性质）。改了它们反而会让"校正"这个动作凭空制造出
    足内差异。
    """
    return [
        replace(
            cycle,
            t_ic=cycle.t_ic - delta,
            t_to=cycle.t_to - delta,
            t_ic_next=cycle.t_ic_next - delta,
        )
        for cycle in cycles
    ]


def _metrics(
    left: Sequence[GaitCycle],
    right: Sequence[GaitCycle],
    sync_quality: dict[str, Any],
) -> tuple[float, float, float]:
    """三个跨足读数：双支撑期占比、**配对**双支撑差、步时对称性指数。

    两个双支撑口径都要，因为它们对 Δ 的敏感度差了近两个数量级，而只报其中一个
    都会误导：

    - `fraction`（均值口径）对恒定 Δ **几乎免疫** —— 一类相位 +Δ、另一类 −Δ，
      求均值时抵消（`sync/selfcheck.py` 实测：80 ms 的 offset 只让均值动 2.05 ms）。
      只报它，V3′ 会得出"双支撑期不受同步误差影响"的错误结论。
    - `paired_double_support`（配对口径，两类均值之差）在恒定 Δ 下**等于 2Δ**，
      是真正承载偏差的那个量（RAY-211/263）。

    保留 `fraction` 不是凑数：它的偏差近零本身是 V3′ 要记录的结论之一 —— 它说明
    "报告里那个双支撑期占比"读数稳，而"左右谁先离地的时间结构"不稳。

    一处必须知道的读法：`fraction` 对恒定 Δ **不是完全免疫，只是极其迟钝**。残余
    来自两类相位的**个数之差**：`Δ·(n_右前 − n_左前) / N`（`sync/selfcheck.py`
    模块文档 §3 的闭式）。实测剔掉静止前导后 n=(19, 18)、N=37、步时 555 ms，
    Δ=30 ms 给出 +0.146 pp —— 与闭式算出的 30·1/37/555 = 0.146 pp **分毫不差**。

    这个数从前不是这样。RAY-296 之前它读作 ∓0.000 pp（Δ=30 ms 一档跳到 +2.815 pp），
    那个"完美的零"是**静止前导污染出来的假象**，不是免疫性的证据。现在的小而线性
    的残余才是真的，而且它同时验证了本管线与 §3 的闭式互相对得上。
    """
    support = double_support(left, right, sync_quality=sync_quality)
    symmetry = step_time_symmetry(left, right, sync_quality=sync_quality)
    return support.fraction, paired_double_support(left, right), symmetry.index


def paired_double_support(
    left: Sequence[GaitCycle], right: Sequence[GaitCycle]
) -> PairedSupport:
    """配对双支撑差：按「先离地的是哪只脚」分两类，取两类均值之差。

    恒定偏差 Δ 下它**等于 2Δ** —— 一类相位加 Δ、另一类减 Δ（RAY-211/263）。这是
    双支撑期里真正承载同步偏差的那个量。

    ## 为什么不复用 `sync/selfcheck.py` 与 `analysis/events.py` 里的实现

    **本节曾经写错过一次归因，RAY-290 查清后由 RAY-296 改正。留下原委，因为那个
    错误的说法已经被引用去开过一个 Issue。**

    原文记的是：那两处按「起点排序取相邻对」配相位，**平移量一大就出现同足相邻**，
    于是两类均值之差整个跳掉；并举实测「Δ=10/20 ms 给出 +20.00/+40.00 ms，Δ=30 ms
    给出 −128.87 ms」为证，结论是失效恰好落在 PRD §8 的容差上界里。

    那个 −128.87 ms 是真的，**归因是错的**。RAY-290 复现后查明：

    * Δ=30 ms 处同足相邻次数为 **0** —— 交替一次都没被打破。相邻配对的真实失效点
      在**一个步时**上（步频 108/140/160 时为 556/429/375 ms），是 30 ms 的 12.5~18.5 倍。
    * 真正的成因是当时喂进来的数据里还留着**起步前的静止前导**（一个 1.67~2.23 s
      的"支撑相"，而典型双支撑相位只有约 110 ms）。它落进哪一类，哪一类的均值就被
      它一个人拖走。`cli/v3prime.py::_cycles()` 现已在 ZUPT 边界上剔掉它（RAY-296）。

    所以那两处对它们各自的输入是对的，**理由不是**「本模块要在平移过的轴上再算一遍」，
    而是它们的输入里没有前导：`sync/selfcheck.py` 由 `drop_still_lead()` 剔，
    `analysis/events.py` 的产品路径由 `analysis/segments.py` 的分段剔。

    ## 区间交集定义仍然保留

    改正归因不动结论。对**本模块的输入**（细化后的生理边界、有真实重叠）区间交集
    仍是更好的定义，理由是它自己的那一条：双支撑相位 = 两足支撑区间的重叠
    `min(to) − max(ic)`，先离地的一足 = `to` 较小的那只。它不依赖任何排序相邻性，
    因此在平移下连续 —— 分类只在两足离地顺序**真正翻转**时才变，而那时它本来就
    该变；而且它能识别并剔除包含型相位（见下），相邻法做不到。
    """
    lead_left: list[float] = []
    lead_right: list[float] = []
    contained = 0
    for cycle_l in left:
        for cycle_r in right:
            overlap = min(cycle_l.t_to, cycle_r.t_to) - max(cycle_l.t_ic, cycle_r.t_ic)
            if overlap <= 0.0:
                continue  # 没有重叠：这一对之间是腾空，不是双支撑相位。
            if (cycle_l.t_ic > cycle_r.t_ic) == (cycle_l.t_to < cycle_r.t_to):
                # 一足的支撑区间被另一足**完全包含**：重叠的两端同属一足，于是
                # 这个相位量的是**足内时长**，恒定偏差够不着它（RAY-263 §4 的
                # 同一条性质）。算进均值只会稀释信号 —— 实测合成对称步态里这类
                # 相位占 1/20，配对差因此系统性地少 2.5%，读作 0.975·2Δ。
                contained += 1
                continue
            (lead_left if cycle_l.t_to < cycle_r.t_to else lead_right).append(overlap)
    if not lead_left or not lead_right:
        # 一类为空时"两类之差"没有意义。返回 nan 而不是 0：0 会被读成"没有偏差"。
        return PairedSupport(float("nan"), len(lead_left), len(lead_right), contained)
    return PairedSupport(
        float(np.mean(lead_left) - np.mean(lead_right)),
        len(lead_left),
        len(lead_right),
        contained,
    )


def evaluate_trial(
    label: str,
    anchor: AnchorReport,
    left: Sequence[GaitCycle],
    right: Sequence[GaitCycle],
    *,
    sync_quality: dict[str, Any],
    delta_selfcheck: float | None = None,
) -> TrialResult:
    """一趟的完整评估：锚点真值 → 指标偏差 →（可选）与差分法互证。

    `left` / `right` 是**同一条主机时基**上的步态周期（`analysis/events.py` 的
    输出）。`sync_quality` 按 PRD §13 强制随跨足指标传递，本函数只做搬运。

    对碰段与步行段可以来自同一次采集的不同时段：Δ 是恒定量，在哪一段量出来都
    一样 —— 这正是"锚点做真值"能成立的前提。若 Δ 在一次采集里就不恒定，
    `AnchorReport.drift_s_per_min` 会显示出来，那本身就是一条结论。
    """
    if anchor.alignment_applied_s is not None:
        raise V3PrimeError(
            "这份锚点报告经过粗对齐（alignment_applied_s 非 None），Δ 的绝对值已被"
            "对齐吃掉，只剩散布可用。用它校正指标等于用一个被定义为零的量去量偏差。"
            "请用在线采集（cli/v3prime.py --live，两台设备同进程、t_host 共钟），"
            "或为离线录制提供 epoch。"
        )
    if not anchor.pairs:
        raise V3PrimeError(f"趟次 {label!r} 没有配对到任何对碰，无法给出真值 Δ。")

    delta_truth = anchor.offset_median
    corrected_left = shift_cycles(left, delta_truth)
    host = _metrics(left, right, sync_quality)
    fixed = _metrics(corrected_left, right, sync_quality)
    host_fraction, host_paired, host_symmetry = host
    fixed_fraction, fixed_paired, fixed_symmetry = fixed
    comparable = host_paired.structure == fixed_paired.structure
    return TrialResult(
        label=label,
        anchor=anchor,
        delta_truth=delta_truth,
        delta_selfcheck=delta_selfcheck,
        metrics=(
            MetricBias(
                name="double_support_fraction", host=host_fraction, corrected=fixed_fraction
            ),
            MetricBias(
                name="double_support_leading_difference",
                host=host_paired.difference,
                corrected=fixed_paired.difference,
                comparable=comparable,
                note=None
                if comparable
                else (
                    f"相位结构在校正下改变（主机时基 {host_paired.structure} → 真值时基 "
                    f"{fixed_paired.structure}，三元组为左类/右类/包含型相位数）："
                    "两次读数不是同一个量，相减无意义。RAY-296 之前这条会在 Δ=30 ms 上"
                    "触发，成因是输入里残留的静止前导让包含型判定对浮点回程的 1 ulp "
                    "敏感；前导剔除后结构在全档稳定。再次出现时先查输入里是不是又混进了"
                    "不该算作一步的支撑相。"
                ),
            ),
            MetricBias(
                name="step_time_symmetry", host=host_symmetry, corrected=fixed_symmetry
            ),
        ),
    )


def summarize(trials: Sequence[TrialResult]) -> Verdict:
    """汇总所有趟次，按 R1 判据给出结论。

    判定用的是**逐次对碰的 Δ**，不是逐趟的中位数：先按趟平均会把趟内的散布
    抹掉，而尾部正是 90 分位那条判据要抓的东西。
    """
    deltas = np.concatenate([trial.anchor.deltas for trial in trials]) if trials else np.zeros(0)
    return Verdict(deltas=deltas, trials=len(trials), taps=int(deltas.size))
