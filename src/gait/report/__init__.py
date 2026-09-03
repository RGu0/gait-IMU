"""本地基础报告（RAY-224 `basic-report`）。

组装层：把 `analysis/` 的结果与 `quality/` 的定级拼成一份报告 payload。
**不实现任何质量规则**（FR-08），**不定义 payload 形状**（以模板与 RAY-248 契约为准）。
"""

from gait.report.basic import (
    ReportError,
    build_comparison,
    build_metrics,
    build_parameters,
    build_report,
    build_timeline,
)
from gait.report.wording import FORBIDDEN_WORDS, NOT_APPLICABLE

__all__ = [
    "FORBIDDEN_WORDS",
    "NOT_APPLICABLE",
    "ReportError",
    "build_comparison",
    "build_metrics",
    "build_parameters",
    "build_report",
    "build_timeline",
]
