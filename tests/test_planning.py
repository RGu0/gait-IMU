"""RAY-328 `dual-foot-qc-windowing`：跨脚周期校验（判据 1）与净窗宽闸（判据 2）。

两条判据的共同背景是**同一份数据要过两道精度完全不同的闸**：步态参数要在时间轴上
做微分，宽闸给它用就过松；周期规划只要数出"有几个周期、每个从哪到哪"，严闸给它用
就过严 —— 实测整个 S1-sport 被 `timebase_trustworthy` 判为不可信，而它的周期估计好
得很。所以这里立第二道闸，并且用测试钉住**第一道一个字都没动**。

判据 1 的数据全部来自 2026-09-02 的可行性实测（`evidence/ray-328/feasibility/`，
24 格 = 2 鞋型 × 6 趟 × 2 脚）。测试不重跑那次采集 —— 它钉的是"这些实测周期喂进
判据，落点必须是这样"，而这正是 R2 定阈 1.15 时看的那张表。
"""

from dataclasses import replace

import numpy as np
import pytest

from gait.analysis.planning import FootPlanInput, plan_periods
from gait.config import AlgoConfig, ConfigError
from gait.core.dualfoot import DualFootError, check_cross_foot_period
from gait.core.zupt import PeriodReport
from gait.sync.integrity import assess
from gait.sync.planning import (
    PlanningError,
    cycle_is_net,
    net_window,
    plan_dual_net_window,
)
from gait.sync.timebase import build_timebase

NOMINAL_FS = 200.0

#: 2026-09-02 可行性实测的 12 趟，逐趟 `(鞋型, 速度档, T_L, T_R, fs_L, fs_R,
#: 单脚一致_L, 单脚一致_R)`。周期是秒，fs 是**实测**采样率 —— 两脚的 fs 差到 1.1%，
#: 而要判的阈只有 1.15，所以换算必须各用各的。
MEASURED = [
    ("S1-sport", "slow-a", 2.7856232321, 2.7853540541, 198.1603232043, 196.0253488064, True, True),
    ("S1-sport", "slow-b", 3.0769319963, 2.7672599690, 198.5744243744, 198.3911906183, True, False),
    ("S1-sport", "mid-a", 1.5926650061, 1.7575164178, 198.4095831717, 198.5756698885, True, True),
    ("S1-sport", "mid-b", 1.5611208068, 1.6820070345, 198.5752791495, 198.5722967615, True, True),
    ("S1-sport", "fast-a", 0.9971557336, 1.0071879562, 198.5647711145, 198.5726683528, True, True),
    ("S1-sport", "fast-b", 1.0273172043, 1.0071931244, 198.5754732323, 198.5716494163, True, True),
    ("S1-flat", "slow-a", 3.7920343056, 3.2347504152, 198.5741528979, 198.4697171613, True, True),
    ("S1-flat", "slow-b", 3.3740695049, 3.4143387748, 198.5732656137, 198.5743198683, True, True),
    ("S1-flat", "mid-a", 2.3735232687, 2.4172520747, 198.4391752977, 198.5725878644, True, True),
    ("S1-flat", "mid-b", 1.9035938750, 1.8330880967, 198.5717673133, 198.5720166214, True, False),
    ("S1-flat", "fast-a", 1.1079384381, 1.2036194496, 198.5669893130, 198.5677450478, False, True),
    ("S1-flat", "fast-b", 1.1733927104, 1.2439151259, 198.5694967528, 198.5666022216, False, True),
]

#: 12 趟里唯一必须被抓出的那一格。单脚一致性闸放过了它（比值 1.277 < 1.3），
#: 而它的 T_L 估成 3.79 s，真值约 3.06 s。
TRUE_POSITIVE = ("S1-flat", "slow-a")

#: 紧贴阈下的两格。它们是 R2 把阈从 1.10 抬到 1.15 的**全部**理由 —— 1.10 会把这两
#: 格一起标记，而 R1 自己又要求钉住它们不被标记。
NEAR_MISSES = {("S1-sport", "slow-b"), ("S1-sport", "mid-a")}


def period(period_s: float, fs: float, *, consistent: bool = True) -> PeriodReport:
    """把"周期是几秒"包成一份 `PeriodReport`。

    `bounds` 与 `estimates` 给最省的合法值：跨脚校验只读 `period_samples` 与
    `consistent`，别的字段进来是为了让这份报告是**真的** `PeriodReport` 而不是一个
    长得像它的替身 —— 替身会在字段改名时静静地继续通过。
    """
    samples = period_s * fs
    return PeriodReport(
        period_samples=samples,
        cycles=3,
        estimates=(("autocorrelation", samples),),
        ratio=1.0,
        consistent=consistent,
        bounds=((0, round(samples)), (round(samples), round(2 * samples)),
                (round(2 * samples), round(3 * samples))),
    )


def arrivals(
    *, duration_s: float = 60.0, start: float = 0.0, fs: float = 200.3, drop: list[tuple[float, float]] | None = None
) -> np.ndarray:
    """一条干净的到达时刻序列，可按秒挖掉若干段。

    真实采样率刻意不是标称的 200.0（与 `test_integrity.py` 同一个理由：晶振有几百
    ppm 偏差）。挖掉的段用**秒**给，因为净窗判的就是时刻。
    """
    times = start + np.arange(int(duration_s * fs)) / fs
    for lo, hi in drop or []:
        times = times[(times < lo) | (times > hi)]
    return times


# ── 判据 1：跨脚校验落地 ──────────────────────────────────────────────────────


@pytest.mark.parametrize(("trial", "walk", "t_left", "t_right", "fs_left", "fs_right", "ok_left", "ok_right"), MEASURED)
def test_measured_cells_land_where_r2_says_they_do(
    trial, walk, t_left, t_right, fs_left, fs_right, ok_left, ok_right
):
    """24 格上：只有 S1-flat/slow-a 越阈，紧贴阈下的两格一个都不误伤。

    这是判据 1 的正文。三个断言（越阈的是它、不越阈的不是它、比值免方向）分开写会
    让每一格跑三遍，而它们说的是同一件事：**阈 1.15 把这 12 个点分成 1 + 11**。
    """
    check = check_cross_foot_period(
        period(t_left, fs_left, consistent=ok_left),
        fs_left,
        period(t_right, fs_right, consistent=ok_right),
        fs_right,
    )
    assert check is not None
    assert check.agrees is ((trial, walk) != TRUE_POSITIVE)
    if (trial, walk) in NEAR_MISSES:
        # 钉住"紧贴"本身：它们必须在 1.10 之上、1.15 之下。上界没了就不叫紧贴，
        # 下界没了就说明 R1 的 1.10 本来是对的，而 R2 的整个理由就不成立。
        assert 1.10 < check.ratio < 1.15


def test_the_cross_foot_gate_says_something_the_single_foot_gate_did_not():
    """S1-flat/slow-a 是**新增**信息：两脚各自自洽，但彼此对不上。

    这一格的价值全在这里。若跨脚闸抓出来的每一格单脚闸都已经标过，这道闸就是零收益
    的重复劳动，而那正是"多一道判据"最常见的失败方式。
    """
    _, _, t_left, t_right, fs_left, fs_right, ok_left, ok_right = next(
        row for row in MEASURED if (row[0], row[1]) == TRUE_POSITIVE
    )
    check = check_cross_foot_period(
        period(t_left, fs_left, consistent=ok_left), fs_left,
        period(t_right, fs_right, consistent=ok_right), fs_right,
    )
    assert check.new_information is True


def test_exactly_one_of_the_twelve_trials_is_flagged():
    """整张表上越阈的格数恰好是 1。

    逐格测试保证了每一格的落点，这一条保证的是**没有第十三格** —— 若哪天有人往
    `MEASURED` 里加一行而忘了它的落点，逐格测试照样全绿，这条会红。
    """
    flagged = [
        (trial, walk)
        for trial, walk, t_l, t_r, fs_l, fs_r, ok_l, ok_r in MEASURED
        if not check_cross_foot_period(
            period(t_l, fs_l, consistent=ok_l), fs_l, period(t_r, fs_r, consistent=ok_r), fs_r
        ).agrees
    ]
    assert flagged == [TRUE_POSITIVE]


def test_the_ratio_has_no_direction():
    """左右对调，比值一个比特不变。

    有方向就会诱使调用方去读"哪只脚有问题"，而这个量分不出来 —— 两个周期估计里跑掉
    的是哪一个，跨脚比值里没有这个信息。
    """
    forward = check_cross_foot_period(period(3.0, 200.0), 200.0, period(2.5, 200.0), 200.0)
    backward = check_cross_foot_period(period(2.5, 200.0), 200.0, period(3.0, 200.0), 200.0)
    assert forward.ratio == backward.ratio


def test_each_foot_is_converted_with_its_own_sampling_rate():
    """两脚 fs 不同时，换算必须各用各的。

    同样 600 个样本的周期，在 200 Hz 与 190 Hz 下是 3.00 s 与 3.16 s —— 5.3% 的差。
    共用一个 fs 会把这个差整个记到跨脚比值上，而阈只有 1.15，那是把三分之一的余量
    白送掉。
    """
    check = check_cross_foot_period(period(3.0, 200.0), 200.0, period(3.0, 190.0), 190.0)
    assert check.ratio == pytest.approx(1.0)
    assert check.left_period_s == pytest.approx(check.right_period_s)


def test_a_foot_without_a_period_abstains_rather_than_agrees():
    """任一脚没有周期 → `None`，不是 `agrees=True`。

    "两脚都没估出周期"与"两脚完全一致"对下游的意思完全相反，用同一个值表示会让前者
    看起来像后者。
    """
    assert check_cross_foot_period(None, 200.0, period(3.0, 200.0), 200.0) is None
    assert check_cross_foot_period(period(3.0, 200.0), 200.0, None, 200.0) is None
    assert check_cross_foot_period(None, 200.0, None, 200.0) is None


@pytest.mark.parametrize("fs", [0.0, -1.0])
def test_a_non_positive_sampling_rate_is_refused(fs):
    """fs ≤ 0 直接拒。它会让"样本换秒"这一步给出无穷或负的周期，而那之后的比值仍然
    是个有限数 —— 一个看起来正常的错误结论比一个异常更难发现。"""
    with pytest.raises(DualFootError):
        check_cross_foot_period(period(3.0, 200.0), fs, period(3.0, 200.0), 200.0)


def test_the_threshold_travels_with_the_verdict():
    """报告里带着当时用的阈。阈会随数据演进，而一份历史报告要能自证它是按哪个数判的。"""
    cfg = replace(AlgoConfig(), cross_foot_period_ratio_max=1.30)
    check = check_cross_foot_period(period(3.0, 200.0), 200.0, period(2.5, 200.0), 200.0, cfg)
    assert check.threshold == 1.30
    assert check.agrees is True  # 1.2 < 1.30
    assert check.snapshot()["threshold"] == 1.30


def test_a_threshold_of_one_is_refused():
    """阈取 1 等于要求两脚周期逐比特相同，那会把每一趟都标成降级，戳就不再有信息。"""
    with pytest.raises(ConfigError):
        replace(AlgoConfig(), cross_foot_period_ratio_max=1.0)


# ── 判据 1 的反例：只加票，不否决 ────────────────────────────────────────────


def test_a_cross_foot_flag_marks_but_never_drops():
    """单脚一致 + 跨脚超阈 → 降级，但净窗、覆盖率、周期估计一个都不变。

    这是判据 6 点名要的反例。它防的是一种很自然的"改进"：既然两脚对不上，就把这一
    趟从规划里剔掉。剔掉的代价是**系统性偏向病理步态** —— T_L ≠ T_R 在偏瘫、疼痛
    回避、假肢上是真实现象，而没有数据能把它与"估计跑掉了"分开。
    """
    clean = arrivals(duration_s=60.0)
    baseline = plan_periods(
        FootPlanInput(arrival=clean, period=period(3.0, 200.0), fs=200.0),
        FootPlanInput(arrival=clean, period=period(3.0, 200.0), fs=200.0),
        NOMINAL_FS,
    )
    flagged = plan_periods(
        FootPlanInput(arrival=clean, period=period(3.8, 200.0), fs=200.0),
        FootPlanInput(arrival=clean, period=period(3.0, 200.0), fs=200.0),
        NOMINAL_FS,
    )

    assert baseline.degraded is False
    assert flagged.degraded is True
    # 降级了，但数据一点没少：可规划性、净窗、覆盖率逐一相同。
    assert flagged.plannable is baseline.plannable is True
    assert flagged.window.net == baseline.window.net
    assert flagged.window.coverage == baseline.window.coverage
    # 而且两脚各自的周期原样带出，没有被"修正"成一致。
    assert flagged.cross_foot.left_period_s == pytest.approx(3.8)
    assert flagged.cross_foot.right_period_s == pytest.approx(3.0)


def test_plannable_and_degraded_are_orthogonal():
    """宽闸的结论与跨脚的一票互不干涉，`plannable=True, degraded=True` 是正常输出。"""
    clean = arrivals(duration_s=60.0)
    plan = plan_periods(
        FootPlanInput(arrival=clean, period=period(3.8, 200.0), fs=200.0),
        FootPlanInput(arrival=clean, period=period(3.0, 200.0), fs=200.0),
        NOMINAL_FS,
    )
    assert (plan.plannable, plan.degraded) == (True, True)
    assert plan.snapshot()["degraded"] is True
    assert plan.snapshot()["coverage"] == plan.window.coverage


# ── 判据 2：宽闸生效 ─────────────────────────────────────────────────────────


def test_coverage_is_always_reported():
    """覆盖率是必出字段，不是调试信息。PRD §6.1 要求上报，判据 2 明写"输出必须含"。"""
    clean = arrivals(duration_s=60.0)
    window = plan_dual_net_window(clean, clean, NOMINAL_FS)
    assert window.coverage == pytest.approx(1.0)
    assert "coverage" in window.snapshot()
    assert window.snapshot()["coverage"] == window.coverage


def test_a_hole_in_one_foot_leaves_the_dual_window():
    """一只脚的空洞把那段时间从**交集**里去掉，另一只脚完好也不行。

    净窗的定义是"两只脚都完整"，不是"至少一只脚完整"。周期规划要用两脚的信息，缺一
    只的那段时间做不了跨脚的任何事。
    """
    clean = arrivals(duration_s=60.0)
    holed = arrivals(duration_s=60.0, drop=[(20.0, 21.0)])
    window = plan_dual_net_window(holed, clean, NOMINAL_FS)

    assert window.left.gaps == 1
    assert window.right.gaps == 0
    assert not cycle_is_net(20.2, 21.2, window)
    assert cycle_is_net(5.0, 8.0, window)


def test_the_guard_band_widens_the_hole_on_both_sides():
    """保护带按 `planning_gap_guard_s` 向两侧展开，缺口的实际宽度可核。

    需要它是因为 BLE 按连接事件成簇送达：空洞前后那几个样本是迟到扎堆的一簇，到达
    时刻挤在一起，拿它们做时间推断都偏。
    """
    holed = arrivals(duration_s=60.0, drop=[(20.0, 21.0)])
    clean = arrivals(duration_s=60.0)
    wide = plan_dual_net_window(holed, clean, NOMINAL_FS)
    narrow = plan_dual_net_window(
        holed, clean, NOMINAL_FS, cfg=replace(AlgoConfig(), planning_gap_guard_s=0.01)
    )
    # 保护带从 10 ms 加到 100 ms，每处空洞两侧各多挖 90 ms。
    lost = (wide.span[1] - wide.span[0]) * (narrow.coverage - wide.coverage)
    assert lost == pytest.approx(2 * 0.09, abs=0.02)


def test_an_unusable_foot_is_refused_by_name():
    """`assess` 判 unusable 的脚，宽闸照拒 —— 这一条严闸宽闸完全一致。

    宽闸放松的**只有** `SyncReport.stable` 那一条，因为它量的正是"微分够不够准"。
    `unusable` 是 `assess` 自己定义的"不可用"，与用途无关，没有理由为周期规划改它。
    """
    clean = arrivals(duration_s=60.0)
    shredded = arrivals(duration_s=60.0, drop=[(t, t + 0.4) for t in range(5, 55)])
    assert assess(shredded, NOMINAL_FS).grade == "unusable"

    window = plan_dual_net_window(shredded, clean, NOMINAL_FS)
    assert "left_unusable" in window.refusals
    assert window.plannable is False
    # 拒了也照样给净窗与覆盖率：调用方要看得出拒的是哪一条。
    assert window.coverage == window.snapshot()["coverage"]


def test_the_wide_gate_does_not_consult_timebase_stability():
    """时基**不稳**但没有空洞的采集，宽闸照过 —— 这就是判据 2 的整个要点。

    实测里被严闸整体判为不可信的 S1-sport，周期估计好得很，24 格里它贡献了 5 个
    覆盖率 ≥99% 的格。这条测试先确认这份输入确实过不了严闸（`stable is False`），
    再确认宽闸放它过去；少了前半句，后半句证明不了任何事。
    """
    # 采样率中途换挡：前 30 s 跑 200.3 Hz，后 30 s 掉到 195 Hz。**必须是非线性的** ——
    # 在一条直线上再叠一条直线还是直线，分窗拟合出来的 fs 一模一样，闸照样判稳。
    first = np.arange(int(30 * 200.3)) / 200.3
    second = first[-1] + np.arange(1, int(30 * 195.0) + 1) / 195.0
    unstable = np.concatenate([first, second])

    assert build_timebase(unstable, NOMINAL_FS).report.stable is False
    assert assess(unstable, NOMINAL_FS).grade != "unusable"

    window = plan_dual_net_window(unstable, unstable, NOMINAL_FS)
    assert window.plannable is True
    assert window.coverage == pytest.approx(1.0)


def test_coverage_below_the_floor_is_refused_explicitly_not_truncated():
    """覆盖率不够时，理由具名，净窗照给。**无静默截断**。"""
    clean = arrivals(duration_s=60.0)
    holed = arrivals(duration_s=60.0, drop=[(t, t + 0.3) for t in range(10, 40, 2)])
    window = plan_dual_net_window(holed, clean, NOMINAL_FS)

    assert window.coverage < window.minimum_coverage
    assert "coverage_below_minimum" in window.refusals
    assert window.net  # 净窗仍然完整地返回，没有被清空
    assert window.longest_run_s > 0.0


def test_feet_that_do_not_overlap_are_refused_for_that_reason():
    """两脚采集时间不重叠 → `no_common_span`，不是"覆盖率低"。

    补救完全不同：一个要重采，一个只是这一趟质量差。合并成一个理由会让操作员按错的
    那条去改。
    """
    window = plan_dual_net_window(
        arrivals(duration_s=30.0, start=0.0),
        arrivals(duration_s=30.0, start=100.0),
        NOMINAL_FS,
    )
    assert window.refusals[-1] == "no_common_span"
    assert window.net == ()
    assert window.coverage == 0.0


def test_the_longest_run_is_reported_next_to_coverage():
    """同样的覆盖率，碎成很多段与只缺一处，可用性完全不同。"""
    clean = arrivals(duration_s=60.0)
    # 两边挖掉的总时长刻意做成几乎相等（3.0 s 一处 vs 0.1 s 十处，各带 ±0.1 s 保护带），
    # 好让覆盖率给不出区别 —— 那正是需要第二个数的时候。
    one_hole = plan_dual_net_window(arrivals(duration_s=60.0, drop=[(30.0, 33.0)]), clean, NOMINAL_FS)
    many_holes = plan_dual_net_window(
        arrivals(duration_s=60.0, drop=[(t, t + 0.1) for t in range(5, 55, 5)]), clean, NOMINAL_FS
    )
    assert abs(many_holes.coverage - one_hole.coverage) < 0.01
    assert many_holes.longest_run_s < 0.5 * one_hole.longest_run_s


def test_a_cycle_must_fall_entirely_inside_the_net_window():
    """跨过空洞的周期整个作废，不做"大部分落入"的宽容。

    PRD §6.1「空洞跨越的步态周期标记 invalid」。一个跨过空洞的周期，它的边界与内部
    极值分别落在空洞两侧，而两侧之间隔着未知长的时间 —— 它测的那个量根本不存在，
    不是"精度差一点"。
    """
    clean = arrivals(duration_s=60.0)
    window = plan_dual_net_window(arrivals(duration_s=60.0, drop=[(30.0, 30.5)]), clean, NOMINAL_FS)
    assert cycle_is_net(26.0, 29.0, window)
    assert not cycle_is_net(29.0, 32.0, window)
    assert not cycle_is_net(5.0, 5.0, window)  # 空周期不算净


def test_a_single_arrival_cannot_form_a_span():
    """一个到达时刻构不成跨度。碎段应当整段跳过，而不是让覆盖率变成 0/0。"""
    with pytest.raises(PlanningError):
        net_window(np.array([1.0]), NOMINAL_FS)


def test_a_precomputed_integrity_report_is_used_as_given():
    """调用方算过的 `assess` 结果可以直接传进来，结论必须与自己算一致。"""
    holed = arrivals(duration_s=60.0, drop=[(20.0, 21.0)])
    report = assess(holed, NOMINAL_FS)
    assert net_window(holed, NOMINAL_FS, report=report) == net_window(holed, NOMINAL_FS)


# ── 判据 2 的另一半：严闸不动 ────────────────────────────────────────────────


def test_the_strict_path_never_reaches_for_the_wide_gate():
    """步态参数那条路径不认识宽闸。

    判据 2 要求"严闸行为逐字节不变"。最直接的证据是 `cli/v3prime.py`（`timebase_
    trustworthy` 的所在）根本没有 import 到宽闸 —— 只要这条 import 不存在，宽闸就
    不可能改变那条路径的任何一个判定。
    """
    import ast
    from pathlib import Path

    import gait

    source = Path(gait.__file__).with_name("cli") / "v3prime.py"
    imported: set[str] = set()
    for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)

    assert "gait.sync.planning" not in imported
    assert "gait.analysis.planning" not in imported
