"""`gait.sync.integrity` 的到达率监控与数据空洞切分。

验收标准两条：**丢包能被检出并正确切段**；**切段之后逐段拟时基能恢复采样率**。

这个文件里除了那两条，还有两组测试守着实测发现的两件反直觉的事：

1. **抖动不得被当成丢包。** BLE 的排队抖动（十几毫秒）与 PRD 定的空洞阈值（3 个样本
   = 15 ms @200 Hz）是同一个量级，按相邻间隔判必然误报。判据因此建在残差的**台阶**
   上 —— 抖动让残差抖一下，丢包让它永久上移。
2. **逐秒到达率不能用来分级。** 一次 50 ms 的重传会把约 10 个样本推过秒边界，让那一
   秒读作 0.95 —— 而一个样本都没丢。实测无丢包时它最低掉到 0.94。分级因此建在空洞
   检测给出的实测丢失上，那个量不含抖动。
"""

from dataclasses import replace

import numpy as np
import pytest

from gait.config import AlgoConfig, ConfigError
from gait.sync.integrity import (
    GRADES,
    INTEGRITY_REPORT_VERSION,
    Gap,
    IntegrityError,
    assess,
    find_gaps,
    per_second_loss,
    per_second_rate,
    spans_gap,
    split_segments,
)
from gait.sync.timebase import build_timebase

#: 真实采样率。刻意不是 200.0 —— 与 `test_timebase.py` 同一个理由：器件晶振有几百 ppm
#: 偏差，而本模块的残差是**按周期累积**的，用标称值会让基线整段漂移。
FS_TRUE = 200.3
NOMINAL_FS = 200.0
BASE_LATENCY = 0.012


def simulate(
    *,
    n: int = 36000,
    per_packet: int = 4,
    drop: set[int] | None = None,
    jitter_mean: float = 0.004,
    retransmit_rate: float = 0.01,
    seed: int = 0,
) -> tuple[np.ndarray, int]:
    """模拟一台设备的到达时刻。返回 `(arrival, 真实丢失的样本数)`。

    `drop` 是要丢掉的**包**序号 —— 丢的是整个通知，不会丢半包，这与 BLE 的实际行为
    一致，也是 `find_gaps` 只在包边界上找的前提。

    到达时刻强制单调（`max(..., previous + 1e-4)`）：BLE 是有序链路，乱序只可能来自
    两台设备的样本被混进同一个数组，那是 `assess` 要拒绝的输入、不是要建模的现象。
    """
    rng = np.random.default_rng(seed)
    drop = drop or set()
    true_time = np.arange(n) / FS_TRUE
    arrivals: list[np.ndarray] = []
    lost = 0
    previous = -np.inf
    for packet, start in enumerate(range(0, n, per_packet)):
        stop = min(start + per_packet, n)
        if packet in drop:
            lost += stop - start
            continue
        latency = BASE_LATENCY + rng.exponential(jitter_mean)
        if rng.random() < retransmit_rate:
            latency += 0.05
        moment = max(true_time[stop - 1] + latency, previous + 1e-4)
        previous = moment
        # 一包里的样本几乎同时到达 —— 微秒级的递增只是为了保持严格单调。
        arrivals.append(moment + 1e-6 * np.arange(stop - start))
    return np.concatenate(arrivals), lost


# ── 验收标准一：丢包能被检出并正确切段 ────────────────────────────────────────


@pytest.mark.parametrize(
    ("per_packet", "drop", "expected_gaps"),
    [
        (4, {1000}, 1),
        (12, {1000}, 1),
        (4, set(range(2000, 2005)), 1),  # 连续 5 包 → 一处空洞，不是五处
        (8, {500, 3000}, 2),
    ],
)
def test_injected_packet_loss_is_detected_and_counted_exactly(per_packet, drop, expected_gaps):
    """检出的空洞数与丢失样本数都必须**一个不差**。

    "估计丢失"这个名字来自它的算法（时间差除以周期，带一个采样的量化误差），不是来自
    容忍度：在 4/12/20 个样本这些量级上，量化误差还不足以改变整数结果。分级建在这个
    量上，正是因为它这么准。
    """
    arrival, lost = simulate(per_packet=per_packet, drop=drop)
    report = assess(arrival, NOMINAL_FS)

    assert len(report.gaps) == expected_gaps
    assert report.lost_samples == lost


def test_consecutive_lost_packets_are_one_gap_not_many():
    """连丢 5 包是**一处**空洞。

    这不是省事：段的意义是"这中间没有时间断裂"，而连丢的 5 包中间没有任何收到的样本，
    切成 5 段会凭空造出 4 个空段。
    """
    arrival, _ = simulate(drop=set(range(2000, 2005)))
    gaps = find_gaps(arrival, NOMINAL_FS)

    assert len(gaps) == 1
    assert gaps[0].after == gaps[0].before + 1


def test_a_gap_is_reported_once_not_once_per_following_packet():
    """一处空洞只报一次 —— 台阶是**永久**的，检出之后必须让开一个窗口。

    第一版没有让开：台阶之后的一整个窗口里，`before` 窗口仍然含着台阶之前的低残差，
    步长看起来始终为正，于是一个丢了 4 个样本的包被报成 **19 处空洞、估计丢 76 个**。
    这个测试守的就是那个回归。
    """
    arrival, lost = simulate(drop={1000})
    gaps = find_gaps(arrival, NOMINAL_FS)

    assert len(gaps) == 1
    assert sum(gap.estimated_lost for gap in gaps) == lost


def test_segments_tile_the_whole_series_without_dropping_samples():
    """段必须**首尾相接**地铺满 `[0, samples)`。

    契约的 `FootSeries.segments` 与 `core/eskf.run_ins` 都要求段覆盖整个序列。切分不
    丢样本：空洞里的样本本来就不在数组里，段的并集就是全部收到的样本。
    """
    arrival, _ = simulate(drop={500, 3000, 6000})
    report = assess(arrival, NOMINAL_FS)

    assert report.segments[0][0] == 0
    assert report.segments[-1][1] == report.received
    for (_, stop), (start, _) in zip(report.segments[:-1], report.segments[1:], strict=True):
        assert stop == start
    assert sum(stop - start for start, stop in report.segments) == report.received


def test_split_segments_without_gaps_is_a_single_segment():
    assert split_segments(100, []) == [(0, 100)]


def test_split_segments_of_an_empty_series_is_empty():
    assert split_segments(0, []) == []


# ── 验收标准二：切段之后逐段拟时基能恢复采样率 ────────────────────────────────


def test_fitting_the_timebase_per_segment_recovers_the_sampling_rate():
    """这是本模块存在的**理由**，也是"先切分、再拟时基"这个顺序的证明。

    丢包打断了样本序号与真实采样时刻的对应，于是 RAY-209 的回归量到的是**到达率**而
    不是器件的采样率。整段拟合因此有 −0.23% 的偏差；按本模块切出来的段分别拟合，偏差
    回到 1e-4% 量级 —— 好了三个数量级。
    """
    arrival, _ = simulate(drop=set(range(2000, 2020)))
    report = assess(arrival, NOMINAL_FS)

    whole = build_timebase(arrival, NOMINAL_FS).report
    assert abs(whole.fs - FS_TRUE) / FS_TRUE > 1e-3

    long_segments = [(a, b) for a, b in report.segments if b - a >= 4000]
    assert len(long_segments) >= 2
    for start, stop in long_segments:
        piece = build_timebase(arrival[start:stop], NOMINAL_FS).report
        assert abs(piece.fs - FS_TRUE) / FS_TRUE < 1e-4


# ── 抖动不得被当成丢包 ────────────────────────────────────────────────────────


@pytest.mark.parametrize("jitter_mean", [0.002, 0.004, 0.008])
def test_jitter_alone_never_produces_a_gap(jitter_mean):
    """零误报是硬要求。

    抖动均值 8 ms 已经超过 PRD 的空洞阈值（3 个样本 = 15 ms）的一半，而单次抖动的
    尾部远超它 —— 按相邻间隔判在这里必然误报。判据建在台阶上才过得去。
    """
    arrival, lost = simulate(jitter_mean=jitter_mean)
    assert lost == 0

    report = assess(arrival, NOMINAL_FS)
    assert report.gaps == []
    assert report.segments == [(0, report.received)]
    assert report.grade == "normal"


def test_a_retransmit_burst_is_not_a_gap():
    """5% 的包各迟到 50 ms —— 一个样本都没丢，就一处空洞都不许报。

    50 ms @200 Hz 是 10 个样本的时间，是空洞阈值的三倍多。它之所以不算空洞，是因为
    积压随即排空：残差抖一下就回来了，没有台阶。
    """
    arrival, lost = simulate(retransmit_rate=0.05)
    assert lost == 0

    assert find_gaps(arrival, NOMINAL_FS) == []


def test_a_slow_crystal_does_not_drift_the_baseline_into_false_gaps():
    """器件跑在 190 Hz 而标称 200 Hz，也不许报空洞。

    残差是按周期累积的：用标称值算，10 Hz 的偏差会让残差每秒漂 0.05 s，几秒之内就
    盖过任何台阶。所以周期取的是逐包时差的**中位数**（丢包只影响少数包，中位数看不见
    它们），而不是标称值。
    """
    n = 12000
    per_packet = 4
    fs_slow = 190.0
    rng = np.random.default_rng(3)
    arrivals = []
    previous = -np.inf
    for start in range(0, n, per_packet):
        stop = min(start + per_packet, n)
        moment = max((stop - 1) / fs_slow + BASE_LATENCY + rng.exponential(0.004), previous + 1e-4)
        previous = moment
        arrivals.append(moment + 1e-6 * np.arange(stop - start))
    arrival = np.concatenate(arrivals)

    assert find_gaps(arrival, NOMINAL_FS) == []


# ── 逐秒到达率不能用来分级 ────────────────────────────────────────────────────


def test_the_per_second_rate_dips_below_the_warn_threshold_with_zero_loss():
    """**这个测试记录的是一个缺陷，不是一个特性。**

    它证明逐秒到达率含抖动：5% 重传率下，一个样本都没丢，却有若干秒读到 0.98 以下、
    最低到 0.94 —— 一次 50 ms 的重传把约 10 个样本推过了秒边界。

    所以分级不能建在它上面。它留在报告里是为了**定位**（哪一秒链路忙），而 PRD §6.1
    要的"到达率逐秒监控"这句话本身也指的是它。
    """
    arrival, lost = simulate(retransmit_rate=0.05)
    assert lost == 0

    rates = per_second_rate(arrival, NOMINAL_FS)
    assert rates.min() < AlgoConfig().integrity_rate_warn


def test_the_grade_is_normal_when_nothing_was_lost_however_bad_the_jitter():
    """同一份数据，分级必须是 normal —— 因为分级看的是实测丢失。"""
    for retransmit_rate in (0.0, 0.01, 0.05):
        for jitter_mean in (0.002, 0.008):
            arrival, lost = simulate(retransmit_rate=retransmit_rate, jitter_mean=jitter_mean)
            assert lost == 0
            report = assess(arrival, NOMINAL_FS)
            assert report.grade == "normal"
            assert report.worst_second_loss == 0


def test_per_second_loss_puts_a_gap_in_the_second_it_started():
    arrival, _ = simulate(drop={1000})
    gaps = find_gaps(arrival, NOMINAL_FS)
    losses = per_second_loss(arrival, gaps, NOMINAL_FS, seconds=200)

    assert losses.sum() == 4
    # 包 1000 × 4 个样本/包 = 样本 4000，@200.3 Hz 约在第 19~20 秒。
    assert 18 <= int(np.argmax(losses)) <= 21


def test_per_second_loss_of_a_clean_session_is_all_zero():
    arrival, _ = simulate()
    losses = per_second_loss(arrival, [], NOMINAL_FS, seconds=180)

    assert losses.sum() == 0


# ── 分级 ──────────────────────────────────────────────────────────────────────


def _synthetic_losses(worst: int, seconds: int = 60) -> np.ndarray:
    losses = np.zeros(seconds, dtype=np.int64)
    losses[10] = worst
    return losses


@pytest.mark.parametrize(
    ("worst_second_samples", "expected"),
    [
        (0, "normal"),
        (4, "normal"),  # 4/200 = 2%，接收率正好 0.98 —— 阈值是"低于"才降级
        (5, "degraded"),
        (20, "degraded"),  # 20/200 = 10%，接收率正好 0.90 —— 同上
        (21, "unusable"),
    ],
)
def test_the_grade_boundaries_are_decided_by_the_received_fraction(worst_second_samples, expected):
    """边界比的是**接收率**，不是丢失率。

    写成 `丢失率 > 1 − 阈值` 会在边界上被浮点表示翻转：`1.0 - 0.90` 在 float 里是
    0.09999999999999998，于是"正好丢 10%"判成 unusable。实测撞上过这个，所以边界值
    进了参数表。
    """
    from gait.sync.integrity import _grade

    grade = _grade(0.0, _synthetic_losses(worst_second_samples), NOMINAL_FS, AlgoConfig())
    assert grade == expected


def test_loss_spread_thin_across_the_session_still_trips_the_overall_line():
    """均匀分布的丢包每一秒都不越线，但总量越线 —— 总体那条线就是为它准备的。"""
    from gait.sync.integrity import _grade

    losses = np.full(600, 2, dtype=np.int64)  # 每秒丢 2 个 = 1%，逐秒都过得去
    assert _grade(0.01, losses, NOMINAL_FS, AlgoConfig()) == "normal"
    assert _grade(0.05, losses, NOMINAL_FS, AlgoConfig()) == "degraded"


def test_every_grade_is_one_of_the_declared_values():
    for drop in (set(), {1000}, set(range(2000, 2060))):
        arrival, _ = simulate(drop=drop)
        assert assess(arrival, NOMINAL_FS).grade in GRADES


# ── 报告 ──────────────────────────────────────────────────────────────────────


def test_the_snapshot_is_plain_json_types():
    """`SessionMeta.integrity_report` 要能直接序列化 —— numpy 标量进不去 JSON。"""
    import json

    arrival, _ = simulate(drop={1000})
    snapshot = assess(arrival, NOMINAL_FS).snapshot()

    text = json.dumps(snapshot, ensure_ascii=False)
    assert json.loads(text)["version"] == INTEGRITY_REPORT_VERSION
    assert isinstance(snapshot["gaps"][0]["estimated_lost"], int)
    assert all(isinstance(value, float) for value in snapshot["per_second_rate"])
    assert all(isinstance(value, int) for value in snapshot["per_second_loss"])


def test_the_snapshot_keeps_the_per_second_arrays():
    """PRD §6.1 要的是"逐秒监控"。只存一个总体到达率等于把那句话做掉一半。"""
    arrival, _ = simulate()
    snapshot = assess(arrival, NOMINAL_FS).snapshot()

    assert len(snapshot["per_second_rate"]) > 100
    assert len(snapshot["per_second_loss"]) == len(snapshot["per_second_rate"])


def test_the_overall_rate_may_exceed_one_because_the_crystal_is_fast():
    """器件实跑 200.3 Hz 而标称 200 Hz，到达率就该读到 1.0016 —— 不截断。

    截断会把真实的晶振偏差藏起来，而那个偏差正是 RAY-209 的时基要量的东西。
    """
    arrival, _ = simulate()
    report = assess(arrival, NOMINAL_FS)

    assert report.overall_rate > 1.0
    assert report.overall_rate == pytest.approx(FS_TRUE / NOMINAL_FS, rel=1e-3)


def test_longest_segment_fraction_says_how_much_usable_data_is_left():
    """段数说明不了还剩多少可用数据 —— 丢在第 1 秒和丢在正中间，段数都是 2。"""
    early, _ = simulate(per_packet=8, drop={100})
    middle, _ = simulate(per_packet=8, drop={2250})

    assert len(assess(early, NOMINAL_FS).segments) == 2
    assert len(assess(middle, NOMINAL_FS).segments) == 2
    assert assess(early, NOMINAL_FS).longest_segment_fraction > 0.9
    assert assess(middle, NOMINAL_FS).longest_segment_fraction == pytest.approx(0.5, abs=0.05)


# ── 空洞跨越判断（RAY-216 的步态周期要用）────────────────────────────────────


def test_a_cycle_inside_one_segment_does_not_span_a_gap():
    segments = [(0, 1000), (1000, 2000)]
    assert not spans_gap(100, 200, segments)


def test_a_cycle_ending_exactly_on_a_segment_boundary_does_not_span_a_gap():
    """半开区间的边界情形：`stop == 段的终点` 时每个样本都还在同一段里。"""
    segments = [(0, 1000), (1000, 2000)]
    assert not spans_gap(900, 1000, segments)


def test_a_cycle_crossing_a_segment_boundary_spans_a_gap():
    """PRD §6.1：「空洞跨越的步态周期标记 invalid」。"""
    segments = [(0, 1000), (1000, 2000)]
    assert spans_gap(999, 1001, segments)


def test_an_empty_interval_is_rejected_rather_than_answered():
    segments = [(0, 1000)]
    with pytest.raises(IntegrityError, match="非空"):
        spans_gap(500, 500, segments)


# ── 输入校验 ──────────────────────────────────────────────────────────────────


def test_out_of_order_arrivals_are_rejected():
    """逆序只可能来自两台设备的样本被混进同一个数组 —— 那是调用方的 bug。"""
    arrival = np.array([0.0, 0.01, 0.005, 0.02])
    with pytest.raises(IntegrityError, match="单调"):
        assess(arrival, NOMINAL_FS)


def test_a_two_dimensional_input_is_rejected():
    with pytest.raises(IntegrityError, match="一维"):
        assess(np.zeros((4, 2)), NOMINAL_FS)


def test_a_single_sample_cannot_have_an_arrival_rate():
    with pytest.raises(IntegrityError, match="2 个样本"):
        assess(np.array([0.0]), NOMINAL_FS)


def test_a_non_positive_nominal_fs_is_rejected():
    with pytest.raises(IntegrityError, match="nominal_fs"):
        assess(np.array([0.0, 0.005]), 0.0)


def test_per_second_rate_of_a_sub_second_series_is_empty():
    """不满一秒没有"逐秒"可言，返回空数组而不是一个分母不对的比率。"""
    assert per_second_rate(np.linspace(0.0, 0.5, 100), NOMINAL_FS).size == 0


# ── 配置 ──────────────────────────────────────────────────────────────────────


def test_the_gap_threshold_must_be_at_least_one_sample():
    """取 0 表示任何一个采样周期的空白都算空洞，而 BLE 抖动本来就有那个量级。"""
    with pytest.raises(ConfigError, match="integrity_gap_samples"):
        replace(AlgoConfig(), integrity_gap_samples=0)


def test_the_unusable_threshold_must_be_stricter_than_the_warn_threshold():
    """反过来会让 degraded 永远比 unusable 更严，分级失去意义。"""
    with pytest.raises(ConfigError, match="unusable < warn"):
        replace(AlgoConfig(), integrity_rate_warn=0.80, integrity_rate_unusable=0.95)


def test_the_gap_threshold_is_three_samples_because_the_prd_says_so():
    """PRD §6.1 写死的是 3。改这个数要改 PRD，不是改代码。"""
    assert AlgoConfig().integrity_gap_samples == 3


def test_a_larger_gap_threshold_lets_a_small_loss_through():
    """阈值是可调的，且方向必须是对的 —— 调高就该漏掉小的空洞。"""
    arrival, _ = simulate(drop={1000})
    assert len(find_gaps(arrival, NOMINAL_FS)) == 1

    lenient = replace(AlgoConfig(), integrity_gap_samples=8)
    assert find_gaps(arrival, NOMINAL_FS, lenient) == []


# ── Gap ───────────────────────────────────────────────────────────────────────


def test_a_gap_has_no_index_for_the_lost_samples():
    """无序号硬件下"只能检测不能定位"的直接体现：`before` 与 `after` 相邻。

    丢失的样本在数组里根本不存在，中间没有任何索引可以指向它们 —— 这正是 PRD §6.1
    说"绝不插值续算"的原因：连补在哪里都不知道。
    """
    arrival, _ = simulate(drop={1000})
    gap = find_gaps(arrival, NOMINAL_FS)[0]

    assert gap.after == gap.before + 1
    assert gap.estimated_lost > 0


def test_gap_duration_is_the_elapsed_time():
    gap = Gap(before=10, after=11, elapsed=0.03, estimated_lost=6)
    assert gap.duration == 0.03
