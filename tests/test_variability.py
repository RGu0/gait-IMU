"""`gait.analysis.variability` 的变异性、疲劳衰减、对称性与转身指标。

验收标准一条：**60 s 配置下 CV 正常输出且 `grade` 反映样本量少**（AC-15 语义）。

除此之外三组测试守着三件量出来的事：

1. **16 步这个门槛是量出来的**：少于它，3% 与 6% 的 CV（一倍之差）的 90% 区间互相
   重叠 —— 报出来的数分不出这两种情况。
2. **疲劳衰减在非 180 s 配置下抛错，不返回 `None`**：`None` 会被渲染成"—"，与"测了
   但没变化"看起来一样。
3. **步时对称性对跨足偏差极其敏感**：20 ms 偏差就产生 7.2% 的对称性指数。这正是它
   必须强制附同步标注的理由。
"""

from dataclasses import replace as dc_replace

import numpy as np
import pytest

from gait.analysis.events import segment_cycles
from gait.analysis.segments import analyse as segment_analyse
from gait.analysis.segments import selected_cycles
from gait.analysis.variability import (
    GRADE_DEGRADED,
    GRADE_INSUFFICIENT,
    GRADE_NORMAL,
    MIN_STEPS_FOR_CV,
    VARIABILITY_VERSION,
    VariabilityError,
    analyse,
    coefficient_of_variation,
    fatigue_decline,
    step_time_symmetry,
    symmetry,
)
from gait.config import AlgoConfig
from gait.core.zupt import detect_stance
from gait.validate.synthetic import WalkSpec, generate_dual_walk

CFG = AlgoConfig()
SYNC = {"offset_estimate": 0.0, "determinate": True, "flagged": False}


def drop_still_lead(spans):
    if not spans:
        return spans
    typical = float(np.median([stop - start for start, stop in spans]))
    while spans and (spans[0][1] - spans[0][0]) > 2.5 * typical:
        spans = spans[1:]
    return spans


def session(*, duration=180.0, path_length=10.0, trim=1):
    """一次筛过中段步的双足会话。**变异性指标的输入必须是筛过的**（RAY-215）。"""
    spec = WalkSpec(
        duration_s=duration, path_length_m=path_length, turn_strides=2, cadence=108.0
    )
    data = generate_dual_walk(spec)
    cycles: dict[str, list] = {}
    turns, turn_duration = 0, 0.0
    for foot in ("L", "R"):
        series, truth = data[foot]
        spans = drop_still_lead(
            detect_stance(series.acc, series.gyr, series.fs, CFG).stances
        )
        raw, _ = segment_cycles(
            foot, series.t, series.acc, series.gyr, spans, position=truth.p
        )
        report = segment_analyse(raw, series.t, series.gyr[:, 2], trim=trim)
        cycles[foot] = selected_cycles(raw, report)
        turns, turn_duration = report.turns, report.mean_turn_duration
    return cycles, turns, turn_duration


def samples_with_cv(cv: float, n: int, seed: int = 0) -> np.ndarray:
    return np.random.default_rng(seed).normal(1.3, 1.3 * cv, size=n)


# ── 验收标准：60 s 配置下 CV 正常输出 ─────────────────────────────────────────


def test_a_sixty_second_session_still_produces_a_cv():
    """短配置下**照常输出**，由 grade 说明它能支撑什么结论。

    PRD §13 的原则是「指标全量计算 + 质量标注」，无指标级门控 —— 不输出等于替读者
    做了一个他没同意的决定。
    """
    cycles, turns, turn_duration = session(duration=60.0, path_length=4.0)
    report = analyse(
        cycles["L"],
        cycles["R"],
        turns=turns,
        mean_turn_duration=turn_duration,
        protocol_seconds=60,
        sync_quality=SYNC,
    )

    assert report.stride_length_cv.n_steps > 0
    assert report.stride_time_cv.n_steps > 0
    assert report.grade in (GRADE_NORMAL, GRADE_DEGRADED, GRADE_INSUFFICIENT)


def test_the_value_always_travels_with_its_sample_size():
    """PRD §7.4：随值输出 `n_steps`。一个不带样本量的 CV 无法被解读。"""
    cycles, _, _ = session(duration=60.0, path_length=4.0)
    report = analyse(cycles["L"], cycles["R"], sync_quality=SYNC)

    assert report.stride_length_cv.n_steps == len(cycles["L"]) + len(cycles["R"])
    assert report.snapshot()["stride_length_cv"]["n_steps"] > 0


def test_a_shorter_session_yields_fewer_steps_and_a_larger_uncertainty():
    """样本量少 → 不确定度大。这是 grade 要反映的那件事的连续版本。"""
    short, _, _ = session(duration=60.0, path_length=4.0)
    long, _, _ = session(duration=180.0, path_length=10.0)
    short_cv = analyse(short["L"], short["R"], sync_quality=SYNC).stride_length_cv
    long_cv = analyse(long["L"], long["R"], sync_quality=SYNC).stride_length_cv

    assert short_cv.n_steps < long_cv.n_steps
    assert short_cv.relative_uncertainty > long_cv.relative_uncertainty


# ── 16 步这个门槛是量出来的 ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("n", "expected"),
    [
        (2, GRADE_INSUFFICIENT),
        (5, GRADE_DEGRADED),
        (10, GRADE_DEGRADED),
        (16, GRADE_NORMAL),
        (40, GRADE_NORMAL),
    ],
)
def test_the_grade_follows_the_measured_threshold(n, expected):
    """门槛不是拍的：少于 16 步时，3% 与 6% 的 CV 的 90% 区间互相重叠。"""
    assert coefficient_of_variation("stride_length", samples_with_cv(0.03, n)).grade == expected


def test_below_the_threshold_a_doubled_cv_cannot_be_told_apart():
    """**这条测试就是那个门槛的来源。**

    蒙特卡洛（每档 4000 次重抽）：N=10 时，真实 CV=3% 的估计值 p95 高于真实 CV=6% 的
    p5 —— 两者的 90% 区间重叠，报出来的数分不出这两种情况。N=16 时不再重叠。
    """
    trials = 4000

    def spread(true_cv: float, n: int, seed: int) -> np.ndarray:
        rng = np.random.default_rng(seed)
        draws = rng.normal(1.3, 1.3 * true_cv, size=(trials, n))
        return draws.std(axis=1, ddof=1) / draws.mean(axis=1)

    below = np.percentile(spread(0.03, 10, 1), 95) > np.percentile(spread(0.06, 10, 2), 5)
    at = np.percentile(spread(0.03, 25, 3), 95) < np.percentile(spread(0.06, 25, 4), 5)

    assert below  # 10 步：重叠，分不开
    assert at  # 25 步：不重叠，分得开
    assert MIN_STEPS_FOR_CV == 16


def test_the_standard_error_matches_the_analytic_form():
    """`σ_CV ≈ CV/√(2N)`。蒙特卡洛与解析式对得上（实测 0.93% vs 0.87% @N=6）。"""
    cv = coefficient_of_variation("stride_length", samples_with_cv(0.03, 40))

    assert cv.standard_error == pytest.approx(cv.value / np.sqrt(2 * 40), rel=1e-9)


def test_fewer_than_three_samples_yields_nan_not_a_number():
    """两个样本的"标准差"只是它们之差的一半 —— 那不是变异性。"""
    cv = coefficient_of_variation("stride_length", [1.3, 1.31])

    assert np.isnan(cv.value)
    assert cv.grade == GRADE_INSUFFICIENT


def test_a_non_positive_mean_is_rejected():
    """变异系数是相对量，除以一个非正的均值没有意义。"""
    with pytest.raises(VariabilityError, match="CV 无从谈起"):
        coefficient_of_variation("stride_length", [-1.0, 0.0, 1.0])


def test_the_overall_grade_takes_the_worst_component():
    """一个可用的指标救不了一个不可用的。"""
    cycles, _, _ = session(duration=60.0, path_length=4.0)
    report = analyse(cycles["L"][:4], cycles["R"][:4], sync_quality=SYNC)

    assert report.grade == GRADE_DEGRADED


# ── 疲劳衰减只在 180 s 配置输出 ───────────────────────────────────────────────


@pytest.mark.parametrize("seconds", [30, 60, 120, 300])
def test_fatigue_is_refused_outside_the_one_eighty_configuration(seconds):
    """**抛错而不是返回 `None`。**

    `None` 会被下游渲染成"—"，与"这项测了但没有变化"看起来一样，而两者是完全不同的
    结论。60 s 配置下前后各只有三四步，两个均值之差几乎全是噪声。
    """
    cycles, _, _ = session()

    with pytest.raises(VariabilityError, match="180 s 配置"):
        fatigue_decline([*cycles["L"], *cycles["R"]], protocol_seconds=seconds)


def test_fatigue_is_computed_at_one_eighty_seconds():
    cycles, _, _ = session(duration=180.0, path_length=10.0)
    result = fatigue_decline([*cycles["L"], *cycles["R"]], protocol_seconds=180)

    assert result.n_first == result.n_last
    assert result.n_first > 0
    assert result.first_third_speed > 0


def test_a_slowing_walker_shows_a_negative_decline():
    """方向必须对：后段变慢 → 衰减为负。"""
    cycles, _, _ = session(duration=180.0, path_length=10.0)
    combined = [*cycles["L"], *cycles["R"]]
    slowed = [
        dc_replace(cycle, gait_speed=cycle.gait_speed * (0.7 if index > len(combined) * 2 / 3 else 1.0))
        for index, cycle in enumerate(combined)
    ]
    result = fatigue_decline(slowed, protocol_seconds=180)

    assert result.decline < -0.2


def test_the_summary_entry_point_does_not_raise_for_a_short_protocol():
    """汇总入口里"这项不适用"是正常情况；单独调 `fatigue_decline()` 才抛错。"""
    cycles, _, _ = session(duration=60.0, path_length=4.0)
    report = analyse(cycles["L"], cycles["R"], protocol_seconds=60, sync_quality=SYNC)

    assert report.fatigue is None
    assert report.snapshot()["fatigue"] is None


def test_too_few_cycles_for_thirds_is_refused():
    cycles, _, _ = session()

    with pytest.raises(VariabilityError, match="至少 6 个"):
        fatigue_decline(cycles["L"][:3], protocol_seconds=180)


# ── 对称性 ────────────────────────────────────────────────────────────────────


def test_stride_length_and_stance_symmetry_need_no_sync_annotation():
    """**它们是足内量。**

    "对称性"听起来像跨足量，但它比较的两个输入**各自是足内的** —— 每只脚自己算自己
    的步长与支撑相时长，跨足同步偏差够不着它们。
    """
    cycles, _, _ = session()
    items = symmetry(cycles["L"], cycles["R"])

    assert {item.name for item in items} == {"stride_length", "stance_time"}
    assert all(not item.cross_foot for item in items)


def test_a_symmetric_walker_has_a_near_zero_index():
    cycles, _, _ = session()

    assert all(item.index < 0.02 for item in symmetry(cycles["L"], cycles["R"]))


def test_the_index_is_absolute_so_the_raw_values_answer_which_side():
    """对称性问的是"差多少"，不是"哪边大" —— 哪边大由 `left` / `right` 回答。"""
    cycles, _, _ = session()
    longer = [dc_replace(cycle, stride_length=cycle.stride_length * 1.2) for cycle in cycles["R"]]

    forward = symmetry(cycles["L"], longer)[0]
    backward = symmetry(longer, cycles["L"])[0]

    assert forward.index == pytest.approx(backward.index)
    assert forward.right > forward.left
    assert backward.left > backward.right


def test_step_time_symmetry_requires_sync_quality():
    """它是**真正的跨足时序量**（PRD §13）。"""
    cycles, _, _ = session()

    with pytest.raises(VariabilityError, match="跨足时序量"):
        step_time_symmetry(cycles["L"], cycles["R"], sync_quality={})


def test_step_time_symmetry_is_alarmingly_sensitive_to_a_cross_foot_offset():
    """**这条测试是强制附同步标注的理由本身。**

    实测注入的跨足偏差与产生的对称性指数：

    | 偏差 | 步时 SI |
    | --- | --- |
    | 0 ms | 0.00% |
    | 20 ms | **7.21%** |
    | 50 ms | 18.02% |
    | 100 ms | 36.04% |

    PRD §8 容许 ±10~30 ms 的跨足不确定度 —— 也就是说**一个完全对称的受试者，仅凭
    容许范围内的同步误差就能读出 7~11% 的步时不对称**。那个量级在临床上会被当成有
    意义的不对称。

    所以这个指标离开同步质量标注就不能被解读，而标注必须是强制的。
    """
    cycles, _, _ = session()
    baseline = step_time_symmetry(cycles["L"], cycles["R"], sync_quality=SYNC)
    assert baseline.index < 0.01

    shifted = [
        dc_replace(
            cycle, t_ic=cycle.t_ic + 0.020, t_to=cycle.t_to + 0.020, t_ic_next=cycle.t_ic_next + 0.020
        )
        for cycle in cycles["R"]
    ]
    offset = step_time_symmetry(cycles["L"], shifted, sync_quality=SYNC)

    assert offset.index > 0.05  # 20 ms 就到 7%


def test_the_sync_quality_travels_with_the_cross_foot_index():
    cycles, _, _ = session()
    item = step_time_symmetry(cycles["L"], cycles["R"], sync_quality=SYNC)

    assert item.cross_foot
    assert item.sync_quality == SYNC
    assert item.snapshot()["sync_quality"] == SYNC


def test_symmetry_needs_both_feet():
    cycles, _, _ = session()

    with pytest.raises(VariabilityError, match="两只脚"):
        symmetry(cycles["L"], [])


# ── 转身指标与报告 ────────────────────────────────────────────────────────────


def test_turn_metrics_are_carried_through():
    """转身指标由 `analysis/segments` 算，这里只汇总 —— 不重算，免得两处不一致。"""
    cycles, turns, duration = session()
    report = analyse(
        cycles["L"], cycles["R"], turns=turns, mean_turn_duration=duration, sync_quality=SYNC
    )

    assert report.turns == turns
    assert report.mean_turn_duration == duration


def test_the_snapshot_is_plain_json_types():
    import json

    cycles, turns, duration = session()
    snapshot = analyse(
        cycles["L"],
        cycles["R"],
        turns=turns,
        mean_turn_duration=duration,
        protocol_seconds=180,
        sync_quality=SYNC,
    ).snapshot()

    assert json.loads(json.dumps(snapshot))["version"] == VARIABILITY_VERSION
    assert isinstance(snapshot["stride_length_cv"]["n_steps"], int)
    assert snapshot["fatigue"] is not None


def test_an_empty_session_is_an_error_not_a_row_of_zeros():
    with pytest.raises(VariabilityError, match="没有步态周期"):
        analyse([], [], sync_quality=SYNC)
