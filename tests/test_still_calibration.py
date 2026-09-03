"""RAY-208 `still-calibration`：静立 5 s 的会话标定。

本 Issue 唯一一条分明的量化验收是「**松动注入测试 100% 检出**」（v1.4 D1 修订后：
戴反一项已删除，不由算法承担）。这份测试的主体就是那条 —— 而且是**双向**的：
既要求超限的全被检出，也要求未超限的全不误报。只验一边等于把阈值调到 0 也能通过。
"""

from __future__ import annotations

import numpy as np
import pytest

from gait.calib import (
    CalibrationError,
    calibrate_still,
    check_looseness,
    verdict,
)

FS = 200.0
SECONDS = 6.0
GRAVITY = np.array([0.0, 0.0, 9.80665])


def still(seed: int = 7, *, seconds: float = SECONDS, bias: float = 0.002, noise: float = 0.02):
    """一段静立样本。噪声量级取自真实静止段，不是零噪声理想数据。"""
    rng = np.random.default_rng(seed)
    n = int(seconds * FS)
    acc = GRAVITY + rng.normal(0, noise, (n, 3))
    gyr = rng.normal(0, 0.005, (n, 3)) + bias
    return acc, gyr


def rotated(acc: np.ndarray, degrees: float, axis: int = 0) -> np.ndarray:
    """把比力绕某轴转一个已知角度 —— 模块松动之后重力方向就是这么变的。"""
    angle = np.radians(degrees)
    cos, sin = np.cos(angle), np.sin(angle)
    matrices = {
        0: np.array([[1, 0, 0], [0, cos, -sin], [0, sin, cos]]),
        1: np.array([[cos, 0, sin], [0, 1, 0], [-sin, 0, cos]]),
    }
    return acc @ matrices[axis].T


def reference(**kwargs):
    acc, gyr = still(**kwargs)
    return calibrate_still("L", acc, gyr, FS), acc


# ── 验收：松动注入 100% 检出，且不误报 ────────────────────────────────────


@pytest.mark.parametrize("degrees", [5.5, 6, 7, 8, 10, 12, 15, 20, 30, 45])
@pytest.mark.parametrize("axis", [0, 1])
def test_every_injected_looseness_beyond_the_limit_is_caught(degrees: float, axis: int) -> None:
    """超过 5° 的注入必须**全部**检出。这是本 Issue 的验收原文。"""
    calibration, acc = reference()
    check = check_looseness(calibration, rotated(acc, degrees, axis))
    assert check.loose, f"{degrees}° 绕轴 {axis} 未被检出（量到 {check.deviation_deg:.2f}°）"


@pytest.mark.parametrize("degrees", [0.0, 0.5, 1.0, 2.0, 3.0, 4.0, 4.5])
@pytest.mark.parametrize("axis", [0, 1])
def test_nothing_below_the_limit_is_reported_as_loose(degrees: float, axis: int) -> None:
    """未超限的**全不**误报。

    只验「超限的检出」是不够的：把阈值调到 0 也能让那一半全绿，而那样的检测器会
    把每一次正常佩戴都判成松动 —— 比不检测更糟（RAY-260 的教训就是这个形状）。
    """
    calibration, acc = reference()
    check = check_looseness(calibration, rotated(acc, degrees, axis))
    assert not check.loose, f"{degrees}° 被误报为松动（量到 {check.deviation_deg:.2f}°）"


def test_the_measured_deviation_matches_the_injected_angle() -> None:
    """量出来的角度要**对**，不只是过阈值。

    没有这条，一个恒返回 90° 的实现也能通过上面两组 —— 它会「检出」一切。
    """
    calibration, acc = reference()
    for degrees in (2.0, 7.0, 13.0):
        measured = check_looseness(calibration, rotated(acc, degrees)).deviation_deg
        assert abs(measured - degrees) < 0.5, f"注入 {degrees}°，量到 {measured:.2f}°"


def test_the_deviation_is_reported_even_when_it_passes() -> None:
    """「过了但偏了 4.8°」与「过了且偏了 0.2°」不是一回事。"""
    calibration, acc = reference()
    close = check_looseness(calibration, rotated(acc, 4.5))
    assert not close.loose
    assert close.deviation_deg > 4.0


# ── 陀螺零偏 ──────────────────────────────────────────────────────────────


def test_the_gyro_bias_is_measured_not_assumed() -> None:
    """静止时陀螺的输出就是零偏本身。"""
    injected = 0.012
    acc, gyr = still(bias=injected)
    calibration = calibrate_still("L", acc, gyr, FS)
    assert np.allclose(calibration.gyro_bias, injected, atol=0.002)


def test_there_is_no_second_stillness_criterion_here() -> None:
    """本模块**不**自己判「零偏大不大」。

    第一版写了一个 `MAX_GYRO_BIAS_RAD_S` 上限。写完去触发它才发现够不着：
    `find_still_window` 用 `core/zupt.detect_stance` 选窗口，而那个检测器在每轴零偏
    约 0.02 rad/s 就已经判「这段不静止」并抛错 —— 比任何我会设的上限都严。

    那条检查因此是一段**看起来在保护什么**的死代码，而且违反了 `find_still_window`
    文档里的原话：「同一件事有两处判据时，它们迟早对不上」。

    这条测试把这个事实钉住：零偏偏大时，拒绝来自**窗口选取**，而不是来自本模块的
    某个上限。若将来有人在这里重新加一道零偏闸，这条会提醒他先看这段。
    """
    from gait.core.alignment import AlignmentError

    acc, gyr = still(bias=0.05)
    with pytest.raises(AlignmentError, match="静止段"):
        calibrate_still("L", acc, gyr, FS)


def test_a_realistic_bias_still_calibrates_fine() -> None:
    """真实的 wt901 静态零偏是毫弧度量级，必须正常通过。

    没有这条，上面那条可以靠「把一切都拒绝掉」来满足。
    """
    acc, gyr = still(bias=0.003)
    calibration = calibrate_still("L", acc, gyr, FS)
    assert calibration.gyro_bias_magnitude < 0.02
    assert verdict({"L": calibration, "R": calibrate_still("R", *still(seed=9)[:2], FS)}).passed


# ── 判定与措辞 ────────────────────────────────────────────────────────────


def test_both_feet_are_required() -> None:
    """少一只不是「那只脚没问题」，是这次标定没覆盖它。

    与 `orchestration.preflight_battery` 同一口径，不另立一套。
    """
    acc, gyr = still()
    with pytest.raises(CalibrationError, match="没覆盖它"):
        verdict({"L": calibrate_still("L", acc, gyr, FS)})


def test_failure_reasons_speak_in_actions_not_algorithms() -> None:
    """PRD §6.1：失败提示用动作语言。操作员对「安装角不收敛」无事可做。"""
    calibration, acc = reference()
    loose = check_looseness(calibration, rotated(acc, 12.0))
    result = verdict(
        {"L": calibration, "R": calibrate_still("R", *still(seed=9)[:2], FS)},
        looseness={"L": loose},
    )
    assert not result.passed
    joined = " ".join(result.reasons)
    assert "绑紧" in joined
    for jargon in ("安装角", "收敛", "零偏", "四元数", "协方差"):
        assert jargon not in joined


def test_a_clean_calibration_passes_with_no_reasons() -> None:
    calibrations = {
        "L": calibrate_still("L", *still(seed=1)[:2], FS),
        "R": calibrate_still("R", *still(seed=2)[:2], FS),
    }
    result = verdict(calibrations)
    assert result.passed
    assert result.reasons == ()


# ── 输入边界 ──────────────────────────────────────────────────────────────


def test_a_short_still_segment_is_refused() -> None:
    """样本不够时噪声压不下去，零偏会把噪声当成偏置带进整场会话。"""
    acc, gyr = still(seconds=2.0)
    with pytest.raises(CalibrationError, match="不足"):
        calibrate_still("L", acc, gyr, FS)


def test_mismatched_shapes_are_refused() -> None:
    acc, gyr = still()
    with pytest.raises(CalibrationError, match="等长"):
        calibrate_still("L", acc, gyr[:-10], FS)


def test_the_snapshot_carries_the_basis_for_later_comparison() -> None:
    """它进 `SessionMeta.calib_snapshot`，而松动检测靠它 —— 存的必须是方向向量。"""
    calibration, _ = reference()
    snapshot = calibration.snapshot()
    assert len(snapshot["gravity_direction"]) == 3
    assert abs(np.linalg.norm(snapshot["gravity_direction"]) - 1.0) < 1e-9
    assert "gyro_bias" in snapshot and "tilt_sigma" in snapshot
