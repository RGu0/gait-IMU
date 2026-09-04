"""`device.footseries` 的标定路径（RAY-360 `raw-to-series`）。

MVP 桥（RAY-345）的测试在 `test_footseries_bridge.py`，这里只测它没做的三件事：
真实时基、空洞切段、标定补偿；外加两条守卫（安装矩阵必须是真旋转、出厂标定必填）。

**每一条都写成能失败的判据**：单位换算钉绝对物理值而不是「跑通了」，时基钉实测
采样率与标称值**不同**，空洞钉段数与段边界。
"""

import asyncio
import math
import struct
from pathlib import Path

import numpy as np
import pytest
from wt901.recording import RecordedChunk, Recording, write_recording

from gait.calib.still import StillCalibration
from gait.calib.walk import MountingCalibration
from gait.contracts import Quality, RawFrame
from gait.core.alignment import Alignment
from gait.device.capture import replay_raw_frames
from gait.device.footseries import (
    NO_ACCEL_CALIBRATION,
    FootSeriesError,
    calibrated_foot_series,
    read_recorded_frames,
)

#: 满量程：加速度 ±16 g、角速度 ±2000 °/s，int16 满刻度 32768（wt901 协议 §3.1）。
COUNTS_PER_G = 32768 // 16          # 2048
COUNTS_PER_1000_DPS = 32768 // 2    # 16384


def _frame_bytes(counts: tuple[int, ...]) -> bytes:
    """一帧 0x55 0x61 运动数据。"""
    return b"\x55\x61" + struct.pack("<9h", *counts)


def _raw(acc=(0, 0, 0), gyr=(0, 0, 0), t=0.0, saturated=False) -> RawFrame:
    return RawFrame(
        t_host=t,
        acc_raw=np.asarray(acc, dtype=np.int16),
        gyr_raw=np.asarray(gyr, dtype=np.int16),
        ang_raw=np.asarray((0, 0, 0), dtype=np.int16),
        saturated=saturated,
    )


def _still(gyro_bias=(0.0, 0.0, 0.0)) -> StillCalibration:
    """一份零偏可控的静立标定。**直接构造**而不是跑 `calibrate_still`：
    这里要的是「零偏恰好是这个数」，跑一遍标定只会引入它自己的估计误差。"""
    return StillCalibration(
        foot="L",
        gravity_direction=np.array([0.0, 0.0, 1.0]),
        gyro_bias=np.asarray(gyro_bias, dtype=np.float64),
        samples=1000,
        seconds=5.0,
        alignment=Alignment(
            q=np.array([1.0, 0.0, 0.0, 0.0]),
            roll=0.0,
            pitch=0.0,
            samples=1000,
            window=(0, 1000),
            gravity_residual=0.0,
            tilt_sigma=0.0,
        ),
    )


def _mounting(rotation: np.ndarray | None = None) -> MountingCalibration:
    rotation = np.eye(3) if rotation is None else rotation
    return MountingCalibration(
        foot="L",
        rotation=np.asarray(rotation, dtype=np.float64),
        q=np.array([1.0, 0.0, 0.0, 0.0]),
        principal_ratio=10.0,
        peak_asymmetry=1.4,
        samples=1000,
    )


def _series(frames, **kwargs):
    kwargs.setdefault("still", _still())
    kwargs.setdefault("mounting", _mounting())
    kwargs.setdefault("accel_calibration", NO_ACCEL_CALIBRATION)
    return calibrated_foot_series(frames, "L", **kwargs)


def _uniform(n: int, fs: float, acc=(0, 0, 0), gyr=(0, 0, 0)) -> list[RawFrame]:
    return [_raw(acc=acc, gyr=gyr, t=index / fs) for index in range(n)]


# ── 单位换算：钉绝对物理值 ────────────────────────────────────────────────


def test_unit_conversion_lands_on_known_physical_values():
    """已知刻度进去，已知物理量出来。

    这条判据能失败：把加速度与角速度的换算搞混、量程常数写错、少乘一次 π/180，
    三者都会让下面某个数对不上。只断言「跑通了」的测试对这三种错误全都免疫。
    """
    frames = _uniform(
        n=800,
        fs=200.0,
        acc=(0, 0, COUNTS_PER_G),           # 模块 Z 轴 +1 g
        gyr=(COUNTS_PER_1000_DPS, 0, 0),    # 模块 X 轴 +1000 °/s
    )
    series, _ = _series(frames)

    assert series.acc[0, 2] == pytest.approx(9.80665, rel=1e-9)
    assert series.acc[0, 0] == pytest.approx(0.0, abs=1e-12)
    assert series.gyr[0, 0] == pytest.approx(math.radians(1000.0), rel=1e-9)
    assert series.gyr[0, 1] == pytest.approx(0.0, abs=1e-12)


def test_gyro_bias_is_subtracted_in_module_frame():
    bias = np.array([0.01, -0.02, 0.03])
    frames = _uniform(n=800, fs=200.0, gyr=(0, 0, 0))
    series, _ = _series(frames, still=_still(gyro_bias=bias))
    assert series.gyr[0] == pytest.approx(-bias)


def test_accel_calibration_is_applied():
    class Doubling:
        name = "test-double"

        def apply(self, acc):
            return acc * 2.0

    frames = _uniform(n=800, fs=200.0, acc=(0, 0, COUNTS_PER_G))
    series, _ = _series(frames, accel_calibration=Doubling())
    assert series.acc[0, 2] == pytest.approx(2 * 9.80665, rel=1e-9)


def test_accel_calibration_has_no_default():
    """不传就报错 —— 「这份数据标没标定」不能靠默认值回答。"""
    frames = _uniform(n=800, fs=200.0)
    with pytest.raises(TypeError, match="accel_calibration"):
        calibrated_foot_series(frames, "L", still=_still(), mounting=_mounting())


# ── 时基：实测，不是标称 ──────────────────────────────────────────────────


def test_fs_comes_from_arrival_times_not_the_nominal_value():
    """器件晶振偏 2.5% 时，`fs` 必须跟着偏 —— 否则整场会话的时间参数系统性错位。

    这正是 MVP 桥用 `t[k] = k / 200` 时量不出来的东西：那条路径下 `fs` 恒为 200，
    本断言在它上面永远为假。
    """
    true_fs = 195.0
    frames = _uniform(n=2000, fs=true_fs)
    series, timebase = _series(frames)

    assert series.fs == pytest.approx(true_fs, rel=1e-6)
    assert series.fs != pytest.approx(200.0, rel=1e-3)
    assert timebase.report.nominal_fs == pytest.approx(200.0)
    assert series.t[-1] == pytest.approx(series.t[0] + (len(frames) - 1) / true_fs, rel=1e-6)


# ── 空洞：切段并标边界 ────────────────────────────────────────────────────


def test_gap_splits_segments_and_marks_edges():
    fs = 200.0
    before = _uniform(n=1000, fs=fs)
    # 掉了 0.5 s（100 个样本）之后继续。
    resume = 999 / fs + 0.5
    after = [_raw(t=resume + index / fs) for index in range(1000)]
    series, _ = _series(before + after)

    assert len(series.segments) == 2, "空洞两侧必须切成两段"
    assert series.segments[0] == (0, 1000)
    assert series.segments[1] == (1000, 2000)
    assert series.quality[999] & Quality.GAP_EDGE
    assert series.quality[1000] & Quality.GAP_EDGE
    assert not series.quality[500] & Quality.GAP_EDGE


def test_clean_stream_is_one_segment():
    series, _ = _series(_uniform(n=2000, fs=200.0))
    assert series.segments == [(0, 2000)]
    assert not series.quality.any()


# ── 守卫：安装矩阵必须是真旋转 ────────────────────────────────────────────


def test_mirrored_mounting_is_rejected():
    """det = −1 的映射会让角速度整体反号、步长静默偏低约 18%（RAY-390）。

    拿的正是 MVP 桥在**左足**上实际使用的那个矩阵 —— 它不是假想的坏输入。
    """
    mirror = np.array([[0.0, 1.0, 0.0], [1.0, 0.0, 0.0], [0.0, 0.0, 1.0]])
    assert np.linalg.det(mirror) == pytest.approx(-1.0)
    with pytest.raises(FootSeriesError, match="行列式"):
        _series(_uniform(n=800, fs=200.0), mounting=_mounting(mirror))


def test_proper_rotation_is_accepted():
    proper = np.array([[0.0, 1.0, 0.0], [-1.0, 0.0, 0.0], [0.0, 0.0, 1.0]])
    assert np.linalg.det(proper) == pytest.approx(1.0)
    series, _ = _series(_uniform(n=800, fs=200.0), mounting=_mounting(proper))
    assert series.acc.shape == (800, 3)


# ── 到达时刻确实来自录制文件 ──────────────────────────────────────────────


def _write_recording(path: Path, times: list[float]) -> None:
    chunks = [
        RecordedChunk(t=t, data=_frame_bytes((0, 0, COUNTS_PER_G, 0, 0, 0, 0, 0, 0)))
        for t in times
    ]
    write_recording(path, Recording(device_id="dev-L", created_utc="", note="", chunks=tuple(chunks)))


def test_read_recorded_frames_keeps_the_original_arrival_times(tmp_path):
    """本 scope 的支点：真实到达时刻一直在盘上，丢掉它的是回放路径。

    同一份文件，两条路：本函数拿回原始时刻，`replay_raw_frames` 拿到的是回放时刻。
    """
    times = [round(index / 200.0, 6) for index in range(20)]
    path = tmp_path / "L.jsonl"
    _write_recording(path, times)

    frames = read_recorded_frames(path)
    assert [frame.t_host for frame in frames] == pytest.approx(times)

    async def _replayed():
        return [frame async for frame in replay_raw_frames(path)]

    replayed = asyncio.run(_replayed())
    assert len(replayed) == len(frames)
    # 载荷一致，时刻不一致 —— 后者正是本 scope 绕开 ReplayTransport 的理由。
    assert np.array_equal(replayed[0].acc_raw, frames[0].acc_raw)
    assert [frame.t_host for frame in replayed] != pytest.approx(times)


def test_recording_gap_survives_the_read(tmp_path):
    """录制里的空洞，读回来之后仍然是空洞 —— 不被均匀时轴抹平。"""
    times = [round(index / 200.0, 6) for index in range(1000)]
    times += [round(times[-1] + 0.5 + index / 200.0, 6) for index in range(1000)]
    path = tmp_path / "L.jsonl"
    _write_recording(path, times)

    series, _ = _series(read_recorded_frames(path))
    assert len(series.segments) == 2
