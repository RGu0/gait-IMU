"""零速段位置锚定的测试（`gait.core.stance_anchor`）。

模块名带 `stance_` 前缀是为了与 `gait.sync.anchor`（RAY-212 的物理对碰锚点）区分 ——
同一个"锚点"在本仓里指两件不同的事。

锚定要做的事只有一句：**支撑相内位置是常数**。所以测试的主体就是把这句话写成断言，
再把"它有没有把别的东西弄坏"（总位移、相外样本、无支撑相的退化输入）逐条钉住。
"""

import numpy as np
import pytest

from gait.config import AlgoConfig
from gait.contracts import NavResult
from gait.core.eskf import run_ins, run_ins_with_history
from gait.core.rts import smooth
from gait.core.stance_anchor import AnchorError, anchor_stance_positions
from gait.validate.synthetic import NoiseModel, WalkSpec, generate_walk

SENSOR = NoiseModel(accel_density=1.5e-3, gyro_density=3.0e-4, seed=3)


def walk(duration=30.0, **kwargs):
    return generate_walk(WalkSpec(duration_s=duration, **kwargs), noise=SENSOR)


def intra_stance_spread(navigation):
    """每个支撑相内位置到该相均值的最大偏离。"""
    return [
        float(np.max(np.linalg.norm(navigation.p[start:end] - navigation.p[start:end].mean(axis=0), axis=1)))
        for start, end in navigation.stances
    ]


class TestTheInvariant:
    def test_positions_inside_a_stance_become_exactly_constant(self):
        series, _ = walk()
        anchored = anchor_stance_positions(run_ins(series, AlgoConfig())).navigation
        for start, end in anchored.stances:
            spread = np.max(np.linalg.norm(anchored.p[start:end] - anchored.p[start], axis=1))
            assert spread == pytest.approx(0.0, abs=1e-12), f"支撑相 [{start},{end}) 内位置仍在动"

    def test_it_removes_the_creep_ray_261_measured(self):
        """RAY-261 量到前向链每个支撑相内约 4 cm 蠕动。锚定后应当归零。"""
        series, _ = walk()
        forward = run_ins(series, AlgoConfig())
        before = np.median(intra_stance_spread(forward))
        assert before > 0.005, "前向链本应有可观的相内蠕动，否则这条测试测不到东西"

        anchored = anchor_stance_positions(forward)
        assert np.max(intra_stance_spread(anchored.navigation)) == pytest.approx(0.0, abs=1e-12)
        assert anchored.report.median_creep_before > 0.005
        assert anchored.report.stances == len(forward.stances)


class TestItDoesNotBreakOtherThings:
    def test_the_overall_displacement_barely_moves(self):
        """锚定该去掉的是相内蠕动，不是整条轨迹的位移。"""
        series, _ = walk()
        forward = run_ins(series, AlgoConfig())
        result = anchor_stance_positions(forward)
        total = float(np.linalg.norm(forward.p[-1] - forward.p[0]))
        assert result.report.total_displacement_change < 0.02 * total

    def test_the_trajectory_stays_continuous_across_swing(self):
        """摆动相的修正量是线性插值的，所以修正后的轨迹不应出现新的跳变。"""
        series, _ = walk()
        forward = run_ins(series, AlgoConfig())
        anchored = anchor_stance_positions(forward).navigation
        jumps_before = np.max(np.linalg.norm(np.diff(forward.p, axis=0), axis=1))
        jumps_after = np.max(np.linalg.norm(np.diff(anchored.p, axis=0), axis=1))
        assert jumps_after < jumps_before * 1.5

    def test_attitude_and_velocity_are_untouched(self):
        """只修位置。理由见模块文档 —— 由位置差分反推速度会造出一堆边界尖峰。"""
        series, _ = walk(duration=10.0)
        forward = run_ins(series, AlgoConfig())
        anchored = anchor_stance_positions(forward).navigation
        assert np.array_equal(anchored.q, forward.q)
        assert np.array_equal(anchored.v, forward.v)
        assert np.array_equal(anchored.zupt, forward.zupt)


class TestDegenerateInput:
    def test_no_stances_is_a_no_op_not_an_error(self):
        """一段纯摆动是合法输入，只是这一步无事可做。"""
        series, _ = walk(duration=5.0)
        forward = run_ins(series, AlgoConfig())
        without = NavResult(
            t=forward.t, q=forward.q, v=forward.v, p=forward.p, bg=forward.bg, ba=forward.ba,
            zupt=np.zeros(len(forward.t), dtype=bool), stances=[],
            degraded=forward.degraded, score=forward.score,
        )
        result = anchor_stance_positions(without)
        assert result.report.stances == 0
        assert np.array_equal(result.navigation.p, forward.p)

    def test_a_non_navresult_is_refused(self):
        with pytest.raises(AnchorError):
            anchor_stance_positions(object())  # type: ignore[arg-type]


class TestWhatItAddsAfterSmoothing:
    """RTS 之后锚定还剩多少事可做 —— 这条测试记录的是一个**实测结论**。

    实测：RTS 平滑本身已经把相内蠕动从约 4 cm 压到亚毫米量级，锚定在它之后基本
    无事可做。这不是缺陷，但它是一件必须被记录的事 —— 否则下一个人会以为完整链
    的收益里有一份来自锚定。
    """

    def test_smoothing_alone_already_removes_most_of_the_creep(self):
        series, _ = walk()
        forward, history = run_ins_with_history(series, AlgoConfig())
        smoothed = smooth(forward, history).navigation

        creep_forward = np.median(intra_stance_spread(forward))
        creep_smoothed = np.median(intra_stance_spread(smoothed))
        assert creep_forward > 0.005
        assert creep_smoothed < creep_forward / 10.0

    def test_anchoring_still_enforces_the_invariant_exactly(self):
        """"基本无事可做"不等于"可以不做"：不变量成立与否是一个是非题。"""
        series, _ = walk()
        forward, history = run_ins_with_history(series, AlgoConfig())
        smoothed = smooth(forward, history).navigation
        assert np.max(intra_stance_spread(smoothed)) > 0.0

        anchored = anchor_stance_positions(smoothed).navigation
        assert np.max(intra_stance_spread(anchored)) == pytest.approx(0.0, abs=1e-12)
