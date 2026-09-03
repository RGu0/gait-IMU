"""RAY-208 `walk-calibration`：安装误差角与坐标系重排。

## 判据是往返，不是「跑通了」

合成器 `generate_walk` 产出的 `FootSeries` 本身就在足部系（它经姿态四元数构造）。
所以每条测试都是：**注入一个已知的安装旋转 → 恢复 → 断言还原回原始数据**。

这比断言「返回了一个旋转矩阵」强得多：它要求估计出来的旋转**对**。
"""

from __future__ import annotations

import numpy as np
import pytest

from gait.calib import (
    MIN_PEAK_ASYMMETRY,
    MIN_PRINCIPAL_RATIO,
    CalibrationError,
    calibrate_still,
    estimate_mounting,
)
from gait.validate.synthetic import WalkSpec, generate_walk


def rotation(roll: float, pitch: float, yaw: float) -> np.ndarray:
    """内在 ZYX 旋转，度。**足部系向量 @ R = 模块体系向量**（见各测试的注入方式）。"""
    r, p, y = np.radians([roll, pitch, yaw])
    cr, sr = np.cos(r), np.sin(r)
    cp, sp = np.cos(p), np.sin(p)
    cy, sy = np.cos(y), np.sin(y)
    return (
        np.array([[cy, -sy, 0], [sy, cy, 0], [0, 0, 1]])
        @ np.array([[cp, 0, sp], [0, 1, 0], [-sp, 0, cp]])
        @ np.array([[1, 0, 0], [0, cr, -sr], [0, sr, cr]])
    )


def scenario(roll: float = 0.0, pitch: float = 0.0, yaw: float = 0.0, **spec_kwargs):
    """一场行走，模块以 (roll,pitch,yaw) 歪着绑。返回真值与模块体系读数。"""
    defaults = {"duration_s": 14.0, "path_length_m": 1e6}
    defaults.update(spec_kwargs)
    series, _ = generate_walk(WalkSpec(**defaults))
    matrix = rotation(roll, pitch, yaw)
    return series, series.acc @ matrix, series.gyr @ matrix


def calibrated(acc_module: np.ndarray, gyr_module: np.ndarray, fs: float):
    """先做静立标定（它给向上方向），再解安装角。

    静立段用开头那一段重复铺满 —— 合成器的静止前导只有 1 s，而静立标定要 5 s。
    重复的是同一段真实静止样本，不是造出来的新数据。
    """
    lead = int(0.9 * fs)
    still_acc = np.repeat(acc_module[:lead], 8, axis=0)[: int(6 * fs)]
    still_gyr = np.repeat(gyr_module[:lead], 8, axis=0)[: int(6 * fs)]
    still = calibrate_still("L", still_acc, still_gyr, fs)
    start = int(2.5 * fs)
    return still, estimate_mounting(still, acc_module[start:], gyr_module[start:]), start


# ── 往返：注入已知角，恢复它 ──────────────────────────────────────────────


@pytest.mark.parametrize(
    ("roll", "pitch", "yaw"),
    [
        (0.0, 0.0, 0.0),
        (12.0, -7.0, 35.0),
        (-20.0, 15.0, -60.0),
        (5.0, 5.0, 120.0),
        (-30.0, -10.0, 170.0),
        (25.0, 20.0, -145.0),
    ],
)
def test_the_injected_mounting_rotation_is_recovered(roll: float, pitch: float, yaw: float) -> None:
    series, acc_module, gyr_module = scenario(roll, pitch, yaw)
    _, mounting, _ = calibrated(acc_module, gyr_module, series.fs)
    recovered = np.array(mounting.mounting_angles_deg)
    assert np.allclose(recovered, [roll, pitch, yaw], atol=1.0), (
        f"注入 {(roll, pitch, yaw)}，恢复 {tuple(np.round(recovered, 2))}"
    )


@pytest.mark.parametrize(("roll", "pitch", "yaw"), [(12.0, -7.0, 35.0), (-30.0, -10.0, 170.0)])
def test_applying_the_rotation_restores_the_original_foot_frame_data(
    roll: float, pitch: float, yaw: float
) -> None:
    """**这条才是最终判据**：角度对不对是中间量，数据还原得回来才是目的。"""
    series, acc_module, gyr_module = scenario(roll, pitch, yaw)
    _, mounting, start = calibrated(acc_module, gyr_module, series.fs)
    acc_foot, gyr_foot = mounting.apply(acc_module[start:], gyr_module[start:])
    assert np.abs(acc_foot - series.acc[start:]).max() < 1e-6
    assert np.abs(gyr_foot - series.gyr[start:]).max() < 1e-6


def test_the_rotation_is_a_proper_rotation() -> None:
    """正交且行列式为 +1。行列式为 −1 是镜像 —— 它会把左右悄悄换掉。"""
    series, acc_module, gyr_module = scenario(12.0, -7.0, 35.0)
    _, mounting, _ = calibrated(acc_module, gyr_module, series.fs)
    matrix = mounting.rotation
    assert np.allclose(matrix @ matrix.T, np.eye(3), atol=1e-9)
    assert abs(float(np.linalg.det(matrix)) - 1.0) < 1e-9


# ── 前后歧义：本模块唯一真正棘手的地方 ────────────────────────────────────


@pytest.mark.parametrize("yaw", [0.0, 90.0, 180.0, -90.0, 179.0, -179.0])
def test_forward_is_never_off_by_a_hundred_and_eighty_degrees(yaw: float) -> None:
    """主成分给的是轴不是方向；不消歧会有一半会话把前向装反。

    后果是步长与航向全错，**而且不会报错** —— 数据看起来完全正常，只是走反了。
    所以这条对整圈 yaw 都验一遍：任何一个角度上翻了 180°，还原就对不上。
    """
    series, acc_module, gyr_module = scenario(0.0, 0.0, yaw)
    _, mounting, start = calibrated(acc_module, gyr_module, series.fs)
    acc_foot, _ = mounting.apply(acc_module[start:], gyr_module[start:])
    forward_truth = series.acc[start:, 0]
    forward_recovered = acc_foot[:, 0]
    # 翻了 180° 的话，前向分量会整体反号 —— 相关系数会是 −1 而不是 +1。
    correlation = float(np.corrcoef(forward_truth, forward_recovered)[0, 1])
    assert correlation > 0.99, f"yaw={yaw}° 时前向相关系数 {correlation:.3f}"


def test_the_disambiguation_refuses_rather_than_guesses_when_it_cannot_tell() -> None:
    """峰值对称时**不猜**。

    猜错的代价是整场数据前后颠倒且不会报错 —— 那比拒绝严重得多。这条把角速度做成
    严格对称的（原数据与其反号拼接），确保判据认输而不是掷硬币。
    """
    series, acc_module, gyr_module = scenario(0.0, 0.0, 20.0)
    start = int(2.5 * series.fs)
    symmetric = np.concatenate([gyr_module[start:], -gyr_module[start:]])
    doubled_acc = np.concatenate([acc_module[start:], acc_module[start:]])
    still, _, _ = calibrated(acc_module, gyr_module, series.fs)
    with pytest.raises(CalibrationError, match="无法判定足部前向"):
        estimate_mounting(still, doubled_acc, symmetric)


def test_a_real_walk_clears_the_asymmetry_threshold_with_margin() -> None:
    """反向：正常行走必须**明显**过线。

    没有这条，上面那条可以靠把阈值调到很高来满足 —— 而那样每一次正常标定都会失败。
    """
    series, acc_module, gyr_module = scenario(10.0, 5.0, 40.0)
    _, mounting, _ = calibrated(acc_module, gyr_module, series.fs)
    assert mounting.peak_asymmetry > MIN_PEAK_ASYMMETRY * 1.2


# ── 拒绝不像直线行走的数据 ────────────────────────────────────────────────


def test_motion_without_a_dominant_direction_is_refused() -> None:
    """由噪声定出来的前向轴会让整场会话的步长与航向都错。"""
    series, acc_module, gyr_module = scenario(0.0, 0.0, 0.0)
    still, _, start = calibrated(acc_module, gyr_module, series.fs)
    rng = np.random.default_rng(3)
    isotropic = rng.normal(0, 1.0, acc_module[start:].shape) + acc_module[start:].mean(axis=0)
    with pytest.raises(CalibrationError, match="没有明确的主方向"):
        estimate_mounting(still, isotropic, gyr_module[start:])


def test_a_dominant_direction_is_not_treated_as_an_error() -> None:
    """横向运动恰好为零时主方向**极度主导** —— 那是最好的情况，不是错误。

    第一版把「次大特征值为 0」当错误抛了出去，逻辑正好反了：比值越大越好。
    合成数据的横向加速度精确为零，正好把它暴露出来。
    """
    series, acc_module, gyr_module = scenario(8.0, 0.0, 15.0)
    _, mounting, _ = calibrated(acc_module, gyr_module, series.fs)
    assert mounting.principal_ratio >= MIN_PRINCIPAL_RATIO


def test_too_few_samples_is_refused() -> None:
    series, acc_module, gyr_module = scenario()
    still, _, start = calibrated(acc_module, gyr_module, series.fs)
    with pytest.raises(CalibrationError, match="不足以解出主方向"):
        estimate_mounting(still, acc_module[start : start + 50], gyr_module[start : start + 50])


def test_mismatched_shapes_are_refused() -> None:
    series, acc_module, gyr_module = scenario()
    still, _, start = calibrated(acc_module, gyr_module, series.fs)
    with pytest.raises(CalibrationError, match="等长"):
        estimate_mounting(still, acc_module[start:], gyr_module[start:-10])


def test_the_snapshot_carries_the_rotation_itself_not_only_the_angles() -> None:
    """欧拉角在接近奇异时不唯一。存下来供计算用的必须是矩阵。"""
    series, acc_module, gyr_module = scenario(12.0, -7.0, 35.0)
    _, mounting, _ = calibrated(acc_module, gyr_module, series.fs)
    snapshot = mounting.snapshot()
    assert np.allclose(np.array(snapshot["rotation"]), mounting.rotation)
    assert len(snapshot["q"]) == 4
    assert "mounting_yaw_deg" in snapshot
