"""`device.footseries` —— 原始帧到 `FootSeries` 的数据桥（RAY-345）。

坐标系重排是这里唯一「有物理对错」的地方（《BS-BT91 硬件适配》：必须写成显式常量
并加单元测试），所以重排的单测占大头；其余断言只钉住桥接不丢数据、不造空值。
"""

import numpy as np
import pytest
from wt901.protocol.units import accel_to_m_s2, angular_velocity_to_rad_s

from gait.contracts import FootLabel, FootSeries, Quality, RawFrame
from gait.device.footseries import (
    FootSeriesError,
    frames_to_foot_series,
    reorder_module_to_foot,
)


def _frame(acc=(0, 0, 0), gyr=(0, 0, 0), t=0.0, saturated=False) -> RawFrame:
    return RawFrame(
        t_host=t,
        acc_raw=np.asarray(acc, dtype=np.int16),
        gyr_raw=np.asarray(gyr, dtype=np.int16),
        ang_raw=np.asarray((0, 0, 0), dtype=np.int16),
        saturated=saturated,
    )


def test_left_foot_reorder_swaps_x_and_y():
    acc = np.array([[1.0, 2.0, 3.0]])
    gyr = np.array([[4.0, 5.0, 6.0]])
    foot_acc, foot_gyr = reorder_module_to_foot(acc, gyr, "L")
    # foot = [module_y, +module_x, module_z]
    assert np.array_equal(foot_acc[0], [2.0, 1.0, 3.0])
    assert np.array_equal(foot_gyr[0], [5.0, 4.0, 6.0])


def test_right_foot_reorder_flips_outward_axis():
    acc = np.array([[1.0, 2.0, 3.0]])
    gyr = np.array([[4.0, 5.0, 6.0]])
    foot_acc, foot_gyr = reorder_module_to_foot(acc, gyr, "R")
    # foot = [module_y, −module_x, module_z]
    assert np.array_equal(foot_acc[0], [2.0, -1.0, 3.0])
    assert np.array_equal(foot_gyr[0], [5.0, -4.0, 6.0])


def test_reorder_rejects_unknown_label():
    acc = np.zeros((1, 3))
    with pytest.raises(FootSeriesError):
        reorder_module_to_foot(acc, acc, "X")


def test_si_conversion_and_reorder_are_applied():
    # 模块 X=左=+1g（2048 计数），Y、Z 为 0。左足外侧=左，于是足部 Y 应为 +g。
    frames = [_frame(acc=(2048, 0, 0))]
    series = frames_to_foot_series(frames, "L")
    assert series.acc[0, 1] == pytest.approx(accel_to_m_s2(2048), abs=1e-6)
    assert series.acc[0, 0] == pytest.approx(0.0, abs=1e-9)


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
    # 右足系里 gyro 的 Y 分量 = ±module_x；左足取 +module_x。
    expected = angular_velocity_to_rad_s(3277)
    assert series.gyr[0, 1] == pytest.approx(expected, rel=1e-6)
