"""加计多姿态标定的测试（RAY-207 R2）。

判据是**往返**：注入一组已知的器件误差（标度、交叉轴、零偏），让模长法去解，断言解出
来的正是注入的那组。这比「跑通了」强 —— 它要求估计**对**，而不只是存在。

多处断言成对出现（拒绝坏输入 / 接受好输入）。只有前者时，把判据调到「拒绝一切」就能
全绿，而那样的标定器比没有标定更糟：它会把每一次正常采集都判成失败。

本文件还钉住两条 R2 换方法的**理由**，因为它们正是这个模块存在的形状：

* 摆放倾斜不影响结果（`test_tilt_does_not_move_the_answer`）—— 这是换掉六面法的原因；
* 姿态太少 / 太挤会被拒（`test_too_few_orientations_*`）—— 恰定时残差恒为 0，
  那个 0 不能证伪任何东西。
"""

import numpy as np
import pytest

from gait.calib.accel import (
    DEFAULT_SIGMA_MG,
    MAX_CONDITION_NUMBER,
    MILLI_G,
    MIN_ORIENTATIONS,
    MIN_SAMPLES_PER_ORIENTATION,
    PARAMETER_COUNT,
    STANDARD_GRAVITY,
    TARGET_BIAS_SIGMA_MG,
    TARGET_CROSS_SIGMA_PPT,
    AccelCalibration,
    OrientationObservation,
    observability,
    observe_orientation,
    solve_orientations,
)
from gait.calib.still import CalibrationError

SAMPLES = MIN_SAMPLES_PER_ORIENTATION * 2

#: 一组有代表性的器件误差。**对称**：模长判据看不见反对称部分（模块文档写明了这一点），
#: 拿一个带反对称部分的真值来做往返，会把「看不见」误报成「解错了」。
TRUE_MATRIX = np.array(
    [
        [1.004, 0.003, -0.002],
        [0.003, 0.997, 0.004],
        [-0.002, 0.004, 1.006],
    ]
)
#: 三轴零偏 30 / −20 / 25 mg，正好落在规格书说的 ±20~40 mg 区间。
TRUE_BIAS = np.array([30.0, -20.0, 25.0]) * MILLI_G

#: 器件式：`a_meas = M · a_true + b`，其中 `M = A⁻¹`。
DEVICE_MATRIX = np.linalg.inv(TRUE_MATRIX)


def spread_directions(count: int, seed: int = 2) -> list[np.ndarray]:
    """`count` 个方向分散的单位向量。"""
    rng = np.random.default_rng(seed)
    out: list[np.ndarray] = []
    while len(out) < count:
        vector = rng.normal(size=3)
        norm = np.linalg.norm(vector)
        if norm > 1e-6:
            out.append(vector / norm)
    return out


def synth(
    direction: np.ndarray,
    *,
    matrix: np.ndarray = DEVICE_MATRIX,
    bias: np.ndarray = TRUE_BIAS,
    noise: float = 0.0,
    tilt_deg: float = 0.0,
    samples: int = SAMPLES,
    seed: int = 0,
) -> np.ndarray:
    """造一个姿态的静置样本：`a_meas = M · (g·u) + b + 噪声`。"""
    rng = np.random.default_rng(seed)
    up = np.asarray(direction, dtype=np.float64)
    if tilt_deg:
        perpendicular = rng.normal(size=3)
        perpendicular -= perpendicular @ up * up
        perpendicular /= np.linalg.norm(perpendicular)
        angle = np.radians(tilt_deg)
        up = up * np.cos(angle) + perpendicular * np.sin(angle)
    measured = matrix @ (up * STANDARD_GRAVITY) + bias
    return measured + rng.normal(0.0, noise, size=(samples, 3))


def observe_all(count: int = MIN_ORIENTATIONS + 6, **kwargs) -> list[OrientationObservation]:
    return [
        observe_orientation(synth(direction, seed=index, **kwargs))
        for index, direction in enumerate(spread_directions(count))
    ]


def test_round_trip_recovers_the_injected_matrix_and_bias():
    """注入已知误差 → 解 → 断言解出来的就是注入的那组。本文件的主判据。"""
    calibration = solve_orientations("AA:BB", observe_all())

    np.testing.assert_allclose(calibration.matrix, TRUE_MATRIX, atol=2e-6)
    np.testing.assert_allclose(calibration.bias, TRUE_BIAS, atol=2e-5)
    np.testing.assert_allclose(calibration.bias_mg, [30.0, -20.0, 25.0], atol=0.01)


def test_tilt_does_not_move_the_answer():
    """**R2 换方法的核心理由。**

    六面法在同样的倾斜下会解出 22.8‰ 的假交叉轴项与 5 mg 的假零偏（见模块文档的实测
    对照）。模长判据不使用朝向，所以摆放倾斜根本不是一个误差源 —— 这条把那句话钉成
    判据：给每个姿态注入 3° 倾斜，答案必须几乎不动。
    """
    level = solve_orientations("AA:BB", observe_all())
    tilted = solve_orientations("AA:BB", observe_all(tilt_deg=3.0))

    shift_mg = float(np.linalg.norm(tilted.bias - level.bias) / MILLI_G)
    assert shift_mg < 0.5, f"3° 倾斜把零偏推了 {shift_mg:.2f} mg，模长法不该有这种敏感性"
    assert np.abs(tilted.matrix - level.matrix).max() * 1000 < 0.5


def test_apply_removes_the_error_it_was_fitted_for():
    """成对断言：改正**前**误差在规格书量级，改正**后**落到验收量级。

    只断言「改正后很小」是不够的 —— 一个恒返回零的 `apply` 也能满足它。
    """
    calibration = solve_orientations("AA:BB", observe_all())

    truth = np.array([0.3, -0.5, 9.2])
    measured = DEVICE_MATRIX @ truth + TRUE_BIAS

    before_mg = float(np.linalg.norm(measured - truth) / MILLI_G)
    after_mg = float(np.linalg.norm(calibration.apply(measured) - truth) / MILLI_G)

    assert before_mg > 20.0, "注入的误差本来就没到规格书量级，这个对比说明不了什么"
    assert after_mg < 1.0
    assert after_mg < before_mg / 20.0


def test_residual_and_loo_reach_the_acceptance_target_under_noise():
    """验收口径：标定后残差落在 2~5 mg。噪声取器件量级。

    留一与残差一起断言：残差只说「拟合得贴」，留一才说「对没参与拟合的姿态也准」。
    """
    calibration = solve_orientations("AA:BB", observe_all(noise=0.02, tilt_deg=2.0))
    assert calibration.residual_mg < 5.0
    assert calibration.loo_mg < 5.0


def test_loo_is_not_silently_nan():
    """留一在实现里有一条 `except: continue` 的兜底。全部折叠都失败时它会退化成 nan，
    而 `nan < 5.0` 是 False —— 但一个只看「有没有这个字段」的验收会放它过去。"""
    calibration = solve_orientations("AA:BB", observe_all())
    assert np.isfinite(calibration.loo_mg)


def test_the_matrix_stays_symmetric():
    """模长数据看不见旋转，因此 A 被约束为对称。这条钉住那个约束真的生效了 ——
    不对称的解意味着实现把三维规范自由度放开了，而漂出来的那部分是假的。"""
    calibration = solve_orientations("AA:BB", observe_all())
    np.testing.assert_allclose(calibration.matrix, calibration.matrix.T, atol=1e-12)


def test_the_antisymmetric_part_is_documented_as_invisible():
    """真值带反对称部分时，本方法只能解出对称部分 —— 且**误差恰好等于**那个反对称
    部分，不会更多。模块文档写了这条代价，这里把它量化钉住。

    没有这条，有人会以为「解得不准」而去调迭代或加姿态，但那部分信息根本不在数据里。
    """
    antisymmetric = np.array([[0.0, 0.0015, 0.0], [-0.0015, 0.0, 0.0], [0.0, 0.0, 0.0]])
    asymmetric_truth = TRUE_MATRIX + antisymmetric
    device = np.linalg.inv(asymmetric_truth)

    observations = [
        observe_orientation(synth(direction, matrix=device, seed=index))
        for index, direction in enumerate(spread_directions(MIN_ORIENTATIONS + 6))
    ]
    calibration = solve_orientations("AA:BB", observations)

    symmetric_truth = (asymmetric_truth + asymmetric_truth.T) / 2
    np.testing.assert_allclose(calibration.matrix, symmetric_truth, atol=1e-5)


def test_too_few_orientations_is_refused():
    """恰定时残差恒为 0 —— 那个 0 不能证伪任何东西。"""
    observations = observe_all(count=MIN_ORIENTATIONS - 1)
    with pytest.raises(CalibrationError, match="少于"):
        solve_orientations("AA:BB", observations)


def test_enough_orientations_is_accepted():
    """上一条的反面。没有它，把 `solve_orientations` 写成「一律拒绝」也能全绿。"""
    calibration = solve_orientations("AA:BB", observe_all(count=MIN_ORIENTATIONS))
    assert len(calibration.orientations) == MIN_ORIENTATIONS
    assert calibration.condition_number < 60.0


def clustered(scale: float, seed: int = 3) -> list[OrientationObservation]:
    """姿态个数够，但都挤在 +Z 附近。`scale` 控制挤的程度。"""
    rng = np.random.default_rng(seed)
    base = np.array([0.0, 0.0, 1.0])
    observations = []
    for index in range(MIN_ORIENTATIONS + 6):
        direction = base + rng.normal(scale=scale, size=3)
        direction /= np.linalg.norm(direction)
        observations.append(observe_orientation(synth(direction, seed=index)))
    return observations


def test_severely_clustered_orientations_fail_to_converge():
    """挤得很紧时高斯牛顿收不敛 —— 而**不收敛必须报错**。

    没有这条，`_fit` 会把迭代用尽时手上那组参数当结果返回：量纲对、量级也对，只是
    错的，且与真正收敛的结果在返回值上长得一模一样。
    """
    with pytest.raises(CalibrationError, match="收敛"):
        solve_orientations("AA:BB", clustered(0.05))


def test_moderately_clustered_orientations_are_refused_by_conditioning():
    """挤得没那么紧时拟合能收敛，但条件数上不了台面（实测 70~930）。

    **这条与上一条必须并存**：它们各自守着一段不同的区间，而两段都是真实可达的。
    只留一条，另一条守的那段就没人管 —— 而那段照样会给出很离谱的参数且不报错。
    """
    with pytest.raises(CalibrationError, match="条件数|分布不足"):
        solve_orientations("AA:BB", clustered(0.5))


def test_the_conditioning_gate_is_reachable_not_dead_code():
    """钉住上一条守的区间**确实存在**（拟合收敛、条件数超限）。

    `calib.still` 有过一道够不着的闸：看起来在保护什么，实际永远轮不到它。这条防止
    条件数闸变成同一种东西 —— 若哪天收敛判据收紧到把这段也吃掉，它会红。
    """
    from gait.calib.accel import _fit

    measured = np.array([item.mean for item in clustered(0.5)])
    _matrix, _offset, jacobian = _fit(measured)  # 收敛，不抛
    assert np.linalg.cond(jacobian) > MAX_CONDITION_NUMBER


def test_six_axis_faces_alone_are_refused():
    """R1 的那六个面：既不够数，也观测不到交叉轴项。**这条是 R2 的回归护栏** ——
    有人把姿态数下限调回 6 时，它会红。"""
    faces = []
    for axis in range(3):
        for sign in (1.0, -1.0):
            direction = np.zeros(3)
            direction[axis] = sign
            faces.append(observe_orientation(synth(direction, seed=axis)))
    with pytest.raises(CalibrationError, match="少于"):
        solve_orientations("AA:BB", faces)


def test_a_segment_that_was_not_still_is_refused():
    rng = np.random.default_rng(1)
    shaky = synth(np.array([0.0, 0.0, 1.0])) + rng.normal(0.0, 0.5, size=(SAMPLES, 3))
    with pytest.raises(CalibrationError, match="没有静置好"):
        observe_orientation(shaky)


def test_a_still_segment_is_accepted():
    """上一条的反面：把静止判据调到 0 就能让上一条通过，这条不让。"""
    observation = observe_orientation(
        synth(np.array([0.0, 0.0, 1.0]), noise=0.01, seed=3)
    )
    assert observation.samples == SAMPLES


def test_any_stable_orientation_is_accepted_including_awkward_ones():
    """模长法不检查摆放角度。一个「斜靠着书」的姿态必须照收 —— 六面法会拒绝它，
    而 R2 的整个易用性论据就建立在这上面。"""
    slanted = np.array([0.4, 0.5, 0.77])
    slanted /= np.linalg.norm(slanted)
    assert observe_orientation(synth(slanted)).samples == SAMPLES


def test_too_few_samples_is_refused():
    short = synth(np.array([0.0, 0.0, 1.0]), samples=10)
    with pytest.raises(CalibrationError, match="样本"):
        observe_orientation(short)


def test_a_segment_that_is_not_gravity_is_refused():
    weak = np.tile(np.array([0.0, 0.0, 4.0]), (SAMPLES, 1))
    with pytest.raises(CalibrationError, match="偏离 1 g"):
        observe_orientation(weak)


def test_offset_is_not_the_device_bias():
    """`offset` 与 `bias` 是两个不同的量，模块文档专门写了不可混用。

    没有这条，有人会把 `bias` 「简化」成直接返回 `offset` —— 数值接近、量纲也对，
    只是错的，而错法是安静的。
    """
    calibration = solve_orientations("AA:BB", observe_all())
    assert not np.allclose(calibration.offset, calibration.bias, atol=1e-6)
    np.testing.assert_allclose(
        calibration.offset, -calibration.matrix @ calibration.bias, atol=1e-9
    )


def test_snapshot_carries_what_a_later_review_needs():
    calibration = solve_orientations("AA:BB", observe_all())
    snapshot = calibration.snapshot()

    assert snapshot["device"] == "AA:BB"
    assert snapshot["method"] == "multi-orientation-magnitude"
    assert len(snapshot["orientations"]) == MIN_ORIENTATIONS + 6

    restored = AccelCalibration(
        device=snapshot["device"],
        matrix=np.array(snapshot["matrix"]),
        offset=np.array(snapshot["offset"]),
        residual_mg=snapshot["residual_mg"],
        loo_mg=snapshot["loo_mg"],
        condition_number=snapshot["condition_number"],
        orientations=calibration.orientations,
    )
    probe = np.array([0.1, 0.2, 9.7])
    np.testing.assert_allclose(restored.apply(probe), calibration.apply(probe), atol=1e-12)


def test_snapshot_is_json_serialisable():
    """它要进 `SessionMeta.calib_snapshot` 落盘。numpy 标量会让 json 当场抛错。"""
    import json

    calibration = solve_orientations("AA:BB", observe_all())
    json.dumps(calibration.snapshot())


def test_parameter_count_matches_the_symmetric_model():
    """9 = 对称 A 的 6 个独立分量 + c 的 3 个。改了模型却忘了改这个常数，
    姿态数下限的理由就与实现脱节了。"""
    assert PARAMETER_COUNT == 9


# --- 自适应引导：边采边判「够了没有」 -------------------------------------------


def flat_only(count: int = 22) -> list[OrientationObservation]:
    """只平放：姿态全落在六个轴向附近。这是最容易出现的坏采集。"""
    rng = np.random.default_rng(0)
    axes = [(1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1)]
    out = []
    for index in range(count):
        direction = np.array(axes[index % 6], dtype=float) + rng.normal(scale=0.04, size=3)
        direction /= np.linalg.norm(direction)
        out.append(observe_orientation(synth(direction, seed=index)))
    return out


def test_observability_predicts_what_monte_carlo_measures():
    """停止判据建立在 `σ²(JᵀJ)⁻¹` 上。它若与实际误差对不上，整条引导就是假的。

    这里用一组姿态做小规模蒙特卡洛，断言预测的零偏不确定度与实测 RMS 误差同量级。
    """
    observations = observe_all(count=24)
    predicted = observability(observations).bias_sigma_mg

    rng = np.random.default_rng(5)
    errors = []
    for _ in range(60):
        noisy = [
            OrientationObservation(
                mean=item.mean + rng.normal(0.0, DEFAULT_SIGMA_MG * MILLI_G, 3),
                std=item.std,
                samples=item.samples,
            )
            for item in observations
        ]
        errors.append(
            float(np.linalg.norm(solve_orientations("x", noisy).bias - TRUE_BIAS) / MILLI_G)
        )
    measured = float(np.sqrt(np.mean(np.square(errors))))
    assert 0.6 < measured / predicted < 1.7, (
        f"预测 {predicted:.2f} mg 与实测 {measured:.2f} mg 差得太远，"
        "这个数不能用来判「够了没有」"
    )


def test_flat_only_capture_is_not_called_sufficient():
    """**只看零偏会漏掉一整类坏采集。**

    22 个只平放的姿态零偏 σ 已经达标，但交叉轴 σ 差 5 倍。没有交叉轴那一条，工装会
    对一个只会平放的操作员说「够了」，而那正是 R2 要避开的失效（六面法的 180‰ 是它
    的极端版）。
    """
    status = observability(flat_only())
    assert status.bias_sigma_mg <= TARGET_BIAS_SIGMA_MG, "前提变了：零偏本来是达标的"
    assert status.cross_sigma_ppt > TARGET_CROSS_SIGMA_PPT
    assert not status.sufficient
    assert "斜着" in status.advice


def test_following_the_advice_converges():
    """**引导有没有用，判据是它收不收敛。**

    上一条的反面：`sufficient` 恒为 False 也能让上一条通过，而那样的工装会让操作员
    永远摆下去。这条模拟一个照着建议做的操作员 —— 只要建议说「斜着放」就补一个斜
    姿态 —— 并要求在有限步内达标。

    步数也断言了上界：一条把人往错方向指的建议同样会收敛，只是要摆很多个。
    """
    rng = np.random.default_rng(1)
    observations = flat_only()
    assert not observability(observations).sufficient

    added = 0
    while added < 30:
        status = observability(observations)
        if status.sufficient:
            break
        assert "斜着" in status.advice, f"建议变成了别的：{status.advice}"
        direction = np.array([1.0, 1.0, 0.3]) + rng.normal(scale=0.5, size=3)
        direction /= np.linalg.norm(direction)
        observations.append(observe_orientation(synth(direction, seed=100 + added)))
        added += 1

    status = observability(observations)
    assert status.sufficient, f"照着建议补了 {added} 个仍未达标"
    assert status.cross_sigma_ppt <= TARGET_CROSS_SIGMA_PPT
    assert added <= 15, f"照着建议要补 {added} 个才达标，引导得太差"


def test_a_good_spread_capture_is_sufficient_at_the_floor():
    """方向分散时，到姿态数下限就该达标 —— 否则引导会白白拖长流程。"""
    status = observability(observe_all(count=MIN_ORIENTATIONS))
    assert status.sufficient


def test_observability_refuses_to_guess_below_the_parameter_count():
    """姿态数少于参数个数时 `(JᵀJ)⁻¹` 没有意义，不该报一个看起来正常的 σ。"""
    status = observability(observe_all(count=24)[:6])
    assert not status.sufficient
    assert not np.isfinite(status.bias_sigma_mg)
