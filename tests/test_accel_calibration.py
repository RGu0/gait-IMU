"""加计六面法标定的测试。

判据是**往返**：注入一组已知的器件误差（标度、交叉轴、零偏），让六面法去解，断言解
出来的正是注入的那组。这比「跑通了」强 —— 它要求估计**对**，而不只是存在。

`walk-calibration` 的交付记录里写过同一条理由，这里沿用：只验「能解出东西」的话，
一个恒返回单位阵的实现也能通过。

多处断言成对出现（拒绝坏输入 / 接受好输入）。只有前者时，把判据调到「拒绝一切」就能
全绿，而那样的标定器比没有标定更糟 —— 它会把每一次正常的采集都判成失败。
"""

import numpy as np
import pytest

from gait.calib.accel import (
    FACES,
    MAX_CONDITION_NUMBER,
    MILLI_G,
    MIN_SAMPLES_PER_FACE,
    STANDARD_GRAVITY,
    AccelCalibration,
    FaceObservation,
    identify_face,
    observe_face,
    solve_six_face,
)
from gait.calib.still import CalibrationError

SAMPLES = MIN_SAMPLES_PER_FACE * 2


def face_vector(face: str) -> np.ndarray:
    axis = "XYZ".index(face[1])
    vector = np.zeros(3)
    vector[axis] = 1.0 if face[0] == "+" else -1.0
    return vector


def synth_face(
    face: str,
    *,
    matrix: np.ndarray,
    bias: np.ndarray,
    noise: float = 0.0,
    tilt_deg: float = 0.0,
    samples: int = SAMPLES,
    seed: int = 0,
) -> np.ndarray:
    """造一面的静置样本：`a_meas = M · (g·u) + b + 噪声`。

    `tilt_deg` 把真值方向绕一根正交轴偏一点，模拟桌面不平。
    """
    up = face_vector(face)
    if tilt_deg:
        axis = int(np.argmax(np.abs(up)))
        other = (axis + 1) % 3
        angle = np.radians(tilt_deg)
        tilted = up * np.cos(angle)
        tilted[other] += np.sin(angle) * (1.0 if up[axis] > 0 else -1.0)
        up = tilted / np.linalg.norm(tilted)
    truth = up * STANDARD_GRAVITY
    measured = matrix @ truth + bias
    rng = np.random.default_rng(seed + hash(face) % 1000)
    return measured + rng.normal(0.0, noise, size=(samples, 3))


def observe_all(**kwargs) -> list[FaceObservation]:
    return [observe_face(synth_face(face, **kwargs)) for face in FACES]


#: 一组有代表性的器件误差：对角是千分之几的标度误差，非对角是交叉轴不对准，
#: 零偏三轴分别 30 / −20 / 25 mg —— 正好落在规格书说的 ±20~40 mg 区间。
TRUE_MATRIX = np.array(
    [
        [1.004, 0.003, -0.002],
        [0.002, 0.997, 0.004],
        [-0.003, 0.001, 1.006],
    ]
)
TRUE_BIAS = np.array([30.0, -20.0, 25.0]) * MILLI_G


def test_round_trip_recovers_the_injected_matrix_and_bias():
    """注入已知误差 → 解 → 断言解出来的就是注入的那组。本文件的主判据。"""
    calibration = solve_six_face("AA:BB", observe_all(matrix=TRUE_MATRIX, bias=TRUE_BIAS))

    # 改正矩阵应当是器件矩阵的逆。
    np.testing.assert_allclose(calibration.matrix, np.linalg.inv(TRUE_MATRIX), atol=1e-9)
    # 还原出来的器件零偏应当就是注入的那个。
    np.testing.assert_allclose(calibration.bias, TRUE_BIAS, atol=1e-9)
    np.testing.assert_allclose(calibration.bias_mg, [30.0, -20.0, 25.0], atol=1e-6)


def test_apply_removes_the_error_it_was_fitted_for():
    """成对断言：改正**前**误差在规格书量级，改正**后**落到验收量级。

    只断言「改正后很小」是不够的 —— 一个恒返回零的 `apply` 也能满足它。
    """
    calibration = solve_six_face("AA:BB", observe_all(matrix=TRUE_MATRIX, bias=TRUE_BIAS))

    truth = np.array([0.3, -0.5, 9.2])
    measured = TRUE_MATRIX @ truth + TRUE_BIAS

    before_mg = np.linalg.norm(measured - truth) / MILLI_G
    after_mg = np.linalg.norm(calibration.apply(measured) - truth) / MILLI_G

    assert before_mg > 20.0, "注入的误差本来就没到规格书量级，这个对比说明不了什么"
    assert after_mg < 1.0
    assert after_mg < before_mg / 20.0


def test_residual_reaches_the_acceptance_target_under_realistic_noise():
    """验收口径：标定后残差落在 2~5 mg。噪声取器件量级。"""
    calibration = solve_six_face(
        "AA:BB", observe_all(matrix=TRUE_MATRIX, bias=TRUE_BIAS, noise=0.02, seed=7)
    )
    assert calibration.residual_mg < 5.0


def test_tilt_of_a_few_degrees_barely_moves_the_bias():
    """模块文档说倾斜对零偏是一阶抵消的。这条把那句话钉成判据。"""
    level = solve_six_face("AA:BB", observe_all(matrix=TRUE_MATRIX, bias=TRUE_BIAS))
    tilted = solve_six_face(
        "AA:BB", observe_all(matrix=TRUE_MATRIX, bias=TRUE_BIAS, tilt_deg=3.0)
    )
    shift_mg = np.linalg.norm(tilted.bias - level.bias) / MILLI_G
    assert shift_mg < 5.0, f"3° 倾斜把零偏推了 {shift_mg:.1f} mg，与一阶抵消的说法矛盾"


def test_offset_is_not_the_device_bias():
    """`offset` 与 `bias` 是两个不同的量，模块文档专门写了不可混用。

    没有这条，有人会把 `bias` 「简化」成直接返回 `offset` —— 数值接近、量纲也对，
    只是错的，而错法是安静的。
    """
    calibration = solve_six_face("AA:BB", observe_all(matrix=TRUE_MATRIX, bias=TRUE_BIAS))
    assert not np.allclose(calibration.offset, calibration.bias, atol=1e-6)
    # 关系是 c = −A·b。
    np.testing.assert_allclose(
        calibration.offset, -calibration.matrix @ calibration.bias, atol=1e-9
    )


@pytest.mark.parametrize("face", FACES)
def test_identify_face_reads_the_face_off_the_data(face):
    mean = face_vector(face) * STANDARD_GRAVITY
    assert identify_face(mean) == face


def test_a_missing_face_is_refused():
    observations = observe_all(matrix=TRUE_MATRIX, bias=TRUE_BIAS)
    with pytest.raises(CalibrationError, match="缺少这些面"):
        solve_six_face("AA:BB", observations[:-1])


def test_a_repeated_face_is_refused_when_a_seventh_observation_duplicates_one():
    """操作员重采了一个面却没丢掉第一段 —— 七段里有一个面出现两次。

    **必须是第七段**：恰好六段时「六个面都在」按鸽笼原理就蕴含「没有重复」，缺面那
    道闸会先拦下，重复这道闸够不着。它不是死代码，但只在段数多于六时才可达，测试要
    照着可达的那条路走 —— 否则这条测试验的其实是缺面检查，而重复检查一行没跑到。
    """
    observations = observe_all(matrix=TRUE_MATRIX, bias=TRUE_BIAS)
    observations.append(observations[0])
    with pytest.raises(CalibrationError, match="采了不止一次"):
        solve_six_face("AA:BB", observations)


def test_a_replaced_face_is_reported_as_missing_not_as_duplicate():
    """把第六段换成第一段的重复：缺面先报。钉住两道闸的先后，免得有人调换顺序后
    以为「重复」那条仍在起作用。"""
    observations = observe_all(matrix=TRUE_MATRIX, bias=TRUE_BIAS)
    observations[-1] = observations[0]
    with pytest.raises(CalibrationError, match="缺少这些面"):
        solve_six_face("AA:BB", observations)


def test_six_distinct_faces_are_accepted():
    """上面两条的反面。没有它，把 `solve_six_face` 写成「一律拒绝」也能全绿。"""
    calibration = solve_six_face("AA:BB", observe_all(matrix=TRUE_MATRIX, bias=TRUE_BIAS))
    assert calibration.condition_number < MAX_CONDITION_NUMBER
    assert len(calibration.faces) == 6


def test_a_face_that_was_not_still_is_refused():
    rng = np.random.default_rng(1)
    shaky = synth_face("+Z", matrix=TRUE_MATRIX, bias=TRUE_BIAS) + rng.normal(
        0.0, 0.5, size=(SAMPLES, 3)
    )
    with pytest.raises(CalibrationError, match="没有静置好"):
        observe_face(shaky)


def test_a_still_face_is_accepted():
    """上一条的反面：把静止判据调到 0 就能让上一条通过，这条不让。"""
    observation = observe_face(
        synth_face("+Z", matrix=TRUE_MATRIX, bias=TRUE_BIAS, noise=0.01, seed=3)
    )
    assert observation.face == "+Z"
    assert observation.samples == SAMPLES


def test_too_few_samples_is_refused():
    short = synth_face("+Z", matrix=TRUE_MATRIX, bias=TRUE_BIAS, samples=10)
    with pytest.raises(CalibrationError, match="样本"):
        observe_face(short)


def test_a_module_standing_on_an_edge_is_refused():
    """立在棱上：两轴各分到约 0.7 g，不落在任何一个面上。"""
    edge = np.tile(
        np.array([0.0, 1.0, 1.0]) / np.sqrt(2) * STANDARD_GRAVITY, (SAMPLES, 1)
    )
    with pytest.raises(CalibrationError, match="倾斜"):
        observe_face(edge)


def test_a_segment_that_is_not_gravity_is_refused():
    weak = np.tile(np.array([0.0, 0.0, 4.0]), (SAMPLES, 1))
    with pytest.raises(CalibrationError, match="偏离 1 g"):
        observe_face(weak)


def test_an_ill_conditioned_fit_is_refused():
    """六个不同的标签，但方向几乎共面 —— 条件数会爆。

    直接构造 `FaceObservation`（绕过 `observe_face` 的摆放检查），因为要验的正是
    `solve_six_face` 自己那道闸；两道闸各守各的。
    """
    observations = []
    for index, face in enumerate(FACES):
        mean = np.array([STANDARD_GRAVITY, 0.001 * index, 0.001])
        observations.append(
            FaceObservation(face=face, mean=mean, std=np.zeros(3), samples=SAMPLES)
        )
    with pytest.raises(CalibrationError, match="病态"):
        solve_six_face("AA:BB", observations)


def test_snapshot_carries_what_a_later_review_needs():
    calibration = solve_six_face("AA:BB", observe_all(matrix=TRUE_MATRIX, bias=TRUE_BIAS))
    snapshot = calibration.snapshot()

    assert snapshot["device"] == "AA:BB"
    assert len(snapshot["faces"]) == 6
    # 快照必须能还原出 apply()，否则它不足以复核一份历史报告。
    restored = AccelCalibration(
        device=snapshot["device"],
        matrix=np.array(snapshot["matrix"]),
        offset=np.array(snapshot["offset"]),
        residual_mg=snapshot["residual_mg"],
        condition_number=snapshot["condition_number"],
        faces=calibration.faces,
    )
    probe = np.array([0.1, 0.2, 9.7])
    np.testing.assert_allclose(restored.apply(probe), calibration.apply(probe), atol=1e-12)


def test_snapshot_is_json_serialisable():
    """它要进 `SessionMeta.calib_snapshot` 落盘。numpy 标量会让 json 当场抛错。"""
    import json

    calibration = solve_six_face("AA:BB", observe_all(matrix=TRUE_MATRIX, bias=TRUE_BIAS))
    json.dumps(calibration.snapshot())
