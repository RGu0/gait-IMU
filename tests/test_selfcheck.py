"""`gait.sync.selfcheck` 的同步质量自检。

验收标准一条：**人为注入 offset 偏差后自检可检出**。

这个文件里除了那一条，还有三组测试守着实测发现的三件事 —— PRD §8 给的两条判据字面上
都不能用，而真正管用的量 PRD 没提：

1. **「左右步周期差 < 10%」对 offset 完全免疫。** 一个恒定 offset 把一只脚的所有事件
   整体平移，它自己的 stride 周期一点不变。
2. **「双支撑期应为正」会在正常快走上误报。** ZUPT 检出的支撑相边界不是生理边界，
   单侧削掉约 50 ms，步频 125 步/分时测得的双支撑期已经是负的。
3. **真实的左右不对称会伪装成 offset。** 解法是用**足内**支撑相时长差修正 —— offset
   够不着足内的量。

再加一组守着可估性闸门：相位在漂时必须拒绝给出估计，而不是给一个编造的数字。
"""

from dataclasses import replace

import numpy as np
import pytest

from gait.config import AlgoConfig, ConfigError
from gait.core.zupt import detect_stance
from gait.sync.selfcheck import (
    REASON_CADENCE,
    REASON_DRIFTING,
    REASON_OFFSET,
    REASON_TOO_FEW_PHASES,
    SYNC_QUALITY_VERSION,
    TELEMETRY_EVENT,
    SelfCheckError,
    check,
    double_support,
    drop_still_lead,
    stance_spans,
    stride_periods,
)
from gait.validate.synthetic import (
    NoiseModel,
    WalkSpec,
    generate_dual_walk,
    generate_walk,
)

CFG = AlgoConfig()


def spans(series) -> list[tuple[float, float]]:
    detection = detect_stance(series.acc, series.gyr, series.fs, CFG)
    return stance_spans(series.t, detection.stances)


def shift(intervals: list[tuple[float, float]], delta: float) -> list[tuple[float, float]]:
    """整体平移一只脚的全部事件 —— 这**就是**一个跨足同步偏差的定义。"""
    return [(start + delta, stop + delta) for start, stop in intervals]


def dual(**kwargs) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
    spec = WalkSpec(duration_s=24.0, **kwargs)
    data = generate_dual_walk(spec)
    return spans(data["L"][0]), spans(data["R"][0])


def asymmetric(
    *, right_stance_ratio: float = 0.60, right_cadence_scale: float = 1.0
) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
    """左右**真实**不同的步态，且两足严格同步（offset = 0）。

    右足的半个 stride 相位偏移仍由 `still_lead_s` 承担，与 `generate_dual_walk` 一致 ——
    合成数据里不引入任何同步误差，那正是这些测试要区分开的东西。
    """
    base = WalkSpec(duration_s=24.0, cadence=108.0, stance_ratio=0.60)
    stride_time = 120.0 / base.cadence
    right_spec = replace(
        base,
        stance_ratio=right_stance_ratio,
        cadence=base.cadence / right_cadence_scale,
        still_lead_s=base.still_lead_s + 0.5 * stride_time,
    )
    left_series, _ = generate_walk(base, foot="L", noise=NoiseModel(seed=0))
    right_series, _ = generate_walk(right_spec, foot="R", noise=NoiseModel(seed=1))
    return spans(left_series), spans(right_series)


# ── 验收标准：注入 offset 后可检出 ────────────────────────────────────────────


@pytest.mark.parametrize("delta_ms", [10, 30, 50, 80, 120, -80, -120])
def test_an_injected_offset_is_recovered_to_within_a_millisecond(delta_ms):
    """估计值必须**定量**准确，不只是"有异常"。

    准确的估计比一个布尔标志有用得多：它让后台能区分"同步真的坏了"与"步态很不对称"，
    也让 RAY-213 的真值标定有东西可比。实测误差 0.1 ms。
    """
    left, right = dual()
    quality = check(left, shift(right, delta_ms / 1000.0), CFG)

    assert quality.determinate
    assert quality.offset_estimate is not None
    # 右足事件推后 δ，等价于左足相对**早** δ —— 符号约定见 `SyncQuality.offset_estimate`。
    assert -1000 * quality.offset_estimate == pytest.approx(delta_ms, abs=1.0)


@pytest.mark.parametrize("delta_ms", [60, 80, 120, -80])
def test_an_offset_beyond_the_budget_is_flagged(delta_ms):
    left, right = dual()
    quality = check(left, shift(right, delta_ms / 1000.0), CFG)

    assert quality.flagged
    assert REASON_OFFSET in quality.reasons


@pytest.mark.parametrize("delta_ms", [0, 10, 30])
def test_an_offset_within_the_budget_is_not_flagged(delta_ms):
    """PRD §8 容许 ±10~30 ms 的跨足不确定度。阈值必须高于它，否则每次采集都被标。"""
    left, right = dual()
    quality = check(left, shift(right, delta_ms / 1000.0), CFG)

    assert not quality.flagged
    assert quality.reasons == []


def test_the_estimate_is_linear_in_the_injected_offset():
    """斜率必须是 1 —— 这是"配对差 = 2Δ"那条关系的直接检验。"""
    left, right = dual()
    injected = np.array([0.0, 0.02, 0.04, 0.06, 0.08, 0.10])
    estimated = np.array(
        [-check(left, shift(right, delta), CFG).offset_estimate for delta in injected]
    )

    slope = np.polyfit(injected, estimated, 1)[0]
    assert slope == pytest.approx(1.0, abs=0.01)


# ── PRD §8 判据一：stride 周期差对 offset 免疫 ────────────────────────────────


def test_the_stride_period_difference_does_not_move_with_the_offset():
    """**这个测试记录的是一个缺陷，不是一个特性。**

    PRD §8 把「左右步周期差 < 10%」列为同步判据。它不是：一个恒定 offset 把一只脚的
    所有事件整体平移，那只脚**自己的** stride 周期一点不变。这里 offset 从 0 加到
    200 ms，这个量纹丝不动。

    它留在报告里，但摆在正确的位置上 —— 它抓的是节律不对称，不是同步偏差。
    """
    left, right = dual()
    values = [
        check(left, shift(right, delta_ms / 1000.0), CFG).stride_period_difference
        for delta_ms in (0, 50, 100, 200)
    ]

    assert max(values) - min(values) < 1e-9


def test_a_genuine_cadence_asymmetry_is_flagged_as_such():
    """降级不等于废弃 —— 它抓的东西（拖步、疼痛回避）是真实的，只是不叫同步问题。"""
    left, right = asymmetric(right_cadence_scale=1.20)
    quality = check(left, right, CFG)

    assert quality.stride_period_difference > CFG.selfcheck_stride_period_tolerance
    assert REASON_CADENCE in quality.reasons


# ── PRD §8 判据二：双支撑期为负是常态 ─────────────────────────────────────────


@pytest.mark.parametrize("cadence", [125.0, 140.0])
def test_fast_normal_walking_reads_a_negative_double_support_and_is_still_clean(cadence):
    """**这个测试记录的是一个缺陷，不是一个特性。**

    PRD §8 说「双支撑期应为正」。在 ZUPT 边界下它不为正：检出的支撑相比生理支撑相
    每侧短约 50 ms，于是双支撑期读数系统性地小约 100 ms。步频 125 步/分（一个寻常的
    快走速度）时它**全部为负** —— 数据完全正常、同步完全正确。

    照字面判会把这些会话全部标成同步可疑。所以本模块把它当观测量报出去，而不当判据。
    """
    left, right = dual(cadence=cadence)
    quality = check(left, right, CFG)

    assert quality.double_support.mean < 0
    assert quality.double_support.negative_phases == quality.double_support.phases
    assert not quality.flagged


@pytest.mark.parametrize("cadence", [90.0, 108.0, 125.0, 140.0])
def test_the_offset_estimate_is_independent_of_cadence(cadence):
    """那 50 ms 的削减量是**共模**的，在配对差分里精确抵消。

    这正是配对差比双支撑期本身可靠的原因：它不需要知道削减量是多少。
    """
    left, right = dual(cadence=cadence)
    quality = check(left, right, CFG)

    assert quality.offset_estimate == pytest.approx(0.0, abs=0.002)


def test_the_mean_double_support_is_far_less_sensitive_than_the_paired_difference():
    """一类相位变长多少，另一类就变短多少 —— 均值只随两类**个数之差**变化。

    所以"双支撑期占比"这个指标即使算对了，也基本检不出同步偏差：80 ms 的 offset 只
    让它动 2 ms 左右，而配对差动了整整 160 ms。能检出的是两类的**差**，不是均值。

    残余不为零，是因为两类相位个数一般差一个（`Δ·(n_右前 − n_左前) / N`）。断言写成
    比值而不是绝对值，守的正是"迟钝多少倍"这件事。
    """
    left, right = dual()
    reports = [
        check(left, shift(right, delta_ms / 1000.0), CFG).double_support
        for delta_ms in (0, 80)
    ]
    mean_swing = abs(reports[1].mean - reports[0].mean)
    paired_swing = abs(reports[1].leading_difference - reports[0].leading_difference)

    assert paired_swing == pytest.approx(0.160, abs=0.002)
    assert mean_swing < paired_swing / 50


# ── 特异性：真实不对称不得被误判成同步失效 ────────────────────────────────────


@pytest.mark.parametrize("right_stance_ratio", [0.62, 0.65, 0.68, 0.72])
def test_a_genuinely_asymmetric_gait_is_not_mistaken_for_a_sync_failure(right_stance_ratio):
    """这是本模块最重要的一条特异性要求。

    病理步态本来就左右不对称，而那正是本系统最需要工作的人群。未修正时，右足支撑相
    占比 0.65 会给出 −55.6 ms 的配对差 —— 与 Δ = 27.8 ms 的同步偏差无法区分。

    足内修正之后，即使 0.72 对 0.60 这种严重不对称也只留下 5.5 ms 的假 offset。
    """
    left, right = asymmetric(right_stance_ratio=right_stance_ratio)
    quality = check(left, right, CFG)

    assert quality.determinate
    assert abs(quality.offset_estimate) < 0.010
    assert not quality.flagged


def test_the_within_foot_correction_is_what_makes_that_work():
    """把修正项拿掉，同一份数据就会被误判 —— 证明修正不是可有可无的装饰。"""
    left, right = asymmetric(right_stance_ratio=0.72)
    quality = check(left, right, CFG)

    uncorrected = 0.5 * quality.double_support.leading_difference
    assert abs(uncorrected) > CFG.selfcheck_offset_warn_s  # 未修正会触发告警
    assert abs(quality.offset_estimate) < 0.010  # 修正后不会


def test_asymmetry_and_a_real_offset_can_coexist_and_still_be_separated():
    """两件事同时发生时，估计出来的仍应是 offset 那一件。"""
    left, right = asymmetric(right_stance_ratio=0.68)
    quality = check(left, shift(right, 0.080), CFG)

    assert -1000 * quality.offset_estimate == pytest.approx(80.0, abs=10.0)
    assert REASON_OFFSET in quality.reasons


def test_the_within_foot_difference_is_blind_to_the_offset():
    """修正项本身必须不受 offset 影响，否则它会把要估的东西一起扣掉。"""
    left, right = asymmetric(right_stance_ratio=0.68)
    values = [
        check(left, shift(right, delta_ms / 1000.0), CFG).within_foot_stance_difference
        for delta_ms in (0, 50, 100)
    ]

    assert max(values) - min(values) < 1e-9


# ── 可估性：相位在漂时必须拒绝给数 ────────────────────────────────────────────


@pytest.mark.parametrize("scale", [1.02, 1.05, 1.08, 1.12, 1.20])
def test_a_drifting_phase_relationship_yields_no_estimate_at_all(scale):
    """左右步频不同时**不存在**一个恒定 offset，给数字就是编造。

    `1.02` 这一档尤其重要：stride 周期差只有 2.23%、稳稳在 PRD 的 10% 之内，而未加
    一致性闸门时它能编出 110 ms 的假 offset —— 刚好大到会触发告警。百分比闸门拦不住
    这一档，一致性闸门拦得住。
    """
    left, right = asymmetric(right_cadence_scale=scale)
    quality = check(left, right, CFG)

    assert not quality.determinate
    assert quality.offset_estimate is None
    assert REASON_DRIFTING in quality.reasons


def test_a_constant_offset_is_consistent_across_the_two_halves():
    """真恒定的 offset 前后半程读数相同 —— 实测差 0.0 ms。"""
    left, right = dual()
    for delta_ms in (0, 30, 120):
        quality = check(left, shift(right, delta_ms / 1000.0), CFG)
        assert quality.offset_consistency < 0.005


def test_a_severely_asymmetric_but_synchronised_gait_is_still_consistent():
    """不对称是**恒定**的不对称 —— 它不该被一致性闸门挡掉。"""
    left, right = asymmetric(right_stance_ratio=0.72)
    quality = check(left, right, CFG)

    assert quality.offset_consistency < 0.005
    assert quality.determinate


def test_too_few_phases_yields_no_estimate():
    """均值的方差在样本太少时压不住。"""
    left, right = dual()
    quality = check(left[:3], right[:3], CFG)

    assert not quality.determinate
    assert quality.offset_estimate is None
    assert REASON_TOO_FEW_PHASES in quality.reasons


def test_indeterminate_never_reports_a_number():
    """`None` 表示不可估计，不是零偏差 —— 这个不变式值得单独守。"""
    for scale in (1.05, 1.20):
        left, right = asymmetric(right_cadence_scale=scale)
        quality = check(left, right, CFG)
        assert (quality.offset_estimate is None) == (not quality.determinate)


# ── 不拦截，只标注 ────────────────────────────────────────────────────────────


def test_the_check_never_raises_on_a_flagged_session():
    """PRD §8：「不拦截，进 `sync_quality`」。标注是数据，不是异常。"""
    left, right = dual()
    quality = check(left, shift(right, 0.200), CFG)

    assert quality.flagged
    assert isinstance(quality.snapshot(), dict)


def test_the_telemetry_payload_exists_only_when_flagged():
    left, right = dual()
    assert check(left, right, CFG).telemetry is None
    assert check(left, shift(right, 0.120), CFG).telemetry is not None


def test_the_telemetry_payload_carries_enough_to_tell_the_causes_apart():
    """只报一个布尔值的话，真同步故障与严重不对称步态在后台看起来一模一样。"""
    left, right = dual()
    payload = check(left, shift(right, 0.120), CFG).telemetry

    assert payload["event"] == TELEMETRY_EVENT
    assert REASON_OFFSET in payload["reasons"]
    assert payload["offset_estimate"] is not None
    assert "within_foot_stance_difference" in payload
    assert "stride_period_difference" in payload


def test_the_snapshot_is_plain_json_types():
    """`SessionMeta.sync_quality` 要能直接序列化。"""
    import json

    left, right = dual()
    snapshot = check(left, shift(right, 0.080), CFG).snapshot()

    text = json.dumps(snapshot, ensure_ascii=False)
    assert json.loads(text)["version"] == SYNC_QUALITY_VERSION
    assert isinstance(snapshot["double_support"]["phases"], int)
    assert isinstance(snapshot["flagged"], bool)


# ── 静止前导 ──────────────────────────────────────────────────────────────────


def test_the_still_lead_is_dropped_before_anything_else():
    """前导会被 ZUPT 检成一个很长的支撑相。它不是一步。

    留着它，它的"触地时刻"会把整个左右配对错开一位，配对差随即失去意义。
    """
    left, _ = dual()
    kept = drop_still_lead(left, CFG)

    assert len(kept) < len(left)
    durations = [stop - start for start, stop in kept]
    assert max(durations) < CFG.selfcheck_still_lead_factor * float(np.median(durations))


def test_dropping_the_still_lead_from_an_empty_list_is_empty():
    assert drop_still_lead([], CFG) == []


def test_a_series_without_a_still_lead_loses_nothing():
    spans_without_lead = [(float(k), k + 0.5) for k in range(10)]
    assert drop_still_lead(spans_without_lead, CFG) == spans_without_lead


# ── 组件 ──────────────────────────────────────────────────────────────────────


def test_stride_periods_of_a_single_stance_is_empty():
    assert stride_periods([(0.0, 0.5)]).size == 0


def test_double_support_skips_two_stances_of_the_same_foot_in_a_row():
    """配对差的前提是两类相位来自同一个交替序列 —— 漏检的一步不能拿来凑数。"""
    left = [(0.0, 0.6), (1.1, 1.7), (2.2, 2.8)]
    right = [(0.55, 1.15)]  # 只有一个，中间左足连着来了两次
    support = double_support(left, right, step_time=0.55)

    assert support.phases == 2


def test_double_support_of_an_empty_pairing_is_nan_not_zero():
    """没有相位时返回 nan —— 0 会被读成"双支撑期为零"，那是个截然不同的断言。"""
    support = double_support([(0.0, 0.5)], [], step_time=0.55)

    assert support.phases == 0
    assert np.isnan(support.mean)


# ── 输入校验 ──────────────────────────────────────────────────────────────────


def test_stance_spans_rejects_an_out_of_range_interval():
    with pytest.raises(SelfCheckError, match="越界"):
        stance_spans(np.linspace(0.0, 1.0, 10), [(5, 20)])


def test_stance_spans_rejects_a_two_dimensional_time_axis():
    with pytest.raises(SelfCheckError, match="一维"):
        stance_spans(np.zeros((4, 2)), [(0, 2)])


def test_too_few_stances_to_have_a_period_is_rejected():
    with pytest.raises(SelfCheckError, match="至少 2 个支撑相"):
        check([(0.0, 0.5)], [(0.3, 0.8)], CFG)


# ── 配置 ──────────────────────────────────────────────────────────────────────


def test_the_offset_threshold_sits_above_the_sync_budget():
    """PRD §8 容许 ±10~30 ms。阈值低于它会让每一次采集都被标。"""
    assert CFG.selfcheck_offset_warn_s > 0.030


def test_the_stride_period_tolerance_is_the_ten_percent_from_the_prd():
    assert CFG.selfcheck_stride_period_tolerance == 0.10


def test_a_still_lead_factor_of_one_or_less_is_rejected():
    """取 1 或更小会把典型长度的支撑相当成静止前导剔掉。"""
    with pytest.raises(ConfigError, match="selfcheck_still_lead_factor"):
        replace(AlgoConfig(), selfcheck_still_lead_factor=1.0)


def test_fewer_than_two_required_phases_is_rejected():
    with pytest.raises(ConfigError, match="selfcheck_min_phases"):
        replace(AlgoConfig(), selfcheck_min_phases=1)


def test_a_tighter_offset_threshold_flags_a_smaller_offset():
    """阈值可调，且方向必须是对的。"""
    left, right = dual()
    shifted = shift(right, 0.040)

    assert not check(left, shifted, CFG).flagged
    strict = replace(AlgoConfig(), selfcheck_offset_warn_s=0.020)
    assert check(left, shifted, strict).flagged
