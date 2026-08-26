"""完整链编排的测试。

这里最重要的一条是 `test_the_forward_stage_is_bit_identical_to_the_basic_chain`：
端云同构（PRD FR-08、整体设计 §0.2 取舍 2）说的是两条链共用同一个内核，而
"我们都调了同一个函数"是一句无法被违反的话 —— 只有逐位比较才是一条能失败的断言。
"""

import numpy as np
import pytest

from gait.cloud.chain import (
    BASIC_CHAIN_ALGO_VERSION,
    FULL_CHAIN_ALGO_VERSION,
    ChainError,
    run_basic_chain,
    run_full_chain,
)
from gait.config import AlgoConfig
from gait.core.eskf import run_ins, run_ins_with_history
from gait.quality.annotate import CHAIN_BASIC, CHAIN_FULL, QualityError, summarize
from gait.validate.synthetic import NoiseModel, WalkSpec, generate_dual_walk

SYNC = {"determinate": True, "flagged": False, "residual_p95": 0.002}
SENSOR = NoiseModel(accel_density=1.5e-3, gyro_density=3.0e-4, seed=3)


def dual(duration=20.0, **kwargs):
    pair = generate_dual_walk(WalkSpec(duration_s=duration, **kwargs), noise=SENSOR)
    return {label: pair[label][0] for label in pair}, pair


class TestTheSharedKernelRedLine:
    def test_the_forward_stage_is_bit_identical_to_the_basic_chain(self):
        """完整链的前向部分必须与基础链逐位相同。

        比的是 `run_ins` 的直接产物，不是链末的指标 —— 后者会被平滑改掉，那是应该的。
        这条断言守的是"前向内核只有一份"。
        """
        series, _ = dual()
        for label, foot_series in series.items():
            basic = run_ins(foot_series, AlgoConfig())
            recorded, _ = run_ins_with_history(foot_series, AlgoConfig())
            for field in ("p", "v", "q", "bg", "ba", "score"):
                assert np.array_equal(getattr(basic, field), getattr(recorded, field)), (
                    f"{label}.{field}"
                )

    def test_both_chains_annotate_through_the_same_entry_point(self):
        """两条链的标注项集合必须一致 —— 差别只在 `chain` 字段，不在"算了哪些指标"。"""
        series, _ = dual()
        basic = run_basic_chain(series, sync_quality=SYNC)
        full = run_full_chain(series, sync_quality=SYNC)
        assert [item.metric for item in basic.annotations] == [
            item.metric for item in full.annotations
        ]
        assert {item.chain for item in basic.annotations} == {CHAIN_BASIC}
        assert {item.chain for item in full.annotations} == {CHAIN_FULL}

    def test_the_footer_records_which_chain_produced_it(self):
        series, _ = dual()
        assert run_basic_chain(series, sync_quality=SYNC).footer.chain == CHAIN_BASIC
        assert run_full_chain(series, sync_quality=SYNC).footer.chain == CHAIN_FULL

    def test_a_report_cannot_mix_the_two_chains(self):
        """`quality.summarize` 已经拒绝混链。这条测试确认那道闸对本模块的产出也有效。"""
        series, _ = dual()
        mixed = [
            run_basic_chain(series, sync_quality=SYNC).annotations[0],
            run_full_chain(series, sync_quality=SYNC).annotations[0],
        ]
        with pytest.raises(QualityError, match="计算链"):
            summarize(mixed)


class TestTheFullChainRunsEveryStage:
    def test_every_stage_leaves_evidence(self):
        """任何一步被静默跳过都应当在这里暴露 —— 报告为空就是没跑。"""
        series, _ = dual()
        result = run_full_chain(series, sync_quality=SYNC)
        assert result.diagnostics["stages"] == [
            "forward_eskf", "rts_smoothing", "stance_anchoring", "dualfoot_constraint",
        ]
        for label, outcome in result.feet.items():
            assert outcome.smooth_report is not None, label
            assert outcome.smooth_report.max_position_shift > 0.0, label
            assert outcome.anchor_report is not None, label
            assert outcome.anchor_report.stances > 0, label
        assert result.dualfoot is not None

    def test_the_basic_chain_carries_no_smoothing_evidence(self):
        series, _ = dual()
        result = run_basic_chain(series, sync_quality=SYNC)
        for outcome in result.feet.values():
            assert outcome.smooth_report is None
            assert outcome.anchor_report is None
        assert result.dualfoot is None

    def test_the_full_chain_is_more_accurate_on_stride_length(self):
        """完整链存在的理由。标称档下不劣于前向链，且实际更好。"""
        series, pair = dual()
        truth_length = pair["L"][1].spec.stride_length
        basic = run_basic_chain(series, sync_quality=SYNC)
        full = run_full_chain(series, sync_quality=SYNC)

        basic_error = abs(basic.feet["L"].spatiotemporal.stride_length - truth_length)
        full_error = abs(full.feet["L"].spatiotemporal.stride_length - truth_length)
        assert full_error <= basic_error


class TestVersioning:
    def test_the_algo_version_reaches_the_result(self):
        """PRD §12：报告页脚含算法版本。它必须从链里带出来，不是由报告层现编。"""
        series, _ = dual()
        assert run_full_chain(series, sync_quality=SYNC).algo_version == FULL_CHAIN_ALGO_VERSION
        assert run_basic_chain(series, sync_quality=SYNC).algo_version == BASIC_CHAIN_ALGO_VERSION

    def test_an_explicit_version_is_honoured(self):
        """历史重算要能用旧版本号跑，否则"可回溯重算"无从谈起。"""
        series, _ = dual()
        result = run_full_chain(series, sync_quality=SYNC, algo_version="full-0.9.0")
        assert result.algo_version == "full-0.9.0"
        assert result.snapshot()["algo_version"] == "full-0.9.0"

    def test_the_snapshot_is_json_serialisable(self):
        import json

        series, _ = dual()
        payload = run_full_chain(series, sync_quality=SYNC).snapshot()
        assert json.loads(json.dumps(payload, ensure_ascii=False))["chain"] == CHAIN_FULL


class TestPartialInput:
    def test_a_single_foot_session_still_produces_a_report(self):
        """缺一只脚是数据问题，不是调用错误。跨足指标降级，其余照常。"""
        series, _ = dual()
        result = run_full_chain({"L": series["L"]}, sync_quality=SYNC)
        assert result.dualfoot is None, "单足不该跑双足约束"
        assert result.feet["L"].smooth_report is not None
        assert result.footer.chain == CHAIN_FULL
        cross = [item for item in result.annotations if item.metric == "double_support_ratio"]
        assert cross and cross[0].grade == "uncomputable"

    def test_no_feet_at_all_is_refused(self):
        with pytest.raises(ChainError, match="没有任何一只脚"):
            run_full_chain({})

    def test_missing_sync_quality_downgrades_cross_foot_metrics(self):
        """跨足指标离开同步质量就没有意义。降级而不是抛错 —— PRD §13 不做门控。"""
        series, _ = dual()
        result = run_full_chain(series)
        cross = [item for item in result.annotations if item.metric == "symmetry_index"]
        assert cross and cross[0].grade == "low"
        assert "missing_sync_quality" in cross[0].reasons
