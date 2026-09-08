"""本地基础报告：把步态周期与质量标注组装成一份 payload。

## 形状不是这里定的

报告 payload 的形状**已经被定义了两次**，本模块以它们为准，不另造：

1. `packages/report-template/ReportDocument.jsx` —— 唯一一份模板（R-4），它渲染
   哪些字段就是哪些；
2. RAY-248 的 IPC 契约：「报告 payload：全部指标 + **完整质量标注字段**
   （`n_steps` / `sync_quality` / `zupt_quality` / `chain` / `grade`）」。

两者要的东西不同而不冲突：模板要的是**能印出来的**（标题、数值、单位、等级），
契约要的是**能追查的**（这个等级是凭什么定的）。所以每一项指标同时带两组字段，
`quality` 子对象装后者。少了它，三个月后没人能回答「这份报告里这一项为什么是
参考级」—— 而那正是 `QualityAnnotation` 存在的理由。

## 等级不在这里算

FR-08：质量逻辑只在 `gait/quality/` 实现一次，端云同构。本模块**只调用**
`quality.annotate`，一个阈值都不写。真按「显示得快」在这里照阈值算一遍，端云同构
当场失效，而且是悄悄失效的 —— 两边都跑得好好的，只是结论会分岔。

## 指标永不留空

PRD §12：算不出来的指标显示「本次不适用」+ 通俗原因，**不显示空白、0 或 N/A**。
所以本模块产出的每一项指标都占着它的位置：一个消失的指标读起来是「没有这个量」，
一个空的读起来是「这个量是零」，两个都是错的。措辞见 `wording.py`。
"""

from __future__ import annotations

import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from gait.analysis import events
from gait.contracts import FootLabel, GaitCycle
from gait.quality.annotate import (
    CHAIN_BASIC,
    GRADE_UNCOMPUTABLE,
    QualityAnnotation,
    annotate,
    summarize,
)
from gait.report.wording import (
    NOT_APPLICABLE,
    caliber_note,
    metric_note,
    quality_label,
    reason_text,
)


class ReportError(ValueError):
    """报告无从生成。"""


#: ZUPT 边界相对生理边界的**单侧**削减，ms。
#:
#: ⚠️ **这个数出自合成探针，不是实测**：
#: `evidence/ray-211/sync-selfcheck/acceptance/probe_trim.txt` 八格的均值（区间
#: 47.5~56.1 ms）。那张表里的「真支撑」是 `stride × stance_r` 算出来的，`stance_r`
#: 是**假设的输入参数** —— 真值由生成器定义，不是量出来的。
#:
#: 真机标定要用测力台或压力垫作金标准（RAY-288 §范围 3 → RAY-230 待标定项），
#: 而那件器材本项目不具备（RAY-434 A1）。**标定完成后这里换成实测值**，届时
#: `UNCALIBRATED_NOTE` 那句标注才可以撤。
_TRIM_PER_SIDE_MS: float = 52.9

#: 两个估计量的标识。值进 payload，供下游与测试指名，不要改字面量。
CALIBER_PHASE_OVERLAP: str = "phase-overlap"
CALIBER_STANCE_IDENTITY: str = "stance-identity"


@dataclass(frozen=True)
class _DoubleSupport:
    """双支撑期占比，连同**它是哪个估计量算出来的**。

    口径必须跟着值一起走。只返回一个百分数，调用方就只能靠「我传没传
    `sync_quality`」去猜走了哪一支 —— 那正是 RAY-437 要消掉的那个猜测：分支条件在
    `_double_support` 里，调用方复述一遍就会有第二份真相。
    """

    value: float | None
    caliber: str | None


#: 核心指标（P-09 与报告 §3）。`cross_foot` 决定它要不要同步证据。
_CORE_METRICS: tuple[tuple[str, str, str, bool], ...] = (
    # (key, 标题, 单位, 是否跨足)
    ("speed", "步速", "m/s", False),
    ("cadence", "步频", "步/分", False),
    ("stride", "步长", "m", False),
    ("double-support", "双支撑期占比", "%", True),
)


#: 每个单位印几位小数。**「次」必须是 0 位** —— 一份写着「转身 14.00 次」的报告
#: 在暗示它测到了百分之一次转身，而转身次数是数出来的整数。
_DIGITS: dict[str, int] = {"%": 1, "步/分": 1, "次": 0}


def _by_foot(cycles: Sequence[GaitCycle], foot: FootLabel) -> list[GaitCycle]:
    return [cycle for cycle in cycles if cycle.foot == foot]


def _valid(cycles: Sequence[GaitCycle]) -> list[GaitCycle]:
    """只有 `valid` 的周期进指标。

    无效周期不是「差一点的数据」，是**已经被判定为不该参与计算的数据**。把它们
    算进去，等于让一次已经识别出来的错误重新影响结论。
    """
    return [cycle for cycle in cycles if cycle.valid]


def _mean(values: Sequence[float]) -> float | None:
    return statistics.fmean(values) if values else None


def _reference_percent(
    estimate: _DoubleSupport, mean_stride_time: float | None
) -> float | None:
    """未标定参考值，%（RAY-288 R2）。**只对相位重叠那一支给出。**

    走站立相恒等式的那一支**不给**：那 ~100 ms 是 ZUPT 边界削掉的两个过渡段，
    是**相位重叠**才受到的偏差。恒等式算的是 `2 × 平均站立相 − 100`，一个足内量，
    它本来就没受这个偏差 —— 给它加回去等于凭空抬高一个数。

    换算必须落在**百分点**上，不能把 ms 直接加到百分数上：占比的分母是步周期，
    所以同样的 100 ms 在不同步频下是不同的百分点。125 步/分（周期 0.96 s）下
    约 11.6 个百分点，而该指标正常值才 ~20% —— **修正量是被修正量的一半数量级**，
    这正是它必须带未标定标注的原因。
    """
    if estimate.caliber != CALIBER_PHASE_OVERLAP or estimate.value is None:
        return None
    if not mean_stride_time:
        return None
    offset_pp = (2 * _TRIM_PER_SIDE_MS / 1000.0) / mean_stride_time * 100.0
    return estimate.value + offset_pp


def _metric_values(
    cycles: Sequence[GaitCycle], sync_quality: dict[str, Any] | None = None
) -> tuple[dict[str, float | None], _DoubleSupport, float | None]:
    """四项核心指标的原始数值。算不出来的是 `None`，不是 0。

    连同双支撑期的口径与平均步周期一起返回：口径要跟着值走（见 `_DoubleSupport`），
    步周期是参考值换算的分母，两者都在这里已经算过，重算一遍就会有第二份真相。
    """
    speeds = [cycle.gait_speed for cycle in cycles]
    strides = [cycle.stride_length for cycle in cycles]
    stride_times = [cycle.stride_time for cycle in cycles]

    cadence = None
    mean_stride_time = _mean(stride_times)
    if mean_stride_time:
        # 一个步周期含两步，所以 60 / (周期/2)。
        cadence = 120.0 / mean_stride_time

    estimate = _double_support(cycles, sync_quality)
    values = {
        "speed": _mean(speeds),
        "cadence": cadence,
        "stride": _mean(strides),
        "double-support": estimate.value,
    }
    return values, estimate, mean_stride_time


def _double_support(
    cycles: Sequence[GaitCycle], sync_quality: dict[str, Any] | None
) -> _DoubleSupport:
    """双支撑期占比，%。**估计量由证据决定，不由调用方决定。**

    这一项有两个不同的估计量，它们要的证据不一样，所以哪个能用不是一个偏好问题：

    1. **相位重叠口径**（`events.double_support`）—— 真正把两足的支撑相配对、量重叠。
       它是 PRD §13 点名的那个（「ZUPT 边界口径」，系统性偏低约 100 ms，RAY-288），
       也是唯一能与另一次采集比较的那个。但它**跨足**：`events.double_support` 在
       `sync_quality is None` 时直接拒绝，理由写在那个函数里 —— 80 ms 的跨足偏差
       会让占比整体挪 8 个百分点，而读数本身看不出任何异常。
    2. **站立相恒等式**（`2 × 平均站立相 − 100`）—— 每只脚的站立相占比是**足内量**，
       算这个数完全不碰跨足时序，所以它在没有同步依据时依然成立。代价是它给的是
       全程平均的一个推论，而不是量出来的相位。

    于是分支落在**手里有没有同步依据**上，而不是落在「谁在调我」上：拿得到同步质量
    的走 ①，拿不到的走 ②。两条报告路径喂同样的证据就走同一条分支、得同样的数 ——
    这正是「装配层只有一个」要保证的事。反过来，为了让两条路径的数字长得一样而
    强行统一成其中一个，会**要么**让采集端那份没有同步依据的报告失去一个 PRD §12
    的核心指标，**要么**让云端那份把量出来的相位换成一个推论。两个都是拿真实性换
    整齐，而 RAY-395 的第二条指控（`honest-sync-quality`）说的就是不许这么换。

    配对失败时（RAY-354 判据 1）返回 `None` 而不是退回 ②：同步依据在手却配不上步序，
    说明这次的跨足时序本身有问题，那时给一个推论出来是在替它圆场。

    **算不出来时 `caliber` 也是 `None`** —— 没有数就没有口径可标。给一个不可算的
    指标标上口径，等于声称我们知道那个不存在的数会是哪一支算的。
    """
    if sync_quality is not None:
        left, right = _by_foot(cycles, "L"), _by_foot(cycles, "R")
        if left and right:
            try:
                fraction = events.double_support(
                    left, right, sync_quality=sync_quality
                ).fraction
            except events.EventError:
                return _DoubleSupport(None, None)
            return _DoubleSupport(fraction * 100.0, CALIBER_PHASE_OVERLAP)
        return _DoubleSupport(None, None)

    mean_stance = _mean([cycle.stance_ratio for cycle in cycles])
    if mean_stance is None:
        return _DoubleSupport(None, None)
    candidate = 2 * mean_stance - 100.0
    # 负值不是一个小误差，是同步或事件检测出了问题（RAY-211 的自检判据之一）。
    # 印一个负的双支撑期比印「本次不适用」更糟：它看起来是个数。
    if candidate < 0:
        return _DoubleSupport(None, None)
    return _DoubleSupport(candidate, CALIBER_STANCE_IDENTITY)


def _format(value: float | None, unit: str) -> str:
    if value is None:
        return NOT_APPLICABLE
    return f"{value:.{_DIGITS.get(unit, 2)}f}"


def build_metrics(
    cycles: Sequence[GaitCycle],
    *,
    chain: str = CHAIN_BASIC,
    sync_quality: dict[str, Any] | None = None,
    zupt_quality: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], list[QualityAnnotation]]:
    """核心指标 + 它们各自的质量标注。

    返回两样东西是有意的：模板要前者，契约要后者，而它们必须来自**同一次**标注。
    先算指标再另外标一次，两者就可能对不上。
    """
    usable = _valid(cycles)
    n_steps = len(usable)
    values, estimate, mean_stride_time = _metric_values(usable, sync_quality)
    reference = _reference_percent(estimate, mean_stride_time)

    metrics: list[dict[str, Any]] = []
    annotations: list[QualityAnnotation] = []
    for key, title, unit, cross_foot in _CORE_METRICS:
        value = values[key]
        annotation = annotate(
            key,
            n_steps=n_steps,
            chain=chain,
            cross_foot=cross_foot,
            sync_quality=sync_quality,
            zupt_quality=zupt_quality,
            computable=value is not None,
        )
        annotations.append(annotation)
        metrics.append(
            _metric_row(
                key,
                title,
                unit,
                value,
                annotation,
                caliber=estimate.caliber if key == "double-support" else None,
                reference=reference if key == "double-support" else None,
            )
        )
    return metrics, annotations


def _metric_row(
    key: str,
    title: str,
    unit: str,
    value: float | None,
    annotation: QualityAnnotation,
    *,
    caliber: str | None = None,
    reference: float | None = None,
) -> dict[str, Any]:
    """一个核心指标块。

    `note` 与 `reason` **都要有，且各有各的位置** —— 这不是重复。模板
    （`ReportDocument.jsx` 的 `MetricValue`）读的是两个不同的键：不可算时印
    `metric.reason`，`low` 时印 `metric.note`。本层从前只给 `note`，于是一个不可算的
    核心指标在版面上是「本次不适用」后面跟一个**空的原因**；`assemble` 那条路从前
    只给 `reason`，于是 `low` 的指标落到模板里那句写死的通用话。两边各缺一个，而
    模板只有一份（R-4）—— 拉齐之后两个键一起给，缺口才真正合上。

    `note` 为 `None` 时**整个键不出现**，而不是出现一个 `null`：payload 要跨 IPC
    （RAY-248），一个 `null` 在 JSON 里读起来是「这里有个说明，但它是空的」。
    """
    row: dict[str, Any] = {
        "key": key,
        "title": title,
        "value": _format(value, unit),
        "unit": "" if annotation.grade == GRADE_UNCOMPUTABLE else unit,
        "grade": annotation.grade,
        "qualityLabel": quality_label(annotation.grade),
        # 契约要的「完整质量标注字段」。它不进版面，进的是可追溯性。
        "quality": annotation.snapshot(),
    }
    # 口径标注与等级说明是**两件事**，都要出现：口径说这个数是怎么算的，等级说它
    # 有多可信。合成一句而不是各占一处，是本 scope 对 R-4 的裁定 —— 模板只有一份，
    # 为参考值新加一处渲染要动那一份，而 `note` 本就是「关于这个数的一句话」。
    caliber_text = caliber_note(
        caliber, _format(reference, unit) + unit if reference is not None else None
    )
    grade_text = metric_note(annotation.grade, annotation.reasons)
    note = "；".join(part for part in (caliber_text, grade_text) if part)
    if note:
        row["note"] = note
    if annotation.grade == GRADE_UNCOMPUTABLE:
        row["reason"] = reason_text(list(annotation.reasons))
    if caliber is not None:
        row["caliber"] = caliber
    if reference is not None:
        # 结构化的参考值只进 payload、不进版面（版面那份在 `note` 里）。它在这里是
        # 为了让「强制携带未标定标注」这条判据可以被机器查，而不是靠读那句中文。
        row["reference"] = {
            "value": round(reference, 1),
            "unit": unit,
            "calibrated": False,
            "offsetSource": "synthetic-probe:probe_trim.txt",
        }
    return row


def build_comparison(cycles: Sequence[GaitCycle]) -> list[dict[str, Any]]:
    """左右对比。缺任一侧就整块不出 —— 单侧的「对比」不是对比。"""
    usable = _valid(cycles)
    rows: list[dict[str, Any]] = []
    for label, unit, getter in (
        ("步长", "m", lambda cycle: cycle.stride_length),
        ("站立相时长", "s", lambda cycle: cycle.stance_time),
    ):
        left = _mean([getter(c) for c in usable if c.foot == "L"])
        right = _mean([getter(c) for c in usable if c.foot == "R"])
        if left is None or right is None:
            continue
        rows.append({"label": label, "left": round(left, 3), "right": round(right, 3), "unit": unit})
    return rows


#: 模板 SVG 的可用横向范围。`ReportDocument.jsx` 的时序条 `viewBox` 是 `0 0 480 90`，
#: 基线从 x=10 画到 x=470，落步刻度直接当 `x1`/`x2` 用。
_TIMELINE_X0: float = 10.0
_TIMELINE_X1: float = 470.0


def build_timeline(cycles: Sequence[GaitCycle]) -> dict[str, list[float]]:
    """步态周期时序条：每只脚的初始触地时刻，**映射到模板 SVG 的 x 坐标**。

    这里出的不是秒。模板与 `report/html.py` 都把这两串数直接写进 `<line x1=...>`，
    所以一份以秒为单位的时序（一次 60 秒测试 → x ∈ [0, 60]）会把整条时序条挤在
    左边框那一小段里 —— 图照样画得出来，只是它不再表示任何东西。本层从前给的正是
    秒，`assemble` 那条路给的是坐标；拉齐取坐标，因为模板（R-4，唯一一份）读的是坐标。

    钳回 `[10, 470]`：浮点除法会让末点落在 470.00000000000006 之类，越出 viewBox。
    """
    usable = _valid(cycles)
    times = [cycle.t_ic for cycle in usable]
    if not times:
        return {"left": [], "right": []}
    t_min, t_max = min(times), max(times)
    span = (t_max - t_min) or 1.0
    width = _TIMELINE_X1 - _TIMELINE_X0

    def scale(value: float) -> float:
        x = _TIMELINE_X0 + width * (value - t_min) / span
        return min(_TIMELINE_X1, max(_TIMELINE_X0, x))

    return {
        "left": [scale(c.t_ic) for c in usable if c.foot == "L"],
        "right": [scale(c.t_ic) for c in usable if c.foot == "R"],
    }


def build_parameters(
    cycles: Sequence[GaitCycle],
    *,
    chain: str = CHAIN_BASIC,
    duration_s: int,
    turns: int | None = None,
    zupt_quality: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], list[QualityAnnotation]]:
    """专业参数：变异性、疲劳衰减、支撑/摆动相与转身次数，各带质量标注。

    疲劳衰减**只在 180 秒配置下产出**。这不是算法能力问题 —— 短协议里根本没有
    「前三分之一 vs 后三分之一」可比，所以它是 `uncomputable` 而不是 `low`。
    RAY-224 的描述专门点过这个容易实现错的地方：完整链不会把协议上就不产出的
    指标变出来。
    """
    from gait.analysis.variability import coefficient_of_variation, fatigue_decline

    usable = _valid(cycles)
    n_steps = len(usable)
    rows: list[dict[str, Any]] = []
    annotations: list[QualityAnnotation] = []

    for key, title, values in (
        ("stride-cv", "步长变异系数", [c.stride_length for c in usable]),
        ("cycle-cv", "步周期变异系数", [c.stride_time for c in usable]),
    ):
        value: float | None = None
        if len(values) >= 2:
            value = coefficient_of_variation(key, values).value
        annotation = annotate(
            key, n_steps=n_steps, chain=chain, computable=value is not None
        )
        annotations.append(annotation)
        rows.append(_parameter_row(title, value, "%", annotation))

    # 疲劳衰减：**能不能算由 `fatigue_decline` 自己回答**。
    #
    # 第一版在这里写了 `duration_s >= 180 and len(usable) >= 6` —— 那是把那个函数
    # 已经拥有的规则又实现了一遍。它自己文档写得很清楚：非 180 s 配置直接抛错，
    # 且需要至少 6 个有效周期。抄一份到这里，两处迟早对不上，而对不上的那天，
    # 报告会宣称一个那个函数根本没算的数。
    from gait.analysis.variability import VariabilityError

    fatigue: float | None = None
    protocol_too_short = False
    try:
        fatigue = fatigue_decline(usable, protocol_seconds=duration_s).decline * 100.0
    except VariabilityError as exc:
        protocol_too_short = "配置下输出" in str(exc)

    fatigue_annotation = annotate(
        "fatigue-decline", n_steps=n_steps, chain=chain, computable=fatigue is not None
    )
    annotations.append(fatigue_annotation)
    row = _parameter_row("疲劳衰减", fatigue, "%", fatigue_annotation)
    if protocol_too_short:
        # 覆盖通用原因：这一项不适用与步数无关，与本次时长配置有关。说成步数不足
        # 会让操作员去改一件改不了的事。
        row["note"] = f"本次为 {duration_s} 秒配置，该项需要 180 秒配置才产出。"
    rows.append(row)

    # ── 从 `assemble` 那条路并进来的几行（RAY-395 拉齐） ──────────────────────
    #
    # 它们不是新指标：PRD §13 的 v1 输出清单里就有「支撑相/摆动相占比（左/右）」与
    # 「转身次数」，只是从前只有走 `assemble_report` 的那条路把它们印出来。两条路
    # 合成一个装配层时，**并进来而不是删掉** —— 删掉等于让一份 PRD 认可的指标因为
    # 换了个装配函数而消失。
    for label, title in (("L", "左站立相占比"), ("R", "右站立相占比")):
        foot_cycles = _by_foot(usable, label)
        value = _mean([cycle.stance_ratio for cycle in foot_cycles])
        annotation = annotate(
            f"stance-ratio.{label}",
            n_steps=len(foot_cycles),
            chain=chain,
            zupt_quality=zupt_quality,
            computable=value is not None,
        )
        annotations.append(annotation)
        rows.append(_parameter_row(title, value, "%", annotation))

    mean_stance = _mean([cycle.stance_ratio for cycle in usable])
    # 摆动相占比是站立相的补 —— 一个步周期非站即摆。这不是一条质量规则，是定义，
    # 所以它待在这里而不是 `quality/`（FR-08 管的是定级，不是定义）。
    swing = None if mean_stance is None else 100.0 - mean_stance
    swing_annotation = annotate(
        "swing-ratio", n_steps=n_steps, chain=chain,
        zupt_quality=zupt_quality, computable=swing is not None,
    )
    annotations.append(swing_annotation)
    rows.append(_parameter_row("摆动相占比", swing, "%", swing_annotation))

    stride_time = _mean([cycle.stride_time for cycle in usable])
    stride_time_annotation = annotate(
        "stride-time", n_steps=n_steps, chain=chain,
        zupt_quality=zupt_quality, computable=stride_time is not None,
    )
    annotations.append(stride_time_annotation)
    rows.append(_parameter_row("步周期时长", stride_time, "s", stride_time_annotation))

    # 转身次数：**拿不到时是不可算，不是 0。**`assemble` 那条路从前写死
    # `grade=normal` 并在没有变异性报告时给 0 —— 那让报告断言「这次一次都没转身」，
    # 而实际情况是「这次没人数过」。0 是一个断言（与 `conditions` 里那句「未记录」
    # 同一个道理），所以这里把可算性绑在 `turns is not None` 上。
    turns_annotation = annotate(
        "turns", n_steps=n_steps, chain=chain, computable=turns is not None
    )
    annotations.append(turns_annotation)
    rows.append(
        _parameter_row("转身次数", None if turns is None else float(turns), "次", turns_annotation)
    )
    return rows, annotations


def _parameter_row(
    title: str, value: float | None, unit: str, annotation: QualityAnnotation
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "label": title,
        "value": _format(value, unit),
        "unit": "" if annotation.grade == GRADE_UNCOMPUTABLE else unit,
        "grade": annotation.grade,
        "qualityLabel": quality_label(annotation.grade),
        "quality": annotation.snapshot(),
    }
    note = metric_note(annotation.grade, annotation.reasons)
    if note is not None:
        row["note"] = note
    return row


#: 摘要与建议。**规则驱动，且刻意贫瘠。**
#:
#: PRD §12 不允许诊断措辞，只允许「建议关注 / 建议复测 / 建议进一步评估」。一句
#: 从数据里生成的、听起来很懂的话，正是这条规矩要防的东西 —— 它会被当成结论读。
#: 所以这里只按整体质量分三档说话，不解读任何具体数值。
_SUMMARY: dict[str, tuple[str, str]] = {
    "normal": (
        "本次步行的各项指标均已取得，数据质量良好。",
        "建议按常规随访；如近期有跌倒或步态改变，建议复测。",
    ),
    "low": (
        "本次步行的部分指标证据有限，报告中已逐项标注。",
        "建议关注标注为「参考」的项；必要时建议复测。",
    ),
    "uncomputable": (
        "本次步行有指标未能取得，报告中已逐项说明原因。",
        "建议复测；如反复出现，建议进一步评估采集条件。",
    ),
}


def build_report(
    cycles: Sequence[GaitCycle],
    *,
    report_id: str,
    organization: str,
    subject_label: str,
    assessed_at: str,
    duration_s: int,
    algo_version: str,
    protocol_version: str,
    valid_seconds: float | None,
    turns: int | None = None,
    protocol_name: str = "定时步行测试",
    annotations_text: Sequence[str] = (),
    chain: str = CHAIN_BASIC,
    sync_quality: dict[str, Any] | None = None,
    zupt_quality: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """一份基础报告的完整 payload。

    字段名与 `packages/report-template/ReportDocument.jsx` 一一对应（R-4：模板只有
    一份）；每项指标额外带 `quality`，那是 RAY-248 契约要的完整标注证据。
    """
    if not cycles:
        raise ReportError(
            "没有任何步态周期，无法生成报告。会话级无效不生成报告（PRD §13）—— "
            "调用方应当先看会话判定，而不是让报告层产出一份空报告。"
        )

    metrics, metric_annotations = build_metrics(
        cycles, chain=chain, sync_quality=sync_quality, zupt_quality=zupt_quality
    )
    parameters, parameter_annotations = build_parameters(
        cycles, chain=chain, duration_s=duration_s, turns=turns, zupt_quality=zupt_quality
    )
    # 页脚统计**全部**指标 —— 它回答的是「这份报告是怎么算出来的」。
    footer = summarize(metric_annotations + parameter_annotations)

    # 但摘要只看**核心指标**。
    #
    # `QualityFooter.overall` 取最差的一项，而 120 秒配置下疲劳衰减必然是
    # `uncomputable`（协议就不产出它）。用它来决定摘要，等于每一场 120 秒检测都会
    # 被写成「有指标未能取得……建议复测」—— 让操作员去重做一场完全正常的检测。
    #
    # 「本次不适用」与「这次没采好」是两件事，而只有后者值得让人重测。核心指标
    # 全部取得就说明这次采集是成功的，专业参数里协议不产出的那些不改变这个判断。
    summary, advice = _SUMMARY[summarize(metric_annotations).overall]
    usable = _valid(cycles)

    conditions = [
        {"label": "时长配置", "value": f"{duration_s} 秒"},
        # 有效时长同理：没人量过就说「未记录」。走 `assemble_report` 那条路的调用方
        # 手里只有一个 `ChainResult`，它不含有效时长 —— 编一个 0 秒出来会把一场
        # 正常的检测写成一场没采到东西的检测。
        {
            "label": "有效时长",
            "value": (
                f"{valid_seconds:.0f} 秒（{valid_seconds / duration_s:.0%}）"
                if valid_seconds is not None
                else "未记录"
            ),
        },
        {"label": "有效步数", "value": str(len(usable))},
        # 转身次数拿不到时说「未记录」，不写 0 —— 0 是一个断言，未记录不是。
        # PRD §12 ⑦ 把它列在「测试条件」里，PRD §13 又把它列进 v1 输出指标 ——
        # 两处都要，所以它同时出现在这里和专业参数表，而不是二选一。
        {"label": "转身次数", "value": str(turns) if turns is not None else "未记录"},
        # 「计算链」从 `assemble` 那条路并进来：基础版与完整版的同一个报告编号必须
        # 看得出差别（`_EDITIONS` 的理由），而封面那个角标印的是版本名，不是链名。
        {"label": "计算链", "value": "基础版" if chain == CHAIN_BASIC else "完整版"},
    ]

    return {
        "organization": organization,
        "subjectLabel": subject_label,
        "assessedAt": assessed_at,
        "protocolName": protocol_name,
        "protocolSeconds": duration_s,
        "edition": "基础版" if chain == CHAIN_BASIC else "完整版",
        "reportId": report_id,
        "algoVersion": algo_version,
        "protocolVersion": protocol_version,
        "annotations": list(annotations_text),
        "summary": summary,
        "advice": advice,
        "metrics": metrics,
        "comparison": build_comparison(cycles),
        "parameters": parameters,
        "timeline": build_timeline(cycles),
        "conditions": conditions,
        # PRD §13：grade 汇总规则版本化，进报告页脚。
        "qualityFooter": footer.snapshot(),
    }
