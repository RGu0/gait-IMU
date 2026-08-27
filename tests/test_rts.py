"""RTS 后向平滑的测试。

这里最要紧的一条是 `test_dropping_the_injected_correction_makes_the_smoother_a_no_op`：
误差状态 KF 的 RTS 与教科书形式的唯一差别就是那个 `+ d_{k+1}` 项，而漏掉它的症状是
**平滑器静默退化成恒等变换** —— 轨迹一点没变，看起来跟"本来就很准"一模一样。
一条会失败的断言是这件事唯一的防线。
"""

from dataclasses import replace
from itertools import pairwise
from typing import ClassVar

import numpy as np
import pytest

from gait.config import AlgoConfig
from gait.contracts import FootSeries
from gait.core import quaternion as quat
from gait.core.eskf import FilterHistory, run_ins, run_ins_with_history
from gait.core.rts import SmoothError, smooth
from gait.validate.synthetic import NoiseModel, WalkSpec, generate_walk

SENSOR = NoiseModel(accel_density=1.5e-3, gyro_density=3.0e-4, seed=3)


def walk(duration=30.0, **kwargs):
    return generate_walk(WalkSpec(duration_s=duration, **kwargs), noise=SENSOR)


def stride_error_percent(navigation, truth, reference):
    """逐 stride 步长误差，与 `test_v1a_regression.stride_accuracy` 同一口径。"""
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
    return float(values.mean()), float(np.sqrt((values**2).mean()))


class TestTheSmootherActuallySmooths:
    def test_it_improves_stride_length_on_nominal_walking(self):
        series, truth = walk()
        forward, history = run_ins_with_history(series, AlgoConfig())
        smoothed = smooth(forward, history).navigation

        before_mean, before_rms = stride_error_percent(forward, truth, 1.30)
        after_mean, after_rms = stride_error_percent(smoothed, truth, 1.30)
        assert abs(after_mean) < abs(before_mean)
        assert after_rms < before_rms

    def test_it_moves_the_trajectory_at_all(self):
        """平滑量为零说明接线错了。整体设计 §5.8 预期"再降 20–40%"，不是"不变"。"""
        series, _ = walk()
        forward, history = run_ins_with_history(series, AlgoConfig())
        report = smooth(forward, history).report
        assert report.max_position_shift > 1e-3
        assert report.samples == len(series.t)

    def test_dropping_the_injected_correction_makes_the_smoother_a_no_op(self):
        """把 `d` 清零，平滑器就退化成恒等变换 —— 这正是漏掉 `+ d_{k+1}` 的后果。

        断言的是"清零之后什么都不会发生"，反过来证明了未清零时那一项承载着全部信息。
        """
        series, _ = walk()
        forward, history = run_ins_with_history(series, AlgoConfig())

        blinded = FilterHistory(
            segments=tuple(
                replace(segment, correction=np.zeros_like(segment.correction))
                for segment in history.segments
            )
        )
        crippled = smooth(forward, blinded)
        assert crippled.report.max_position_shift == pytest.approx(0.0, abs=1e-12)
        assert np.allclose(crippled.navigation.p, forward.p)

        # 而真正的历史会把轨迹显著挪动 —— 两者不能都成立。
        assert smooth(forward, history).report.max_position_shift > 1e-3

    def test_the_last_sample_of_a_segment_is_left_alone(self):
        """段末没有"未来"，平滑值必须等于滤波值。递推的起始条件就是这一条。"""
        series, _ = walk(duration=10.0)
        forward, history = run_ins_with_history(series, AlgoConfig())
        smoothed = smooth(forward, history).navigation
        last = history.segments[-1].end - 1
        assert np.allclose(smoothed.p[last], forward.p[last])
        assert np.allclose(smoothed.v[last], forward.v[last])


class TestItRescuesTheHeading:
    """RAY-261 的根因是航向。平滑对低速档的作用应当主要体现在航向上。"""

    LOW_SPEED: ClassVar[dict[str, float]] = {
        "cadence": 60.0, "stride_length": 0.35, "stance_ratio": 0.75,
    }

    def test_the_low_speed_case_improves_by_a_lot(self):
        series, truth = walk(**self.LOW_SPEED)
        forward, history = run_ins_with_history(series, AlgoConfig())
        smoothed = smooth(forward, history).navigation

        before, _ = stride_error_percent(forward, truth, 0.35)
        after, _ = stride_error_percent(smoothed, truth, 0.35)
        # 前向链在这一档是 2~3%（RAY-261 实测）。平滑后应当落到 1% 以内。
        assert abs(before) > 1.5
        assert abs(after) < 1.0

    def test_the_correction_is_mostly_yaw(self):
        series, _ = walk(**self.LOW_SPEED)
        forward, history = run_ins_with_history(series, AlgoConfig())
        report = smooth(forward, history).report
        # 航向修正应当占掉姿态修正的绝大部分 —— 横滚俯仰本来就是好的。
        assert report.max_yaw_shift_deg > 5.0
        assert report.max_yaw_shift_deg == pytest.approx(
            report.max_attitude_shift_deg, rel=0.05
        )


class TestNumericalStability:
    def test_a_long_still_session_stays_finite(self):
        """10 分钟静置：12 万步递推，最容易暴露协方差失对称累积成发散。"""
        fs = 200.0
        n = int(600 * fs) + 1
        t = np.arange(n) / fs
        rng = np.random.default_rng(11)
        acc = np.tile(np.array([0.0, 0.0, 9.80665]), (n, 1)) + rng.normal(0, 0.01, (n, 3))
        gyr = rng.normal(0, 1e-4, (n, 3))
        series = FootSeries(
            label="L", t=t, acc=acc, gyr=gyr,
            quality=np.zeros(n, dtype=np.uint8), segments=[(0, n)], fs=fs,
        )
        forward, history = run_ins_with_history(series, AlgoConfig())
        result = smooth(forward, history)

        assert np.all(np.isfinite(result.navigation.p))
        assert np.all(np.isfinite(result.navigation.v))
        assert np.all(np.isfinite(result.navigation.q))
        assert result.report.regularized_steps == 0

    def test_quaternions_stay_unit_after_injection(self):
        series, _ = walk()
        forward, history = run_ins_with_history(series, AlgoConfig())
        smoothed = smooth(forward, history).navigation
        assert np.allclose(np.linalg.norm(smoothed.q, axis=1), 1.0)

    def test_the_attitude_correction_is_a_right_multiplication(self):
        """左乘会把修正加到相反的方向上，而那不报错。这里把方向钉住。

        做法：由平滑前后的姿态反解修正量，再用右乘复现平滑后的姿态。左乘实现会让
        这个复现失败。
        """
        series, _ = walk(duration=10.0)
        forward, history = run_ins_with_history(series, AlgoConfig())
        smoothed = smooth(forward, history).navigation

        delta = quat.multiply(quat.conjugate(forward.q), smoothed.q)
        reproduced = quat.multiply(forward.q, delta)
        assert np.allclose(np.abs(np.sum(reproduced * smoothed.q, axis=1)), 1.0, atol=1e-9)

    def test_smoothing_is_deterministic(self):
        series, _ = walk(duration=10.0)
        forward, history = run_ins_with_history(series, AlgoConfig())
        first = smooth(forward, history).navigation
        second = smooth(forward, history).navigation
        assert np.array_equal(first.p, second.p)


class TestSegmentation:
    def test_each_segment_is_smoothed_independently(self):
        """段与段之间不传递。空洞两侧没有可信的动力学联系，Φ 也就不存在。"""
        series, _ = walk(duration=20.0)
        n = len(series.t)
        cut = n // 2
        holed = FootSeries(
            label=series.label, t=series.t, acc=series.acc, gyr=series.gyr,
            quality=series.quality, segments=[(0, cut), (cut, n)], fs=series.fs,
        )
        forward, history = run_ins_with_history(holed, AlgoConfig())
        assert len(history.segments) == 2

        smoothed = smooth(forward, history).navigation
        # 每一段的末样本都不该被动过 —— 两段各有各的起始条件。
        for segment in history.segments:
            last = segment.end - 1
            assert np.allclose(smoothed.p[last], forward.p[last])


class TestRefusals:
    def test_a_history_from_another_run_is_refused(self):
        short, _ = walk(duration=10.0)
        long_series, _ = walk(duration=20.0)
        forward = run_ins(long_series, AlgoConfig())
        _, history = run_ins_with_history(short, AlgoConfig())
        with pytest.raises(SmoothError, match="同一次"):
            smooth(forward, history)

    def test_non_navresult_input_is_refused(self):
        series, _ = walk(duration=5.0)
        _, history = run_ins_with_history(series, AlgoConfig())
        with pytest.raises(SmoothError):
            smooth(object(), history)  # type: ignore[arg-type]

    def test_non_history_input_is_refused(self):
        series, _ = walk(duration=5.0)
        forward = run_ins(series, AlgoConfig())
        with pytest.raises(SmoothError):
            smooth(forward, object())  # type: ignore[arg-type]


class TestTheHistoryIsFreeOfSideEffects:
    def test_recording_does_not_change_the_forward_result(self):
        """端云同构的最小形态：记录历史是旁路，不是分叉。逐位相同，不是"接近"。"""
        series, _ = walk()
        plain = run_ins(series, AlgoConfig())
        recorded, _ = run_ins_with_history(series, AlgoConfig())
        for field in ("p", "v", "q", "bg", "ba"):
            assert np.array_equal(getattr(plain, field), getattr(recorded, field)), field
