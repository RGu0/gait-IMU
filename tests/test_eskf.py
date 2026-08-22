"""`gait.core.eskf` 的 15 维误差状态滤波。

两条验收标准：静置 10 min 位置漂移 < 5 cm；合成数据端到端回归通过。前者有一条直接
对应的测试（跑满 120001 个采样，约 5 s，是本套件最慢的一条 —— 它是本模块的核心断言，
值这个时间）。后者的正式判据属 RAY-206 的 `v1a-e2e-regression` scope，这里先给一个
同口径的步长误差检查。

除此之外，这个文件里有一批**可观测性**测试。它们断言的不是"滤波器收敛了"，而是
"哪些量收敛、哪些量不收敛" —— 后者才是判断这个滤波器有没有按物理该有的样子工作的
依据。一个把不可观测的量也"收敛"掉的滤波器，是在编造信息。
"""

from dataclasses import replace

import numpy as np
import pytest

from gait.config import AlgoConfig, ConfigError
from gait.contracts import FootSeries, NavResult, Quality
from gait.core import quaternion as quat
from gait.core.alignment import AlignmentError, initial_alignment
from gait.core.eskf import (
    STATE_DIM,
    THETA,
    EskfError,
    FilterState,
    _exp_so3,
    _residual,
    _run_segment,
    _update,
    _Workspace,
    run_ins,
)
from gait.core.ins import GRAVITY_STANDARD, gravity_vector
from gait.core.zupt import detect_stance
from gait.validate.synthetic import NoiseModel, WalkSpec, generate_walk

FS = 200.0
#: 与 `NoiseModel.bs_bt91` 同一口径，但**不含零偏** —— 零偏单独在
#: `TestBiasObservability` 里考察，混在一起会让"是谁把误差压下去的"说不清。
SENSOR_NOISE = NoiseModel(accel_density=1.5e-3, gyro_density=3.0e-4, seed=3)


def still_series(seconds: float, *, fs: float = FS, seed: int = 0) -> FootSeries:
    """一段静置的会话。模块平放，只有传感器白噪声。"""
    n = round(seconds * fs) + 1
    rng = np.random.default_rng(seed)
    acc = np.tile([0.0, 0.0, GRAVITY_STANDARD], (n, 1)) + rng.normal(
        scale=1.5e-3 * np.sqrt(fs), size=(n, 3)
    )
    gyr = rng.normal(scale=3.0e-4 * np.sqrt(fs), size=(n, 3))
    return FootSeries(
        label="L",
        t=np.arange(n) / fs,
        acc=acc,
        gyr=gyr,
        quality=np.full(n, Quality.NONE, dtype=np.uint8),
        segments=[(0, n)],
        fs=fs,
    )


def walk(seconds: float = 20.0, noise: NoiseModel | None = None, **spec_kwargs):
    return generate_walk(
        WalkSpec(duration_s=seconds, **spec_kwargs), noise=noise or SENSOR_NOISE
    )


def stride_error_percent(nav: NavResult, truth, fs: float) -> float:
    """估计步长相对真值的百分比误差，只看直行 stride。"""
    contacts = [round(stride.t_ic * fs) for stride in truth.strides]
    estimated = [
        float(np.linalg.norm(nav.p[contacts[i + 1], :2] - nav.p[contacts[i], :2]))
        for i in range(len(contacts) - 1)
    ]
    reference = [stride.stride_length for stride in truth.strides[:-1]]
    return 100.0 * (np.mean(estimated) - np.mean(reference)) / np.mean(reference)


def final_state(series: FootSeries, cfg: AlgoConfig | None = None) -> FilterState:
    """跑完一段并返回**滤波器状态**（含协方差）—— `run_ins` 只返回契约结构。"""
    cfg = cfg or AlgoConfig()
    detection = detect_stance(series.acc, series.gyr, series.fs, cfg)
    alignment = initial_alignment(series.acc, series.gyr, series.fs, cfg)
    _, state = _run_segment(
        series.acc,
        series.gyr,
        1.0 / series.fs,
        detection,
        FilterState.initial(alignment, cfg),
        cfg,
        gravity_vector(),
    )
    return state


def navigation_frame_attitude_sigma(state: FilterState) -> np.ndarray:
    """把足部系的姿态误差协方差转到导航系，返回三轴 1σ（rad）。

    误差状态是**局部**（足部系）表述，直接读对角线得到的是绕体轴的不确定度。行走时
    足部一直在俯仰，体轴 z 与导航系竖直方向并不重合 —— 不换算就会把"航向不确定度"
    读成一个混了倾角的量。
    """
    covariance = state.rotation @ state.covariance[THETA, THETA] @ state.rotation.T
    return np.sqrt(np.diag(covariance))


class TestExponentialMapIsPinnedToTheQuaternionModule:
    """`_exp_so3` 是指数映射的第二份实现。没有这条测试，它就不该存在。"""

    def test_exp_so3_agrees_with_the_quaternion_path(self):
        rng = np.random.default_rng(0)
        for scale in (0.0, 1e-12, 1e-3, 0.5, 3.0):
            for _ in range(20):
                rotation_vector = rng.normal(size=3) * scale
                assert np.allclose(
                    _exp_so3(rotation_vector),
                    quat.to_matrix(quat.from_rotation_vector(rotation_vector)),
                    atol=1e-12,
                )

    def test_exp_so3_returns_a_rotation(self):
        matrix = _exp_so3(np.array([0.3, -1.2, 0.7]))
        assert np.allclose(matrix @ matrix.T, np.eye(3))
        assert np.isclose(np.linalg.det(matrix), 1.0)


class TestAcceptance:
    def test_ten_minutes_still_drifts_less_than_five_centimetres(self):
        """验收标准之一。120001 个采样，本套件最慢的一条。

        它测的不是"积分准不准"—— 纯积分在 10 分钟里会漂到几公里之外。它测的是
        **ZUPT 有没有真的把误差按住**：静置时每一个采样都满足零速判据，滤波器应当
        把速度反复拉回零，位置因此几乎不动。

        若哪天这条变红，先看的不是积分而是检测器：一个漏检的检测器会让这个数变成米级。
        """
        navigation = run_ins(still_series(600.0))
        drift = float(np.max(np.linalg.norm(navigation.p, axis=1)))
        assert drift < 0.05, f"10 min 漂移 {drift * 100:.2f} cm"

    def test_stride_length_on_synthetic_gait(self):
        """V1-a 的正式判据属 RAY-206 的第二个 scope；这里先按同一口径量一次。"""
        series, truth = walk(30.0)
        error = stride_error_percent(run_ins(series), truth, series.fs)
        assert abs(error) < 0.5, f"步长误差 {error:.3f}%"


class TestObservability:
    """哪些量收敛、哪些量不收敛，才是这个滤波器有没有按物理工作的依据。"""

    def test_gyro_bias_converges_on_all_three_axes(self):
        """ZARU 直接观测陀螺零偏，所以三轴都该收敛到真值。"""
        bias = (0.004, -0.002, 0.003)
        series, _ = walk(30.0, noise=replace(SENSOR_NOISE, gyro_bias=bias))
        estimated = run_ins(series).bg[-1]
        assert np.allclose(estimated, bias, atol=2e-4)

    def test_accel_bias_converges_only_off_the_rotation_axis(self):
        """加计零偏只在**垂直于旋转轴**的两个体轴上可观测。

        本模型的足部只绕体轴 y 俯仰。绕 y 的旋转会把体轴 x 与 z 的零偏在导航系里
        扫成随时间变化的方向，于是它们与"固定的初始倾角误差"可以分开；体轴 y 的零偏
        始终映射到同一个导航方向，与 roll 误差**结构上不可分**。

        这不是缺陷，是几何。它的实际后果也不严重：滤波器把 y 零偏与 roll 误差凑成一个
        自洽的组合，步长精度不受影响（同一份数据下仍然 < 0.5%）。但它解释了为什么
        `ba` 的某一个分量看起来"没收敛"—— 那个分量本来就没有单独的信息。
        """
        bias = (0.08, -0.05, 0.03)
        series, truth = walk(30.0, noise=replace(SENSOR_NOISE, accel_bias=bias))
        navigation = run_ins(series)
        estimated = navigation.ba[-1]
        assert abs(estimated[0] - bias[0]) < 0.01, "体轴 x 应当收敛"
        assert abs(estimated[2] - bias[2]) < 0.01, "体轴 z 应当收敛"
        assert abs(estimated[1] - bias[1]) > 0.02, "体轴 y（旋转轴）不该收敛"
        # 但整体状态仍然自洽：步长不受影响。
        assert abs(stride_error_percent(navigation, truth, series.fs)) < 0.5

    def test_heading_stalls_while_tilt_keeps_converging(self):
        """航向是**弱可观测**：掉下来一次就卡住，倾角不会。

        摆动相里比力方向变化很大，姿态-速度耦合因此在各方向都非零，航向被间接约束了
        一点。但它不会持续收敛 —— 这正是 RAY-205 双足距离约束存在的理由。

        断言写成"两个量的收敛**速率**不同"而不是绝对阈值：绝对值随噪声与步态参数变，
        而"航向卡住、倾角不卡"是结构性的。
        """
        short, _ = walk(30.0)
        long, _ = walk(60.0)
        sigma_short = navigation_frame_attitude_sigma(final_state(short))
        sigma_long = navigation_frame_attitude_sigma(final_state(long))

        # 航向的不确定度比倾角大一到两个数量级。
        assert sigma_long[2] > 20.0 * sigma_long[1]
        # 时长加倍：倾角继续明显收敛，航向几乎不动。
        tilt_gain = 1.0 - sigma_long[1] / sigma_short[1]
        heading_gain = 1.0 - sigma_long[2] / sigma_short[2]
        assert heading_gain < 0.25, f"航向收敛了 {heading_gain:.0%}，不像卡住"
        assert tilt_gain > heading_gain

    def test_the_initial_heading_prior_is_deliberately_large(self):
        """30° 是"承认不知道"，不是调出来的数。给小了会让滤波器过分自信。"""
        assert AlgoConfig().eskf_initial_yaw_sigma > 10.0 * AlgoConfig().eskf_initial_tilt_sigma


class TestHeightConstraint:
    def test_it_suppresses_vertical_drift_on_flat_ground(self):
        series, _ = walk(30.0)
        with_constraint = run_ins(series)
        without = run_ins(series, replace(AlgoConfig(), eskf_enable_height_constraint=False))
        assert abs(with_constraint.p[-1, 2]) < 0.5 * abs(without.p[-1, 2])

    def test_disabling_it_changes_nothing_else_structurally(self):
        """关掉它只该影响高度，不该让水平步长垮掉 —— 上下楼时正是要这么用。"""
        series, truth = walk(30.0)
        without = run_ins(series, replace(AlgoConfig(), eskf_enable_height_constraint=False))
        assert abs(stride_error_percent(without, truth, series.fs)) < 0.5


class TestDegradedObservations:
    def test_a_degraded_zupt_is_trusted_less(self):
        """软零速的观测噪声按 `eskf_degraded_r_scale` 放大，修正量因此更小。

        直接比较同一份状态在两种 R 下的修正：放大 R 必须让修正变小，否则那个配置项
        就是个摆设。
        """
        cfg = AlgoConfig()
        series = still_series(2.0)
        state = final_state(series, cfg)
        biased = replace(state, velocity=np.array([0.3, 0.0, 0.0]))

        workspace = _Workspace.build(cfg, 1.0 / FS, gravity_vector())
        jacobian = workspace.jacobians[(True, True)]
        residual = _residual(biased, np.zeros(3), True, True, 0.0, workspace)
        hard = _update(biased, residual, jacobian, workspace.noises[(True, True, False)], workspace.identity)
        soft = _update(biased, residual, jacobian, workspace.noises[(True, True, True)], workspace.identity)
        assert np.linalg.norm(hard.velocity) < np.linalg.norm(soft.velocity)

    def test_the_scale_cannot_be_below_one(self):
        """小于 1 表示软零速比硬零速**更**可信，与降级的含义正好相反。"""
        with pytest.raises(ConfigError, match="正好相反"):
            replace(AlgoConfig(), eskf_degraded_r_scale=0.5)


class TestContractShape:
    def test_the_result_satisfies_the_navigation_contract(self):
        """`NavResult` 在构造时自校验，所以能返回就说明形状与长度都对。"""
        series, _ = walk(10.0)
        navigation = run_ins(series)
        assert isinstance(navigation, NavResult)
        n = len(series.t)
        assert navigation.q.shape == (n, 4)
        for name in ("v", "p", "bg", "ba"):
            assert getattr(navigation, name).shape == (n, 3)
        assert np.array_equal(navigation.t, series.t)

    def test_stances_match_the_zupt_mask(self):
        series, _ = walk(10.0)
        navigation = run_ins(series)
        rebuilt = np.zeros_like(navigation.zupt)
        for start, end in navigation.stances:
            rebuilt[start:end] = True
        assert np.array_equal(rebuilt, navigation.zupt)

    def test_attitude_stays_a_unit_quaternion(self):
        series, _ = walk(10.0)
        assert np.allclose(np.linalg.norm(run_ins(series).q, axis=1), 1.0)

    def test_the_filter_is_deterministic(self):
        """同一份输入必须给出逐 bit 相同的结果 —— 否则回归测试无从谈起。"""
        series, _ = walk(10.0)
        first = run_ins(series)
        second = run_ins(series)
        assert np.array_equal(first.p, second.p)
        assert np.array_equal(first.q, second.q)


class TestCovarianceHealth:
    def test_it_stays_symmetric_and_positive_definite(self):
        """Joseph 形式换来的就是这个。标准形式在上万次更新后会失对称并最终发散。"""
        state = final_state(walk(30.0)[0])
        assert np.allclose(state.covariance, state.covariance.T, atol=0.0)
        assert np.min(np.linalg.eigvalsh(state.covariance)) > 0.0

    def test_the_state_dimension_is_fifteen(self):
        state = final_state(still_series(2.0))
        assert state.covariance.shape == (STATE_DIM, STATE_DIM)


class TestSegments:
    def build_two_segments(self):
        """把一段行走从中间切开，模拟空洞切分之后的两段。"""
        series, truth = walk(20.0)
        cut = len(series.t) // 2
        return (
            FootSeries(
                label=series.label,
                t=series.t,
                acc=series.acc,
                gyr=series.gyr,
                quality=series.quality,
                segments=[(0, cut), (cut, len(series.t))],
                fs=series.fs,
            ),
            truth,
        )

    def test_two_segments_are_filtered_independently(self):
        series, _ = self.build_two_segments()
        navigation = run_ins(series)
        assert np.all(np.isfinite(navigation.p))
        assert len(navigation.t) == len(series.t)

    def test_velocity_is_reset_at_a_segment_boundary(self):
        """空洞之后受试者的速度是未知的。假装知道会让第一个 ZUPT 被过分自信的先验压住。"""
        series, _ = self.build_two_segments()
        cut = series.segments[1][0]
        navigation = run_ins(series)
        # 段首那一刻的速度应当接近零（复位值经第一次观测微调后）。
        assert np.linalg.norm(navigation.v[cut]) < 0.05

    def test_uncovered_samples_are_refused(self):
        """该填什么由 RAY-210 定义。在它定义之前，发明一个填法比停下来更糟。"""
        series, _ = walk(5.0)
        n = len(series.t)
        holed = FootSeries(
            label=series.label,
            t=series.t,
            acc=series.acc,
            gyr=series.gyr,
            quality=series.quality,
            segments=[(0, n // 3), (2 * n // 3, n)],
            fs=series.fs,
        )
        with pytest.raises(EskfError, match="覆盖整个序列"):
            run_ins(holed)


class TestRejections:
    def test_a_non_foot_series_is_refused(self):
        with pytest.raises(EskfError, match="FootSeries"):
            run_ins(object())  # type: ignore[arg-type]

    def test_a_session_without_a_still_lead_aligns_on_the_first_stance(self):
        """没有静立前导也能对准 —— 用第一个支撑相。

        这不是本来预期的行为（PRD §7 的流程有静立 5 s），但它是正确的：支撑相本身就是
        一段静止，`find_still_window` 找到的就是它。写成测试而不是留着当惊喜，因为
        它有一个使用上的后果：**在这种会话里，对准窗口只有几百毫秒**，倾角不确定度
        比静立 5 s 的情形大一个量级。质量标注（RAY-218）应当据 `Alignment.samples`
        区分这两种情况。
        """
        series, _ = walk(6.0, still_lead_s=0.0)
        alignment = initial_alignment(series.acc, series.gyr, series.fs)
        assert alignment.window[1] - alignment.window[0] < round(1.0 * series.fs)
        assert np.all(np.isfinite(run_ins(series).p))

    def test_a_still_window_shorter_than_required_is_refused(self):
        """要求的窗口长过任何一个支撑相时，就该拒绝而不是凑合。"""
        series, _ = walk(6.0, still_lead_s=0.0)
        with pytest.raises(AlignmentError, match="静止段"):
            initial_alignment(series.acc, series.gyr, series.fs, minimum_seconds=3.0)

    def test_a_supplied_alignment_bypasses_the_still_window_search(self):
        """会话标定已经给过初始姿态时，不必再找一次静止段。"""
        series, _ = walk(6.0)
        alignment = initial_alignment(series.acc, series.gyr, series.fs)
        assert np.all(np.isfinite(run_ins(series, alignment=alignment).p))
