"""`gait.validate.synthetic` 的生成器。

这个文件里最重要的一条是 `TestSelfConsistency` —— 它验证的不是"数据看起来像步态"，
而是**生成的 acc/gyr 与声称的真值确实描述同一段运动**。生成器一旦在这件事上错了，
后面每一个用它做验收的 Issue 都会得到一个精确的错误答案。

其余测试守的是模型的**已声明性质**（支撑相严格静止、直行 stride 步长恒定、转身
stride 步长接近零、噪声按密度缩放）。这些性质是下游 Issue 会依赖的前提：RAY-203 靠
"支撑相严格静止"定义召回率，RAY-215 靠"转身 stride 步长接近零"验证分离。
"""

from itertools import pairwise

import numpy as np
import pytest

from gait.contracts import FootSeries, Quality
from gait.core import ins
from gait.core import quaternion as quat
from gait.validate.synthetic import (
    NoiseModel,
    SyntheticError,
    WalkSpec,
    generate_dual_walk,
    generate_walk,
)


def index_at(t: np.ndarray, moment: float) -> int:
    return int(np.searchsorted(t, moment, side="left"))


class TestContractCompliance:
    def test_returns_a_valid_foot_series(self):
        """`FootSeries` 在构造时自校验，所以这条能过就说明形状与 dtype 都对。"""
        series, truth = generate_walk(WalkSpec(duration_s=5.0))
        assert isinstance(series, FootSeries)
        assert series.label == "L"
        assert series.fs == 200.0
        assert series.t.shape == (1001,)
        assert series.acc.shape == series.gyr.shape == (1001, 3)
        assert truth.p.shape == truth.v.shape == (1001, 3)
        assert truth.q.shape == (1001, 4)
        assert np.array_equal(truth.t, series.t)

    def test_time_axis_is_uniform_at_the_configured_rate(self):
        series, _ = generate_walk(WalkSpec(duration_s=3.0, fs=100.0))
        assert np.allclose(np.diff(series.t), 1.0 / 100.0)
        assert series.t[-1] == pytest.approx(3.0)

    def test_quality_is_all_normal_and_the_series_is_one_segment(self):
        """合成数据没有饱和、插值或空洞 —— 这是一个判断，不是默认值。"""
        series, _ = generate_walk(WalkSpec(duration_s=2.0))
        assert np.all(series.quality == Quality.NONE)
        assert series.segments == [(0, len(series.t))]

    def test_both_feet_are_available(self):
        for foot in ("L", "R"):
            series, truth = generate_walk(WalkSpec(duration_s=2.0), foot=foot)
            assert series.label == foot
            assert truth.label == foot

    def test_an_unknown_foot_is_refused(self):
        with pytest.raises(SyntheticError):
            generate_walk(WalkSpec(duration_s=2.0), foot="X")


class TestRestConvention:
    def test_a_still_foot_reads_plus_g_on_z(self):
        """静止前导段里，模块平放、姿态为单位，比力必须是 (0, 0, +g)。

        这一条把生成器与 `core/ins.py` 的**比力**约定绑在一起。若这里写成 -g，
        整条算法链都会在一个自洽但错误的世界里工作，而每个单独的模块看起来都对。
        """
        series, truth = generate_walk(WalkSpec(duration_s=3.0, still_lead_s=1.0))
        lead = index_at(series.t, 1.0)
        assert lead > 0
        assert np.allclose(series.acc[:lead], [0.0, 0.0, ins.GRAVITY_STANDARD])
        assert np.allclose(series.gyr[:lead], 0.0)
        assert np.allclose(truth.v[:lead], 0.0)
        assert np.allclose(quat.angle_between(truth.q[:lead], quat.identity()), 0.0)

    def test_the_configured_gravity_is_the_one_that_appears(self):
        series, _ = generate_walk(WalkSpec(duration_s=2.0), gravity=9.79)
        assert series.acc[0, 2] == pytest.approx(9.79)


class TestStancePhaseIsExactlyStill:
    """RAY-203 靠这条性质定义零速检测的召回率。"""

    def test_velocity_and_angular_rate_vanish_on_stance(self):
        series, truth = generate_walk(WalkSpec(duration_s=6.0))
        for start, end in truth.stance:
            assert np.max(np.abs(truth.v[start:end])) == 0.0
            assert np.max(np.abs(series.gyr[start:end])) == 0.0

    def test_attitude_and_position_are_constant_on_stance(self):
        _, truth = generate_walk(WalkSpec(duration_s=6.0))
        for start, end in truth.stance:
            assert np.allclose(truth.p[start:end], truth.p[start])
            assert np.max(quat.angle_between(truth.q[start:end], truth.q[start])) == 0.0

    def test_the_foot_is_on_the_ground_during_stance(self):
        _, truth = generate_walk(WalkSpec(duration_s=6.0))
        for start, end in truth.stance:
            assert np.allclose(truth.p[start:end, 2], 0.0)

    def test_stance_segments_are_ordered_and_disjoint(self):
        """与契约 `_check_segments` 同样的要求：升序、不重叠、落在范围内。"""
        series, truth = generate_walk(WalkSpec(duration_s=8.0))
        n = len(series.t)
        previous = 0
        for start, end in truth.stance:
            assert 0 <= start < end <= n
            assert start >= previous
            previous = end


class TestStrideBookkeeping:
    def test_event_times_are_strictly_increasing(self):
        """契约 §3.4 要求 `t_ic < t_to < t_ic_next`，真值台账必须先满足它。"""
        _, truth = generate_walk(WalkSpec(duration_s=10.0))
        for stride in truth.strides:
            assert stride.t_ic < stride.t_to < stride.t_ic_next

    def test_stance_ratio_is_honoured(self):
        spec = WalkSpec(duration_s=10.0, stance_ratio=0.62)
        _, truth = generate_walk(spec)
        for stride in truth.strides:
            ratio = (stride.t_to - stride.t_ic) / stride.stride_time
            assert ratio == pytest.approx(0.62)

    def test_stride_time_follows_cadence_with_two_steps_per_stride(self):
        """cadence 是**步/分**，一个 stride 含两步，所以是 120/cadence 而不是 60/cadence。

        这个 2 倍关系是步态参数里最常见的口误，值得一条测试。
        """
        spec = WalkSpec(duration_s=10.0, cadence=120.0)
        _, truth = generate_walk(spec)
        assert spec.stride_time == pytest.approx(1.0)
        for stride in truth.strides:
            assert stride.stride_time == pytest.approx(1.0)

    def test_straight_strides_advance_by_exactly_the_configured_length(self):
        spec = WalkSpec(duration_s=10.0, stride_length=1.42)
        _, truth = generate_walk(spec)
        for stride in truth.straight_strides:
            assert stride.stride_length == pytest.approx(1.42)
        assert truth.mean_stride_length == pytest.approx(1.42)

    def test_the_foot_lands_where_the_ledger_says(self):
        """真值轨迹与台账不是两份数据，必须逐点对得上。"""
        series, truth = generate_walk(WalkSpec(duration_s=8.0))
        for stride in truth.strides:
            assert np.allclose(truth.p[index_at(series.t, stride.t_ic)], stride.start)

    def test_nominal_speed_matches_stride_length_over_stride_time(self):
        spec = WalkSpec(stride_length=1.3, cadence=120.0)
        assert spec.gait_speed == pytest.approx(1.3)


class TestClearance:
    def test_the_peak_swing_height_is_the_configured_clearance(self):
        spec = WalkSpec(duration_s=6.0, clearance=0.07)
        _, truth = generate_walk(spec)
        assert np.max(truth.p[:, 2]) == pytest.approx(0.07, rel=1e-3)

    def test_the_foot_never_goes_below_the_ground(self):
        _, truth = generate_walk(WalkSpec(duration_s=6.0))
        assert np.min(truth.p[:, 2]) >= -1e-12


class TestTurnaroundMode:
    """RAY-215（直行段/转身段分离）依赖的性质。"""

    def test_turn_strides_barely_advance(self):
        spec = WalkSpec(duration_s=25.0, path_length_m=4.0)
        _, truth = generate_walk(spec)
        turns = [stride for stride in truth.strides if stride.is_turn]
        assert turns, "4 米往返模式必须产生转身 stride"
        for stride in turns:
            assert stride.stride_length < 0.2 * spec.stride_length

    def test_a_full_turn_reverses_the_heading(self):
        spec = WalkSpec(duration_s=25.0, path_length_m=4.0, turn_strides=2)
        _, truth = generate_walk(spec)
        turns = [stride for stride in truth.strides if stride.is_turn]
        # 转身分摊到 turn_strides 个 stride，合起来正好 180°。
        assert turns[1].heading_end - turns[0].heading_start == pytest.approx(np.pi)

    def test_straight_strides_are_unaffected_by_the_turns(self):
        spec = WalkSpec(duration_s=25.0, path_length_m=4.0)
        _, truth = generate_walk(spec)
        for stride in truth.straight_strides:
            assert stride.stride_length == pytest.approx(spec.stride_length)

    def test_the_walk_stays_within_the_configured_path(self):
        """往返走不该越走越远 —— 那说明转身没把人带回来。"""
        spec = WalkSpec(duration_s=40.0, path_length_m=4.0)
        _, truth = generate_walk(spec)
        extent = np.ptp(truth.p[:, :2], axis=0)
        assert np.max(extent) < spec.path_length_m + 2.0 * spec.stride_length

    def test_without_a_path_length_the_walk_never_turns(self):
        _, truth = generate_walk(WalkSpec(duration_s=25.0))
        assert all(not stride.is_turn for stride in truth.strides)
        assert np.ptp(truth.p[:, 0]) > 10.0

    def test_a_path_shorter_than_one_stride_is_refused(self):
        with pytest.raises(SyntheticError, match="放不下一步"):
            generate_walk(WalkSpec(stride_length=1.3, path_length_m=0.5))


def self_consistency(fs: float, seconds: float = 4.0):
    """把生成的测量喂回前向机械编排，返回 (整段最大误差, 触地时刻最大误差)。"""
    series, truth = generate_walk(WalkSpec(duration_s=seconds, fs=fs))
    q, _, p = ins.mechanize(
        series.acc, series.gyr, 1.0 / fs, q0=truth.q[0], v0=truth.v[0], p0=truth.p[0]
    )
    ic = [index_at(series.t, stride.t_ic) for stride in truth.strides[1:]]
    return {
        "position": float(np.max(np.linalg.norm(p - truth.p, axis=1))),
        "attitude": float(np.max(quat.angle_between(q, truth.q))),
        "position_at_ic": float(np.max(np.linalg.norm((p - truth.p)[ic], axis=1))),
        "attitude_at_ic": float(np.max(quat.angle_between(q[ic], truth.q[ic]))),
    }


class TestSelfConsistency:
    """生成的 acc/gyr 与声称的真值必须描述同一段运动。

    验证方式是把测量喂回 `core/ins.py` 的前向机械编排 —— 它与生成方向严格互逆：

        生成:  f_f = C_n^f · (p̈ - g_n)
        积分:  a_n = C_f^n · f_f + g_n

    生成器若有任何不自洽（符号反了、姿态与位置各说各的、ω 与 q 对不上），残差**不会
    随 dt 趋于零**。因此这里断言的是收敛，不是某个绝对阈值。
    """

    def test_the_residual_converges_to_zero(self):
        coarse = self_consistency(100.0)
        fine = self_consistency(800.0)
        for key in ("position", "attitude"):
            assert fine[key] < 0.2 * coarse[key], key

    def test_at_heel_strike_the_reconstruction_is_essentially_exact(self):
        """摆动相内的离散误差在触地时刻几乎完全抵消。

        原因是姿态误差正比于 ω 相对摆动起点的增量，而 ω 在每个 stride 首尾都回到零，
        于是整段摆动的误差自消。这不是巧合，是"支撑相严格静止"这个模型性质的推论 ——
        也正是它让本数据适合做步长精度的基准：**步长只取决于两次触地之间的位置差**。

        200 Hz 下实测：姿态残差 ~1e-5 rad，位置残差 ~2.4e-4 m，相对 1.3 m 的步长是
        0.018%，远在 V1-a 的 0.5% 预算之内。
        """
        result = self_consistency(200.0)
        assert result["attitude_at_ic"] < 1e-4
        assert result["position_at_ic"] < 1e-3

    def test_the_mid_swing_residual_is_first_order_and_that_is_the_integrator(self):
        """摆动相中段的残差按一阶收敛，不是二阶。

        这是**积分器的性质，不是生成器的缺陷**：`ins.propagate` 把区间起点的 ω 当作
        整个区间的常量（左矩形），ω 变化时每步引入 ½·ω̇·dt²，全局一阶。RAY-201 的测试
        全部用**恒定** ω，那种情形下姿态更新是精确的，因此结构上看不到这一项。

        这条测试把它钉成一个已知量而不是一个惊喜。已登记为后继 Issue（梯形角增量可
        把它变成二阶），影响的是足廓清高度与瞬时速度曲线这类摆动相中段的指标。
        """
        errors = [self_consistency(fs)["attitude"] for fs in (100.0, 200.0, 400.0, 800.0)]
        ratios = [coarse / fine for coarse, fine in pairwise(errors)]
        assert all(1.8 < ratio < 2.2 for ratio in ratios), ratios


class TestNoise:
    def test_the_default_is_noiseless(self):
        """算法本身对不对，应该能被单独回答。"""
        series_a, _ = generate_walk(WalkSpec(duration_s=2.0))
        series_b, _ = generate_walk(WalkSpec(duration_s=2.0), noise=NoiseModel())
        assert np.array_equal(series_a.acc, series_b.acc)
        # 无噪声时静止段的比力精确等于重力，一个 bit 都不差。
        assert np.max(np.abs(series_a.acc[0] - [0, 0, ins.GRAVITY_STANDARD])) == 0.0

    def test_the_same_seed_reproduces_the_same_data(self):
        noise = NoiseModel(accel_density=2e-3, gyro_density=5e-4, seed=42)
        first, _ = generate_walk(WalkSpec(duration_s=2.0), noise=noise)
        second, _ = generate_walk(WalkSpec(duration_s=2.0), noise=noise)
        assert np.array_equal(first.acc, second.acc)
        assert np.array_equal(first.gyr, second.gyr)

    def test_a_different_seed_gives_different_data(self):
        spec = WalkSpec(duration_s=2.0)
        first, _ = generate_walk(spec, noise=NoiseModel(accel_density=2e-3, seed=1))
        second, _ = generate_walk(spec, noise=NoiseModel(accel_density=2e-3, seed=2))
        assert not np.array_equal(first.acc, second.acc)

    def test_noise_is_specified_as_a_density_not_a_standard_deviation(self):
        """σ = density·√fs。同一份参数在 100 Hz 与 200 Hz 下必须描述同一个传感器。

        写成标准差的话，换个采样率就等于换了一台设备 —— 而 Allan 方差给出的本来
        就是密度（ARW / VRW），两边对得上。
        """
        density = 3e-3
        deviations = {}
        for fs in (100.0, 400.0):
            clean, _ = generate_walk(WalkSpec(duration_s=20.0, fs=fs))
            noisy, _ = generate_walk(
                WalkSpec(duration_s=20.0, fs=fs), noise=NoiseModel(accel_density=density)
            )
            deviations[fs] = float(np.std(noisy.acc - clean.acc))
            assert deviations[fs] == pytest.approx(density * np.sqrt(fs), rel=0.05)
        assert deviations[400.0] / deviations[100.0] == pytest.approx(2.0, rel=0.05)

    def test_bias_is_a_constant_offset(self):
        bias = (0.29, -0.1, 0.05)
        clean, _ = generate_walk(WalkSpec(duration_s=2.0))
        biased, _ = generate_walk(WalkSpec(duration_s=2.0), noise=NoiseModel(accel_bias=bias))
        assert np.allclose(biased.acc - clean.acc, bias)

    def test_the_hardware_preset_carries_the_documented_accel_bias(self):
        """30 mg 是规格 ±20~40 mg 的中值，也是这个预设里**唯一**有硬件依据的数。"""
        preset = NoiseModel.bs_bt91()
        assert preset.accel_bias[0] == pytest.approx(0.030 * ins.GRAVITY_STANDARD)


class TestDualFoot:
    def test_both_feet_share_one_time_axis(self):
        """合成数据里不存在同步误差 —— 那是 RAY-209/213 的题目，不该被这里悄悄引入。"""
        pair = generate_dual_walk(WalkSpec(duration_s=6.0))
        assert np.array_equal(pair["L"][0].t, pair["R"][0].t)

    def test_the_right_foot_lags_by_half_a_stride(self):
        """双支撑期、对称性、交替支撑一致性都建立在这半个周期上。"""
        spec = WalkSpec(duration_s=6.0)
        pair = generate_dual_walk(spec)
        left = pair["L"][1].strides[0].t_ic
        right = pair["R"][1].strides[0].t_ic
        assert right - left == pytest.approx(0.5 * spec.stride_time)

    def test_feet_are_separated_by_the_step_width(self):
        spec = WalkSpec(duration_s=6.0, step_width=0.14)
        pair = generate_dual_walk(spec)
        left = pair["L"][1].strides[0].start
        right = pair["R"][1].strides[0].start
        assert float(np.linalg.norm(left - right)) == pytest.approx(0.14)

    def test_double_support_exists_and_has_the_expected_share(self):
        """走路的双支撑期占比是 2·stance_ratio − 1。它是 RAY-211 自检的基础。"""
        spec = WalkSpec(duration_s=12.0, stance_ratio=0.60)
        pair = generate_dual_walk(spec)
        n = len(pair["L"][0].t)
        masks = {}
        for foot in ("L", "R"):
            mask = np.zeros(n, dtype=bool)
            for start, end in pair[foot][1].stance:
                mask[start:end] = True
            masks[foot] = mask
        both = masks["L"] & masks["R"]
        assert both.any()
        # 只在两只脚都走起来之后统计，避开各自的静止前导。
        settled = slice(int(4.0 * spec.fs), n)
        share = float(np.mean(both[settled]))
        assert share == pytest.approx(2.0 * spec.stance_ratio - 1.0, abs=0.05)

    def test_the_two_feet_get_different_noise(self):
        """同一份噪声会让对称性指标得到一个假的完美值。"""
        pair = generate_dual_walk(
            WalkSpec(duration_s=3.0), noise=NoiseModel(accel_density=2e-3)
        )
        clean = generate_dual_walk(WalkSpec(duration_s=3.0))
        left = pair["L"][0].acc - clean["L"][0].acc
        right = pair["R"][0].acc - clean["R"][0].acc
        assert not np.allclose(left, right)


class TestRejections:
    @pytest.mark.parametrize(
        "kwargs",
        [
            {"stride_length": 0.0},
            {"cadence": -1.0},
            {"duration_s": 0.0},
            {"fs": 0.0},
            {"clearance": 0.0},
            {"stance_ratio": 0.0},
            {"stance_ratio": 1.0},
            {"still_lead_s": -0.5},
            {"path_length_m": 0.0},
        ],
    )
    def test_impossible_parameters_are_refused(self, kwargs):
        with pytest.raises(SyntheticError):
            WalkSpec(**kwargs)

    def test_stance_ratio_message_explains_both_extremes(self):
        with pytest.raises(SyntheticError, match="零速修正无从谈起"):
            WalkSpec(stance_ratio=0.0)

    def test_a_duration_too_short_for_two_samples_is_refused(self):
        with pytest.raises(SyntheticError, match="无法构成序列"):
            generate_walk(WalkSpec(duration_s=0.001, fs=100.0))
