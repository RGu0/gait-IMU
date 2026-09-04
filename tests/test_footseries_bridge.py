"""`device.footseries` —— 原始帧到 `FootSeries` 的数据桥（RAY-345）。

坐标系重排是这里唯一「有物理对错」的地方（《BS-BT91 硬件适配》：必须写成显式常量
并加单元测试），所以重排的单测占大头；其余断言只钉住桥接不丢数据、不造空值。
"""

import numpy as np
import pytest
from wt901.protocol.units import accel_to_m_s2, angular_velocity_to_rad_s

from gait.contracts import FootSeries, Quality, RawFrame
from gait.device.footseries import (
    MODULE_TO_FOOT,
    FootSeriesError,
    frames_to_foot_series,
    reorder_module_to_foot,
)
from gait.validate.synthetic import WalkSpec, generate_walk


def _frame(acc=(0, 0, 0), gyr=(0, 0, 0), t=0.0, saturated=False) -> RawFrame:
    return RawFrame(
        t_host=t,
        acc_raw=np.asarray(acc, dtype=np.int16),
        gyr_raw=np.asarray(gyr, dtype=np.int16),
        ang_raw=np.asarray((0, 0, 0), dtype=np.int16),
        saturated=saturated,
    )


def test_both_feet_use_the_same_identity_mapping():
    """模块系与足部系逐轴相同（RAY-390 R1），所以映射恒等、两脚一致。

    **两只脚必须一样。** 让左右脚的映射不同正是 RAY-390 那个缺陷的来源：
    偏侧性归 `label`，不归坐标轴。
    """
    acc = np.array([[1.0, 2.0, 3.0]])
    gyr = np.array([[4.0, 5.0, 6.0]])
    for label in ("L", "R"):
        foot_acc, foot_gyr = reorder_module_to_foot(acc, gyr, label)
        assert np.array_equal(foot_acc, acc)
        assert np.array_equal(foot_gyr, gyr)


def test_module_to_foot_is_a_proper_rotation():
    """行列式必须是 +1。det = −1 是镜射，而角速度是伪矢量。

    这条**能失败**：RAY-390 之前左足用的是 `[[0,1,0],[1,0,0],[0,0,1]]`，det = −1，
    合成数据上步长 1.068 m 对真值 1.300 m，而步频步时分毫不差。
    """
    assert np.linalg.det(MODULE_TO_FOOT) == pytest.approx(1.0)
    # 正交也一并钉住：带缩放的矩阵行列式也可能是 1。
    assert MODULE_TO_FOOT @ MODULE_TO_FOOT.T == pytest.approx(np.eye(3))


def test_medio_lateral_energy_lands_on_the_y_axis():
    """**轴的对应**：矢状面的踝背屈/跖屈绕内外侧轴，所以陀螺能量必须落在足部 Y。

    只守行列式拦不住错的轴交换 —— RAY-390 之前右足的映射 det 就是 +1，却把内外侧轴
    当成了前进轴。所以这一条单独存在。

    用合成步行（它按契约的足部系造数）当模块系输入：映射既是恒等，能量就该原样
    留在 Y 上。换成任何一个把 X/Y 对调的映射，这条断言立刻变红 —— 真机上也是这个
    量：RAY-230 T-230-03 的 24 个格子里陀螺能量有 67%~90% 压在同一根轴上。
    """
    series, _ = generate_walk(WalkSpec(fs=200.0), foot="L")
    foot_acc, foot_gyr = reorder_module_to_foot(series.acc, series.gyr, "L")

    energy = (foot_gyr ** 2).sum(axis=0)
    share = energy / energy.sum()
    assert np.argmax(share) == 1, f"内外侧轴应是 Y，实测能量分布 {share}"
    assert share[1] > 0.6

    # 重力（静立前导）落在 Z 上，而不是被搬到别的轴。
    still = foot_acc[:100].mean(axis=0)
    assert np.argmax(np.abs(still)) == 2
    assert still[2] > 0


def test_reorder_rejects_unknown_label():
    acc = np.zeros((1, 3))
    with pytest.raises(FootSeriesError):
        reorder_module_to_foot(acc, acc, "X")


def test_si_conversion_and_reorder_are_applied():
    # 模块 X = 前 = +1 g（2048 计数）。映射恒等，所以足部 X 也是 +g。
    frames = [_frame(acc=(2048, 0, 0))]
    series = frames_to_foot_series(frames, "L")
    assert series.acc[0, 0] == pytest.approx(accel_to_m_s2(2048), abs=1e-6)
    assert series.acc[0, 1] == pytest.approx(0.0, abs=1e-9)


def test_frames_to_foot_series_shape_and_timebase():
    n = 50
    frames = [_frame(acc=(2048, 0, 0), gyr=(100, 0, 0), t=float(i)) for i in range(n)]
    series = frames_to_foot_series(frames, "L")
    assert isinstance(series, FootSeries)
    assert series.label == "L"
    assert series.acc.shape == (n, 3)
    assert series.gyr.shape == (n, 3)
    assert series.quality.shape == (n,)
    assert series.fs == pytest.approx(200.0)
    assert series.segments == [(0, n)]
    # 标称均匀时轴：t[k] = k / 200
    assert series.t[0] == 0.0
    assert series.t[-1] == pytest.approx((n - 1) / 200.0)


def test_saturated_frames_are_flagged():
    frames = [_frame(acc=(2048, 0, 0)), _frame(acc=(2048, 0, 0), saturated=True)]
    series = frames_to_foot_series(frames, "L")
    assert series.quality[0] == Quality.NONE
    assert series.quality[1] & Quality.SATURATED


def test_empty_input_is_rejected():
    with pytest.raises(FootSeriesError):
        frames_to_foot_series([], "L")


def test_gyro_is_in_si_rad_s():
    frames = [_frame(gyr=(3277, 0, 0))]
    series = frames_to_foot_series(frames, "L")
    # 映射恒等，module_x 就是足部 X。这里钉的是单位，不是轴。
    expected = angular_velocity_to_rad_s(3277)
    assert series.gyr[0, 0] == pytest.approx(expected, rel=1e-6)


def test_both_feet_land_on_the_same_stride_length():
    """RAY-390 的验收判据二：左右足相对真值的偏差同号同量级。

    这一条走的是**整条链**（`run_basic_chain`），而不是只看矩阵 —— 因为那个缺陷的
    危险之处正在于矩阵之外：`det = −1` 让左足角速度反号，链照跑不误，只是步长
    静默偏低 17.8%，而步频步时分毫不差。修复前是 L 1.0681 / R 1.3001（真值 1.3），
    表现为一个**不存在的左右不对称** —— 而左右不对称在步态报告里是个临床读数。

    慢（两次合成 + 两次前向链），但它是唯一能量到「不对称」这件事的判据。

    **它单独并不够，这点要说清楚。** 变异验证过：把两只脚**同时**换成 det = −1 的
    映射，本条仍然通过 —— 两只脚一样错就不是不对称了。挡住那一层的是
    `test_module_to_foot_is_a_proper_rotation`。三条判据各挡一层，谁也不能替谁：

    | 判据 | 挡的是 |
    | -- | -- |
    | 行列式 = +1 | 镜射（角速度反号） |
    | 内外侧能量在 Y | 轴交换错（det 可以是 +1） |
    | 两足步长一致 | 左右用了不同的约定 |
    """
    from gait.cloud.chain import run_basic_chain

    lengths = {}
    for label in ("L", "R"):
        series, _ = generate_walk(WalkSpec(fs=200.0), foot=label)
        acc, gyr = reorder_module_to_foot(series.acc, series.gyr, label)
        rebuilt = FootSeries(
            label=label, t=series.t, acc=acc, gyr=gyr, quality=series.quality,
            segments=series.segments, fs=series.fs,
        )
        outcome = run_basic_chain({label: rebuilt}).feet[label].snapshot()
        lengths[label] = outcome["spatiotemporal"]["stride_length"]

    assert lengths["L"] == pytest.approx(lengths["R"], rel=1e-6), (
        f"两足步长应当一致，实测 L={lengths['L']:.4f} R={lengths['R']:.4f}。"
        "不一致意味着两只脚走的不是同一个坐标约定 —— 那会在报告里变成一个"
        "不存在的偏侧性结论。"
    )
