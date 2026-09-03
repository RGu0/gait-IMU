"""`report.assemble` —— 基础链结果到报告 dict 的桥（RAY-345）。

这里的断言守三件事：

1. **形状与 `ReportDocument` 对齐**：模板要的键一个不缺，也不多定义新键。
2. **「不得 NaN/空白」**：整份报告 JSON 序列化后不含 `NaN`/`Infinity`，每个可算值
   都是有限数，不可算的落在 `grade="uncomputable"` 并带 `reason`。
3. **措辞受限**（PRD §12）：摘要与建议不出现诊断语，建议只走三种许可形式之一。
"""

import json
import math
import uuid

import pytest

from gait.cloud.chain import run_basic_chain
from gait.contracts import SessionMeta
from gait.quality.annotate import GRADE_UNCOMPUTABLE
from gait.report.assemble import assemble_report
from gait.validate.synthetic import WalkSpec, generate_dual_walk

_SYNC = {"determinate": True, "flagged": False, "residual_p95": 0.002}

_REPORT_KEYS = {
    "organization",
    "subjectLabel",
    "assessedAt",
    "protocolName",
    "protocolSeconds",
    "edition",
    "annotations",
    "summary",
    "advice",
    "metrics",
    "comparison",
    "parameters",
    "timeline",
    "conditions",
    "reportId",
    "algoVersion",
    "protocolVersion",
}


def make_meta(session_id: str = "20260901T120000Z-abcdef12", duration_s: int = 60) -> SessionMeta:
    return SessionMeta(
        session_id=session_id,
        created_at="2026-09-01T12:00:00Z",
        subject_uuid=str(uuid.uuid4()),
        scenario="walk",
        devices={"L": {"mac": "AA"}, "R": {"mac": "BB"}},
        config_snapshot={"rate_hz": 200},
        calib_snapshot={"L": {}, "R": {}},
        algo_version="basic-0.0.0",
        algo_params={"preset": "default"},
        sync_report={"anchors": 4},
        integrity_report={"loss_rate": 0.0},
        protocol_config={"duration_s": duration_s, "version": "2.3"},
    )


def make_chain(duration_s: float = 20.0):
    dual = generate_dual_walk(WalkSpec(duration_s=duration_s))
    series = {label: dual[label][0] for label in dual}
    return run_basic_chain(series, sync_quality=_SYNC, protocol_seconds=int(duration_s))


@pytest.fixture(scope="module")
def report():
    chain = make_chain()
    return assemble_report(chain, make_meta())


def test_report_has_exactly_the_template_keys(report):
    assert set(report) == _REPORT_KEYS


def test_footer_identifies_the_report(report):
    assert report["reportId"]
    assert report["algoVersion"]
    assert report["protocolVersion"]


def test_json_serializable_with_no_non_finite_numbers(report):
    text = json.dumps(report, ensure_ascii=False)
    assert "NaN" not in text
    assert "Infinity" not in text
    assert "null" not in text  # 没有空槽：可算给数值，不可算给 grade=uncomputable


def test_metrics_are_finite_or_uncomputable(report):
    for metric in report["metrics"]:
        assert metric["grade"] in {"normal", "low", "uncomputable"}
        if metric["grade"] == GRADE_UNCOMPUTABLE:
            assert metric.get("reason")
        else:
            value = float(metric["value"])
            assert math.isfinite(value)


def test_parameters_never_leave_a_blank_value(report):
    """可算参数必须有值（哪怕就是 `0`）；不可算的落 uncomputable 并带标签。"""
    for row in report["parameters"]:
        assert row["grade"] in {"normal", "low", "uncomputable"}
        if row["grade"] != GRADE_UNCOMPUTABLE:
            assert str(row["value"]).strip() != ""


def test_comparison_values_are_finite_numbers(report):
    for row in report["comparison"]:
        assert math.isfinite(row["left"])
        assert math.isfinite(row["right"])


def test_timeline_coordinates_are_within_the_svg_viewbox(report):
    for x in [*report["timeline"]["left"], *report["timeline"]["right"]]:
        assert 10.0 <= x <= 470.0


def test_wording_is_a_screening_not_a_diagnosis(report):
    import re

    assert not re.search(r"诊断|确诊|患有|疾病|异常步态|病症|阳性|阴性", report["summary"])
    assert not re.search(r"诊断|确诊|患有|疾病|异常步态|病症|阳性|阴性", report["advice"])
    assert re.search(r"建议关注|建议复测|建议进一步评估", report["advice"])


def test_uncomputable_when_only_one_foot():
    """单足输入下跨足与步数依赖项应落到 uncomputable，而不是抛错或给 NaN。"""
    dual = generate_dual_walk(WalkSpec(duration_s=20.0))
    series = {"L": dual["L"][0]}
    chain = run_basic_chain(series, sync_quality=_SYNC, protocol_seconds=20)
    report = assemble_report(chain, make_meta())
    text = json.dumps(report, ensure_ascii=False)
    assert "NaN" not in text and "Infinity" not in text
