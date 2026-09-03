"""报告 dict → 自包含 HTML（RAY-345 MVP 的无头渲染器）。

**这是 MVP 的临时渲染器，不是产品模板。** 产品里那份唯一的报告模板是
`packages/report-template/ReportDocument.jsx`（React），屏幕预览与 PDF 导出都走它
（R-4「永不两套实现」）。本文件存在的唯一理由是：在没有 Electron/浏览器的 CLI 里，
把 `assemble_report` 的产物落成一份能打开看的 `report.html`，好让
`python -m gait.cli.mvp --synthetic` 这条端到端闭环跑得通、可验收。

因此它必须遵守两条，且**只**遵守这两条：

1. **段序与措辞和 `ReportDocument` 对齐**——八个段（封面 / 标注条 / 筛查摘要 /
   核心指标 / 左右对比 / 专业参数 / 步态时序 / 测试条件 + 页脚），以及「本次不适用」
   这个不可算项的固定措辞。`tests/test_report_html.py` 断言这些段标题与措辞，
   正是为了不让这个临时渲染器和真正的模板在**语义**上漂移。
2. **不追求 `report.css` 的版式保真**——打印排版、灰度打印（C-9）、A4 边距这些是
   产品模板的验收项，不属于 MVP。这里只有一套让八个段可读的最小内联样式。
"""

from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any, Final

from gait.quality.annotate import GRADE_LOW, GRADE_NORMAL, GRADE_UNCOMPUTABLE

#: 产品模板里不可算项的固定措辞。这里必须一致，测试会断言。
_UNCOMPUTABLE_TEXT: Final[str] = "本次不适用"

#: 低质量指标的统一提示，与模板 `GRADE_NOTE.low` 一致。
_LOW_NOTE: Final[str] = "本次有效步数较少，此项仅供参考。"

_CSS: Final[str] = """
body { font-family: "Noto Sans SC", "PingFang SC", "Microsoft YaHei", sans-serif;
       color: #1c2b3a; max-width: 700px; margin: 0 auto; padding: 24px; line-height: 1.6; }
h1 { font-size: 22px; text-align: center; margin: 0 0 4px; }
h2 { font-size: 16px; border-bottom: 1px solid #dce7f2; padding-bottom: 4px; margin: 28px 0 12px; }
.cover { text-align: center; margin-bottom: 8px; }
.cover .org { color: #2569bc; font-weight: 600; }
.cover .edition { display: inline-block; background: #eef4fb; color: #2569bc;
                  border-radius: 3px; padding: 1px 8px; font-size: 12px; margin-left: 8px; }
.cover dl { display: inline-block; text-align: left; font-size: 13px; color: #5a6b7b; }
.annotation { background: #fff7e6; border-left: 3px solid #f0b429; padding: 6px 10px; font-size: 13px; }
.metrics { display: grid; grid-template-columns: repeat(2, 1fr); gap: 12px; }
.metric { border: 1px solid #dce7f2; border-radius: 6px; padding: 12px; }
.metric .title { font-size: 13px; color: #5a6b7b; }
.metric .value { font-size: 22px; font-weight: 700; margin: 4px 0 0; }
.metric .unit { font-size: 12px; color: #5a6b7b; font-weight: 400; }
.metric.low .value { color: #b05a00; }
.metric.uncomputable .value { color: #8a97a3; font-weight: 400; font-size: 15px; }
.metric .note { font-size: 12px; color: #b05a00; margin: 6px 0 0; }
.metric .reason { font-size: 12px; color: #8a97a3; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th, td { border: 1px solid #e6eef5; padding: 6px 10px; text-align: left; }
th { background: #f6fafd; }
.tag { display: inline-block; border-radius: 3px; padding: 1px 8px; font-size: 12px; }
.tag--normal { background: #e6f6e8; color: #1e7a2f; }
.tag--low { background: #fff2e0; color: #b05a00; }
.tag--uncomputable { background: #eef1f4; color: #5a6b7b; }
.compare { display: flex; align-items: center; gap: 10px; margin: 8px 0; font-size: 13px; }
.compare .name { width: 96px; }
.compare .side { font-size: 11px; color: #5a6b7b; width: 14px; }
.compare .bar { height: 14px; border-radius: 3px; }
.compare .l { background: #2569bc; }
.compare .r { background: repeating-linear-gradient(45deg, #17a2c4, #17a2c4 4px, #eaf6f9 4px, #eaf6f9 8px); }
.compare .num { width: 90px; text-align: right; }
.footer { margin-top: 32px; border-top: 1px solid #dce7f2; padding-top: 8px; font-size: 11px;
          color: #5a6b7b; text-align: center; }
"""


def _text(value: Any) -> str:
    """文本安全转义。数值转字符串，其余按文本。"""
    return html.escape(str(value))


def _metric_value(metric: dict[str, Any]) -> str:
    """与模板 `MetricValue` 同一条规则：不可算给「本次不适用 + reason」，否则给数值。"""
    if metric.get("grade") == GRADE_UNCOMPUTABLE:
        reason = metric.get("reason", "")
        return (
            f'<p class="value">{_text(_UNCOMPUTABLE_TEXT)}'
            + (f'<span class="reason">（{_text(reason)}）</span>' if reason else "")
            + "</p>"
        )
    return (
        f'<p class="value">{_text(metric.get("value", ""))}'
        f'<span class="unit">{_text(metric.get("unit", ""))}</span></p>'
    )


def _parameter_value(row: dict[str, Any]) -> str:
    """与模板 `parameterValue` 同一条规则。"""
    if row.get("grade") == GRADE_UNCOMPUTABLE:
        return _UNCOMPUTABLE_TEXT
    return _text(row.get("value", ""))


def render_report_html(report: dict[str, Any]) -> str:
    """把 `assemble_report` 的产物渲染成自包含 HTML 字符串。"""
    metrics_html = "\n".join(
        f'<div class="metric {metric.get("grade", GRADE_NORMAL)}">'
        f'<p class="title">{_text(metric.get("title", ""))}</p>'
        f"{_metric_value(metric)}"
        + (
            f'<p class="note">{_text(metric.get("note", _LOW_NOTE))}</p>'
            if metric.get("grade") == GRADE_LOW
            else ""
        )
        + "</div>"
        for metric in report.get("metrics", [])
    )

    comparison_html = "\n".join(
        f'<div class="compare">'
        f'<span class="name">{_text(row.get("label", ""))}</span>'
        f'<span class="side">左</span>'
        f'<div class="bar l" style="width:{max(row.get("left", 0.0), 0.0) * 0.0 + 60}px"></div>'
        f'<span class="num">{_text(row.get("left", ""))}</span>'
        f'<span class="side">右</span>'
        f'<div class="bar r" style="width:{max(row.get("right", 0.0), 0.0) * 0.0 + 60}px"></div>'
        f'<span class="num">{_text(row.get("right", ""))}</span>'
        f'<span>{_text(row.get("unit", ""))}</span>'
        f"</div>"
        for row in report.get("comparison", [])
    )

    parameters_html = "\n".join(
        "<tr>"
        f'<td>{_text(row.get("label", ""))}</td>'
        f"<td>{_parameter_value(row)}</td>"
        f'<td>{_text(row.get("unit", ""))}</td>'
        f'<td><span class="tag tag--{row.get("grade", GRADE_NORMAL)}">'
        f'{_text(row.get("qualityLabel", ""))}</span></td>'
        "</tr>"
        for row in report.get("parameters", [])
    )

    timeline = report.get("timeline", {})
    ticks_html = "".join(
        f'<line x1="{_text(x)}" y1="45" x2="{_text(x)}" y2="20" stroke="#2569BC" '
        'stroke-width="2.5" stroke-linecap="round"/>'
        for x in timeline.get("left", [])
    ) + "".join(
        f'<line x1="{_text(x)}" y1="45" x2="{_text(x)}" y2="70" stroke="#17A2C4" '
        'stroke-width="2.5" stroke-dasharray="6 4" stroke-linecap="round"/>'
        for x in timeline.get("right", [])
    )

    conditions_html = "\n".join(
        "<tr>"
        f'<td>{_text(row.get("label", ""))}</td>'
        f'<td>{_text(row.get("value", ""))}</td>'
        "</tr>"
        for row in report.get("conditions", [])
    )

    annotations = report.get("annotations") or []
    annotation_html = (
        f'<p class="annotation">{"；".join(_text(a) for a in annotations)}</p>'
        if annotations
        else ""
    )

    return "\n".join(
        [
            "<!DOCTYPE html>",
            '<html lang="zh-CN">',
            "<head>",
            '<meta charset="utf-8"/>',
            f"<title>{_text(report.get('subjectLabel', ''))} 步态检测报告</title>",
            f"<style>{_CSS}</style>",
            "</head>",
            "<body>",
            # ① 封面
            '<section class="cover">',
            '<h1>步态检测报告</h1>',
            (f'<p><span class="org">{_text(report.get("organization", ""))}</span>'
            f'<span class="edition">{_text(report.get("edition", ""))}</span></p>'),
            "<dl>",
            f"<dt>受检者编号：{_text(report.get('subjectLabel', ''))}</dt>",
            f"<dt>检测日期：{_text(report.get('assessedAt', ''))}</dt>",
            (f"<dt>测试项目：{_text(report.get('protocolName', ''))}"
            f"（{_text(report.get('protocolSeconds', ''))} 秒）</dt>"),
            "</dl>",
            "</section>",
            # ② 标注条（紧接封面）
            annotation_html,
            # ③ 筛查摘要
            "<section>",
            "<h2>筛查摘要</h2>",
            f"<p>{_text(report.get('summary', ''))}</p>",
            f"<p>{_text(report.get('advice', ''))}</p>",
            "</section>",
            # ④ 核心指标
            "<section>",
            "<h2>核心指标</h2>",
            f'<div class="metrics">{metrics_html}</div>',
            "</section>",
            # ⑤ 左右对比
            "<section>",
            "<h2>左右对比</h2>",
            comparison_html,
            "</section>",
            # ⑥ 专业参数
            "<section>",
            "<h2>专业参数</h2>",
            "<table><thead><tr><th>参数</th><th>数值</th><th>单位</th><th>质量</th></tr></thead>",
            f"<tbody>{parameters_html}</tbody></table>",
            "</section>",
            # ⑦ 步态时序
            "<section>",
            "<h2>步态时序</h2>",
            '<svg class="chart" viewBox="0 0 480 90" role="img" aria-label="步态周期时序条">',
            '<rect x="0.5" y="0.5" width="479" height="89" rx="6" fill="#F6FAFD" stroke="#DCE7F2"/>',
            '<line x1="10" y1="45" x2="470" y2="45" stroke="rgba(37,105,188,0.12)" stroke-width="1.5"/>',
            ticks_html,
            "</svg>",
            "</section>",
            # ⑧ 测试条件
            "<section>",
            "<h2>测试条件</h2>",
            "<table><tbody>",
            conditions_html,
            "</tbody></table>",
            "</section>",
            # 页脚
            '<footer class="footer">',
            (f"报告编号 {_text(report.get('reportId', ''))} · "
            f"算法版本 {_text(report.get('algoVersion', ''))} · "
            f"协议配置 {_text(report.get('protocolVersion', ''))}"),
            "</footer>",
            "</body>",
            "</html>",
        ]
    )


def write_report_html(report: dict[str, Any], out: Path, *, json_out: Path | None = None) -> Path:
    """把报告写成自包含 `report.html`，可选再写一份 `report.json` 留档。"""
    out = Path(out)
    out.write_text(render_report_html(report), encoding="utf-8")
    if json_out is not None:
        Path(json_out).write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    return out
