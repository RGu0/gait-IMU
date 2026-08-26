"""wt901 样本 → 契约 `RawFrame` 的适配与加速度饱和判定。RAY-195 R2。

这里最要紧的两条断言，都是关于**安静地做错事**的：

* 帧布局变了要当场炸，而不是把陀螺搬进姿态角字段；
* 负向触底与正向触顶一样算饱和 —— 只判正向会让足跟触地那一半削顶漏掉。
"""

import numpy as np
import pytest
from wt901 import Euler, ImuSample, RawImuCounts, Vec3

from gait.contracts import ContractError, Quality
from gait.device.adapter import (
    ACCEL_AXES,
    COUNTS_PER_FRAME,
    INT16_MAX,
    INT16_MIN,
    accel_saturated,
    to_raw_frame,
)

#: 一帧里九个互不相同的计数，好让"哪个值跑到哪个字段"的错误无所遁形。
#: 全用同一个数的话，切片错位是看不出来的。
DISTINCT = (11, 12, 13, 21, 22, 23, 31, 32, 33)


def make_sample(counts=DISTINCT, *, t_host: float = 1234.5) -> ImuSample:
    """只经 `RawImuCounts` 构造，不走 `from_frame` —— 本模块只读 `raw`。"""
    return ImuSample(
        device_id="L",
        t_host=t_host,
        seq=7,
        accel=Vec3(0.0, 0.0, 9.8),
        gyro=Vec3(0.0, 0.0, 0.0),
        euler=Euler(0.0, 0.0, 0.0),
        raw=RawImuCounts(values=tuple(counts)),
    )


class TestFieldMapping:
    def test_each_triplet_lands_in_its_own_field(self):
        """手册 §3.1 的顺序：ax ay az | wx wy wz | roll pitch yaw。"""
        frame = to_raw_frame(make_sample())
        assert list(frame.acc_raw) == [11, 12, 13]
        assert list(frame.gyr_raw) == [21, 22, 23]
        assert list(frame.ang_raw) == [31, 32, 33]

    def test_arrays_are_int16_as_the_contract_requires(self):
        frame = to_raw_frame(make_sample())
        for array in (frame.acc_raw, frame.gyr_raw, frame.ang_raw):
            assert array.dtype == np.int16
            assert array.shape == (3,)

    def test_host_time_is_passed_through_untouched(self):
        """契约加粗写着它是主机接收时刻，本层不插值、不修正。"""
        assert to_raw_frame(make_sample(t_host=98.75)).t_host == 98.75

    def test_a_sequence_of_frames_stays_in_order_and_independent(self):
        """多帧：逐帧适配互不干扰，顺序与 t_host 原样保留。"""
        samples = [make_sample(t_host=float(i)) for i in range(5)]
        frames = [to_raw_frame(sample) for sample in samples]
        assert [frame.t_host for frame in frames] == [0.0, 1.0, 2.0, 3.0, 4.0]
        assert all(list(frame.acc_raw) == [11, 12, 13] for frame in frames)


class TestSaturation:
    def test_a_normal_frame_is_not_saturated(self):
        assert accel_saturated(DISTINCT) is False
        assert to_raw_frame(make_sample()).saturated is False

    @pytest.mark.parametrize("axis", [0, 1, 2])
    @pytest.mark.parametrize("limit", [INT16_MAX, INT16_MIN])
    def test_a_single_axis_at_either_limit_saturates(self, axis, limit):
        """单轴触顶。两端都测 —— 负向触底是足跟冲击的常见形态。"""
        counts = list(DISTINCT)
        counts[axis] = limit
        assert accel_saturated(counts) is True
        assert to_raw_frame(make_sample(counts)).saturated is True

    def test_all_three_axes_at_the_limit_saturate(self):
        counts = [INT16_MAX, INT16_MIN, INT16_MAX, *DISTINCT[3:]]
        assert accel_saturated(counts) is True

    def test_one_count_below_the_limit_does_not_saturate(self):
        """边界必须是闭区间上的那一个值，不是"接近"。"""
        counts = list(DISTINCT)
        counts[0] = INT16_MAX - 1
        assert accel_saturated(counts) is False
        counts[0] = INT16_MIN + 1
        assert accel_saturated(counts) is False

    @pytest.mark.parametrize("axis", [3, 4, 5, 6, 7, 8])
    def test_gyro_and_angle_at_the_limit_do_not_saturate(self, axis):
        """契约 `Quality.SATURATED` 只说加速度。撑大这一位的含义是有代价的 ——
        报告里只有一个标志，读的人分不清削的是加速度还是角速度。
        """
        counts = list(DISTINCT)
        counts[axis] = INT16_MAX
        assert accel_saturated(counts) is False
        assert to_raw_frame(make_sample(counts)).saturated is False

    def test_the_accel_slice_really_points_at_the_first_three(self):
        """`ACCEL_AXES` 是帧布局的唯一出处，它指错了上面所有断言都会一起错。"""
        assert list(DISTINCT[ACCEL_AXES]) == [11, 12, 13]

    def test_the_flag_matches_the_contract_bit_it_feeds(self):
        """下游把它写进 `FootSeries.quality` 的 SATURATED 位，语义必须是同一个。"""
        assert Quality.SATURATED == 1
        counts = list(DISTINCT)
        counts[0] = INT16_MAX
        assert to_raw_frame(make_sample(counts)).saturated is True


class TestFrameLayoutGuard:
    @pytest.mark.parametrize("length", [0, 6, 8, 10])
    def test_a_wrong_frame_length_is_refused_loudly(self, length):
        """长度对不上说明 wt901 改了帧布局。按原切片继续会把陀螺搬进姿态角 ——
        那是不报错、只是安静给出错误结果的一类失败。
        """
        sample = make_sample(tuple(range(length)))
        with pytest.raises(ContractError, match="int16 计数"):
            to_raw_frame(sample)

    def test_the_expected_length_is_nine(self):
        assert COUNTS_PER_FRAME == 9

    def test_an_out_of_range_count_raises_instead_of_wrapping(self):
        """没有显式扫描码值范围（见模块文档），靠 numpy 兜底 —— 它抛错而不是
        静默回绕。把这个行为钉住：若哪天它改成回绕，越界值会变成一个合法但错误
        的小数字。
        """
        counts = list(DISTINCT)
        counts[0] = INT16_MAX + 1
        with pytest.raises(OverflowError):
            to_raw_frame(make_sample(counts))
