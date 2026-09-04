"""`gait.analysis.events` 的步态事件分割与时空参数。

验收标准两条：**合成数据下事件时刻误差 < 20 ms**；**参数量级合理性检查通过**。

这个文件里最重要的一组测试守的不是"细化更准"，而是**细化与检测窗口无关**。原始偏差
跟着 `zupt_window_samples` 从 22 ms 涨到 130 ms；细化之后纹丝不动。那条性质区分了两种
做法：按窗口乘系数（系数实测在 1.27~1.38 之间漂，是标定出来的）与逐样本外推（由信号
本身决定）。前者会在有人调窗口的那天悄悄失效，而且不会有任何东西报错。
"""

from dataclasses import replace
from itertools import pairwise

import numpy as np
import pytest

from gait.analysis.events import (
    EVENTS_VERSION,
    STEPS_PER_STRIDE,
    EventError,
    StanceEdges,
    detect_stance_intervals,
    double_support,
    refine_stance_edges,
    segment_cycles,
    summarize,
)
from gait.config import AlgoConfig
from gait.core.ins import GRAVITY_STANDARD
from gait.core.zupt import detect_stance
from gait.validate.synthetic import (
    NoiseModel,
    WalkSpec,
    generate_dual_walk,
    generate_walk,
)

CFG = AlgoConfig()
#: 验收线。PRD/Issue 给的是 20 ms。
EVENT_TOLERANCE_MS = 20.0


def drop_still_lead(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """去掉起步前的静止前导 —— 它不是一步。"""
    if not spans:
        return spans
    typical = float(np.median([stop - start for start, stop in spans]))
    while spans and (spans[0][1] - spans[0][0]) > 2.5 * typical:
        spans = spans[1:]
    return spans


def walk(*, cadence=108.0, stance_ratio=0.60, noise=None, cfg=None, duration=24.0):
    cfg = cfg or CFG
    spec = WalkSpec(duration_s=duration, cadence=cadence, stance_ratio=stance_ratio)
    series, truth = generate_walk(spec, foot="L", noise=noise or NoiseModel(seed=0))
    spans = drop_still_lead(detect_stance(series.acc, series.gyr, series.fs, cfg).stances)
    return series, truth, spans


def event_errors(series, truth, spans, cfg=None):
    """细化后的 IC/TO 相对真值的误差，ms。

    配对按**包含关系**：支撑相的中点落在哪个 stride 的 `[t_ic, t_ic_next)` 里。按顺序
    配（第 k 个配第 k 个）在漏检或多检时会整体错位一格，误差变成一整个 stride
    （实测 1000+ ms）—— 那是配对的毛病，会掩盖被测的量。
    """
    cfg = cfg or CFG
    edges = refine_stance_edges(series.acc, series.gyr, spans, cfg)
    ic_err, to_err = [], []
    for edge in edges:
        t_ic = float(series.t[edge.ic])
        t_to = float(series.t[min(edge.to, series.t.size - 1)])
        middle = 0.5 * (t_ic + t_to)
        stride = next((s for s in truth.strides if s.t_ic <= middle < s.t_ic_next), None)
        if stride is None:
            continue
        ic_err.append(1000 * (t_ic - stride.t_ic))
        to_err.append(1000 * (t_to - stride.t_to))
    return np.array(ic_err), np.array(to_err)


def raw_errors(series, truth, spans):
    """**不**细化时的误差，用来对照。"""
    ic_err, to_err = [], []
    for start, stop in spans:
        t_ic = float(series.t[start])
        t_to = float(series.t[stop - 1])
        middle = 0.5 * (t_ic + t_to)
        stride = next((s for s in truth.strides if s.t_ic <= middle < s.t_ic_next), None)
        if stride is None:
            continue
        ic_err.append(1000 * (t_ic - stride.t_ic))
        to_err.append(1000 * (t_to - stride.t_to))
    return np.array(ic_err), np.array(to_err)


NOISE_CASES = [
    ("clean", NoiseModel(seed=0)),
    ("bs_bt91", NoiseModel.bs_bt91(seed=0)),
    (
        "bs_bt91_x3",
        replace(
            NoiseModel.bs_bt91(seed=1),
            accel_density=3 * NoiseModel.bs_bt91().accel_density,
            gyro_density=3 * NoiseModel.bs_bt91().gyro_density,
        ),
    ),
]


# ── 验收标准一：事件时刻误差 < 20 ms ──────────────────────────────────────────


@pytest.mark.parametrize("label,noise", NOISE_CASES, ids=[case[0] for case in NOISE_CASES])
@pytest.mark.parametrize("cadence", [90.0, 108.0, 125.0])
def test_refined_events_land_within_the_acceptance_tolerance(label, noise, cadence):
    """实测最坏 6.67 ms，验收线 20 ms。"""
    series, truth, spans = walk(cadence=cadence, noise=noise)
    ic, to = event_errors(series, truth, spans)

    assert np.abs(ic).max() < EVENT_TOLERANCE_MS
    assert np.abs(to).max() < EVENT_TOLERANCE_MS


def test_the_raw_zupt_boundary_would_fail_the_acceptance_tolerance():
    """**这条测试记录的是细化为什么必要。**

    整体设计 §6.1 估计 ZUPT 边界有 10~30 ms 偏差。实测是 **48~56 ms** —— 估计偏小了
    大约一倍，且远超 20 ms 的验收线。所以细化不是锦上添花。
    """
    series, truth, spans = walk()
    ic, to = raw_errors(series, truth, spans)

    assert ic.mean() > 40.0  # IC 迟到
    assert to.mean() < -40.0  # TO 提前
    assert np.abs(ic).max() > EVENT_TOLERANCE_MS


def test_the_raw_bias_is_systematic_not_noise():
    """标准差只有 1.4 ms —— 它是系统性的。

    这件事决定了它可修：随机误差修不掉，系统性偏差可以。
    """
    series, truth, spans = walk()
    ic, _ = raw_errors(series, truth, spans)

    assert ic.std() < 5.0
    assert ic.mean() > 40.0


# ── 细化与检测窗口无关（这一组是本文件的核心）────────────────────────────────


@pytest.mark.parametrize("window", [7, 15, 31, 41])
def test_the_refined_error_does_not_move_with_the_detection_window(window):
    """**原始偏差随窗口从 22 ms 涨到 130 ms；细化后纹丝不动。**

    这条性质区分了两种做法。按窗口乘系数也能把当前这套参数下的偏差修掉，但那个系数
    实测在 1.27~1.38 之间漂（窗口 7~41），是标定出来的 —— 有人调 `zupt_window_samples`
    的那天它会悄悄失效，而且不会有任何东西报错。

    逐样本外推由信号本身决定，所以窗口怎么变都不影响结果。
    """
    cfg = replace(CFG, zupt_window_samples=window)
    series, truth, spans = walk(noise=NoiseModel.bs_bt91(seed=0), cfg=cfg)
    ic, _ = event_errors(series, truth, spans, cfg)

    assert np.abs(ic).max() < 5.0


def test_the_raw_error_does_move_with_the_detection_window():
    """上一条的对照：不细化时，偏差确实跟着窗口走。"""
    errors = {}
    for window in (7, 41):
        cfg = replace(CFG, zupt_window_samples=window)
        series, truth, spans = walk(noise=NoiseModel.bs_bt91(seed=0), cfg=cfg)
        errors[window] = raw_errors(series, truth, spans)[0].mean()

    assert errors[41] > 4 * errors[7]


def test_the_amount_pushed_out_scales_with_the_window():
    """外推的样本数跟着窗口涨 —— 那正是它在补偿的东西。"""
    pushed = {}
    for window in (7, 41):
        cfg = replace(CFG, zupt_window_samples=window)
        series, _, spans = walk(cfg=cfg)
        edges = refine_stance_edges(series.acc, series.gyr, spans, cfg)
        pushed[window] = float(np.median([edge.expanded_start for edge in edges]))

    assert pushed[41] > 4 * pushed[7]


def test_the_expansion_is_bounded_so_a_long_stand_does_not_merge_two_stances():
    """没有上限的话，受试者站着不动的那一段会把相邻两个支撑相连成一片。"""
    series, _, spans = walk()
    edges = refine_stance_edges(series.acc, series.gyr, spans, CFG)
    limit = round(1.5 * CFG.zupt_window_samples)

    assert all(edge.expanded_start <= limit for edge in edges)
    assert all(edge.expanded_stop <= limit for edge in edges)


def test_refined_stances_stay_ordered_and_disjoint():
    """细化不得让区间交叠 —— 交叠会让下游的周期切分产生非法的时刻顺序。"""
    series, _, spans = walk()
    edges = refine_stance_edges(series.acc, series.gyr, spans, CFG)

    for current, following in pairwise(edges):
        assert current.ic < current.to <= following.ic


# ── 验收标准二：参数量级合理 ──────────────────────────────────────────────────


def test_the_spatiotemporal_parameters_match_the_ground_truth():
    """步频、步长、步速、支撑相占比都应当对得上生成参数。"""
    spec = WalkSpec(duration_s=30.0, cadence=108.0, stance_ratio=0.60, stride_length=1.30)
    data = generate_dual_walk(spec)
    series, truth = data["L"]
    spans = drop_still_lead(detect_stance(series.acc, series.gyr, series.fs, CFG).stances)
    cycles, _ = segment_cycles("L", series.t, series.acc, series.gyr, spans, position=truth.p)
    summary = summarize("L", cycles)

    assert summary.cadence == pytest.approx(108.0, abs=1.0)
    assert summary.stride_length == pytest.approx(1.30, abs=0.02)
    assert summary.gait_speed == pytest.approx(1.30 / (120 / 108), rel=0.02)
    assert summary.stance_ratio == pytest.approx(60.0, abs=1.5)
    assert summary.swing_ratio == pytest.approx(40.0, abs=1.5)


def test_the_two_ratios_add_up_to_one_hundred():
    """支撑相与摆动相是同一个周期的两半 —— 它们必须刚好补满。"""
    spec = WalkSpec(duration_s=24.0)
    data = generate_dual_walk(spec)
    series, truth = data["L"]
    spans = drop_still_lead(detect_stance(series.acc, series.gyr, series.fs, CFG).stances)
    cycles, _ = segment_cycles("L", series.t, series.acc, series.gyr, spans, position=truth.p)
    summary = summarize("L", cycles)

    assert summary.stance_ratio + summary.swing_ratio == pytest.approx(100.0)


def test_cadence_counts_steps_not_strides():
    """临床说的"步频"按**步**算，而一个 stride 是两步。差一倍不是小事。"""
    spec = WalkSpec(duration_s=24.0, cadence=108.0)
    data = generate_dual_walk(spec)
    series, truth = data["L"]
    spans = drop_still_lead(detect_stance(series.acc, series.gyr, series.fs, CFG).stances)
    cycles, _ = segment_cycles("L", series.t, series.acc, series.gyr, spans, position=truth.p)
    summary = summarize("L", cycles)

    assert summary.stride_time == pytest.approx(120.0 / 108.0, abs=0.02)
    assert summary.cadence == pytest.approx(
        60.0 * STEPS_PER_STRIDE / summary.stride_time, rel=1e-6
    )


def test_summaries_use_the_median_so_one_stumble_does_not_move_them():
    """步态参数的临床解读建立在"典型的一步"上，不是"平均的一步"。"""
    spec = WalkSpec(duration_s=24.0)
    data = generate_dual_walk(spec)
    series, truth = data["L"]
    spans = drop_still_lead(detect_stance(series.acc, series.gyr, series.fs, CFG).stances)
    cycles, _ = segment_cycles("L", series.t, series.acc, series.gyr, spans, position=truth.p)
    clean = summarize("L", cycles)

    from dataclasses import replace as dc_replace

    stumbled = [*cycles[:-1], dc_replace(cycles[-1], stride_length=5.0)]
    assert summarize("L", stumbled).stride_length == pytest.approx(clean.stride_length)


# ── 双支撑期 ──────────────────────────────────────────────────────────────────


def dual_cycles(spec=None):
    spec = spec or WalkSpec(duration_s=30.0, cadence=108.0, stance_ratio=0.60)
    data = generate_dual_walk(spec)
    out = {}
    for foot in ("L", "R"):
        series, truth = data[foot]
        spans = drop_still_lead(detect_stance(series.acc, series.gyr, series.fs, CFG).stances)
        out[foot], _ = segment_cycles(
            foot, series.t, series.acc, series.gyr, spans, position=truth.p
        )
    return out


def test_double_support_lands_in_the_physiological_band():
    """**这条测试守的是一个曾经差了一个数量级的指标。**

    RAY-205 实测：直接拿两只脚的 ZUPT 区间取交集，占比读到**约 2%**，而生理值是
    10~25%（整体设计 §6.2）。原因是那 50 ms 的削减 —— 两只脚各削一次，重叠区几乎被
    削光。由细化后的事件算，读到 20.5%，与生成参数的 20.0% 对得上。
    """
    cycles = dual_cycles()
    report = double_support(
        cycles["L"], cycles["R"], sync_quality={"offset_estimate": 0.0, "determinate": True}
    )

    assert 0.10 <= report.fraction <= 0.25
    assert report.fraction == pytest.approx(0.20, abs=0.03)


def test_a_phase_spanning_a_whole_stride_is_excluded_and_counted():
    """**跨过整步的配对不是双支撑期读数**，剔掉它，并把剔了几个记下来。

    构造用的是**真实机制**：两只脚在同一时间窗都缺步（RAY-354 里 `S1-sport/slow-a`
    左脚 33 个周期只剩 6、右脚剩 14），于是缺口两侧只能跨着配，读出一个秒级的
    "相位"。真机实测最负 **−5.58 个步态周期**，其余 11 格 −0.96 ~ −0.02。

    **只挖一只脚没用** —— 另一只会把序列填满，缺口两侧仍然配得上。
    """
    quality = {"offset_estimate": 0.0, "determinate": True}
    clean = dual_cycles()
    good = double_support(clean["L"], clean["R"], sync_quality=quality)

    gap_l = clean["L"][:8] + clean["L"][18:]
    gap_r = clean["R"][:8] + clean["R"][18:]
    skewed = double_support(gap_l, gap_r, sync_quality=quality)

    assert good.excluded == 0, "干净输入不该有东西被剔"
    assert skewed.excluded >= 1, "跨过缺口的那个配对要被剔出来"
    assert skewed.count > 0
    stride = float(np.median([cycle.stride_time for cycle in clean["L"]]))
    assert skewed.worst > -1.5 * stride, "剔完之后不该还留着跨步的配对"


def test_the_median_survives_an_outlier_that_wrecks_the_fraction():
    """**判据 2**：`fraction` 建在均值上，被单个离群相位支配；`median` 不会。

    真机实测 `最小相位 vs fraction` 的相关是 **r = +0.931** —— `S1-flat/slow-b`
    只有 1 个负相位（−6.296 s），占比就从中位 +0.086 被拉到 −0.038。
    """
    quality = {"offset_estimate": 0.0, "determinate": True}
    clean = dual_cycles()
    base = double_support(clean["L"], clean["R"], sync_quality=quality)

    # 只动右脚的**第一个**周期，制造一个恰好在剔除门内、但足以拖垮均值的坏配对。
    stride = float(np.median([cycle.stride_time for cycle in clean["R"]]))
    nudged = list(clean["R"])
    first = nudged[0]
    nudged[0] = replace(
        first, t_ic=first.t_ic - 1.2 * stride, t_to=first.t_to - 1.2 * stride
    )
    spoiled = double_support(clean["L"], nudged, sync_quality=quality)

    assert spoiled.fraction < base.fraction, "均值被那一个坏配对拉走了"
    # 中位几乎不动 —— 这就是为什么报告要带上它。
    assert abs(spoiled.median - base.median) < 0.2 * abs(base.median)


def test_feet_whose_step_sequences_do_not_interleave_are_uncomputable():
    """**判据 1**：配对达成率太低时抛错，让上游标为不可计算。

    完全交替能配出 `nL + nR − 1` 个相位。达成率低说明两只脚的步序**配不上** ——
    真机实测当前 0.88~1.00，但 RAY-354 判据 6 落地**之前**，`S1-sport/slow-a`
    是 5 个相位 / 20 个中段步 = **0.26**。那种失效真发生过。
    """
    quality = {"offset_estimate": 0.0, "determinate": True}
    clean = dual_cycles()
    # 把右脚整段挪到左脚**之后**：两条序列不再交错，配对几乎全是同足相邻。
    span = clean["L"][-1].t_ic_next - clean["L"][0].t_ic
    apart = [
        replace(
            cycle,
            t_ic=cycle.t_ic + span,
            t_to=cycle.t_to + span,
            t_ic_next=cycle.t_ic_next + span,
        )
        for cycle in clean["R"]
    ]
    with pytest.raises(EventError, match="配不上"):
        double_support(clean["L"], apart, sync_quality=quality)


def test_double_support_needs_the_still_lead_gone_from_its_input():
    """**这条测试把一个只写在测试辅助函数里的前提，变成一条会变红的断言。**

    `dual_cycles()` 一直先剔静止前导再分段，所以模块的测试全绿；而模块文档里
    一个字都没提这件事。RAY-213 的 V3′ 工装不走分段、直接喂 `segment_cycles()`
    的输出，前导就跟着进来了 —— 读数被污染了 7~10 个百分点，而生理带宽本身才
    10~25%（模块文档 §5）。

    前导会被 ZUPT 检成一个秒级的支撑相，于是混进来一个秒级的"双支撑相位"。
    产品路径喂的是 `segments.selected_cycles()` 的中段直行步，天然没有它。
    """
    quality = {"offset_estimate": 0.0, "determinate": True}
    clean = dual_cycles()

    spec = WalkSpec(duration_s=30.0, cadence=108.0, stance_ratio=0.60)
    data = generate_dual_walk(spec)
    contaminated = {}
    for foot in ("L", "R"):
        series, truth = data[foot]
        # 与 `dual_cycles()` 唯一的差别：**不剔前导**。
        stances = detect_stance(series.acc, series.gyr, series.fs, CFG).stances
        contaminated[foot], _ = segment_cycles(
            foot, series.t, series.acc, series.gyr, stances, position=truth.p
        )

    good = double_support(clean["L"], clean["R"], sync_quality=quality)
    bad = double_support(contaminated["L"], contaminated["R"], sync_quality=quality)

    assert 0.10 <= good.fraction <= 0.25          # 生理带内
    assert good.excluded == 0                     # 干净输入不该有东西被剔

    # **RAY-354 判据 7 改变了这条测试的结论，而不是废掉它。**
    #
    # 从前这里断言 `bad.fraction > 0.25`：前导混进来的那个秒级"相位"会把占比顶出
    # 生理带 7~10 个百分点 —— 污染是**静默**的，只能靠读数看着不对来发现。
    #
    # 现在跨过 1.5 个步态周期的配对会被剔除并计数，所以同一份被污染的输入**读数
    # 回到了带内**，而污染本身变成了一个**看得见的数**。这才是想要的行为：
    # 静默地扰动一个指标比报错还坏。原来的前提（前导会混进一个秒级相位）不变，
    # 变的是它现在被抓住了。
    assert bad.excluded >= 1                      # 前导那个伪影被剔出来了
    assert 0.10 <= bad.fraction <= 0.25           # 剔完之后回到生理带内


def test_double_support_tracks_the_stance_ratio():
    """支撑相占比越大，双支撑期越长 —— 方向必须对。"""
    low = double_support(
        **_sides(WalkSpec(duration_s=30.0, stance_ratio=0.60)),
        sync_quality={"determinate": True},
    )
    high = double_support(
        **_sides(WalkSpec(duration_s=30.0, stance_ratio=0.68)),
        sync_quality={"determinate": True},
    )

    assert high.fraction > low.fraction


def _sides(spec):
    cycles = dual_cycles(spec)
    return {"left": cycles["L"], "right": cycles["R"]}


def test_sync_quality_is_required_not_optional():
    """**跨足指标离开同步质量就没有意义**（PRD §13「强制附同步质量标注」）。

    一个 80 ms 的跨足偏差会让这个占比整体挪 8 个百分点，而读数本身看不出任何异常。
    所以它是关键字必填参数，而不是一个可以忘记传的可选项。
    """
    cycles = dual_cycles()

    with pytest.raises(TypeError):
        double_support(cycles["L"], cycles["R"])  # type: ignore[call-arg]


def test_the_sync_quality_travels_with_the_measurement():
    cycles = dual_cycles()
    quality = {"offset_estimate": 0.012, "determinate": True, "flagged": False}
    report = double_support(cycles["L"], cycles["R"], sync_quality=quality)

    assert report.sync_quality == quality
    assert report.snapshot()["sync_quality"] == quality


def test_the_double_support_snapshot_is_plain_json_types():
    import json

    cycles = dual_cycles()
    snapshot = double_support(
        cycles["L"], cycles["R"], sync_quality={"determinate": True}
    ).snapshot()

    assert json.loads(json.dumps(snapshot))["version"] == EVENTS_VERSION
    assert all(isinstance(value, float) for value in snapshot["phases"])


def test_double_support_needs_both_feet():
    cycles = dual_cycles()

    with pytest.raises(EventError, match="两只脚"):
        double_support(cycles["L"], [], sync_quality={"determinate": True})


# ── 周期切分 ──────────────────────────────────────────────────────────────────


def test_cycles_have_strictly_increasing_events():
    cycles = dual_cycles()["L"]

    for cycle in cycles:
        assert cycle.t_ic < cycle.t_to < cycle.t_ic_next


def test_stride_time_is_the_sum_of_stance_and_swing():
    cycles = dual_cycles()["L"]

    for cycle in cycles:
        assert cycle.stance_time + cycle.swing_time == pytest.approx(cycle.stride_time)


def test_without_a_position_the_lengths_are_nan_not_invented():
    """区分"没有位置"与"位置为零"很重要 —— 一次只做事件分割的调用不该被迫先跑惯导。"""
    series, _, spans = walk()
    cycles, _ = segment_cycles("L", series.t, series.acc, series.gyr, spans)

    assert cycles
    assert all(np.isnan(cycle.stride_length) for cycle in cycles)
    assert all(np.isnan(cycle.gait_speed) for cycle in cycles)
    # 时间参数照常可用。
    assert all(cycle.stride_time > 0 for cycle in cycles)


def test_fewer_than_two_stances_yields_no_cycles():
    """一个支撑相构不成一个周期 —— 周期需要相邻两次触地。"""
    series, _, spans = walk()
    cycles, edges = segment_cycles("L", series.t, series.acc, series.gyr, spans[:1])

    assert cycles == []
    assert len(edges) == 1


# ── 输入校验 ──────────────────────────────────────────────────────────────────


def test_a_stance_interval_out_of_range_is_rejected():
    series, _, _ = walk()

    with pytest.raises(EventError, match="越界"):
        refine_stance_edges(series.acc, series.gyr, [(0, series.t.size + 10)], CFG)


def test_mismatched_acc_and_gyr_shapes_are_rejected():
    series, _, spans = walk()

    with pytest.raises(EventError, match="形状必须一致"):
        refine_stance_edges(series.acc, series.gyr[:-5], spans, CFG)


def test_a_one_dimensional_acc_is_rejected():
    with pytest.raises(EventError, match=r"\(n,3\)"):
        refine_stance_edges(np.zeros(10), np.zeros(10), [(0, 5)], CFG)


def test_a_mismatched_time_axis_is_rejected():
    series, _, spans = walk()

    with pytest.raises(EventError, match="长度不一致"):
        segment_cycles("L", series.t[:-3], series.acc, series.gyr, spans)


def test_a_mismatched_position_is_rejected():
    series, truth, spans = walk()

    with pytest.raises(EventError, match="position"):
        segment_cycles(
            "L", series.t, series.acc, series.gyr, spans, position=truth.p[:-3]
        )


def test_summarizing_nothing_is_an_error_not_a_row_of_zeros():
    """零不是"没有数据"的答案 —— 它是一个会被当真的读数。"""
    with pytest.raises(EventError, match="没有步态周期"):
        summarize("L", [])


# ── 支撑相**区间**：触地/离地边界，不是零速时刻（RAY-325） ────────────────────
#
# 这一组守的是一件比"更准"更基本的事：`detect_stance_intervals` 量的是**脚在地上的
# 那一段**，而 `core/zupt.py` 量的是**速度为零的那一刻**。两者差一个数量级（真机实测
# 占周期 1%~16% vs 38%~56%），把后者当前者用，`double_support_fraction` 会读到
# −0.62~−0.92 —— 那不是双支撑期，是两个近似零宽的区间取重叠的算术。


def interval_errors(series, truth, cfg=None):
    """`detect_stance_intervals` 的 IC/TO 误差，ms。配对规则同 `event_errors`。"""
    cfg = cfg or CFG
    detection = detect_stance(series.acc, series.gyr, series.fs, cfg)
    edges = detect_stance_intervals(series.acc, series.fs, detection, cfg)
    ic_err, to_err, ratio = [], [], []
    for edge in edges:
        t_ic = float(series.t[edge.ic])
        t_to = float(series.t[min(edge.to, series.t.size - 1)])
        middle = 0.5 * (t_ic + t_to)
        stride = next((s for s in truth.strides if s.t_ic <= middle < s.t_ic_next), None)
        if stride is None:
            continue
        ic_err.append(1000 * (t_ic - stride.t_ic))
        to_err.append(1000 * (t_to - stride.t_to))
        ratio.append((t_to - t_ic) / (stride.t_ic_next - stride.t_ic))
    return np.array(ic_err), np.array(to_err), np.array(ratio)


@pytest.mark.parametrize("label,noise", NOISE_CASES, ids=[case[0] for case in NOISE_CASES])
@pytest.mark.parametrize("cadence", [90.0, 108.0, 125.0])
def test_the_stance_intervals_meet_the_same_twenty_millisecond_line(label, noise, cadence):
    """新路径**不放松**本模块原有的 20 ms 验收线。

    这条是整组的前提。真机上没有 IC/TO 真值（本批次无测力台、无压力垫、无录像），
    所以"这条路给出的边界是对的"只能在合成数据上说 —— 而那里必须一分不让。
    """
    series, truth = generate_walk(
        WalkSpec(duration_s=24.0, cadence=cadence, stance_ratio=0.60),
        foot="L",
        noise=noise,
    )
    ic_err, to_err, _ = interval_errors(series, truth)

    assert ic_err.size >= 10, f"{label} 只配上 {ic_err.size} 步，不足以判断"
    assert np.max(np.abs(ic_err)) < EVENT_TOLERANCE_MS
    assert np.max(np.abs(to_err)) < EVENT_TOLERANCE_MS


@pytest.mark.parametrize("stance_ratio", [0.60, 0.65])
def test_the_intervals_recover_the_stance_ratio_not_the_flat_foot_sub_phase(stance_ratio):
    """还原的是**支撑相**，不是足底平放段。

    平放段只是支撑相的一个子相 —— 承重反应期与蹬伸期都在支撑相里而脚并不平。真机上
    平放段占周期 25%~52%，支撑相占 38%~56%，差的正是这两段。只用姿态判据而不向外推，
    读出来的就是子相；而 `double_support` 读的是两足支撑相的**重叠**，每侧少算 δ 就
    让重叠少掉 2δ —— 这正是占比与双支撑期同时偏低的原因。
    """
    series, truth = generate_walk(
        WalkSpec(duration_s=24.0, cadence=108.0, stance_ratio=stance_ratio),
        foot="L",
        noise=NoiseModel.bs_bt91(seed=0),
    )
    _, _, ratio = interval_errors(series, truth)

    assert np.median(ratio) == pytest.approx(stance_ratio, abs=0.02)


def test_the_outward_reach_does_not_ask_the_foot_to_stop_turning():
    """向外推的判据里**没有** `‖ω‖` —— 这条钉住 RAY-325 的根因不再复发。

    `refine_stance_edges` 的逐样本判据带着 `‖ω‖ < zupt_gyr_threshold`（0.3 rad/s
    ≈ 17 °/s）。终末支撑相绕跖趾关节转动，真机实测 `‖ω‖` 达 **150~290 °/s** 而脚
    **仍然在地上**（`|‖a‖−g|` 全程 < 0.6 —— 传感器离转轴近，转动不产生多少线加速度）。
    要求它停下来转，就是"判据要求一个物理上不发生的状态"。

    这里造的正是那一段：比力恒等于重力，角速度 200 °/s。它必须留在支撑相里。
    """
    fs = 200.0
    still, turning = 200, 60
    acc = np.tile([0.0, 0.0, GRAVITY_STANDARD], (still + turning, 1))
    gyr = np.zeros_like(acc)
    gyr[still:, 1] = np.radians(200.0)

    quiet = (
        np.abs(np.linalg.norm(acc, axis=1) - GRAVITY_STANDARD) < CFG.zupt_acc_threshold
    ) & (np.linalg.norm(gyr, axis=1) < CFG.zupt_gyr_threshold)
    assert not quiet[still:].any(), "构造前提：旧判据把这一段判成运动"

    edges = refine_stance_edges(acc, gyr, [(0, still)], CFG)
    assert edges[0].to <= still + 1, "旧路径应当在脚开始转的地方就停下（对照）"

    from gait.analysis.events import _flat_foot_runs

    merged = _flat_foot_runs(acc, fs, CFG)
    assert merged, "姿态判据应当认出这一整段都是足底平放"
    assert merged[-1][1] >= still + turning - CFG.zupt_window_samples, (
        "转动那一段的比力仍是重力、姿态仍是平放 —— 它必须留在平放段里"
    )


def test_an_impact_is_recognised_by_its_shape_not_its_height():
    """撞击是**瞬态**；摆动相的加速度隆起是**持续的**。判据必须是形状。

    幅值分不开两者：真机触地峰 14~79 m/s²、合成步行的摆动峰 19~58 m/s²，区间重叠 ——
    3 g 那道门（`anchor_threshold_m_s2`）在慢/中档漏掉 40%~97% 的真触地。
    """
    from gait.analysis.events import _impact

    width = CFG.stance_impact_max_width_s * 200.0
    index = np.arange(120)

    spike = np.full(120, 0.1)
    spike[60:63] = [20.0, 40.0, 20.0]
    assert _impact(spike, width) == 61

    # 更**高**但更**宽**的隆起：不是撞击，宁可不动
    hump = 60.0 * np.exp(-(((index - 60) / 25.0) ** 2))
    assert _impact(hump, width) is None
    assert hump.max() > spike.max(), "构造前提：隆起比尖峰还高"


def test_one_interval_per_cycle_so_a_step_cannot_be_split_or_merged():
    """区间数由周期栅格定死，且区间升序不重叠。

    "两周期并成一个"或"一周期劈成两个"是致命失效 —— 它让左右配对整体错位，
    `same_foot_adjacencies` 与 `double_support_fraction` 一起失真。个数由构造给出
    之后，这两种失效在结构上不可能发生。
    """
    series, _truth = generate_walk(
        WalkSpec(duration_s=24.0, cadence=108.0, stance_ratio=0.60),
        foot="L",
        noise=NoiseModel.bs_bt91(seed=0),
    )
    detection = detect_stance(series.acc, series.gyr, series.fs, CFG)
    edges = detect_stance_intervals(series.acc, series.fs, detection, CFG)

    assert detection.period is not None
    assert len(edges) <= detection.period.cycles
    for current, following in pairwise(edges):
        assert current.ic < current.to <= following.ic


def test_double_support_from_the_intervals_is_positive_not_a_negative_artefact():
    """**这条守的是那个曾经恒为负的读数。**

    真机基线 −0.62~−0.92（12 格全为负）。那不是双支撑期，是拿两个跨度 15~58 ms 的
    零速时刻算重叠得到的负一个 step 时长。换成支撑相区间之后，真机 10/12 格转正
    （−0.069~+0.123），合成数据上落回生理带宽。
    """
    spec = WalkSpec(duration_s=30.0, cadence=108.0, stance_ratio=0.60)
    data = generate_dual_walk(spec)
    cycles = {}
    for foot in ("L", "R"):
        series, truth = data[foot]
        detection = detect_stance(series.acc, series.gyr, series.fs, CFG)
        edges = detect_stance_intervals(series.acc, series.fs, detection, CFG)
        # 前导要按**区间自己的**口径剔：`drop_still_lead` 的判据是"比典型支撑相长
        # 2.5 倍"，而两条路的典型支撑相差一个数量级，拿零速区间的个数去切对不上。
        typical = float(np.median([edge.samples for edge in edges]))
        while edges and edges[0].samples > 2.5 * typical:
            edges = edges[1:]
        cycles[foot], _ = segment_cycles(
            foot, series.t, series.acc, series.gyr, detection.stances,
            position=truth.p, stance_edges=edges,
        )
    report = double_support(
        cycles["L"], cycles["R"], sync_quality={"offset_estimate": 0.0, "determinate": True}
    )

    assert report.fraction > 0.0
    assert 0.10 <= report.fraction <= 0.25


def test_passing_the_edges_in_skips_the_refinement_entirely():
    """`stance_edges` 是**显式**参数：传了它，`refine_stance_edges` 就不再参与。

    两条路给出的支撑相宽度差一个数量级，调用方必须知道自己拿的是哪一条 —— 所以是
    参数而不是内部分支。
    """
    series, _truth, spans = walk()
    handmade = [
        StanceEdges(ic=start, to=stop, expanded_start=0, expanded_stop=0)
        for start, stop in spans
    ]
    cycles, edges = segment_cycles(
        "L", series.t, series.acc, series.gyr, spans, cfg=CFG, stance_edges=handmade
    )

    assert edges == handmade
    assert all(edge.expanded_start == 0 and edge.expanded_stop == 0 for edge in edges)
    assert cycles


def test_the_still_lead_is_dropped_by_the_intervals_own_scale():
    """前导按**区间自己的**时长剔，不能拿零速区间那边剩下的个数去切。

    两条路的区间数不一样（真机实测 35~42 vs 34~37）。拿一边的个数去切另一边，
    切掉的就不是前导那一段 —— 而前导是一个秒级的假支撑相，混进去会以一个秒级的
    "相位"污染双支撑期均值（RAY-296 实测 20.5% 读成 30.5%）。
    """
    from gait.cli.v3prime import _drop_lead_edges

    fs = 200.0
    t = np.arange(4000) / fs
    lead = StanceEdges(ic=0, to=400, expanded_start=0, expanded_stop=0)  # 2.0 s
    steps = [
        StanceEdges(ic=start, to=start + 60, expanded_start=0, expanded_stop=0)
        for start in range(600, 3600, 300)  # 0.3 s 一个，典型支撑相
    ]

    assert _drop_lead_edges(t, [lead, *steps], CFG) == steps
    assert _drop_lead_edges(t, steps, CFG) == steps, "没有前导时一个都不该剔"
