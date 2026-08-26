"""完整链 vs 前向链的对照回归（RAY-227，回贴 RAY-261）。

与 `test_v1a_regression.py` 同一口径：30 s 合成会话，BS-BT91 量级传感器噪声，逐 stride
比对**真值触地时刻**之间的位移，只统计直行 stride。差别只有一条 —— 那边跑前向链，
这边比前向链与完整链。

## 实测（2026-08-25，单足）

| 步态 | n | 前向 mean % | 前向 rms % | 完整 mean % | 完整 rms % | rms 降幅 |
| --- | --- | --- | --- | --- | --- | --- |
| 走 108 spm | 25 | +0.145 | 0.264 | +0.020 | 0.065 | 75.3% |
| 快走 150 spm | 35 | −0.003 | 0.083 | −0.041 | 0.054 | 34.7% |
| 慢跑 170 spm | 40 | +0.115 | 0.219 | +0.021 | 0.079 | 64.1% |
| 4 米往返 | 15 | +0.177 | 0.352 | +0.137 | 0.223 | 36.6% |
| **低速 60 spm s3** | 13 | **+2.225** | 4.961 | **+0.464** | 1.076 | 78.3% |
| **低速 60 spm s7** | 13 | **+3.067** | 6.641 | **+0.328** | 0.594 | 91.1% |

**rms 是这里的主指标，不是 mean。** 快走档的前向 mean 已经是 −0.003%，拿它做分母算
"降幅"会得到一个荒谬的数（−1393%），而那只是除以零的近亲，不是退化。rms 在六档上
一致下降。

## 收益的来源不是"航向变准了"

这是本次实验最值得记下来的一条，因为它与直觉相反：

| 步态 | 链 | 航向误差均值 | 航向误差标准差 | 每 stride 内航向漂移（中位） |
| --- | --- | --- | --- | --- |
| 走 108 spm | 前向 | −3.34° | 1.21° | 0.172° |
| 走 108 spm | 完整 | −3.87° | **0.08°** | **0.014°** |
| 低速 s3 | 前向 | +6.58° | 4.60° | 0.680° |
| 低速 s3 | 完整 | **+10.56°** | **0.28°** | **0.046°** |
| 低速 s7 | 前向 | +10.24° | 5.71° | 0.961° |
| 低速 s7 | 完整 | +11.50° | **0.18°** | **0.070°** |

航向误差的**均值没有变小，反而略大**；变小的是它的**标准差**与**相内漂移**，都降了
一个数量级以上。

原因是步长的定义：相邻触地之间位移的模长。**一个恒定的航向偏置是一次刚性旋转，
保模长**；破坏步长的是航向在一个 stride 之内的**漂移** —— 它让同一段积分的各个片段
被转到互不一致的方向上。RTS 把"边走边漂"的航向误差改造成"从头到尾一样偏"的航向误差，
步长因此被救回，而**航向本身没有被救回**。

这条区分是有后果的，所以不能省：依赖绝对航向的东西（轨迹图、行进方向、闭环终点偏差）
**不会**因为完整链而变好。RAY-261 记录的根因是航向可观测性坍塌，本实验没有推翻它。
"""

from itertools import pairwise
from typing import ClassVar

import numpy as np
import pytest

from gait.cloud.chain import run_basic_chain, run_full_chain
from gait.config import AlgoConfig
from gait.core import quaternion as quat
from gait.core import rts, stance_anchor
from gait.core.eskf import run_ins_with_history
from gait.validate.synthetic import (
    NoiseModel,
    WalkSpec,
    generate_dual_walk,
    generate_walk,
)

#: V1-a 的预算（PRD v1.2 §17.1）。这里用它做"完整链不劣于前向链"的参照，
#: **不**用它给低速档定判据 —— 那已由 RAY-206 R2 定为"不设数值判据，只降级标注"。
BUDGET_PERCENT = 0.5

NOMINAL = {
    "walk": ({}, 1.30),
    "fast_walk": ({"cadence": 150.0, "stance_ratio": 0.52, "stride_length": 1.7}, 1.70),
    "jog": ({"cadence": 170.0, "stance_ratio": 0.38, "stride_length": 2.4}, 2.40),
    "turnaround_4m": ({"path_length_m": 4.0}, 1.30),
}
LOW_SPEED = ({"cadence": 60.0, "stride_length": 0.35, "stance_ratio": 0.75}, 0.35)


def sensor(seed=3):
    return NoiseModel(accel_density=1.5e-3, gyro_density=3.0e-4, seed=seed)


def chains(spec_kwargs, seed=3, seconds=30.0):
    """同一份数据上的前向链与完整链。返回 `(前向 NavResult, 完整 NavResult, 真值)`。"""
    series, truth = generate_walk(
        WalkSpec(duration_s=seconds, **spec_kwargs), noise=sensor(seed)
    )
    forward, history = run_ins_with_history(series, AlgoConfig())
    smoothed = rts.smooth(forward, history).navigation
    full = stance_anchor.anchor_stance_positions(smoothed).navigation
    return forward, full, truth


def stride_stats(navigation, truth, reference):
    """逐 stride 步长误差的 (mean%, rms%, n)。"""
    fs = 1.0 / float(np.median(np.diff(navigation.t)))
    strides = [item for item in truth.strides if not item.is_turn]
    errors = []
    for current, following in pairwise(strides):
        start = round(current.t_ic * fs)
        end = round(following.t_ic * fs)
        if end >= len(navigation.p):
            break
        displacement = navigation.p[end, :2] - navigation.p[start, :2]
        errors.append(
            (float(np.linalg.norm(displacement)) - current.stride_length) * 100.0 / reference
        )
    values = np.array(errors)
    return float(values.mean()), float(np.sqrt((values**2).mean())), len(values)


def yaw_error_deg(navigation, truth):
    """逐样本航向误差，deg，已解缠到 (−180, 180]。"""
    _, _, estimated = quat.to_euler(navigation.q)
    _, _, true_yaw = quat.to_euler(truth.q)
    return np.degrees(np.arctan2(np.sin(estimated - true_yaw), np.cos(estimated - true_yaw)))


def intra_stride_yaw_drift(navigation, truth):
    """每个 stride 内航向误差的变化量，deg。破坏步长的正是这一项。"""
    error = yaw_error_deg(navigation, truth)
    fs = 1.0 / float(np.median(np.diff(navigation.t)))
    strides = [item for item in truth.strides if not item.is_turn]
    drift = []
    for current, following in pairwise(strides):
        start = round(current.t_ic * fs)
        end = round(following.t_ic * fs)
        if end >= len(error):
            break
        drift.append(abs(float(error[end] - error[start])))
    return np.array(drift)


class TestTheFullChainIsNeverWorse:
    @pytest.mark.parametrize("gait", sorted(NOMINAL))
    def test_nominal_tiers_stay_within_budget(self, gait):
        """标称四档：完整链必须仍在 V1-a 预算内。"""
        spec_kwargs, reference = NOMINAL[gait]
        _, full, truth = chains(spec_kwargs)
        mean, _, _ = stride_stats(full, truth, reference)
        assert abs(mean) < BUDGET_PERCENT

    @pytest.mark.parametrize("gait", sorted(NOMINAL))
    def test_the_spread_shrinks_on_every_nominal_tier(self, gait):
        """rms 是主指标：mean 在某些档上本来就接近零，比它没有意义。"""
        spec_kwargs, reference = NOMINAL[gait]
        forward, full, truth = chains(spec_kwargs)
        _, forward_rms, _ = stride_stats(forward, truth, reference)
        _, full_rms, _ = stride_stats(full, truth, reference)
        assert full_rms < forward_rms


class TestTheLowSpeedTier:
    """RAY-261 的那两个 seed。**只记录数据，不改判据** —— 低速档"不设数值判据、
    只降级标注"已由 RAY-206 R2 定案，本文件不碰它。"""

    @pytest.mark.parametrize("seed", [3, 7])
    def test_the_forward_chain_still_misses_by_a_lot(self, seed):
        """前向链的表现没有变 —— 完整链是**增补**，不是对前向链的修改。"""
        spec_kwargs, reference = LOW_SPEED
        forward, _, truth = chains(spec_kwargs, seed=seed)
        mean, _, _ = stride_stats(forward, truth, reference)
        assert abs(mean) > 1.5, "前向链在低速档本应显著超预算（RAY-261 实测 2.1~3.1%）"

    @pytest.mark.parametrize("seed", [3, 7])
    def test_the_full_chain_recovers_most_of_it(self, seed):
        spec_kwargs, reference = LOW_SPEED
        forward, full, truth = chains(spec_kwargs, seed=seed)
        before, before_rms, _ = stride_stats(forward, truth, reference)
        after, after_rms, _ = stride_stats(full, truth, reference)

        assert abs(after) < abs(before) / 3.0, "至少要救回三分之二"
        assert after_rms < before_rms / 3.0
        # 实测落在 0.33~0.47%。留出余量，但仍是一条会因退化而失败的断言。
        assert abs(after) < 1.0


class TestWhyItWorks:
    """收益来自航向误差**变恒定**，不是**变小**。这条区分有后果，所以钉成测试。"""

    @pytest.mark.parametrize("seed", [3, 7])
    def test_the_absolute_heading_error_does_not_improve(self, seed):
        """反直觉的一半：航向误差的均值没有变好。

        它变坏了这条测试也接受 —— 要断言的是"完整链没有把航向修准"，而不是某个方向。
        """
        spec_kwargs, _ = LOW_SPEED
        forward, full, truth = chains(spec_kwargs, seed=seed)
        forward_bias = abs(float(np.mean(yaw_error_deg(forward, truth))))
        full_bias = abs(float(np.mean(yaw_error_deg(full, truth))))
        assert full_bias > forward_bias * 0.8, (
            "若航向的绝对误差真的被修好了，本实验的结论需要重写"
        )

    @pytest.mark.parametrize("seed", [3, 7])
    def test_the_heading_error_becomes_nearly_constant(self, seed):
        """有用的一半：航向误差的散布塌掉一个数量级以上。"""
        spec_kwargs, _ = LOW_SPEED
        forward, full, truth = chains(spec_kwargs, seed=seed)
        forward_spread = float(np.std(yaw_error_deg(forward, truth)))
        full_spread = float(np.std(yaw_error_deg(full, truth)))
        assert full_spread < forward_spread / 10.0

    @pytest.mark.parametrize("seed", [3, 7])
    def test_the_intra_stride_drift_collapses(self, seed):
        """这才是与步长直接相关的量：一个 stride 之内航向误差变了多少。"""
        spec_kwargs, _ = LOW_SPEED
        forward, full, truth = chains(spec_kwargs, seed=seed)
        assert float(np.median(intra_stride_yaw_drift(full, truth))) < float(
            np.median(intra_stride_yaw_drift(forward, truth))
        ) / 5.0


class TestTheDualFootGuard:
    """低速档的差分航向拟合会顶到搜索边界。顶到边界的拟合不该被采用。"""

    SYNC: ClassVar[dict[str, bool]] = {"determinate": True, "flagged": False}

    def _dual(self, spec_kwargs, seed):
        pair = generate_dual_walk(WalkSpec(duration_s=30.0, **spec_kwargs), noise=sensor(seed))
        return {label: pair[label][0] for label in pair}, pair

    def test_nominal_walking_applies_the_constraint(self):
        series, _ = self._dual({}, 3)
        result = run_full_chain(series, sync_quality=self.SYNC)
        assert result.diagnostics["dualfoot_applied"] is True
        assert result.dualfoot is not None and not result.dualfoot.hit_search_bound

    def test_the_low_speed_fit_saturates_and_is_declined(self):
        """采用它会把已经被 RTS 修好的低速轨迹重新推歪（实测 0.46% → 1.52%）。"""
        series, _ = self._dual(LOW_SPEED[0], 3)
        result = run_full_chain(series, sync_quality=self.SYNC)
        assert result.dualfoot is not None
        assert result.dualfoot.hit_search_bound is True
        assert result.diagnostics["dualfoot_applied"] is False
        assert result.diagnostics["dualfoot_declined_reason"] == "hit_search_bound"

    def test_declining_still_reports(self):
        """拒绝采用是一个要被看见的决定，不是静默跳过。"""
        series, _ = self._dual(LOW_SPEED[0], 3)
        snapshot = run_full_chain(series, sync_quality=self.SYNC).snapshot()
        assert snapshot["dualfoot"] is not None
        assert snapshot["dualfoot"]["hit_search_bound"] is True


class TestTheDeliveredChainEndToEnd:
    """经由 `cloud/chain.py` 的完整链，而不是手工拼的三步。"""

    SYNC: ClassVar[dict[str, bool]] = {"determinate": True, "flagged": False}

    def test_the_full_chain_beats_the_basic_chain_on_stride_length(self):
        pair = generate_dual_walk(WalkSpec(duration_s=30.0), noise=sensor())
        series = {label: pair[label][0] for label in pair}
        truth_length = pair["L"][1].spec.stride_length

        basic = run_basic_chain(series, sync_quality=self.SYNC)
        full = run_full_chain(series, sync_quality=self.SYNC)
        basic_error = abs(basic.feet["L"].spatiotemporal.stride_length - truth_length)
        full_error = abs(full.feet["L"].spatiotemporal.stride_length - truth_length)
        assert full_error < basic_error

    def test_the_low_speed_session_improves_end_to_end(self):
        pair = generate_dual_walk(WalkSpec(duration_s=30.0, **LOW_SPEED[0]), noise=sensor())
        series = {label: pair[label][0] for label in pair}
        truth_length = pair["L"][1].spec.stride_length

        basic = run_basic_chain(series, sync_quality=self.SYNC)
        full = run_full_chain(series, sync_quality=self.SYNC)
        basic_error = abs(basic.feet["L"].spatiotemporal.stride_length - truth_length)
        full_error = abs(full.feet["L"].spatiotemporal.stride_length - truth_length)
        assert full_error < basic_error
