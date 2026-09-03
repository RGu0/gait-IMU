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
from typing import Any

from gait.contracts import GaitCycle
from gait.quality.annotate import (
    CHAIN_BASIC,
    GRADE_UNCOMPUTABLE,
    QualityAnnotation,
    annotate,
    summarize,
)
from gait.report.wording import NOT_APPLICABLE, metric_note, quality_label


class ReportError(ValueError):
    """报告无从生成。"""


#: 核心指标（P-09 与报告 §3）。`cross_foot` 决定它要不要同步证据。
_CORE_METRICS: tuple[tuple[str, str, str, bool], ...] = (
    # (key, 标题, 单位, 是否跨足)
    ("speed", "步速", "m/s", False),
    ("cadence", "步频", "步/分", False),
    ("stride", "步长", "m", False),
    ("double-support", "双支撑期占比", "%", True),
)


def _valid(cycles: Sequence[GaitCycle]) -> list[GaitCycle]:
    """只有 `valid` 的周期进指标。

    无效周期不是「差一点的数据」，是**已经被判定为不该参与计算的数据**。把它们
    算进去，等于让一次已经识别出来的错误重新影响结论。
    """
    return [cycle for cycle in cycles if cycle.valid]


def _mean(values: Sequence[float]) -> float | None:
    return statistics.fmean(values) if values else None


def _metric_values(cycles: Sequence[GaitCycle]) -> dict[str, float | None]:
    """四项核心指标的原始数值。算不出来的是 `None`，不是 0。"""
    speeds = [cycle.gait_speed for cycle in cycles]
    strides = [cycle.stride_length for cycle in cycles]
    stride_times = [cycle.stride_time for cycle in cycles]
    stance_ratios = [cycle.stance_ratio for cycle in cycles]

    cadence = None
    mean_stride_time = _mean(stride_times)
    if mean_stride_time:
        # 一个步周期含两步，所以 60 / (周期/2)。
        cadence = 120.0 / mean_stride_time

    # 双支撑期占比 = 站立相占比之和 − 100%。它是跨足量：两侧站立相重叠的那部分。
    mean_stance = _mean(stance_ratios)
    double_support = None
    if mean_stance is not None:
        candidate = 2 * mean_stance - 100.0
        # 负值不是一个小误差，是同步或事件检测出了问题（RAY-211 的自检判据之一）。
        # 印一个负的双支撑期比印「本次不适用」更糟：它看起来是个数。
        double_support = candidate if candidate >= 0 else None

    return {
        "speed": _mean(speeds),
        "cadence": cadence,
        "stride": _mean(strides),
        "double-support": double_support,
    }


def _format(value: float | None, unit: str) -> str:
    if value is None:
        return NOT_APPLICABLE
    digits = 1 if unit in ("%", "步/分") else 2
    return f"{value:.{digits}f}"


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
    values = _metric_values(usable)

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
            {
                "key": key,
                "title": title,
                "value": _format(value, unit),
                "unit": "" if annotation.grade == GRADE_UNCOMPUTABLE else unit,
                "grade": annotation.grade,
                "qualityLabel": quality_label(annotation.grade),
                "note": metric_note(annotation.grade, annotation.reasons),
                # 契约要的「完整质量标注字段」。它不进版面，进的是可追溯性。
                "quality": annotation.snapshot(),
            }
        )
    return metrics, annotations


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


def build_timeline(cycles: Sequence[GaitCycle]) -> dict[str, list[float]]:
    """步态周期时序条：每只脚的初始触地时刻。"""
    usable = _valid(cycles)
    return {
        "left": [round(c.t_ic, 3) for c in usable if c.foot == "L"],
        "right": [round(c.t_ic, 3) for c in usable if c.foot == "R"],
    }


def build_parameters(
    cycles: Sequence[GaitCycle], *, chain: str = CHAIN_BASIC, duration_s: int
) -> tuple[list[dict[str, Any]], list[QualityAnnotation]]:
    """专业参数：变异性与疲劳衰减，各带质量标注。

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
    return rows, annotations


def _parameter_row(
    title: str, value: float | None, unit: str, annotation: QualityAnnotation
) -> dict[str, Any]:
    return {
        "label": title,
        "value": _format(value, unit),
        "unit": "" if annotation.grade == GRADE_UNCOMPUTABLE else unit,
        "grade": annotation.grade,
        "qualityLabel": quality_label(annotation.grade),
        "note": metric_note(annotation.grade, annotation.reasons),
        "quality": annotation.snapshot(),
    }


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
    valid_seconds: float,
    turns: int | None = None,
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
        cycles, chain=chain, duration_s=duration_s
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
        {
            "label": "有效时长",
            "value": f"{valid_seconds:.0f} 秒（{valid_seconds / duration_s:.0%}）",
        },
        {"label": "有效步数", "value": str(len(usable))},
        # 转身次数拿不到时说「未记录」，不写 0 —— 0 是一个断言，未记录不是。
        {"label": "转身次数", "value": str(turns) if turns is not None else "未记录"},
    ]

    return {
        "organization": organization,
        "subjectLabel": subject_label,
        "assessedAt": assessed_at,
        "protocolName": "定时步行测试",
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
