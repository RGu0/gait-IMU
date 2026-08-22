"""`gait.core.quaternion` 的约定与数值行为。

这个文件里最重要的不是"函数算得对不对"，而是**约定有没有被钉住**。分量顺序、
Hamilton/JPL、旋转方向这三件事错了都不会抛异常，只会让轨迹缓慢弯曲。所以前几条
测试断言的是具体的数值取值（`identity()[0] == 1`、东转 90° 得到北），它们看起来
像在测显而易见的东西 —— 它们测的是"没人在重构时把约定悄悄换掉"。
"""

import numpy as np
import pytest

from gait.core import quaternion as quat

# 90°，以及一个不对称、不特殊的角度，用来暴露那些只在对称输入下碰巧成立的实现。
RIGHT_ANGLE = np.pi / 2
ODD_ANGLE = 0.7139


def q_about(axis: str, angle: float) -> np.ndarray:
    """绕某一坐标轴转 `angle` 的四元数，用旋转矢量构造。"""
    unit = {"x": [1.0, 0, 0], "y": [0, 1.0, 0], "z": [0, 0, 1.0]}[axis]
    return quat.from_rotation_vector(np.array(unit) * angle)


class TestConventionIsPinned:
    """三个约定的可断言声明。改了实现就会在这里失败。"""

    def test_layout_is_scalar_first(self):
        assert quat.QUATERNION_LAYOUT == "wxyz"
        assert np.allclose(quat.identity(), [1.0, 0.0, 0.0, 0.0])

    def test_rotation_sense_is_foot_to_nav(self):
        assert quat.ROTATION_SENSE == "foot_to_nav"

    def test_east_rotated_90_about_up_becomes_north(self):
        """ENU 下绕 z 正转 90°，东 → 北。

        这一条同时钉住了三件事：分量顺序、右手系正方向、以及 `rotate` 的方向。
        任何一个反了，结果都会是 (0, -1, 0) 而不是 (0, 1, 0)。
        """
        east = np.array([1.0, 0.0, 0.0])
        assert np.allclose(quat.rotate(q_about("z", RIGHT_ANGLE), east), [0.0, 1.0, 0.0])

    def test_hamilton_not_jpl(self):
        """Hamilton 约定：i ⊗ j = k。JPL 下这里会得到 -k。"""
        i = np.array([0.0, 1.0, 0.0, 0.0])
        j = np.array([0.0, 0.0, 1.0, 0.0])
        k = np.array([0.0, 0.0, 0.0, 1.0])
        assert np.allclose(quat.multiply(i, j), k)

    def test_multiply_composes_right_to_left(self):
        """`a ⊗ b` 表示先转 b 再转 a，与矩阵乘法同序。"""
        a = q_about("z", ODD_ANGLE)
        b = q_about("x", RIGHT_ANGLE)
        v = np.array([0.3, -1.2, 2.0])
        assert np.allclose(quat.rotate(quat.multiply(a, b), v), quat.rotate(a, quat.rotate(b, v)))

    def test_euler_sequence_is_declared(self):
        assert quat.EULER_SEQUENCE == "ZYX"


class TestAlgebra:
    def test_conjugate_is_the_inverse(self):
        q = q_about("y", ODD_ANGLE)
        assert np.allclose(quat.multiply(q, quat.conjugate(q)), quat.identity())

    def test_rotate_inverse_undoes_rotate(self):
        q = quat.from_rotation_vector(np.array([0.4, -0.9, 1.3]))
        v = np.array([2.0, -0.5, 0.25])
        assert np.allclose(quat.rotate_inverse(q, quat.rotate(q, v)), v)

    def test_rotation_preserves_length(self):
        q = quat.from_rotation_vector(np.array([1.7, 0.2, -0.6]))
        v = np.array([3.0, -4.0, 12.0])
        assert np.isclose(np.linalg.norm(quat.rotate(q, v)), np.linalg.norm(v))

    def test_matrix_and_rotate_agree(self):
        q = quat.from_rotation_vector(np.array([0.11, 1.4, -0.33]))
        v = np.array([1.0, 2.0, 3.0])
        assert np.allclose(quat.to_matrix(q) @ v, quat.rotate(q, v))

    def test_matrix_is_orthonormal_with_unit_determinant(self):
        """行列式必须是 +1。-1 说明混进了一次镜像，那不是旋转。"""
        q = quat.from_rotation_vector(np.array([-0.8, 0.5, 2.1]))
        matrix = quat.to_matrix(q)
        assert np.allclose(matrix @ matrix.T, np.eye(3))
        assert np.isclose(np.linalg.det(matrix), 1.0)

    def test_rotate_tolerates_a_non_unit_quaternion(self):
        """非单位四元数不得静默放大向量 —— 那看起来正像标定没做好。"""
        q = q_about("z", ODD_ANGLE) * 1.01
        v = np.array([1.0, 0.0, 0.0])
        assert np.isclose(np.linalg.norm(quat.rotate(q, v)), 1.0)


class TestExponentialMap:
    def test_round_trip_at_an_ordinary_angle(self):
        rv = np.array([0.3, -1.1, 0.7])
        assert np.allclose(quat.to_rotation_vector(quat.from_rotation_vector(rv)), rv)

    def test_round_trip_near_pi(self):
        """接近 180° 是 `from_matrix` 的单公式实现会失手的地方，也一并守住。"""
        rv = np.array([0.0, 0.0, np.pi - 1e-6])
        assert np.allclose(quat.to_rotation_vector(quat.from_rotation_vector(rv)), rv)

    @pytest.mark.parametrize("scale", [0.0, 1e-14, 1e-10, 1e-7])
    def test_tiny_rotations_do_not_produce_nan(self, scale):
        """`sin(θ/2)/θ` 在 θ→0 是 0/0。一个 nan 进了姿态，后面整条轨迹都是 nan。

        200 Hz 下支撑相里 `ω·dt` 正是这个量级，所以这条不是理论边界。
        """
        rv = np.array([1.0, -2.0, 0.5]) * scale
        q = quat.from_rotation_vector(rv)
        assert np.all(np.isfinite(q))
        assert np.isclose(np.linalg.norm(q), 1.0)
        assert np.allclose(quat.to_rotation_vector(q), rv, atol=1e-18)

    def test_log_takes_the_short_way_round(self):
        """q 与 -q 是同一个旋转；对数必须落在 [0, π]，否则 ESKF 会收到一个 2π 的"小量"。"""
        q = q_about("z", 0.9)
        assert np.allclose(quat.to_rotation_vector(-q), quat.to_rotation_vector(q))
        assert np.linalg.norm(quat.to_rotation_vector(-q)) <= np.pi


class TestMatrixConversion:
    @pytest.mark.parametrize(
        "rv",
        [
            [0.0, 0.0, 0.0],
            [0.2, 0.3, -0.4],
            [np.pi, 0.0, 0.0],  # 绕 x 转 180°：trace = -1，单公式分支会在这里除以零
            [0.0, np.pi, 0.0],
            [0.0, 0.0, np.pi],
            [1.2, -2.0, 0.9],
        ],
    )
    def test_round_trip_through_a_matrix(self, rv):
        q = quat.from_rotation_vector(np.array(rv))
        recovered = quat.from_matrix(quat.to_matrix(q))
        assert quat.angle_between(q, recovered) < 1e-9

    def test_from_matrix_normalises_the_sign(self):
        """输出固定 w ≥ 0，否则"两个四元数是否相等"没有唯一答案。"""
        q = q_about("z", 3.0)
        assert quat.from_matrix(quat.to_matrix(q))[0] >= 0.0

    def test_from_matrix_rejects_a_wrong_shape(self):
        with pytest.raises(quat.QuaternionError):
            quat.from_matrix(np.eye(4))


class TestEuler:
    @pytest.mark.parametrize(
        ("roll", "pitch", "yaw"),
        [
            (0.0, 0.0, 0.0),
            (0.3, -0.4, 1.9),
            (-1.2, 0.8, -2.7),
        ],
    )
    def test_round_trip(self, roll, pitch, yaw):
        q = quat.from_euler(roll, pitch, yaw)
        r, p, y = quat.to_euler(q)
        assert np.allclose([r, p, y], [roll, pitch, yaw])

    def test_single_axis_angles_land_where_expected(self):
        r, p, y = quat.to_euler(q_about("z", ODD_ANGLE))
        assert np.allclose([r, p, y], [0.0, 0.0, ODD_ANGLE])

    def test_gimbal_lock_stays_finite(self):
        """pitch = ±90° 处 roll 与 yaw 不可分。此处不报错，但绝不能出 nan。"""
        r, p, y = quat.to_euler(quat.from_euler(0.0, RIGHT_ANGLE, 0.0))
        assert np.all(np.isfinite([r, p, y]))
        assert np.isclose(p, RIGHT_ANGLE)


class TestAngularRateIntegration:
    def test_constant_rate_is_exact(self):
        """转轴不变时旋转矢量可直接取指数，因此这一步不是一阶近似而是精确的。"""
        omega = np.array([0.0, 0.0, 1.3])
        q = quat.identity()
        dt = 1.0 / 200.0
        for _ in range(200):
            q = quat.integrate_angular_rate(q, omega, dt)
        assert quat.angle_between(q, q_about("z", 1.3)) < 1e-12

    def test_the_unit_is_radians_per_second(self):
        """2π rad/s 积分 1 s 必须正好转回原位。

        若有人按 deg/s 理解这个参数，这里会转出 360 rad ≈ 57 圈，角度差远大于阈值。
        契约 R2 之后全链路 SI（`contracts.FIELD_UNITS`），这条测试是它在 core 里的落点。
        """
        q = quat.identity()
        omega = np.array([0.0, 0.0, 2.0 * np.pi])
        for _ in range(1000):
            q = quat.integrate_angular_rate(q, omega, 1.0 / 1000.0)
        assert quat.angle_between(q, quat.identity()) < 1e-12

    def test_rate_is_read_in_the_body_frame(self):
        """右乘 = 绕自身轴转。绕体轴 x 转 90° 后再绕体轴 z 转 90°，
        等价于导航系里先绕 x 后绕 **原来的 y**，而不是绕导航系 z。
        """
        after_x = quat.integrate_angular_rate(quat.identity(), np.array([RIGHT_ANGLE, 0, 0]), 1.0)
        after_both = quat.integrate_angular_rate(after_x, np.array([0, 0, RIGHT_ANGLE]), 1.0)
        expected = quat.multiply(q_about("x", RIGHT_ANGLE), q_about("z", RIGHT_ANGLE))
        assert quat.angle_between(after_both, expected) < 1e-12


class TestBatching:
    def test_operations_broadcast_over_a_leading_axis(self):
        """契约里 `NavResult.q` 是 (n, 4)；一次会话 36000 个，逐个走 Python 循环不行。"""
        rv = np.random.default_rng(0).normal(size=(17, 3))
        q = quat.from_rotation_vector(rv)
        assert q.shape == (17, 4)
        assert quat.to_matrix(q).shape == (17, 3, 3)
        assert quat.rotate(q, np.array([1.0, 0.0, 0.0])).shape == (17, 3)
        assert quat.multiply(q, q).shape == (17, 4)
        assert np.allclose(quat.angle_between(q, q), 0.0)

    def test_batched_matrix_round_trip(self):
        rv = np.random.default_rng(1).normal(size=(5, 3)) * 2.0
        q = quat.from_rotation_vector(rv)
        assert np.all(quat.angle_between(q, quat.from_matrix(quat.to_matrix(q))) < 1e-9)


class TestRejections:
    def test_normalize_refuses_a_zero_quaternion(self):
        """零范数只会来自未初始化的内存或已经发散的滤波器。当场停，别继续传播。"""
        with pytest.raises(quat.QuaternionError):
            quat.normalize(np.zeros(4))

    @pytest.mark.parametrize("bad", [np.zeros(3), np.zeros((2, 5)), np.float64(1.0)])
    def test_wrong_quaternion_shape_is_rejected(self, bad):
        with pytest.raises(quat.QuaternionError):
            quat.conjugate(bad)

    def test_wrong_vector_shape_is_rejected(self):
        with pytest.raises(quat.QuaternionError):
            quat.rotate(quat.identity(), np.zeros(4))
