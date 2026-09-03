"""`gait.analysis.segments` 的直行/转身分离与中段步筛选。

验收标准两条：**合成 4 米往返数据下分离准确率达标**；**剔除策略可配且可复算**。

除此之外有两组测试守着两件容易被想反的事：

1. **分离不能用步长做判据**（会循环论证：分段的目的正是让步长可信）。
2. **剔除策略在 4 米协议下是样本量问题**：每端剔 1 步丢掉三分之二，剔 2 步一步不剩。
   所以剔光时要报错而不是返回空 —— 空集会让下游算出 `nan`，而 `nan` 在报告里看起来
   像"这项没测"，不是"这项被剔除策略吃掉了"。
"""

from itertools import pairwise

import numpy as np
import pytest

from gait.analysis.events import segment_cycles, summarize
from gait.analysis.segments import (
    DEFAULT_TRIM_STEPS,
    KIND_STRAIGHT,
    KIND_TURN,
    SEGMENTATION_VERSION,
    PathSegment,
    SegmentationError,
    analyse,
    heading_change_per_cycle,
    select_middle_steps,
    selected_cycles,
    separate,
    separate_with_agreement,
)
from gait.config import AlgoConfig
from gait.core.zupt import detect_stance
from gait.validate.synthetic import (
    NoiseModel,
    WalkSpec,
    generate_dual_walk,
    generate_walk,
)

CFG = AlgoConfig()


def drop_still_lead(spans):
    if not spans:
        return spans
    typical = float(np.median([stop - start for start, stop in spans]))
    while spans and (spans[0][1] - spans[0][0]) > 2.5 * typical:
        spans = spans[1:]
    return spans


def turnaround(*, path_length=4.0, turn_strides=2, duration=60.0, noise=None):
    """一次 4 米往返（PRD §7 的 T-01 协议形状）。"""
    spec = WalkSpec(
        duration_s=duration,
        path_length_m=path_length,
        turn_strides=turn_strides,
        cadence=108.0,
    )
    series, truth = generate_walk(spec, foot="L", noise=noise or NoiseModel(seed=0))
    spans = drop_still_lead(
        detect_stance(series.acc, series.gyr, series.fs, CFG).stances
    )
    cycles, _ = segment_cycles(
        "L", series.t, series.acc, series.gyr, spans, position=truth.p
    )
    return series, truth, cycles, spec


def truth_labels(truth, cycles):
    labels = []
    for cycle in cycles:
        middle = 0.5 * (cycle.t_ic + cycle.t_ic_next)
        stride = next(
            (s for s in truth.strides if s.t_ic <= middle < s.t_ic_next), None
        )
        labels.append(bool(stride.is_turn) if stride is not None else False)
    return np.array(labels)


def predicted_labels(cycles, segments):
    predicted = np.zeros(len(cycles), dtype=bool)
    for segment in segments:
        if segment.kind == KIND_TURN:
            predicted[segment.start : segment.stop] = True
    return predicted


# ── 验收标准一：分离准确率 ────────────────────────────────────────────────────


@pytest.mark.parametrize("path_length", [4.0, 6.0, 10.0])
@pytest.mark.parametrize("turn_strides", [2, 3])
def test_turns_are_separated_from_straight_walking(path_length, turn_strides):
    """实测 12 种条件（三档路长 × 两档转身步数 × 两档噪声）全部 100%。"""
    series, truth, cycles, _ = turnaround(
        path_length=path_length, turn_strides=turn_strides
    )
    changes = heading_change_per_cycle(cycles, series.t, series.gyr[:, 2])
    segments = separate(cycles, changes)

    assert (predicted_labels(cycles, segments) == truth_labels(truth, cycles)).all()


def test_separation_survives_sensor_noise():
    """判据来自角速度积分，噪声在一个周期内基本被积掉。"""
    series, truth, cycles, _ = turnaround(noise=NoiseModel.bs_bt91(seed=0))
    changes = heading_change_per_cycle(cycles, series.t, series.gyr[:, 2])
    segments = separate(cycles, changes)

    assert (predicted_labels(cycles, segments) == truth_labels(truth, cycles)).all()


def test_the_heading_change_separates_the_two_classes_by_a_wide_margin():
    """实测直行 0.000°、转身 90.0° —— 中间的空当有 89.997°，阈值定在哪里几乎都一样。"""
    series, truth, cycles, _ = turnaround()
    changes = np.abs(heading_change_per_cycle(cycles, series.t, series.gyr[:, 2]))
    actual = truth_labels(truth, cycles)

    assert changes[actual].min() - changes[~actual].max() > 50.0


def test_segments_alternate_and_tile_every_cycle():
    """段必须首尾相接地铺满全部周期 —— 漏掉的周期会静静地不进任何统计。"""
    series, _, cycles, _ = turnaround()
    segments = analyse(cycles, series.t, series.gyr[:, 2]).segments

    assert segments[0].start == 0
    assert segments[-1].stop == len(cycles)
    for current, following in pairwise(segments):
        assert current.stop == following.start
        assert current.kind != following.kind


def test_turn_count_and_duration_are_reported():
    """PRD §7.2 要求输出转身次数与平均转身时长。"""
    series, _, cycles, _ = turnaround(turn_strides=2)
    report = analyse(cycles, series.t, series.gyr[:, 2])

    assert report.turns > 0
    assert 1.0 < report.mean_turn_duration < 6.0


def test_more_turn_strides_make_longer_turns():
    """方向必须对：转身分摊到更多 stride，单次转身就更长。"""
    fast = analyse(*_inputs(turn_strides=2))
    slow = analyse(*_inputs(turn_strides=3))

    assert slow.mean_turn_duration > fast.mean_turn_duration


def _inputs(**kwargs):
    series, _, cycles, _ = turnaround(**kwargs)
    return cycles, series.t, series.gyr[:, 2]


# ── 判据不能用步长 ────────────────────────────────────────────────────────────


def test_the_criterion_is_heading_not_stride_length():
    """**拿步长分段会循环论证。**

    分段的目的正是让步长可信；用步长去定义"哪些步算数"，等于先假定它可信。后果具体：
    一个步长异常的直行步（绊了一下）会被判成转身而消失，于是"直行步的步长"这个统计量
    被它自己的定义过滤过了 —— 而那正是最该被看见的一步。

    这里把一个直行周期的步长改成接近零（转身步的量级），分类结果必须不变。
    """
    from dataclasses import replace as dc_replace

    series, truth, cycles, _ = turnaround()
    actual = truth_labels(truth, cycles)
    straight_index = int(np.flatnonzero(~actual)[3])
    tampered = list(cycles)
    tampered[straight_index] = dc_replace(cycles[straight_index], stride_length=0.05)

    changes = heading_change_per_cycle(tampered, series.t, series.gyr[:, 2])
    segments = separate(tampered, changes)

    assert (predicted_labels(tampered, segments) == actual).all()


# ── 验收标准二：剔除策略可配且可复算 ──────────────────────────────────────────


def test_the_trim_is_configurable_and_changes_what_is_kept():
    series, _, cycles, _ = turnaround()
    none_trimmed = analyse(cycles, series.t, series.gyr[:, 2], trim=0)
    one_trimmed = analyse(cycles, series.t, series.gyr[:, 2], trim=1)

    assert len(one_trimmed.selected) < len(none_trimmed.selected)
    assert set(one_trimmed.selected) < set(none_trimmed.selected)


def test_the_same_parameters_reproduce_the_same_selection():
    """「可复查」的实际含义：拿着报告能重算出同一个结果。"""
    series, _, cycles, _ = turnaround()
    first = analyse(cycles, series.t, series.gyr[:, 2], trim=1)
    again = analyse(
        cycles,
        series.t,
        series.gyr[:, 2],
        trim=first.trim,
        turn_degrees=first.turn_degrees,
    )

    assert first.selected == again.selected
    assert first.dropped == again.dropped


def test_the_report_carries_the_parameters_it_used():
    """不带参数的报告重算不出来 —— 那就不叫可复查。"""
    series, _, cycles, _ = turnaround()
    report = analyse(cycles, series.t, series.gyr[:, 2], trim=1)

    assert report.trim == 1
    assert report.turn_degrees > 0
    assert report.snapshot()["trim"] == 1


def test_every_dropped_step_says_why_it_was_dropped():
    """只存最终的步集回答不了"这一步为什么没进统计"。"""
    series, _, cycles, _ = turnaround()
    report = analyse(cycles, series.t, series.gyr[:, 2], trim=1)
    reasons = set(report.dropped.values())

    assert "turn" in reasons
    assert "segment_head" in reasons
    assert "segment_tail" in reasons
    assert set(report.selected).isdisjoint(report.dropped)
    assert len(report.selected) + len(report.dropped) == len(cycles)


def test_the_snapshot_is_plain_json_types():
    import json

    series, _, cycles, _ = turnaround()
    snapshot = analyse(cycles, series.t, series.gyr[:, 2]).snapshot()

    assert json.loads(json.dumps(snapshot))["version"] == SEGMENTATION_VERSION
    assert isinstance(snapshot["selected"][0], int)


# ── 4 米协议下剔除策略是样本量问题 ────────────────────────────────────────────


def test_trimming_one_step_per_end_costs_most_of_the_data_at_four_metres():
    """**实测：32 步 → 10 步，丢掉约七成。**

    Issue 点名「单段直行仅 4~6 步/侧，剔除策略敏感性是数据评估核心问题」。在 4 米协议
    下这个敏感性的真面目是**样本量塌缩**，不是偏差。
    """
    series, _, cycles, _ = turnaround(path_length=4.0)
    keep_all = analyse(cycles, series.t, series.gyr[:, 2], trim=0)
    trim_one = analyse(cycles, series.t, series.gyr[:, 2], trim=1)

    assert len(trim_one.selected) < 0.5 * len(keep_all.selected)


def test_trimming_two_steps_per_end_leaves_nothing_and_says_so():
    """**剔光时报错，不返回空。**

    空的步集会让下游算出 `nan`，而 `nan` 在报告里可能被渲染成"—"，看起来像"这项没测"，
    而不是"这项被剔除策略吃掉了" —— 两者对数据评估是完全不同的结论。
    """
    series, _, cycles, _ = turnaround(path_length=4.0)

    with pytest.raises(SegmentationError, match="把所有步都剔掉了"):
        analyse(cycles, series.t, series.gyr[:, 2], trim=2)


def test_a_longer_path_survives_a_larger_trim():
    """路长换的是样本量 —— 10 米段每段 8 步，剔 2 步还剩得下。"""
    series, _, cycles, _ = turnaround(path_length=10.0)
    report = analyse(cycles, series.t, series.gyr[:, 2], trim=2)

    assert len(report.selected) > 0


def test_a_segment_too_short_to_trim_is_dropped_with_a_named_reason():
    """短到剔不动的段整段剔掉，理由里带上是被哪个 trim 剔的。"""
    series, _, cycles, _ = turnaround(path_length=4.0)
    report = analyse(cycles, series.t, series.gyr[:, 2], trim=1)

    assert any("too_short" in reason for reason in report.dropped.values())


def test_the_kept_count_is_predictable_from_the_segment_sizes():
    """中段步数**完全由直行段的长度分布决定**：`Σ 长度>2·trim 的段 (长度 − 2·trim)`。

    这条不是把实现抄一遍 —— 它钉的是**可预测性**。RAY-354 的现场读数里
    `S1-sport/slow-a/L` 从 33 个周期只剩 6 个中段步，看起来像个说不清的灾难；
    按这条式子一算就清楚了：直行段被碎成 `[5,1,1,5,2,…]`，两个长度 ≤ 2 的碎段
    **整段丢**，其余每段首尾各去 1 —— 3+0+0+3+0 = 6。**逐格账目 24/24 全对。**

    有了它，「分段坏了多少」不必再靠看最终步数猜，可以直接从段长分布算出来。
    """
    series, _, cycles, _ = turnaround(path_length=10.0, turn_strides=3)
    for trim in (0, 1, 2):
        report = analyse(cycles, series.t, series.gyr[:, 2], trim=trim)
        predicted = sum(
            max(0, segment.cycles - 2 * trim)
            for segment in report.straight_segments
            if segment.cycles > 2 * trim
        )
        assert len(report.selected) == predicted, f"trim={trim}"


def test_chopping_a_walk_into_shorter_segments_costs_superlinearly():
    """**碎段的代价是超线性的。** 同样多的周期，段越碎，留下的越少 —— 直到一步不剩。

    这是 RAY-354 的机制：假转身不只丢掉转身那几步，它把直行段**切碎**，而每个碎段
    首尾各再赔 1 步、长度 ≤ 2 的碎段一步都留不下。所以「判错几次转身」与「丢掉多少步」
    **不是线性关系**：中档判错 1 次只丢 5 步，慢档一格却从 33 掉到 6。
    """
    cycles = list(range(30))  # 只用到个数，段的构造与内容无关
    kept = {}
    for size in (30, 10, 5, 3, 2):
        pieces = [
            PathSegment(KIND_STRAIGHT, start, min(start + size, 30), 0.0, 1.0)
            for start in range(0, 30, size)
        ]
        if size <= 2:
            with pytest.raises(SegmentationError, match="剔除策略"):
                select_middle_steps(cycles, pieces, trim=1)
            kept[size] = 0
        else:
            kept[size] = len(select_middle_steps(cycles, pieces, trim=1).selected)

    # 30 个周期：1 段留 28，3 段留 24，6 段留 18，10 段留 10，15 段一步不剩。
    assert kept == {30: 28, 10: 24, 5: 18, 3: 10, 2: 0}
    # 段数翻倍不是把损失翻倍 —— 从 1 段到 10 段，损失从 2 步涨到 20 步（10 倍）。
    assert (30 - kept[30]) * 10 == 30 - kept[3]


def dual_turnaround(*, turn_strides=1, path_length=10.0, duration=60.0):
    """一次**双足**往返 —— 转身是真的，两只脚都经历。"""
    spec = WalkSpec(
        duration_s=duration,
        path_length_m=path_length,
        turn_strides=turn_strides,
        cadence=108.0,
    )
    pair = generate_dual_walk(spec, noise=NoiseModel(seed=0))
    out = {}
    for label, (series, truth) in pair.items():
        spans = drop_still_lead(
            detect_stance(series.acc, series.gyr, series.fs, CFG).stances
        )
        cycles, _ = segment_cycles(
            label, series.t, series.acc, series.gyr, spans, position=truth.p
        )
        out[label] = (
            cycles,
            heading_change_per_cycle(cycles, series.t, series.gyr[:, 2]),
        )
    return out


def turn_count(pieces):
    return sum(1 for item in pieces if item.kind == KIND_TURN)


# ── RAY-354 判据 6：跨足一致性 ────────────────────────────────────────────────
#
# 判别量是"两只脚认不认"，不是"转身有多长"。长度分不开两类：假转身是单步（漂移是逐步
# 噪声），而小角度真转身**也是单步** —— 下面第一条测试就钉住这一点。


def test_a_real_turn_survives_because_both_feet_see_it():
    """**阳性对照，也是本条规则的要害**：`turn_strides=1` 的单步真转身必须活下来。

    「转身至少 N 步」那种过滤器会把它全杀掉 —— 而 `separate` 的判据是逐周期的，
    一步 30° 的转弯按本模块自己的定义就是合法转身。真机实测这条规则保住 5/5。
    """
    dual = dual_turnaround(turn_strides=1)
    plain = {
        label: separate(cycles, changes) for label, (cycles, changes) in dual.items()
    }
    left, right = separate_with_agreement(
        dual["L"], dual["R"], sync_quality={"test": True}
    )

    assert turn_count(plain["L"]) > 0, "合成往返里本来就该有转身"
    assert turn_count(left) == turn_count(plain["L"])
    assert turn_count(right) == turn_count(plain["R"])


def test_a_turn_only_one_foot_sees_is_demoted_to_straight():
    """**阴性对照**：另一只脚没看见的"转身"降级为直行，相邻直行段随之合并。

    真机 T-230-03 转身真值为 0，这条规则把误报从 38 次降到 7 次（−82%），
    而剩下的 7 次全在 `S1-sport/slow-a` —— 那一格另有前向解发散的缺陷。
    """
    dual = dual_turnaround(turn_strides=1)
    cycles_r, changes_r = dual["R"]
    # 右脚全程直行：它一次转身都不判，于是左脚的转身没有任何佐证。
    quiet_right = (cycles_r, np.zeros_like(changes_r))

    plain_left = separate(*dual["L"])
    left, _ = separate_with_agreement(
        dual["L"], quiet_right, sync_quality={"test": True}
    )

    assert turn_count(plain_left) > 0
    assert turn_count(left) == 0
    # 降级之后整趟并成一段，而不是留下一堆碎段 —— 碎段的代价是超线性的。
    assert len(left) == 1


def test_agreement_needs_the_sync_quality_annotation():
    """跨足判断离开同步质量标注没有意义（PRD §13），与 `double_support` 同一口径。"""
    dual = dual_turnaround(turn_strides=1)
    with pytest.raises(SegmentationError, match="同步质量"):
        separate_with_agreement(dual["L"], dual["R"], sync_quality=None)


def test_a_negative_agreement_tolerance_is_rejected():
    dual = dual_turnaround(turn_strides=1)
    with pytest.raises(SegmentationError, match="tolerance_s"):
        separate_with_agreement(
            dual["L"], dual["R"], sync_quality={"test": True}, tolerance_s=-0.1
        )


# ── 分离对哪些指标要紧 ────────────────────────────────────────────────────────


def test_separation_barely_moves_the_median_but_rescues_the_spread():
    """**这条测试解释了本模块为什么必要。**

    RAY-216 的汇总用中位数，而中位数对转身免疫（转身步是少数）—— 所以"步长"看起来
    不分离也对。但变异性指标不是这样：实测不分离时 CV 读到 **71.0%**、均值从 1.300
    掉到 0.833；分离后 CV 是 0.0%、均值回到 1.300。

    RAY-217 的 CV / 对称性指标全部建立在离散度上，它们**必须**先分离。
    """
    series, _, cycles, spec = turnaround()
    everything = np.array([cycle.stride_length for cycle in cycles])
    kept = np.array(
        [
            cycle.stride_length
            for cycle in selected_cycles(
                cycles, analyse(cycles, series.t, series.gyr[:, 2])
            )
        ]
    )

    # 中位数两边都对。
    assert np.median(everything) == pytest.approx(spec.stride_length, abs=0.02)
    assert np.median(kept) == pytest.approx(spec.stride_length, abs=0.02)
    # 均值与离散度只有分离后才对。
    assert everything.mean() < 0.75 * spec.stride_length
    assert kept.mean() == pytest.approx(spec.stride_length, abs=0.02)
    assert everything.std() > 20 * max(kept.std(), 1e-6)


def test_the_selected_cycles_summarize_to_the_true_parameters():
    series, _, cycles, spec = turnaround()
    report = analyse(cycles, series.t, series.gyr[:, 2])
    summary = summarize("L", selected_cycles(cycles, report))

    assert summary.stride_length == pytest.approx(spec.stride_length, abs=0.02)


# ── 输入校验 ──────────────────────────────────────────────────────────────────


def test_a_mismatched_heading_length_is_rejected():
    _, _, cycles, _ = turnaround()

    with pytest.raises(SegmentationError, match="长度"):
        separate(cycles, np.zeros(len(cycles) + 3))


def test_a_mismatched_yaw_rate_shape_is_rejected():
    series, _, cycles, _ = turnaround()

    with pytest.raises(SegmentationError, match="形状必须一致"):
        heading_change_per_cycle(cycles, series.t, series.gyr[:-4, 2])


def test_a_negative_trim_is_rejected():
    series, _, cycles, _ = turnaround()
    segments = separate(
        cycles, heading_change_per_cycle(cycles, series.t, series.gyr[:, 2])
    )

    with pytest.raises(SegmentationError, match="trim 不得为负"):
        select_middle_steps(cycles, segments, trim=-1)


def test_a_non_positive_turn_threshold_is_rejected():
    series, _, cycles, _ = turnaround()
    changes = heading_change_per_cycle(cycles, series.t, series.gyr[:, 2])

    with pytest.raises(SegmentationError, match="turn_degrees"):
        separate(cycles, changes, turn_degrees=0.0)


def test_no_cycles_yields_no_segments():
    assert separate([], np.zeros(0)) == []


def test_straight_walking_has_one_segment_and_no_turns():
    """不转身的直行数据不该被切出任何转身段。"""
    spec = WalkSpec(duration_s=24.0, cadence=108.0)
    series, truth = generate_walk(spec, foot="L", noise=NoiseModel(seed=0))
    spans = drop_still_lead(
        detect_stance(series.acc, series.gyr, series.fs, CFG).stances
    )
    cycles, _ = segment_cycles(
        "L", series.t, series.acc, series.gyr, spans, position=truth.p
    )
    report = analyse(cycles, series.t, series.gyr[:, 2])

    assert report.turns == 0
    assert len(report.segments) == 1
    assert report.segments[0].kind == KIND_STRAIGHT


def test_the_default_trim_is_one_step_per_end():
    """PRD §7.2：默认剔除每段首尾各 1 步。"""
    assert DEFAULT_TRIM_STEPS == 1
