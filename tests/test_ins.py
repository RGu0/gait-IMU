"""`gait.core.ins` 的机械编排。

验收标准是"与解析解对比误差在数值精度内"。这里把它拆成三档，因为三种情况的
"数值精度"根本不是一个量级：

* **姿态**：区间内角速度恒定时旋转矢量可直接取指数 —— 精确，阈值取 1e-12。
* **常加速度**：`p += v·dt + ½·a·dt²` 对恒定加速度是精确的 —— 阈值同样取机器精度。
* **旋转 + 常体轴比力**：有闭式解，但中点法只是二阶。这里不能只断言一个绝对阈值，
  因为任何一个阈值都可能被巧合满足。**还要断言收敛阶**：dt 减半，误差降到约 1/4。
  一个一阶实现、一个符号写反的实现、一个把中点写成起点的实现，都过不了这一条。
"""

import numpy as np
import pytest

from gait.core import ins
from gait.core import quaternion as quat

FS = 200.0  # PRD 的标称采样率
DT = 1.0 / FS


def still_sequence(seconds: float, gravity: float = ins.GRAVITY_STANDARD):
    """静止平放的模块会输出什么：比力 = (0, 0, +g)，角速度 = 0。"""
    n = int(seconds * FS) + 1
    acc = np.tile([0.0, 0.0, gravity], (n, 1))
    gyr = np.zeros((n, 3))
    return acc, gyr


class TestGravity:
    def test_vector_points_down_in_enu(self):
        """ENU 下重力矢量是 (0, 0, -g)。只有这一处构造它，正是为了不让符号散开。"""
        assert np.allclose(ins.gravity_vector(9.8), [0.0, 0.0, -9.8])

    def test_latitude_model_matches_published_values(self):
        """Somigliana 公式在赤道与极点的值是有文献值可对的。"""
        assert np.isclose(ins.gravity_magnitude(0.0), 9.7803253359, atol=1e-7)
        assert np.isclose(ins.gravity_magnitude(90.0), 9.8321849379, atol=1e-6)

    def test_equator_to_pole_difference_is_large_enough_to_matter(self):
        """0.5% 的差异不是学术细节：整体设计 §5.4 说 0.1% 就值 0.01 m/s² 的漂移。"""
        difference = ins.gravity_magnitude(90.0) - ins.gravity_magnitude(0.0)
        assert difference / ins.GRAVITY_STANDARD > 0.005

    def test_altitude_reduces_gravity(self):
        assert ins.gravity_magnitude(30.0, 1000.0) < ins.gravity_magnitude(30.0, 0.0)

    def test_symmetric_in_hemisphere(self):
        assert np.isclose(ins.gravity_magnitude(45.0), ins.gravity_magnitude(-45.0))

    @pytest.mark.parametrize("bad", [-91.0, 91.0])
    def test_rejects_impossible_latitude(self, bad):
        with pytest.raises(ins.InsError):
            ins.gravity_magnitude(bad)

    def test_rejects_non_positive_magnitude(self):
        with pytest.raises(ins.InsError):
            ins.gravity_vector(0.0)


class TestSpecificForceConvention:
    """符号约定。错了不报错，只让静止的模块"掉下去"。"""

    def test_a_still_module_does_not_move_in_ten_minutes(self):
        """静置 10 分钟，位置与速度必须恒为零。

        这是整个模块最重要的一条。加速度计测的是比力 `f = a - g`，所以静止时它读
        `+g` 而不是 0；导航方程是 `a_n = C·f + g_n`，其中 `g_n` 向下。这两个符号里
        任何一个反了，模块都会以 2g "掉下去"或"飞上天"。真正难查的是只有一个反且
        被别处部分抵消 —— 表现为轨迹缓慢下沉。这条测试把它钉死在原点。

        10 分钟 = 120001 个采样，也顺便证明累积误差不来自积分器本身。
        """
        acc, gyr = still_sequence(600.0)
        q, v, p = ins.mechanize(acc, gyr, DT, q0=quat.identity())
        assert np.max(np.abs(v)) < 1e-12
        assert np.max(np.abs(p)) < 1e-12
        assert np.all(quat.angle_between(q, quat.identity()) < 1e-15)

    def test_zero_specific_force_is_free_fall(self):
        """比力为零 = 自由落体。z 应为 -½gt²，不是 +½gt²，也不是 0。"""
        seconds = 2.0
        n = int(seconds * FS) + 1
        _, v, p = ins.mechanize(
            np.zeros((n, 3)), np.zeros((n, 3)), DT, q0=quat.identity()
        )
        assert np.isclose(p[-1, 2], -0.5 * ins.GRAVITY_STANDARD * seconds**2)
        assert np.isclose(v[-1, 2], -ins.GRAVITY_STANDARD * seconds)
        assert np.allclose(p[-1, :2], 0.0)

    def test_the_gravity_used_is_the_one_passed_in(self):
        """传进去的重力必须真的被用上 —— 否则 `gravity_magnitude` 只是装饰。"""
        acc, gyr = still_sequence(1.0, gravity=ins.GRAVITY_STANDARD)
        _, _, p = ins.mechanize(acc, gyr, DT, q0=quat.identity(), gravity=9.7803253359)
        # 用赤道重力去解释一份按标准重力生成的数据：模块"多出"了 Δg 的向上比力，
        # 1 秒内应当上浮约 ½·Δg·t²。方向也要对 —— 用错重力不该表现成下沉。
        expected = 0.5 * (ins.GRAVITY_STANDARD - 9.7803253359) * 1.0
        assert np.isclose(p[-1, 2], expected, rtol=1e-3)
        assert p[-1, 2] > 0.0


class TestConstantAccelerationIsExact:
    def test_straight_line_matches_the_closed_form(self):
        """无旋转、恒定水平比力：v = at、p = ½at²，积分格式对它精确。"""
        seconds = 3.0
        n = int(seconds * FS) + 1
        thrust = 1.7
        acc = np.tile([thrust, 0.0, ins.GRAVITY_STANDARD], (n, 1))
        _, v, p = ins.mechanize(acc, np.zeros((n, 3)), DT, q0=quat.identity())
        assert np.isclose(v[-1, 0], thrust * seconds, rtol=1e-12)
        assert np.isclose(p[-1, 0], 0.5 * thrust * seconds**2, rtol=1e-12)
        assert abs(p[-1, 2]) < 1e-9

    def test_initial_state_is_honoured(self):
        n = 5
        acc, gyr = still_sequence(0.02)
        _, v, p = ins.mechanize(
            acc[:n],
            gyr[:n],
            DT,
            q0=quat.identity(),
            v0=np.array([1.0, 0.0, 0.0]),
            p0=np.array([0.0, 2.0, 0.0]),
        )
        assert np.allclose(v[0], [1.0, 0.0, 0.0])
        assert np.allclose(p[0], [0.0, 2.0, 0.0])
        # 静止的模块以 1 m/s 匀速平移：位置线性增长，速度不变。
        assert np.allclose(v[-1], [1.0, 0.0, 0.0])
        assert np.isclose(p[-1, 0], (n - 1) * DT)


def rotating_reference(amplitude: float, rate: float, seconds: float):
    """常角速度 + 常体轴比力的解析解。

    体轴比力取 `f_b = (A, 0, g)`，绕体轴 z 以 `ω` 匀速自转，初始姿态为单位。转轴
    就是 z，所以 `C(t) = R_z(ωt)` 不动 z 分量，那一项恰好抵掉重力：

        a_n(t) = R_z(ωt)·(A, 0, g) + (0, 0, -g) = (A·cos ωt, A·sin ωt, 0)

    垂直方向因此恒为零（一个刻意的设置：让 z 上任何非零位移都直接指向符号错误），
    水平方向可以直接积分：

        v(t) = (A/ω)·(sin ωt, 1 - cos ωt, 0)
        p(t) = (A/ω²)·(1 - cos ωt, ωt - sin ωt, 0)
    """
    theta = rate * seconds
    v = np.array(
        [
            amplitude / rate * np.sin(theta),
            amplitude / rate * (1.0 - np.cos(theta)),
            0.0,
        ]
    )
    p = np.array(
        [
            amplitude / rate**2 * (1.0 - np.cos(theta)),
            amplitude / rate**2 * (theta - np.sin(theta)),
            0.0,
        ]
    )
    return v, p


def rotating_case(fs: float, amplitude: float = 5.0, rate: float = 3.0, seconds: float = 2.0):
    """按给定采样率跑一遍上面的算例，返回终点的姿态/速度/位置误差。"""
    n = round(seconds * fs) + 1
    acc = np.tile([amplitude, 0.0, ins.GRAVITY_STANDARD], (n, 1))
    gyr = np.tile([0.0, 0.0, rate], (n, 1))
    q, v, p = ins.mechanize(acc, gyr, 1.0 / fs, q0=quat.identity())
    v_true, p_true = rotating_reference(amplitude, rate, seconds)
    attitude_error = float(
        quat.angle_between(q[-1], quat.from_rotation_vector([0.0, 0.0, rate * seconds]))
    )
    return attitude_error, float(np.linalg.norm(v[-1] - v_true)), float(np.linalg.norm(p[-1] - p_true))


class TestRotatingCaseAgainstTheClosedForm:
    def test_attitude_is_exact_regardless_of_step_size(self):
        """姿态不受积分阶数影响：转轴不变时指数映射是精确的。"""
        for fs in (50.0, 200.0, 800.0):
            attitude_error, _, _ = rotating_case(fs)
            assert attitude_error < 1e-12, fs

    def test_position_error_at_the_nominal_rate_is_small(self):
        _, velocity_error, position_error = rotating_case(FS)
        assert velocity_error < 1e-3
        assert position_error < 1e-3

    def test_convergence_is_second_order(self):
        """dt 减半，误差降到约 1/4。

        这一条才是真正验证"中点法"的：一个用区间**起点**姿态转换比力的实现（一阶）
        在这里会给出约 1/2 的比值而不是 1/4，而它完全可以通过上面的绝对阈值。
        """
        _, v_coarse, p_coarse = rotating_case(200.0)
        _, v_fine, p_fine = rotating_case(400.0)
        assert 3.5 < v_coarse / v_fine < 4.5
        assert 3.5 < p_coarse / p_fine < 4.5


class TestSampleAlignment:
    def test_outputs_are_the_same_length_as_the_input(self):
        """契约 §3.3 的 `NavResult` 各数组与时间轴等长，这是它的前提。"""
        acc, gyr = still_sequence(0.5)
        q, v, p = ins.mechanize(acc, gyr, DT, q0=quat.identity())
        assert q.shape == (len(acc), 4)
        assert v.shape == p.shape == (len(acc), 3)

    def test_state_zero_is_the_initial_state_untouched(self):
        acc, gyr = still_sequence(0.1)
        q, v, p = ins.mechanize(acc, gyr, DT, q0=quat.identity())
        assert np.allclose(q[0], quat.identity())
        assert np.allclose(v[0], 0.0)
        assert np.allclose(p[0], 0.0)

    def test_the_last_measurement_is_deliberately_unused(self):
        """最后一个采样之后没有区间，因此它的测量值不参与计算。

        这不是漏了，是"状态与采样一一对应"的必然结果 —— 文档里写了，这里钉住它，
        免得有人"顺手修好"之后输出比时间轴长一个。
        """
        acc, gyr = still_sequence(0.2)
        baseline = ins.mechanize(acc, gyr, DT, q0=quat.identity())
        acc[-1] = [1e4, -1e4, 1e4]
        gyr[-1] = [50.0, 50.0, 50.0]
        perturbed = ins.mechanize(acc, gyr, DT, q0=quat.identity())
        for before, after in zip(baseline, perturbed, strict=True):
            assert np.array_equal(before, after)

    def test_a_single_sample_yields_only_the_initial_state(self):
        """一个采样构不成任何积分区间，但也不是错误 —— 空洞切分会切出这种段。

        契约要求各数组与时间轴等长，所以这里必须返回长度 1 而不是长度 0 或报错。
        """
        q, v, p = ins.mechanize(
            np.array([[0.0, 0.0, ins.GRAVITY_STANDARD]]),
            np.zeros((1, 3)),
            DT,
            q0=quat.identity(),
            v0=np.array([0.5, 0.0, 0.0]),
        )
        assert q.shape == (1, 4)
        assert np.allclose(v[0], [0.5, 0.0, 0.0])
        assert np.allclose(p[0], 0.0)

    def test_propagate_and_mechanize_agree(self):
        """逐步接口与整段接口必须给出同一个答案 —— ESKF 用前者，回归测试用后者。"""
        rng = np.random.default_rng(7)
        n = 40
        acc = rng.normal(scale=3.0, size=(n, 3)) + np.array([0.0, 0.0, ins.GRAVITY_STANDARD])
        gyr = rng.normal(scale=2.0, size=(n, 3))
        q, v, p = ins.mechanize(acc, gyr, DT, q0=quat.identity())

        state = ins.InsState(q=quat.identity(), v=np.zeros(3), p=np.zeros(3))
        gravity = ins.gravity_vector()
        for k in range(n - 1):
            state = ins.propagate(state, acc[k], gyr[k], DT, gravity)
        assert quat.angle_between(state.q, q[-1]) < 1e-13
        assert np.allclose(state.v, v[-1])
        assert np.allclose(state.p, p[-1])


class TestNonUniformSampling:
    def test_an_array_of_intervals_is_accepted(self):
        """空洞切分之后段与段之间不等间隔 —— 这是它存在的理由。"""
        acc, gyr = still_sequence(0.1)
        steps = np.full(len(acc) - 1, DT)
        steps[5] = DT * 3.0
        _, v, p = ins.mechanize(acc, gyr, steps, q0=quat.identity())
        assert np.max(np.abs(v)) < 1e-12
        assert np.max(np.abs(p)) < 1e-12

    def test_intervals_from_time_matches_diff(self):
        t = np.array([0.0, 0.005, 0.01, 0.02])
        assert np.allclose(ins.intervals_from_time(t), [0.005, 0.005, 0.01])

    @pytest.mark.parametrize("t", [[0.0, 0.005, 0.005], [0.0, 0.01, 0.005]])
    def test_a_non_increasing_time_axis_is_refused(self, t):
        """非正的 dt 会让积分沿时间倒着走，且不会报错。在入口拒绝。"""
        with pytest.raises(ins.InsError, match="严格递增"):
            ins.intervals_from_time(np.array(t))

    def test_time_axis_needs_at_least_two_samples(self):
        with pytest.raises(ins.InsError):
            ins.intervals_from_time(np.array([1.0]))


class TestRejections:
    def test_mismatched_lengths(self):
        with pytest.raises(ins.InsError, match="样本数必须一致"):
            ins.mechanize(np.zeros((4, 3)), np.zeros((5, 3)), DT, q0=quat.identity())

    def test_wrong_column_count(self):
        with pytest.raises(ins.InsError):
            ins.mechanize(np.zeros((4, 2)), np.zeros((4, 2)), DT, q0=quat.identity())

    def test_empty_sequence(self):
        with pytest.raises(ins.InsError, match="空序列"):
            ins.mechanize(np.zeros((0, 3)), np.zeros((0, 3)), DT, q0=quat.identity())

    def test_interval_array_of_the_wrong_length(self):
        """区间数比采样数少一个。多给一个通常意味着调用方把 dt 和 t 弄混了。"""
        with pytest.raises(ins.InsError, match="n-1"):
            ins.mechanize(np.zeros((4, 3)), np.zeros((4, 3)), np.full(4, DT), q0=quat.identity())

    def test_non_positive_interval(self):
        with pytest.raises(ins.InsError):
            ins.mechanize(np.zeros((3, 3)), np.zeros((3, 3)), -DT, q0=quat.identity())

    def test_bad_initial_shapes(self):
        acc, gyr = still_sequence(0.05)
        with pytest.raises(ins.InsError, match="q0"):
            ins.mechanize(acc, gyr, DT, q0=np.zeros(3))
        with pytest.raises(ins.InsError, match="v0"):
            ins.mechanize(acc, gyr, DT, q0=quat.identity(), v0=np.zeros(4))

    def test_state_validates_its_own_shapes(self):
        with pytest.raises(ins.InsError):
            ins.InsState(q=np.zeros(3), v=np.zeros(3), p=np.zeros(3))
        with pytest.raises(ins.InsError):
            ins.InsState(q=quat.identity(), v=np.zeros(2), p=np.zeros(3))

    def test_propagate_refuses_a_non_positive_step(self):
        state = ins.InsState(q=quat.identity(), v=np.zeros(3), p=np.zeros(3))
        with pytest.raises(ins.InsError):
            ins.propagate(state, np.zeros(3), np.zeros(3), 0.0, ins.gravity_vector())

    def test_propagate_names_the_offending_argument(self):
        """错的是 `acc`，报出来就得说 `acc` —— 让 quaternion 层去报会说成 `v`。"""
        state = ins.InsState(q=quat.identity(), v=np.zeros(3), p=np.zeros(3))
        with pytest.raises(ins.InsError, match="acc"):
            ins.propagate(state, np.zeros(2), np.zeros(3), DT, ins.gravity_vector())
