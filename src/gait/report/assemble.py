"""报告装配：`ChainResult` + `SessionMeta` → 报告模板 `ReportDocument` 要的 `report` 对象。

这是 RAY-224「本地基础报告生成」的最小切法（RAY-345 的最小 MVP 闭环）：它不画图、
不出 PDF、不接云端，只把已经算好的基础链结果翻译成前端 `packages/report-template`
的 `ReportDocument.jsx` 所消费的那个 dict。

## 契约：`report` 对象的形状只有一个来源

形状由 `packages/report-template/ReportDocument.jsx` 与它的测试
（`test/report.test.jsx`）锁定。本模块**不定义新形状**，只填充它。任何键名、等级
取值（`normal` / `low` / `uncomputable`）与段序的歧义，都以那一份模板为准。

## 为什么「不得 NaN/空白」是这里的责任，而不是模板的

模板的 `MetricValue` 把 `grade === "uncomputable"` 渲染成「本次不适用 + reason」，
其余渲染 `value`。如果这里把一个 `float("nan")` 塞进 `value`，JSON 序列化会产出
非法的 `NaN` 字面量，前端渲染成「NaN」——那读起来像"测到了一个叫 NaN 的东西"，
而不是"这项没算出来"。所以**每个数值在进 dict 之前都必须过一遍有限性检查**：
非有限值一律转成 `grade="uncomputable"` 并附上理由，而不是试着给它找个兜底数字。

## 等级翻译只做查表，不做判断

等级（`grade`）已经在 `quality/annotate.py` 算好、随 `ChainResult.annotations` 进来。
这里只做两件事：按指标名找到对应注解取最差等级，以及把等级翻译成前端要的
`qualityLabel`（`normal`→良好 / `low`→参考 / `uncomputable`→不适用）。**不在这里
重算任何分级**——那会立刻违反「质量逻辑只有 `quality/` 一处实现」的红线（FR-08）。
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any, Final

from gait.cloud.chain import ChainResult
from gait.config import DEFAULT_DURATION_S
from gait.contracts import FootLabel, SessionMeta
from gait.quality import annotate as quality
from gait.quality.annotate import (
    GRADE_LOW,
    GRADE_NORMAL,
    GRADE_UNCOMPUTABLE,
)

#: 前端 `qualityLabel` 的三档中文。等级是机器读的，这串字是给人看的。
_QUALITY_LABELS: Final[dict[str, str]] = {
    GRADE_NORMAL: "良好",
    GRADE_LOW: "参考",
    GRADE_UNCOMPUTABLE: "不适用",
}

#: 不可算项统一给的理由。与「测了但没值」区分开的正是这句话。
_UNCOMPUTABLE_REASON: Final[str] = "本次有效步数不足，未计算出该项。"

#: 前端 `edition` 由计算链决定。基础链与完整链的报告必须看得出差别。
_EDITIONS: Final[dict[str, str]] = {
    quality.CHAIN_BASIC: "基础版",
    quality.CHAIN_FULL: "完整版",
}

#: 摘要措辞受限（PRD §12）：筛查工具不说诊断语。
_SUMMARY_TEMPLATE: Final[str] = "本次 {seconds} 秒定时步行测试已完成，共记录 {steps} 个有效步。"
_ADVICE: Final[str] = "建议关注步态的日常变化，必要时复测。"

#: 一只脚缺 `.cycles` 时，`_timeline` 用的空占位。
_EMPTY_CYCLES: Final[tuple[Any, ...]] = ()


class AssembleError(ValueError):
    """报告装配的输入不完整。"""


def _finite(value: float | None) -> float | None:
    """有限数原样返回，NaN/Inf/非数一律 `None`。"""
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _fmt(value: float | None, ndigits: int) -> str:
    """数值转给前端展示的字符串。调用方必须先确认 `_finite(value) is not None`。"""
    number = _finite(value)
    assert number is not None, "不可算值不得进入格式化"
    text = f"{number:.{ndigits}f}"
    # 只在含小数点时去尾零：`"0"`、`"100"` 这类整数形态不能变成 `""` 或 `"1"`。
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def _grade(annotations: list[quality.QualityAnnotation], metric: str) -> str:
    """取名为 `metric`（或以其为 `.` 前缀）的注解里的最差等级。"""
    grades = [
        item.grade
        for item in annotations
        if item.metric == metric or item.metric.startswith(metric + ".")
    ]
    return quality.worst(grades)


def _foot_value(
    feet: dict[FootLabel, Any], metric: str
) -> tuple[float | None, dict[str, float | None]]:
    """取某指标在左右两脚的值，以及双脚中位数。脚缺失或该脚无数时记 `None`。"""
    per_foot: dict[str, float | None] = {}
    for label in ("L", "R"):
        outcome = feet.get(label)
        spatiotemporal = outcome.spatiotemporal if outcome else None
        raw = getattr(spatiotemporal, metric, None) if spatiotemporal else None
        per_foot[label] = _finite(raw)
    present = [value for value in per_foot.values() if value is not None]
    median = float(sorted(present)[len(present) // 2]) if present else None
    return median, per_foot


def _metric_item(
    key: str,
    title: str,
    unit: str,
    value: float | None,
    grade: str,
    ndigits: int,
) -> dict[str, Any]:
    """一个核心指标块：可算给 `value`，不可算给 `reason`。"""
    item: dict[str, Any] = {"key": key, "title": title, "unit": unit, "grade": grade}
    if grade == GRADE_UNCOMPUTABLE:
        item["reason"] = _UNCOMPUTABLE_REASON
    else:
        item["value"] = _fmt(value, ndigits)
    return item


def _parameter_item(
    label: str,
    unit: str,
    value: float | None,
    grade: str,
    ndigits: int,
) -> dict[str, Any]:
    """专业参数表的一行。`qualityLabel` 与 `grade` 一起走。"""
    item: dict[str, Any] = {
        "label": label,
        "unit": unit,
        "grade": grade,
        "qualityLabel": _QUALITY_LABELS[grade],
    }
    if grade != GRADE_UNCOMPUTABLE:
        item["value"] = _fmt(value, ndigits)
    return item


def _timeline(chain: ChainResult) -> dict[str, list[float]]:
    """足触地时刻映射到模板 SVG 的 x 坐标（10..470）。"""
    cycles = {
        label: getattr(chain.feet.get(label), "cycles", _EMPTY_CYCLES)
        for label in ("L", "R")
    }
    times = [
        t
        for series in cycles.values()
        for cycle in series
        if (t := _finite(cycle.t_ic)) is not None
    ]
    if not times:
        return {"left": [], "right": []}
    t_min, t_max = min(times), max(times)
    span = (t_max - t_min) or 1.0

    def scale(value: float) -> float:
        # 浮点除法会让末点落在 470.00000000000006 之类，钳回 [10, 470] 保证坐标
        # 永不越出模板 SVG 的 viewBox。
        return min(470.0, max(10.0, 10.0 + 460.0 * (value - t_min) / span))

    return {
        "left": [scale(t) for c in cycles["L"] if (t := _finite(c.t_ic)) is not None],
        "right": [scale(t) for c in cycles["R"] if (t := _finite(c.t_ic)) is not None],
    }


def _total_selected(feet: dict[FootLabel, Any]) -> int:
    return sum(len(outcome.selected) for outcome in feet.values())


def assemble_report(
    chain: ChainResult,
    meta: SessionMeta,
    *,
    subject_label: str | None = None,
    organization: str = "",
    protocol_name: str = "定时步行测试",
    report_id: str | None = None,
) -> dict[str, Any]:
    """把一条链的结果与一份会话元数据装配成 `ReportDocument` 消费的 dict。

    入参不要求完整：`meta` 只用到 `subject_uuid` / `created_at` / `protocol_config` /
    `contract_version`；`chain` 里没算出来的指标落到 `uncomputable`，而不是抛错——
    报告永远出得来，缺的东西说得清。
    """
    annotations = list(chain.annotations)
    seconds = int(meta.protocol_config.get("duration_s", DEFAULT_DURATION_S))
    subject = subject_label or f"**{meta.subject_uuid[:4]}"
    assessed = (meta.created_at or "")[:10] or datetime.now(UTC).strftime("%Y-%m-%d")
    edition = _EDITIONS.get(chain.chain, chain.chain)
    total_steps = _total_selected(chain.feet)

    speed_median, speed_foot = _foot_value(chain.feet, "gait_speed")
    cadence_median, _ = _foot_value(chain.feet, "cadence")
    _stride_median, stride_foot = _foot_value(chain.feet, "stride_length")
    _stance_median, stance_foot = _foot_value(chain.feet, "stance_ratio")
    swing_median, _ = _foot_value(chain.feet, "swing_ratio")
    stride_time_median, _ = _foot_value(chain.feet, "stride_time")

    ds_fraction = _finite(chain.double_support.fraction) if chain.double_support else None
    stride_cv = chain.variability.stride_length_cv.value if chain.variability else None
    stride_time_cv = chain.variability.stride_time_cv.value if chain.variability else None
    turns = chain.variability.turns if chain.variability else 0

    # ③ 核心指标。
    metrics = [
        _metric_item("speed", "步速", "m/s", speed_median, _grade(annotations, "gait_speed"), 2),
        _metric_item("cadence", "步频", "步/分", cadence_median, _grade(annotations, "cadence"), 1),
        _metric_item(
            "ds",
            "双支撑期占比",
            "%",
            (ds_fraction * 100) if ds_fraction is not None else None,
            _grade(annotations, "double_support_ratio"),
            1,
        ),
        _metric_item("cv", "步长变异系数", "%", _finite(stride_cv), _grade(annotations, "stride_length_cv"), 1),
    ]

    # ④ 左右对比。数值必须是数字；缺失用 0 占位（图表至少画得出来）。
    comparison = [
        {"label": "步长", "left": stride_foot.get("L") or 0.0, "right": stride_foot.get("R") or 0.0, "unit": "m"},
        {"label": "步速", "left": speed_foot.get("L") or 0.0, "right": speed_foot.get("R") or 0.0, "unit": "m/s"},
        {"label": "站立相占比", "left": stance_foot.get("L") or 0.0, "right": stance_foot.get("R") or 0.0, "unit": "%"},
    ]

    # ⑤ 专业参数。
    parameters = [
        _parameter_item("左站立相占比", "%", stance_foot.get("L"), _grade(annotations, "stance_ratio.L"), 1),
        _parameter_item("右站立相占比", "%", stance_foot.get("R"), _grade(annotations, "stance_ratio.R"), 1),
        _parameter_item("步周期时长", "s", stride_time_median, _grade(annotations, "stride_time"), 2),
        _parameter_item("摆动相占比", "%", swing_median, _grade(annotations, "swing_ratio"), 1),
        _parameter_item("步周期变异系数", "%", _finite(stride_time_cv), _grade(annotations, "stride_time_cv"), 1),
        _parameter_item("转身次数", "次", float(turns), GRADE_NORMAL, 0),
    ]

    conditions = [
        {"label": "时长配置", "value": f"{seconds} 秒"},
        {"label": "有效步数", "value": str(total_steps)},
        {"label": "计算链", "value": edition},
    ]

    protocol_version = meta.protocol_config.get("version", meta.contract_version)
    return {
        "organization": organization,
        "subjectLabel": subject,
        "assessedAt": assessed,
        "protocolName": protocol_name,
        "protocolSeconds": seconds,
        "edition": edition,
        "annotations": [],
        "summary": _SUMMARY_TEMPLATE.format(seconds=seconds, steps=total_steps),
        "advice": _ADVICE,
        "metrics": metrics,
        "comparison": comparison,
        "parameters": parameters,
        "timeline": _timeline(chain),
        "conditions": conditions,
        "reportId": report_id or f"R-{meta.session_id[:8]}-{meta.session_id[-4:]}",
        "algoVersion": chain.algo_version,
        "protocolVersion": f"T-01 v{protocol_version}",
    }
