"""`report.html` —— 无头渲染器的语义对齐测试（RAY-345）。

不测版式（那属于产品模板 `report-template`），只测语义：八个段的标题按
`ReportDocument` 的段序出现、不可算项用「本次不适用」这个固定措辞、页脚带全
三个身份字段、整份 HTML 里没有 `NaN`。
"""

import uuid

from gait.cloud.chain import run_basic_chain
from gait.contracts import SessionMeta
from gait.report.assemble import assemble_report
from gait.report.html import render_report_html
from gait.validate.synthetic import WalkSpec, generate_dual_walk

_SYNC = {"determinate": True, "flagged": False, "residual_p95": 0.002}


def make_meta() -> SessionMeta:
    return SessionMeta(
        session_id="20260901T120000Z-abcdef12",
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
        protocol_config={"duration_s": 60, "version": "2.3"},
    )


def _dual_report():
    dual = generate_dual_walk(WalkSpec(duration_s=20.0))
    series = {label: dual[label][0] for label in dual}
    chain = run_basic_chain(series, sync_quality=_SYNC, protocol_seconds=20)
    return assemble_report(chain, make_meta())


def test_sections_appear_in_template_order():
    markup = render_report_html(_dual_report())
    order = [
        "步态检测报告",
        "筛查摘要",
        "核心指标",
        "左右对比",
        "专业参数",
        "步态时序",
        "测试条件",
        "报告编号",
    ]
    positions = [markup.index(heading) for heading in order]
    assert positions == sorted(positions)


def test_footer_carries_the_three_identity_fields():
    report = _dual_report()
    markup = render_report_html(report)
    assert report["reportId"] in markup
    assert report["algoVersion"] in markup
    assert report["protocolVersion"] in markup


def test_no_nan_anywhere():
    assert "NaN" not in render_report_html(_dual_report())
    assert "Infinity" not in render_report_html(_dual_report())


def test_uncomputable_uses_the_canonical_wording():
    # 单足数据：双支撑期占比不可算，应当渲染成「本次不适用」而不是空白或占位数字。
    dual = generate_dual_walk(WalkSpec(duration_s=20.0))
    chain = run_basic_chain(
        {"L": dual["L"][0]},
        sync_quality=_SYNC,
        protocol_seconds=20,
    )
    report = assemble_report(chain, make_meta())
    markup = render_report_html(report)
    assert "本次不适用" in markup
