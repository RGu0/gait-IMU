"""RAY-224 `basic-report`：本地基础报告的组装层。

验的是三类东西：
1. **形状**与 `packages/report-template/ReportDocument.jsx` 一致（R-4：模板只有一份）；
2. **措辞**守住 PRD §12 的硬规矩（无诊断措辞、指标永不留空）；
3. **规则不在这里**（FR-08）：等级与疲劳衰减的判据都来自各自的模块。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from gait.contracts import GaitCycle
from gait.quality.annotate import GRADE_NORMAL, GRADE_UNCOMPUTABLE
from gait.report import FORBIDDEN_WORDS, NOT_APPLICABLE, ReportError, build_report
from gait.report.basic import (
    _TRIM_PER_SIDE_MS,
    CALIBER_PHASE_OVERLAP,
    CALIBER_STANCE_IDENTITY,
    build_metrics,
)
from gait.report.wording import UNCALIBRATED_NOTE

TEMPLATE = Path(__file__).resolve().parents[1] / "packages/report-template/ReportDocument.jsx"


def cycle(index: int, foot: str, *, speed: float = 1.04, valid: bool = True) -> GaitCycle:
    return GaitCycle(
        foot=foot,
        idx=index,
        t_ic=index * 1.1,
        t_to=index * 1.1 + 0.6,
        t_ic_next=(index + 1) * 1.1,
        stride_length=1.15 + index * 0.005,
        stride_time=1.1,
        gait_speed=speed,
        stance_time=0.66,
        swing_time=0.44,
        stance_ratio=60.0,
        toe_clearance=0.02,
        strike_angle=12.0,
        valid=valid,
        confidence="normal",
    )


def cycles(count: int = 24, **kwargs) -> list[GaitCycle]:
    return [cycle(i, "L" if i % 2 else "R", **kwargs) for i in range(count)]


def report(**overrides):
    defaults = {
        "report_id": "R-2026-0903-0001",
        "organization": "康健社区卫生服务中心",
        "subject_label": "**2781",
        "assessed_at": "2026-09-03",
        "duration_s": 180,
        "algo_version": "gait-core 0.4.1",
        "protocol_version": "T-01 v3",
        "valid_seconds": 168.0,
    }
    data = overrides.pop("cycles", cycles())
    defaults.update(overrides)
    return build_report(data, **defaults)


# ── 形状：以模板为准，不另造 ──────────────────────────────────────────────


def test_every_field_the_template_reads_is_present() -> None:
    """模板读什么，payload 就必须有什么。

    这条断言直接从模板源码里抓 `report.X`，而不是抄一份字段清单 —— 抄的那份会在
    模板改了之后继续通过，而那正是「一份渲染不出来的报告」的来源。
    """
    consumed = set(re.findall(r"report\.([a-zA-Z]+)", TEMPLATE.read_text(encoding="utf-8")))
    assert consumed, "没能从模板里抓到任何字段，正则该更新了"
    missing = consumed - set(report())
    assert not missing, f"模板要读但 payload 没有：{sorted(missing)}"


def test_the_payload_is_json_serialisable() -> None:
    """它要跨 IPC（RAY-248），不能带任何非 JSON 的东西。"""
    json.dumps(report(), ensure_ascii=False)


def test_each_metric_carries_the_full_quality_evidence() -> None:
    """RAY-248 契约：报告 payload 要带完整质量标注字段。

    少了它，三个月后没人能回答「这一项为什么是参考级」。
    """
    for item in report()["metrics"]:
        quality = item["quality"]
        for field in ("n_steps", "sync_quality", "zupt_quality", "chain", "grade"):
            assert field in quality, f"{item['key']} 的 quality 缺 {field}"


def test_the_footer_counts_every_metric() -> None:
    """页脚回答「这份报告是怎么算出来的」，所以它统计全部指标。"""
    data = report()
    footer = data["qualityFooter"]
    assert footer["metrics"] == len(data["metrics"]) + len(data["parameters"])
    assert footer["rules_version"]


# ── 措辞：PRD §12 的硬规矩 ────────────────────────────────────────────────


@pytest.mark.parametrize("word", FORBIDDEN_WORDS)
def test_no_diagnostic_wording_anywhere_in_the_report(word: str) -> None:
    """这不是提醒，是拦截：整份 payload 逐字扫。

    这是一台筛查设备。用了诊断措辞，它在监管上的定性就变了 —— 那不是一句文案能
    承担的后果。
    """
    assert word not in json.dumps(report(), ensure_ascii=False)


def test_advice_uses_the_allowed_verbs() -> None:
    """PRD §12 只允许「建议关注 / 建议复测 / 建议进一步评估」。"""
    advice = report()["advice"]
    assert any(verb in advice for verb in ("建议关注", "建议复测", "建议进一步评估"))


def test_an_uncomputable_metric_never_renders_blank_or_zero() -> None:
    """PRD §12：不显示空白、0 或 N/A。

    一个消失的指标读起来是「没有这个量」，一个空的读起来是「这个量是零」——
    两个都是错的。
    """
    data = report(duration_s=120)
    fatigue = next(row for row in data["parameters"] if row["label"] == "疲劳衰减")
    assert fatigue["grade"] == GRADE_UNCOMPUTABLE
    assert fatigue["value"] == NOT_APPLICABLE
    assert fatigue["value"] not in ("", "0", "0.0", "N/A", "—")
    assert fatigue["note"]


def test_the_reason_for_not_applicable_is_the_protocol_not_the_data() -> None:
    """120 秒下疲劳衰减不适用，与步数无关。

    说成「步数不足」会让操作员去改一件他改不了的事。
    """
    data = report(duration_s=120)
    fatigue = next(row for row in data["parameters"] if row["label"] == "疲劳衰减")
    assert "120 秒配置" in fatigue["note"]
    assert "步数" not in fatigue["note"]


def test_a_short_protocol_does_not_make_the_whole_session_look_failed() -> None:
    """**这条是本 scope 最容易搞错的地方。**

    `QualityFooter.overall` 取最差的一项，而 120 秒配置下疲劳衰减必然
    `uncomputable`。用它来决定摘要，每一场 120 秒检测都会被写成「有指标未能取得
    ……建议复测」—— 让操作员去重做一场完全正常的检测。

    「本次不适用」与「这次没采好」是两件事，只有后者值得让人重测。
    """
    short = report(duration_s=120, sync_quality={"residual_ms": 4.0})
    long = report(duration_s=180, sync_quality={"residual_ms": 4.0})
    assert short["summary"] == long["summary"]
    assert "未能取得" not in short["summary"]


# ── 规则不在这里（FR-08） ────────────────────────────────────────────────


def test_grades_come_from_the_quality_module_not_from_here() -> None:
    """报告层一个阈值都不写。

    这条用「同一批周期、只改同步证据」来证明：等级变了，而报告层没有任何一行
    与同步有关的判断 —— 变化只可能来自 `quality.annotate`。
    """
    without = report()
    with_sync = report(sync_quality={"residual_ms": 4.0})

    def grade(data, key):
        return next(m["grade"] for m in data["metrics"] if m["key"] == key)

    # 双支撑期是跨足量：缺同步证据即降级（annotate 的文档写明这是质量问题）。
    assert grade(without, "double-support") != grade(with_sync, "double-support")
    assert grade(with_sync, "double-support") == GRADE_NORMAL


def test_the_fatigue_rule_is_not_reimplemented_here(monkeypatch) -> None:
    """疲劳衰减能不能算，由 `fatigue_decline` 自己回答。

    第一版在报告层写了 `duration_s >= 180 and len >= 6` —— 把那个函数已经拥有的
    规则又实现了一遍。这条测试钉住「问的是它」：让它对 180 秒也拒绝，报告必须跟着
    变成不适用。若报告层还留着自己的判断，这条会红。
    """
    from gait.analysis import variability

    def refuse(cycles, *, protocol_seconds):
        raise variability.VariabilityError("测试：拒绝计算")

    monkeypatch.setattr(variability, "fatigue_decline", refuse)
    data = report(duration_s=180)
    fatigue = next(row for row in data["parameters"] if row["label"] == "疲劳衰减")
    assert fatigue["grade"] == GRADE_UNCOMPUTABLE


# ── 边界 ──────────────────────────────────────────────────────────────────


def test_invalid_cycles_do_not_enter_the_metrics() -> None:
    """无效周期是**已经被判定为不该参与计算**的数据，不是差一点的数据。"""
    mixed = cycles(12) + [cycle(99, "L", speed=9.9, valid=False)]
    data = build_report(
        mixed,
        report_id="R",
        organization="O",
        subject_label="**1",
        assessed_at="2026-09-03",
        duration_s=180,
        algo_version="v",
        protocol_version="p",
        valid_seconds=170.0,
    )
    speed = next(m for m in data["metrics"] if m["key"] == "speed")
    assert float(speed["value"]) < 2.0  # 9.9 那条若混进来，均值会被拉高


def test_no_cycles_is_refused_rather_than_producing_an_empty_report() -> None:
    """会话级无效不生成报告（PRD §13）。产出一份空报告比拒绝更糟。"""
    with pytest.raises(ReportError, match="不生成报告"):
        report(cycles=[])


def test_turns_not_recorded_says_so_instead_of_zero() -> None:
    """0 是一个断言（「一次都没转」），未记录不是。"""
    conditions = {row["label"]: row["value"] for row in report()["conditions"]}
    assert conditions["转身次数"] == "未记录"
    recorded = {row["label"]: row["value"] for row in report(turns=14)["conditions"]}
    assert recorded["转身次数"] == "14"


# ─────────────────────────────────────────────────────────────────────────────
# RAY-437 `double-support-caliber`：口径标注跟着实际那一支变；
# 参考值只在相位重叠那一支给出（RAY-288 R2）。
# ─────────────────────────────────────────────────────────────────────────────

_SYNC_OK = {"determinate": True, "flagged": False}


def _double_support_row(**kwargs) -> dict:
    metrics, _ = build_metrics(cycles(24), **kwargs)
    return next(m for m in metrics if m["key"] == "double-support")


def test_caliber_follows_the_branch_that_actually_ran() -> None:
    """标注不能写死其中一句。

    两个估计量在版面上都是一个百分数，读者只能靠这句话分辨手上这个是量出来的
    相位重叠还是全程平均的推论。写死任意一句，另一支就会被标错 —— 而采集端
    `reportFor` 从不传同步质量，被标错的恰恰是操作员天天看的那一份。
    """
    with_sync = _double_support_row(sync_quality=_SYNC_OK)
    without = _double_support_row()

    assert with_sync["caliber"] == CALIBER_PHASE_OVERLAP
    assert without["caliber"] == CALIBER_STANCE_IDENTITY
    assert "ZUPT 边界口径" in with_sync["note"]
    assert "非 ZUPT 边界口径" in without["note"]


def test_the_stance_identity_branch_gets_no_reference_value() -> None:
    """那 ~100 ms 是相位重叠才受到的偏差。

    恒等式算的是 `2 × 平均站立相 − 100`，一个足内量，本来就没被 ZUPT 边界削过。
    给它加回去等于凭空抬高一个数 —— 这是 RAY-288 R2 修正 R1 的第二条。
    """
    row = _double_support_row()
    assert "reference" not in row
    assert "参考值" not in row["note"]


def test_the_reference_offset_is_percentage_points_not_milliseconds() -> None:
    """占比的分母是步周期，所以同样的 100 ms 在不同步频下是不同的百分点。

    这一条钉的是 RAY-288 R1 最初写错的那处单位：把 ms 直接加到百分数上。
    """
    row = _double_support_row(sync_quality=_SYNC_OK)
    raw = float(row["value"])
    reference = row["reference"]["value"]

    # 夹具的 stride_time 是 1.1 s。
    expected_offset = (2 * _TRIM_PER_SIDE_MS / 1000.0) / 1.1 * 100.0
    assert reference == pytest.approx(raw + expected_offset, abs=0.05)
    # 若误把 ms 当百分点直接相加，偏移会是 105.8 而不是约 9.6。
    assert expected_offset < 20.0


def test_a_reference_value_always_carries_the_uncalibrated_marker() -> None:
    """R2 把这句标注定为强制项。

    参考值的修正量与被修正量同一数量级（125 步/分下约 11 个百分点，而该指标正常值
    才 ~20%）。一个不带这句话的参考值会被当成测量结果读，而它的偏移来自合成探针。
    """
    row = _double_support_row(sync_quality=_SYNC_OK)
    assert row["reference"]["calibrated"] is False
    assert "probe_trim" in row["reference"]["offsetSource"]
    assert UNCALIBRATED_NOTE in row["note"]


def test_the_reference_value_does_not_change_the_grade() -> None:
    """参考值不参与任何判定（R2 验收）。等级只由 `quality.annotate` 定（FR-08）。"""
    row = _double_support_row(sync_quality=_SYNC_OK)
    assert row["grade"] == row["quality"]["grade"]
    assert "reference" not in row["quality"]


@pytest.mark.parametrize(
    ("cycles_in", "kwargs"),
    [
        # 没有周期：恒等式那一支拿不到平均站立相。
        ([], {}),
        # 有同步依据但只有一只脚：相位重叠配不成对。
        ([cycle(i, "L") for i in range(12)], {"sync_quality": _SYNC_OK}),
    ],
    ids=["no-cycles", "one-foot-only"],
)
def test_an_uncomputable_double_support_carries_no_caliber(cycles_in, kwargs) -> None:
    """没有数就没有口径可标 —— **三条不可算的出口都要守住**。

    给一个不可算的指标标上口径，等于声称我们知道那个不存在的数会是哪一支算的。

    这条最初只测了「没有周期」那一条出口，于是把口径写进另一条出口的变异**没有变红**。
    不可算有三条路（无周期 / 配不成对 / `EventError`），一条测试只走一条，
    另外两条就等于没有守卫。
    """
    metrics, _ = build_metrics(cycles_in, **kwargs)
    row = next(m for m in metrics if m["key"] == "double-support")
    assert row["grade"] == GRADE_UNCOMPUTABLE
    assert "caliber" not in row
    assert "reference" not in row
