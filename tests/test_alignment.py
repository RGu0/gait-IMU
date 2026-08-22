"""`gait.core.alignment` 的初始对准。

验收标准是"合成数据下初始姿态误差 < 0.5°"。这个数不是随便定的：姿态错 1°，重力就有
`g·sin(1°) ≈ 0.17 m/s²` 落进水平方向被当作真实加速度积分两次。

测试因此分成两组：

* **对准本身准不准** —— 给模块一个已知的佩戴姿态，看解出来的差多少。
* **什么会把它顶出预算** —— 加计零偏直接变成倾角误差，`TestBiasBudget` 把这个换算
  钉成一个可读的数，交给 RAY-207 的标定去满足。
"""

import numpy as np
import pytest

from gait.core import quaternion as quat
from gait.core.alignment import (
    HEADING_REFERENCE,
    Alignment,
    AlignmentError,
    align_to_gravity,
    find_still_window,
    initial_alignment,
)
from gait.core.ins import GRAVITY_STANDARD
from gait.validate.synthetic import NoiseModel, WalkSpec, generate_walk

FS = 200.0
BUDGET_RAD = np.radians(0.5)

#: 若干佩戴姿态。yaw 故意取非零 —— 它必须**不影响**解出来的 roll/pitch。
MOUNTINGS = [
    (0.0, 0.0, 0.0),
    (np.radians(7.0), np.radians(-11.0), 0.0),
    (np.radians(-15.0), np.radians(20.0), np.radians(140.0)),
    (np.radians(30.0), np.radians(3.0), np.radians(-95.0)),
]


def mounted(series_acc, series_gyr, roll, pitch, yaw):
    """把测量搬到一个"佩戴姿态为 (roll, pitch, yaw)"的传感器系里。

    传感器系 s 与理想足部系 f 之间差一个固定旋转 `q_sf`；同一个物理量在两个系里的
    分量互为 `rotate_inverse`。合成数据起始姿态是单位，所以 `q_sf` 就是 t=0 时刻的
    真实姿态 —— 这正是对准应该解出来的东西。
    """
    q_sf = quat.from_euler(roll, pitch, yaw)
    return (
        quat.rotate_inverse(q_sf, series_acc),
        quat.rotate_inverse(q_sf, series_gyr),
        q_sf,
    )


def still_acc(n: int, roll: float, pitch: float, yaw: float, *, bias=(0.0, 0.0, 0.0), seed=0, sigma=0.0):
    """一段静止的比力样本，模块以给定姿态佩戴。"""
    q_sf = quat.from_euler(roll, pitch, yaw)
    nominal = quat.rotate_inverse(q_sf, np.array([0.0, 0.0, GRAVITY_STANDARD]))
    samples = np.tile(nominal, (n, 1)) + np.asarray(bias, dtype=np.float64)
    if sigma:
        samples = samples + np.random.default_rng(seed).normal(scale=sigma, size=(n, 3))
    return samples, q_sf


def attitude_error(alignment: Alignment, roll: float, pitch: float) -> float:
    """解出的姿态与"同样 roll/pitch、yaw 归零"的目标之间的角度，rad。"""
    return float(quat.angle_between(alignment.q, quat.from_euler(roll, pitch, 0.0)))


class TestSignConvention:
    """整体设计 §5.3 的公式与本仓库的比力约定相反，照抄会差 180°。"""

    def test_a_flat_module_aligns_to_identity(self):
        alignment = align_to_gravity(np.tile([0.0, 0.0, GRAVITY_STANDARD], (100, 1)))
        assert alignment.roll == pytest.approx(0.0)
        assert alignment.pitch == pytest.approx(0.0)
        assert quat.angle_between(alignment.q, quat.identity()) < 1e-12

    def test_the_design_document_formula_would_be_180_degrees_off(self):
        """把 §5.3 的两行原样实现，代入同一段数据，看它给出什么。

        这条测试不是为了嘲笑文档 —— 那两行在它自己的符号约定下是对的。它存在是因为
        **照抄会得到一个不报错的 180° 错误**，而这类错误一旦进了代码，症状是"轨迹整个
        翻过来"，看起来像是佩戴装反了。把它钉成一条测试，下次有人"按文档修正"时会立刻
        看到代价。
        """
        flat = np.array([0.0, 0.0, GRAVITY_STANDARD])
        document_roll = float(np.arctan2(-flat[1], -flat[2]))
        # arctan2(-0, -g) 落在 -π 一侧；差的是 180°，符号取决于零的符号，与结论无关。
        assert abs(document_roll) == pytest.approx(np.pi)  # 平放的模块被解成翻转 180°
        assert align_to_gravity(np.tile(flat, (50, 1))).roll == pytest.approx(0.0)

    def test_a_nose_up_module_reports_positive_pitch(self):
        """绕体轴 y 正转（脚尖抬起）应当解出正的 pitch，而不是负的。"""
        samples, _ = still_acc(200, 0.0, np.radians(12.0), 0.0)
        assert align_to_gravity(samples).pitch == pytest.approx(np.radians(12.0))

    def test_a_roll_to_the_right_reports_positive_roll(self):
        samples, _ = still_acc(200, np.radians(9.0), 0.0, 0.0)
        assert align_to_gravity(samples).roll == pytest.approx(np.radians(9.0))


class TestAccuracy:
    @pytest.mark.parametrize(("roll", "pitch", "yaw"), MOUNTINGS)
    def test_noiseless_alignment_is_exact(self, roll, pitch, yaw):
        samples, _ = still_acc(400, roll, pitch, yaw)
        assert attitude_error(align_to_gravity(samples), roll, pitch) < 1e-12

    @pytest.mark.parametrize(("roll", "pitch", "yaw"), MOUNTINGS)
    def test_with_sensor_noise_the_error_stays_inside_the_budget(self, roll, pitch, yaw):
        """BS-BT91 量级的白噪声（无零偏），静立 1 s。

        零偏单独考察 —— 见 `TestBiasBudget`。分开是因为两者的性质完全不同：白噪声
        可以靠加长窗口压下去，常值零偏不能。
        """
        sigma = 1.5e-3 * np.sqrt(FS)  # accel_density × √fs，与 NoiseModel 同一口径
        samples, _ = still_acc(round(FS), roll, pitch, yaw, sigma=sigma, seed=7)
        assert attitude_error(align_to_gravity(samples), roll, pitch) < BUDGET_RAD

    def test_yaw_is_never_recovered(self):
        """6 轴下重力只约束两个自由度。这是物理限制，不是缺陷。"""
        for yaw in (0.0, 1.0, -2.5, np.pi):
            samples, _ = still_acc(200, np.radians(5.0), np.radians(-8.0), yaw)
            _, _, recovered_yaw = quat.to_euler(align_to_gravity(samples).q)
            assert recovered_yaw == pytest.approx(0.0, abs=1e-12)

    def test_the_heading_reference_is_declared(self):
        """报告层要把它印出来（PRD §12）。写成常量才能被断言。"""
        assert HEADING_REFERENCE == "session_relative_yaw_zero"
        samples, _ = still_acc(50, 0.0, 0.0, 0.0)
        assert align_to_gravity(samples).heading_reference == HEADING_REFERENCE


class TestBiasBudget:
    """加计零偏直接变成倾角误差。这一组把 0.5° 换算成对标定的要求。"""

    def test_a_horizontal_bias_tilts_the_alignment_by_atan_of_its_ratio_to_g(self):
        """`δθ = arctan(b / g)`。这个换算是整条预算链的支点。"""
        for milli_g in (5.0, 10.0, 30.0):
            bias = milli_g * 1e-3 * GRAVITY_STANDARD
            samples, _ = still_acc(200, 0.0, 0.0, 0.0, bias=(bias, 0.0, 0.0))
            expected = np.arctan(bias / GRAVITY_STANDARD)
            assert attitude_error(align_to_gravity(samples), 0.0, 0.0) == pytest.approx(
                expected, rel=1e-6
            )

    def test_the_raw_device_spec_blows_the_budget_by_more_than_three_times(self):
        """《BS-BT91 硬件适配》发现 1：加计零漂 ±20~40 mg。

        30 mg 折算成 1.72° 的倾角误差，是 0.5° 预算的 3.4 倍。**未标定的加计满足不了
        本 Issue 的验收标准** —— 这不是对准算法的问题，是"对准的输入必须是已标定的
        比力"这件事的量化表达。契约 §3.2 也正是这么定义 `FootSeries.acc` 的
        （"已标定补偿"）。
        """
        bias = 30e-3 * GRAVITY_STANDARD
        samples, _ = still_acc(200, 0.0, 0.0, 0.0, bias=(bias, 0.0, 0.0))
        error = attitude_error(align_to_gravity(samples), 0.0, 0.0)
        assert error > 3.0 * BUDGET_RAD
        assert np.degrees(error) == pytest.approx(1.72, abs=0.02)

    def test_the_budget_implies_a_residual_bias_below_about_nine_milli_g(self):
        """反过来算：0.5° 要求标定后的水平残余零偏 < 8.7 mg。

        这个数是交给 RAY-207（六面法标定与标定参数库）的**派生要求**。写在这里而不是
        只写在文档里，是因为它随 `BUDGET_RAD` 与重力常量变化，而一个写死在文档里的
        数字不会跟着变。
        """
        tolerance_ms2 = np.tan(BUDGET_RAD) * GRAVITY_STANDARD
        assert tolerance_ms2 / GRAVITY_STANDARD == pytest.approx(8.7e-3, rel=0.02)
        # 刚好在容差内的零偏必须通过，刚好超出的必须超预算。
        for factor, should_pass in ((0.95, True), (1.05, False)):
            samples, _ = still_acc(200, 0.0, 0.0, 0.0, bias=(factor * tolerance_ms2, 0.0, 0.0))
            error = attitude_error(align_to_gravity(samples), 0.0, 0.0)
            assert bool(error < BUDGET_RAD) is should_pass

    def test_a_vertical_bias_does_not_tilt_anything(self):
        """竖直方向的零偏只改模值，不改方向 —— 它进 `gravity_residual`，不进倾角。"""
        bias = 30e-3 * GRAVITY_STANDARD
        samples, _ = still_acc(200, 0.0, 0.0, 0.0, bias=(0.0, 0.0, bias))
        alignment = align_to_gravity(samples)
        assert attitude_error(alignment, 0.0, 0.0) < 1e-12
        assert alignment.gravity_residual == pytest.approx(bias, rel=1e-9)


class TestQualityIndicators:
    def test_gravity_residual_is_small_for_a_genuinely_still_window(self):
        sigma = 1.5e-3 * np.sqrt(FS)
        samples, _ = still_acc(round(FS), 0.0, 0.0, 0.0, sigma=sigma, seed=1)
        assert align_to_gravity(samples).gravity_residual < 0.01

    def test_tilt_sigma_shrinks_like_one_over_root_n(self):
        """它回答的是"这段样本够不够长"，不是"对准有多准"。"""
        sigma = 1.5e-3 * np.sqrt(FS)
        short = align_to_gravity(still_acc(100, 0.0, 0.0, 0.0, sigma=sigma, seed=2)[0])
        long = align_to_gravity(still_acc(400, 0.0, 0.0, 0.0, sigma=sigma, seed=2)[0])
        assert short.tilt_sigma / long.tilt_sigma == pytest.approx(2.0, rel=0.3)

    def test_a_moving_window_is_refused_rather_than_aligned(self):
        """在一段不静止的样本上解出的角度毫无意义，而后续每一步都会默默使用它。"""
        moving = np.tile([0.0, 0.0, 3.0], (200, 1))
        with pytest.raises(AlignmentError, match="多半根本不是静止段"):
            align_to_gravity(moving)

    def test_the_window_is_reported(self):
        samples, _ = still_acc(100, 0.0, 0.0, 0.0)
        alignment = align_to_gravity(samples, window=(12, 112))
        assert alignment.window == (12, 112)
        assert alignment.samples == 100


class TestOnSyntheticGait:
    """端到端：从一段完整的合成会话里选窗口并对准。"""

    @pytest.mark.parametrize(("roll", "pitch", "yaw"), MOUNTINGS)
    def test_alignment_from_a_full_session_meets_the_budget(self, roll, pitch, yaw):
        series, _ = generate_walk(
            WalkSpec(duration_s=8.0, still_lead_s=1.0),
            noise=NoiseModel(accel_density=1.5e-3, gyro_density=3.0e-4, seed=5),
        )
        acc, gyr, _ = mounted(series.acc, series.gyr, roll, pitch, yaw)
        alignment = initial_alignment(acc, gyr, series.fs)
        assert attitude_error(alignment, roll, pitch) < BUDGET_RAD

    def test_the_window_lands_in_the_still_lead(self):
        """PRD §7 的流程是静立后开始走，那一段就是为对准准备的。"""
        spec = WalkSpec(duration_s=8.0, still_lead_s=1.0)
        series, truth = generate_walk(spec, noise=NoiseModel.bs_bt91())
        start, end = find_still_window(series.acc, series.gyr, series.fs)
        lead_end = truth.stance[0][1]
        assert start < end <= lead_end

    def test_a_session_that_never_stands_still_is_refused(self):
        """没有静止段就没有可信的初始姿态，此时该提示重来而不是硬着头皮对准。"""
        spec = WalkSpec(duration_s=6.0, still_lead_s=0.0)
        series, _ = generate_walk(spec, noise=NoiseModel.bs_bt91())
        with pytest.raises(AlignmentError, match="静止段"):
            find_still_window(series.acc, series.gyr, series.fs, minimum_seconds=1.5)


class TestRejections:
    def test_empty_window(self):
        with pytest.raises(AlignmentError, match="空窗口"):
            align_to_gravity(np.zeros((0, 3)))

    def test_wrong_shape(self):
        with pytest.raises(AlignmentError):
            align_to_gravity(np.zeros((10, 2)))

    def test_zero_magnitude(self):
        with pytest.raises(AlignmentError):
            align_to_gravity(np.zeros((10, 3)))
