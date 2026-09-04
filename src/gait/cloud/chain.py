"""完整链的算法编排。PRD §6.1 的"云端完整报告"，整体设计 §5.1 第 ⑦⑧ 步与 §5.8。

## 完整链 = 基础链 + 两步，不是另一条链

    基础链：  run_ins ──────────────────────────────▶ 事件分割 → 指标 → 质量标注(basic)
    完整链：  run_ins_with_history → RTS → 锚定 → 双足约束 → 事件分割 → 指标 → 质量标注(full)
              └────────── 同一个前向内核，同一份历史 ──────────┘

前向部分**逐位相同**：`run_ins_with_history` 与 `run_ins` 走同一条代码路径，记录历史
只是旁路。`test_the_forward_stage_is_bit_identical_to_the_basic_chain` 把这件事钉成
一条会失败的断言 —— 端云同构红线（PRD FR-08）说的就是这个，而"我们都调了同一个函数"
是一句无法被违反的话，只有逐位比较才是。

指标与质量标注也走同一批函数（`analysis/`、`quality/annotate`），唯一的差别是传进去的
`chain` 是 `full` 还是 `basic`。

## 为什么顺序与整体设计 §5.1 不同

§5.1 写的是 ⑦ 双足联合约束 → ⑧ RTS 后向平滑。这里是 RTS → 锚定 → 双足约束，原因是
实现形态：`dualfoot.apply_distance_constraint` 是**对两条已完成的轨迹做后处理**（整体
设计写它时预期的是滤波器内联合更新，那属未来工作，见 `dualfoot.py` 模块文档）。

它会把整条轨迹绕原点转一个角度。若先转再平滑，`FilterHistory` 里的 Φ 与 P 就不再是
在当前名义轨迹上线性化的量 —— RTS 的推导会失效，而失效的方式是"结果照常算出来、
只是不再是最优估计"。所以平滑必须紧接前向。

锚定放在双足约束之前：锚定是逐足的相内修正，双足约束拟合的是两足航向差，两者作用在
不同的自由度上，但双足约束的距离违规判据用的是位置，给它更干净的位置更合理。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Final

import numpy as np

from gait.analysis import events, segments, variability
from gait.config import AlgoConfig
from gait.contracts import FootLabel, FootSeries, GaitCycle, NavResult
from gait.core import quaternion as quat
from gait.core import rts, stance_anchor
from gait.core.dualfoot import DualFootError, DualFootReport, apply_distance_constraint
from gait.core.eskf import SegmentDetection, run_ins_with_stances
from gait.quality import annotate as quality

#: 完整链的算法版本。**进产出与报告页脚**（PRD §12 页脚含算法版本、G-08 可回溯重算）。
#:
#: 语义化的三段之外还带链名：同一份原始数据在基础链与完整链下会得到不同的数字，
#: 而"算法版本"要回答的是"这个数字是怎么来的"，链名是那个答案的一部分。
#:
#: 什么时候要改：任何会改变输出数值的改动。加一个字段不算，换一个平滑参数算。
#: 改了它，旧 `algo_version` 的历史结果不会被自动重算 —— 重算是显式触发的
#: （`cloud/recompute.py` 的 `enqueue`），因为重算一整个租户的历史会话是一件
#: 要有人决定的事。
FULL_CHAIN_ALGO_VERSION: Final[str] = "full-1.0.0"

#: 基础链的算法版本，供本地端与对照实验使用。
BASIC_CHAIN_ALGO_VERSION: Final[str] = "basic-1.0.0"

#: 步态指标清单（PRD §13）。每一项都要有质量标注 —— §13 的原则是"指标全量计算 +
#: 质量标注，无指标级门控"，所以这里没有"算不出来就不输出"这条路：算不出来的项
#: 以 `uncomputable` 出现在页脚里。
_CROSS_FOOT_METRICS: Final[tuple[str, ...]] = ("double_support_ratio", "symmetry_index")
_PER_FOOT_METRICS: Final[tuple[str, ...]] = (
    "cadence",
    "stride_length",
    "gait_speed",
    "stance_ratio",
    "swing_ratio",
    "stride_time",
)
_SESSION_METRICS: Final[tuple[str, ...]] = ("stride_length_cv", "stride_time_cv")


class ChainError(ValueError):
    """完整链输入非法。"""


@dataclass(frozen=True)
class FootOutcome:
    """一只脚的重算产物。"""

    label: FootLabel
    navigation: NavResult
    cycles: list[GaitCycle]
    selected: list[GaitCycle]
    segmentation: segments.SegmentationReport
    #: 没有任何中段步时为 `None`。空数据是一个**数据问题**，不是调用错误 —— 让它
    #: 抛异常会把一份本可以出（其余指标标注为 `uncomputable`）的报告整个打掉。
    spatiotemporal: events.SpatioTemporal | None
    smooth_report: rts.SmoothReport | None
    anchor_report: stance_anchor.AnchorReport | None

    def snapshot(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "cycles": len(self.cycles),
            "selected": len(self.selected),
            "segmentation": self.segmentation.snapshot(),
            "spatiotemporal": (
                {
                    "cadence": self.spatiotemporal.cadence,
                    "stride_length": self.spatiotemporal.stride_length,
                    "gait_speed": self.spatiotemporal.gait_speed,
                    "stance_ratio": self.spatiotemporal.stance_ratio,
                    "swing_ratio": self.spatiotemporal.swing_ratio,
                    "stride_time": self.spatiotemporal.stride_time,
                }
                if self.spatiotemporal
                else None
            ),
            "smooth": self.smooth_report.snapshot() if self.smooth_report else None,
            "anchor": self.anchor_report.snapshot() if self.anchor_report else None,
        }


@dataclass(frozen=True)
class ChainResult:
    """一次重算的完整产物。`algo_version` 与 `chain` 一起进报告页脚。"""

    chain: str
    algo_version: str
    feet: dict[FootLabel, FootOutcome]
    double_support: events.DoubleSupport | None
    variability: variability.VariabilityReport | None
    annotations: list[quality.QualityAnnotation]
    footer: quality.QualityFooter
    dualfoot: DualFootReport | None = None
    diagnostics: dict[str, Any] = field(default_factory=dict)

    def snapshot(self) -> dict[str, Any]:
        return {
            "chain": self.chain,
            "algo_version": self.algo_version,
            "feet": {label: outcome.snapshot() for label, outcome in self.feet.items()},
            "double_support": (
                {
                    "mean": self.double_support.mean,
                    "fraction": self.double_support.fraction,
                }
                if self.double_support
                else None
            ),
            "variability": self.variability.snapshot() if self.variability else None,
            "annotations": [item.snapshot() for item in self.annotations],
            "footer": self.footer.snapshot(),
            "dualfoot": _dualfoot_snapshot(self.dualfoot),
            "diagnostics": self.diagnostics,
        }


def _dualfoot_snapshot(report: DualFootReport | None) -> dict[str, Any] | None:
    """`DualFootReport` 没有 `snapshot()`（它进的是 `SessionMeta.sync_report`，
    序列化由那一侧负责）。这里就地摊平，而不是回头去给 `core/` 加方法 —— 那会为了
    一个云端的序列化需求去改一个纯函数库的公开接口。"""
    if report is None:
        return None
    return {
        "max_distance": report.max_distance,
        "step_width": report.step_width,
        "peak_distance_before": report.peak_distance_before,
        "peak_distance_after": report.peak_distance_after,
        "violation_fraction_before": report.violation_fraction_before,
        "violation_fraction_after": report.violation_fraction_after,
        "differential_yaw_rate": report.differential_yaw_rate,
        "hit_search_bound": report.hit_search_bound,
        "improved": report.improved,
    }


def _yaw_rate(navigation: NavResult) -> np.ndarray:
    """导航系的偏航角速率，rad/s。

    用姿态把足部系角速度转到导航系，而不是直接拿 `gyr[:, 2]`。后者只有在足部大致
    水平时才近似成立（`analysis/segments.heading_change_per_cycle` 的文档点名了这件
    事），而完整链手里正好有一条经过平滑的姿态 —— 用近似值是没有理由的。
    """
    # 由姿态数值微分得到，而不是"陀螺减零偏再转系"：后者要重新读原始陀螺，而平滑
    # 之后那份测量已经不是这条轨迹的直接输入了。相邻姿态的相对旋转除以 dt 天然与
    # 最终轨迹自洽。
    rotation = quat.to_matrix(navigation.q)
    times = np.asarray(navigation.t, dtype=np.float64)
    if len(times) < 2:
        return np.zeros(len(times))
    relative = np.einsum("nij,nkj->nik", rotation[1:], rotation[:-1])
    # 小角度下 R 的反对称部分就是旋转矢量；取 z 分量即偏航率。
    yaw = (relative[:, 1, 0] - relative[:, 0, 1]) * 0.5
    dt = np.diff(times)
    rate = np.zeros(len(times))
    rate[1:] = np.divide(yaw, dt, out=np.zeros_like(yaw), where=dt > 0)
    rate[0] = rate[1]
    return rate


def _stance_intervals(
    series: FootSeries,
    detections: Sequence[SegmentDetection],
    cfg: AlgoConfig,
) -> list[events.StanceEdges]:
    """逐段求支撑相区间，把段内下标搬回全局。

    **按段做，不是把整条序列当一段。** `run_ins` 本来就是逐段滤波的（空洞之间不积分），
    每段有自己的周期栅格；真机实测 24 格里有一半是 2~3 段。把它们并成一段再检测，
    得到的会是一份与轨迹所依据的检测**不同**的检测，而且不报错。

    代价已量清：1 段的格无损失，2 段 −1，3 段 −2（网格只铺在首末摆动峰之间，
    每段各丢一个）。真机 24 格均值 −0.71 个周期。**这是按段做的固有代价，而合成
    一段是错的** —— 那会让事件建在一份与轨迹不同的检测上。
    """
    edges: list[events.StanceEdges] = []
    for segment in detections:
        if segment.skipped:
            # 短到没法分析的段（RAY-352）。它没有检测结果，自然也没有支撑相区间 ——
            # 跳过而不是拿一个空的 `StanceDetection` 去装样子。
            continue
        window = series.acc[segment.start : segment.end]
        for edge in events.detect_stance_intervals(window, series.fs, segment.detection, cfg):
            edges.append(
                replace(edge, ic=edge.ic + segment.start, to=edge.to + segment.start)
            )
    return edges


def _analyse_foot(
    label: FootLabel,
    series: FootSeries,
    navigation: NavResult,
    detections: Sequence[SegmentDetection],
    cfg: AlgoConfig,
    smooth_report: rts.SmoothReport | None,
    anchor_report: stance_anchor.AnchorReport | None,
) -> FootOutcome:
    # 支撑相**区间**，不是零速时刻。零速时刻跨度只占周期 0.7%~2.1%，拿两个近似零宽的
    # 区间算重叠，`double_support` 的 `fraction` 必然趋近 −1 个 step 时长 —— 真机实测
    # −0.925~−0.624，而那不是双支撑期读数，是零宽区间的算术（RAY-325 / RAY-351）。
    #
    # 合成数据上两条路几乎不分（+0.260 vs +0.263），因为那里的脚是**真的**停住的。
    # 所以这一行的理由只在真机上看得见，合成测试再多也看不见。
    cycles, _ = events.segment_cycles(
        label,
        navigation.t,
        series.acc,
        series.gyr,
        navigation.stances,
        position=navigation.p,
        cfg=cfg,
        stance_edges=_stance_intervals(series, detections, cfg),
    )
    if cycles:
        report = segments.analyse(cycles, navigation.t, _yaw_rate(navigation))
        selected = segments.selected_cycles(cycles, report)
    else:
        report = segments.SegmentationReport(
            segments=[], selected=[], dropped={}, trim=1,
            turn_degrees=segments.DEFAULT_TURN_DEGREES, turns=0, mean_turn_duration=0.0,
        )
        selected = []
    return FootOutcome(
        label=label,
        navigation=navigation,
        cycles=cycles,
        selected=selected,
        segmentation=report,
        spatiotemporal=events.summarize(label, selected) if selected else None,
        smooth_report=smooth_report,
        anchor_report=anchor_report,
    )


def _agree_turns_across_feet(
    feet: dict[FootLabel, FootOutcome],
    sync_quality: dict[str, Any] | None,
) -> dict[FootLabel, FootOutcome]:
    """把只有一只脚判出的转身降级为直行，然后重选中段步（RAY-354 判据 6）。

    **放在这里而不是 `_analyse_foot` 里**，因为它要两只脚：`_analyse_foot` 是逐足的，
    而"两足是否同时判出"这句话在单足语境下不存在。

    单足会话、缺周期、或没有同步质量标注时**原样返回** —— 这条规则用不了的时候，
    退回逐足判读，而不是抛错把整份报告打掉。
    """
    if sync_quality is None or sorted(feet) != ["L", "R"]:
        return feet
    left, right = feet["L"], feet["R"]
    if not left.cycles or not right.cycles:
        return feet

    changes = {
        label: segments.heading_change_per_cycle(
            outcome.cycles, outcome.navigation.t, _yaw_rate(outcome.navigation)
        )
        for label, outcome in (("L", left), ("R", right))
    }
    agreed = segments.separate_with_agreement(
        (left.cycles, changes["L"]),
        (right.cycles, changes["R"]),
        sync_quality=sync_quality,
    )
    out: dict[FootLabel, FootOutcome] = {}
    for label, outcome, pieces in (("L", left, agreed[0]), ("R", right, agreed[1])):
        report = segments.select_middle_steps(outcome.cycles, pieces)
        selected = segments.selected_cycles(outcome.cycles, report)
        out[label] = replace(
            outcome,
            selected=selected,
            segmentation=report,
            spatiotemporal=events.summarize(label, selected) if selected else None,
        )
    return out


def _annotate_all(
    chain: str,
    feet: dict[FootLabel, FootOutcome],
    double_support: events.DoubleSupport | None,
    session_variability: variability.VariabilityReport | None,
    sync_quality: dict[str, Any] | None,
) -> list[quality.QualityAnnotation]:
    """给每一项指标定级。**调用的是 `quality/annotate` 的唯一实现点**（PRD FR-08）。"""
    annotations: list[quality.QualityAnnotation] = []
    for label, outcome in sorted(feet.items()):
        steps = len(outcome.selected)
        zupt_quality = _zupt_quality(outcome.navigation)
        for metric in _PER_FOOT_METRICS:
            annotations.append(
                quality.annotate(
                    f"{metric}.{label}",
                    n_steps=steps,
                    chain=chain,
                    zupt_quality=zupt_quality,
                    computable=steps > 0,
                )
            )

    # 单足会话下 `min()` 取的是那一只脚的步数，于是「有步数」并不代表跨足指标算得出来。
    # 每一项的可算性都要绑到**它自己的产物在不在**，而不是绑到一个步数：
    # `double_support` 与 `symmetry`（在 `session_variability` 里）都要两只脚，缺一只时
    # `variability.analyse` 会抛错、被 `_assemble` 吞掉，结果里根本不存在这个值。
    # 绑错了的症状是页脚宣称算了一项从未产出的指标 —— 而 PRD §13 的「全量计算 + 质量
    # 标注」正是为了让这种事不发生。
    cross_steps = min((len(item.selected) for item in feet.values()), default=0)
    _cross_foot_computable = {
        "double_support_ratio": double_support is not None,
        "symmetry_index": session_variability is not None,
    }
    for metric in _CROSS_FOOT_METRICS:
        computable = _cross_foot_computable[metric]
        annotations.append(
            quality.annotate(
                metric,
                n_steps=cross_steps,
                chain=chain,
                cross_foot=True,
                sync_quality=sync_quality,
                computable=computable,
            )
        )

    total_steps = sum(len(item.selected) for item in feet.values())
    for metric in _SESSION_METRICS:
        annotations.append(
            quality.annotate(
                metric,
                n_steps=total_steps,
                chain=chain,
                computable=session_variability is not None,
            )
        )
    return annotations


def _zupt_quality(navigation: NavResult) -> dict[str, Any]:
    """`quality.annotate` 要的零速检测质量。字段名与 `annotate` 读的键对齐。"""
    total = len(navigation.zupt)
    stance_samples = int(np.count_nonzero(navigation.zupt))
    degraded = int(np.count_nonzero(navigation.degraded))
    return {
        "degraded_fraction": (degraded / stance_samples) if stance_samples else 0.0,
        "stance_fraction": (stance_samples / total) if total else 0.0,
        "preset": "default",
    }


def run_basic_chain(
    series_by_foot: dict[FootLabel, FootSeries],
    cfg: AlgoConfig | None = None,
    *,
    sync_quality: dict[str, Any] | None = None,
    protocol_seconds: int | None = None,
) -> ChainResult:
    """本地基础链：前向 ESKF，不平滑、不锚定、不做双足约束。

    存在于此不是为了让云端跑基础链，而是为了让"两条链的差别"是一件可以**被执行**的
    事：对照实验与同构测试都要能在同一个进程里跑出两条链的结果来比。
    """
    cfg = cfg or AlgoConfig()
    forward = {
        label: run_ins_with_stances(series, cfg)
        for label, series in series_by_foot.items()
    }
    navigation = {label: value[0] for label, value in forward.items()}
    feet = _agree_turns_across_feet(
        {
            label: _analyse_foot(
                label, series_by_foot[label], navigation[label], forward[label][1], cfg,
                None, None,
            )
            for label in sorted(navigation)
        },
        sync_quality,
    )
    return _assemble(
        quality.CHAIN_BASIC, BASIC_CHAIN_ALGO_VERSION, feet, None,
        sync_quality, protocol_seconds, {},
    )


def run_full_chain(
    series_by_foot: dict[FootLabel, FootSeries],
    cfg: AlgoConfig | None = None,
    *,
    sync_quality: dict[str, Any] | None = None,
    protocol_seconds: int | None = None,
    algo_version: str = FULL_CHAIN_ALGO_VERSION,
) -> ChainResult:
    """云端精算链：前向 ESKF + RTS 平滑 + 零速段锚定 + 双足距离约束。

    单足输入也接受：双足约束这一步跳过（它需要两条轨迹），其余照常。缺一只脚是一个
    **数据问题**，不是调用错误 —— 把它变成异常会让一份本可以出的单足报告整个失败。
    """
    cfg = cfg or AlgoConfig()
    if not series_by_foot:
        raise ChainError("没有任何一只脚的数据，重算无从谈起")

    navigation: dict[FootLabel, NavResult] = {}
    smooth_reports: dict[FootLabel, rts.SmoothReport] = {}
    anchor_reports: dict[FootLabel, stance_anchor.AnchorReport] = {}
    stance_detections: dict[FootLabel, tuple[SegmentDetection, ...]] = {}
    for label, series in sorted(series_by_foot.items()):
        forward, detections, history = run_ins_with_stances(series, cfg, record=True)
        assert history is not None
        stance_detections[label] = detections
        smoothed = rts.smooth(forward, history)
        anchored = stance_anchor.anchor_stance_positions(smoothed.navigation)
        navigation[label] = anchored.navigation
        smooth_reports[label] = smoothed.report
        anchor_reports[label] = anchored.report

    dualfoot_report: DualFootReport | None = None
    dualfoot_applied = False
    declined_reason = ""
    if "L" in navigation and "R" in navigation:
        try:
            constrained = apply_distance_constraint(navigation["L"], navigation["R"], cfg)
        except DualFootError as error:
            # 两足时间轴对不齐（长度不等或时间戳不同）。这与「缺一只脚」是同一类
            # **数据问题**，处理方式也该一样：跳过这一步，其余照常出。
            #
            # 让它冒出去会让整次重算失败，而逐足的轨迹、事件、时空参数其实都好好的 ——
            # 为一个跨足修正丢掉一整份本可以出的报告，是把代价放错了地方。对齐是同步层
            # （RAY-209）的职责，这里只如实记下它没做成。
            constrained = None
            declined_reason = f"unaligned_time_axis: {error}"
        else:
            dualfoot_report = constrained.report
            # 拟合顶到搜索边界时**不采用**这次修正。
            #
            # `dualfoot.py` 把 `hit_search_bound` 定义为"模型或数据有问题，不该被静静
            # 截断"。低速档实测正是这种情况：差分航向拟合饱和在 ±0.02 rad/s，而把一个
            # 饱和的（也就是被截断过的）估计值当作真值施加上去，会把已经被 RTS 修好的
            # 轨迹重新推歪 —— 实测低速档步长误差从 0.46% 退回 1.52%。
            #
            # 报告照常保留：拒绝采用是一个需要被看见的决定，不是一次静默的跳过。
            if constrained.report.hit_search_bound:
                declined_reason = "hit_search_bound"
            else:
                navigation["L"] = constrained.left
                navigation["R"] = constrained.right
                dualfoot_applied = True

    feet = _agree_turns_across_feet(
        {
            label: _analyse_foot(
                label, series_by_foot[label], navigation[label], stance_detections[label], cfg,
                smooth_reports.get(label), anchor_reports.get(label),
            )
            for label in sorted(navigation)
        },
        sync_quality,
    )
    diagnostics = {
        "stages": ["forward_eskf", "rts_smoothing", "stance_anchoring", "dualfoot_constraint"],
        "dualfoot_applied": dualfoot_applied,
        "dualfoot_declined_reason": declined_reason,
    }
    return _assemble(
        quality.CHAIN_FULL, algo_version, feet, dualfoot_report,
        sync_quality, protocol_seconds, diagnostics,
    )


def _assemble(
    chain: str,
    algo_version: str,
    feet: dict[FootLabel, FootOutcome],
    dualfoot_report: DualFootReport | None,
    sync_quality: dict[str, Any] | None,
    protocol_seconds: int | None,
    diagnostics: dict[str, Any],
) -> ChainResult:
    left = feet["L"].selected if "L" in feet else []
    right = feet["R"].selected if "R" in feet else []

    double = None
    if left and right and sync_quality is not None:
        try:
            double = events.double_support(left, right, sync_quality=sync_quality)
        except events.EventError:
            # RAY-354 判据 1 / 7：两足步序配不上、或剔掉跨步配对后一个相位不剩时，
            # `double_support` 抛错。**接住它、标为不可计算**，而不是让一份本可以出
            # （其余指标照常）的报告整个打掉 —— 与 `variability.analyse` 同一处理。
            # `_annotate_all` 把可算性绑在 `double is not None` 上，所以置 None 就
            # 足够让页脚如实说"这项没算出来"。
            double = None

    session_variability = None
    if left or right:
        turns = sum(item.segmentation.turns for item in feet.values())
        durations = [item.segmentation.mean_turn_duration for item in feet.values()]
        try:
            session_variability = variability.analyse(
                left, right,
                turns=turns,
                mean_turn_duration=float(np.mean(durations)) if durations else 0.0,
                protocol_seconds=protocol_seconds,
                sync_quality=sync_quality,
            )
        except variability.VariabilityError:
            session_variability = None

    annotations = _annotate_all(chain, feet, double, session_variability, sync_quality)
    return ChainResult(
        chain=chain,
        algo_version=algo_version,
        feet=feet,
        double_support=double,
        variability=session_variability,
        annotations=annotations,
        footer=quality.summarize(annotations),
        dualfoot=dualfoot_report,
        diagnostics=diagnostics,
    )
