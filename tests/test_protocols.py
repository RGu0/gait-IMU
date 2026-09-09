"""`gait.validate.protocols` 的协议精度验证。

RAY-230 的交付物（判据实现见修订 R2，直线协议参数化见修订 **R3**）。判据三条
（已知距离直线 ≥ 40 m 且误差 < 3%、闭环 < 1.5%、4 米往返与长直线一致性可量化），
这里逐条守住它们的**执行点**，外加两条贯穿性质：

1. **空样本返回 `None`，不返回"合格"。** "没数据"不是"合格" —— 一条没跑过的判据
   和一条跑过且通过的判据在报告里必须长得不一样。
2. **判据是冻结常量。** 门槛只能来自 `STRAIGHT_LINE_MAX_ERROR` /
   `CLOSED_LOOP_MAX_ERROR` / `STRAIGHT_LINE_MIN_DISTANCE_M`，不能散在判断里；
   否则"跑完之后有没有人动过判据"在 git 历史里查不出来。R3 把场地下限也纳入
   这条守护 —— **参数化的是「哪条道算数」，不是「多大误差算过」**。

第三条判据**没有及格线**（原文"可量化并写入协议说明"），所以这里守的是
"它不许假装自己有一个"。
"""

from __future__ import annotations

import json

import numpy as np
import pytest

from gait.contracts import NavResult
from gait.validate.protocols import (
    CLOSED_LOOP_MAX_ERROR,
    PROTOCOL_LOOP,
    PROTOCOL_SHUTTLE,
    PROTOCOL_STRAIGHT,
    STRAIGHT_LINE_MAX_ERROR,
    STRAIGHT_LINE_MIN_DISTANCE_M,
    ClosedLoopVerdict,
    ProtocolConsistency,
    ProtocolError,
    StraightLineVerdict,
    TrialGeometry,
    course_length_from_pitch,
    evaluate_trial,
    stride_length_from_placements,
    summarize,
)

FS = 200.0


def nav(positions: np.ndarray) -> NavResult:
    """把一条位置轨迹包成 `NavResult`。其余状态量与本模块无关，填零即可。"""
    n = positions.shape[0]
    identity = np.tile([1.0, 0.0, 0.0, 0.0], (n, 1))
    return NavResult(
        t=np.arange(n) / FS,
        q=identity,
        v=np.zeros((n, 3)),
        p=np.asarray(positions, dtype=float),
        bg=np.zeros((n, 3)),
        ba=np.zeros((n, 3)),
        zupt=np.zeros(n, dtype=bool),
        stances=[],
        degraded=np.zeros(n, dtype=bool),
        score=np.zeros(n),
    )


#: 走廊前段（砖 1–40）的跨量读数，m（2026-09-05，同侧边缘到同侧边缘）。
COURSE_SPAN_A_M = 24.036
#: 走廊后段（砖 41–75）的跨量读数，m。**从段 A 结束处起量**，两段连续不重叠。
COURSE_SPAN_B_M = 21.067
#: 两段各自的砖距数。40 ＋ 35 ＝ 75，正好覆盖全程。
COURSE_SPAN_A_PITCHES = 40
COURSE_SPAN_B_PITCHES = 35

#: T-230-03 的走廊，**全程实测**：两段跨量之和，**没有外推成分**。
#: R3 之后它是合法的直线协议距离（≥ `STRAIGHT_LINE_MIN_DISTANCE_M`），用在测试里是
#: 为了让"已知距离由数据声明"这件事在用例上就看得见，而不是所有用例都填同一个 50。
#:
#: 这个数改过三次，每次都比上一次少一层假设：
#:
#: | 版本 | 值 | 它是什么 |
#: | --- | --- | --- |
#: | 45.000 | 标称 | 「一块砖 600 mm」 |
#: | 45.148 | 标称 | 75×600 ＋ 74×2，**注释却自称实测** |
#: | 45.0675 | 半实测 | 量 40 块，**外推**到 75 |
#: | **45.103** | **实测** | 两段跨量相加，75 块全量 |
#:
#: 最后一步纠掉 **35.5 mm** —— 因为走廊**不均匀**：前 40 块砖距 600.90 mm，
#: 后 35 块 601.914 mm，差 1.014 mm/块。外推的代价是读数误差（±9 mm）的四倍。
#: 见 `evidence/ray-337/protocol-truth/README.md`。
FIELD_45 = COURSE_SPAN_A_M + COURSE_SPAN_B_M


def straight_walk(distance: float, n: int = 500) -> NavResult:
    """沿 x 轴走 `distance` 米。"""
    p = np.zeros((n, 3))
    p[:, 0] = np.linspace(0.0, distance, n)
    return nav(p)


def shuttle_walk(leg: float, laps: int = 3, n: int = 1200) -> NavResult:
    """在一条 `leg` 米的道上往返 `laps` 个来回，终点回到起点。"""
    phase = np.linspace(0.0, laps * 2.0 * np.pi, n)
    p = np.zeros((n, 3))
    # 三角波：0 → leg → 0 → ...，终点与起点重合。
    p[:, 0] = leg * np.abs(((phase / np.pi) % 2.0) - 1.0) * -1.0 + leg
    p[:, 0] -= p[0, 0]
    return nav(p)


def loop_walk(residual: float, n: int = 800) -> NavResult:
    """走一圈回到起点附近，残差 `residual` 米。"""
    angle = np.linspace(0.0, 2.0 * np.pi, n)
    p = np.zeros((n, 3))
    p[:, 0] = np.cos(angle) - 1.0
    p[:, 1] = np.sin(angle)
    p[:, 0] += np.linspace(0.0, residual, n)
    return nav(p)


def measure(label, protocol, truth, feet):
    return evaluate_trial(TrialGeometry(label, protocol, truth), feet)


class TestDistanceIsMeasuredWithTheRightRulerPerProtocol:
    """三种协议测的是三样东西。共用一个式子必然有一种是错的。"""

    def test_a_straight_trial_reads_its_end_to_end_displacement(self):
        result = measure("s1", PROTOCOL_STRAIGHT, 50.0, {"L": straight_walk(50.0)})
        assert result.measured_m == pytest.approx(50.0)
        assert result.error == pytest.approx(0.0, abs=1e-12)

    def test_a_shuttle_trial_reads_the_lane_not_the_round_trip(self):
        """往返走完回到原点。用首末位移量它会读出约 −100%，那量的是"回没回到原点"。

        这条是本模块最容易写错的地方：`shuttle` 与 `loop` 的轨迹形状都"回到原点"，
        但要问的问题相反 —— 闭环问"回得准不准"，往返问"这条道有多长"。
        """
        walk = shuttle_walk(4.0, laps=3)
        assert np.linalg.norm(walk.p[-1] - walk.p[0]) < 0.05, "构造前提：终点回到起点"

        result = measure("t1", PROTOCOL_SHUTTLE, 4.0, {"L": walk})
        assert result.measured_m == pytest.approx(4.0, abs=0.02)
        assert abs(result.error) < 0.01

    def test_a_loop_trial_reads_the_residual_against_the_perimeter(self):
        residual = 0.09
        result = measure("l1", PROTOCOL_LOOP, 12.0, {"L": loop_walk(residual)})
        assert result.measured_m == pytest.approx(residual, abs=1e-6)
        assert result.error == pytest.approx(residual / 12.0)

    def test_the_loop_error_is_never_negative(self):
        """闭环真值恒为零，残差没有方向可言 —— 负的相对误差在这里没有意义。"""
        for residual in (0.01, 0.5, 3.0):
            result = measure("l", PROTOCOL_LOOP, 12.0, {"L": loop_walk(residual)})
            assert result.error >= 0.0

    def test_the_vertical_axis_does_not_tilt_the_shuttle_axis(self):
        """竖直方向是步态起伏，与道有多长无关。混进主轴会把跨度读长。"""
        walk = shuttle_walk(4.0)
        bumpy = walk.p.copy()
        bumpy[:, 2] = 0.4 * np.sin(np.linspace(0.0, 60.0 * np.pi, bumpy.shape[0]))
        result = measure("t", PROTOCOL_SHUTTLE, 4.0, {"L": nav(bumpy)})
        assert result.measured_m == pytest.approx(4.0, abs=0.02)


class TestTwoFeetAreAveraged:
    def test_the_two_feet_are_averaged(self):
        feet = {"L": straight_walk(49.0), "R": straight_walk(51.0)}
        result = measure("s", PROTOCOL_STRAIGHT, 50.0, feet)
        assert result.measured_m == pytest.approx(50.0)
        assert set(result.per_foot) == {"L", "R"}

    def test_a_trial_with_no_feet_is_refused(self):
        with pytest.raises(ProtocolError):
            measure("s", PROTOCOL_STRAIGHT, 50.0, {})

    def test_a_trajectory_too_short_to_have_a_displacement_is_refused(self):
        with pytest.raises(ProtocolError, match="两个样本"):
            measure("s", PROTOCOL_STRAIGHT, 50.0, {"L": nav(np.zeros((1, 3)))})


class TestTheGeometryIsValidated:
    def test_an_unknown_protocol_is_refused(self):
        with pytest.raises(ProtocolError, match="未知协议"):
            TrialGeometry("x", "zigzag", 10.0)

    def test_a_loop_truth_of_zero_is_refused_with_a_reason(self):
        """闭环真值填**周长**而不是 0 —— 残差要除以它才谈得上相对误差。"""
        with pytest.raises(ProtocolError, match="周长"):
            TrialGeometry("l", PROTOCOL_LOOP, 0.0)


class TestTheCriteriaAreTheOnlySourceOfThresholds:
    """判据开跑前定死、跑完不得修改（06 §5 冻结声明）。"""

    def test_the_straight_line_verdict_turns_exactly_at_the_named_constant(self):
        just_inside = STRAIGHT_LINE_MAX_ERROR * 0.99
        just_outside = STRAIGHT_LINE_MAX_ERROR * 1.01
        inside = measure(
            "a", PROTOCOL_STRAIGHT, 50.0, {"L": straight_walk(50.0 * (1 + just_inside))}
        )
        outside = measure(
            "b",
            PROTOCOL_STRAIGHT,
            50.0,
            {"L": straight_walk(50.0 * (1 + just_outside))},
        )
        assert StraightLineVerdict((inside,)).passed is True
        assert StraightLineVerdict((outside,)).passed is False

    def test_the_closed_loop_verdict_turns_exactly_at_the_named_constant(self):
        perimeter = 12.0
        inside = measure(
            "a",
            PROTOCOL_LOOP,
            perimeter,
            {"L": loop_walk(perimeter * CLOSED_LOOP_MAX_ERROR * 0.99)},
        )
        outside = measure(
            "b",
            PROTOCOL_LOOP,
            perimeter,
            {"L": loop_walk(perimeter * CLOSED_LOOP_MAX_ERROR * 1.01)},
        )
        assert ClosedLoopVerdict((inside,)).passed is True
        assert ClosedLoopVerdict((outside,)).passed is False

    def test_the_straight_line_floor_turns_exactly_at_the_named_constant(self):
        """场地下限也是判据，且**在构造处**执行 —— 不是在 verdict 里静默剔除。

        静默剔除会让报告显示"这趟参与了判定"，而判据一根本不适用于一条太短的道。
        """
        ok = TrialGeometry("at", PROTOCOL_STRAIGHT, STRAIGHT_LINE_MIN_DISTANCE_M)
        assert ok.distance_m == STRAIGHT_LINE_MIN_DISTANCE_M

        with pytest.raises(ProtocolError, match="已知距离"):
            TrialGeometry(
                "under", PROTOCOL_STRAIGHT, STRAIGHT_LINE_MIN_DISTANCE_M - 0.001
            )

    def test_the_floor_binds_only_the_straight_protocol(self):
        """4 米往返与闭环各有各的尺，下限是**直线判据**的一部分，不是全局最短距离。

        往返协议的真值就是 4 m（单程），闭环填周长 —— 拿直线的下限去卡它们，
        等于把一条判据的约束扩散到另外两条上。
        """
        assert TrialGeometry("t", PROTOCOL_SHUTTLE, 4.0).distance_m == 4.0
        assert TrialGeometry("l", PROTOCOL_LOOP, 12.0).distance_m == 12.0

    def test_the_known_distance_is_declared_by_the_data_not_hardcoded(self):
        """R3 的实现口径：距离由数据声明，下限被冻结。

        `FIELD_45`（走廊全程实测值）与 50 m 都是合法的已知距离，
        且**判定门槛对两者相同** —— 判据一量的是相对误差，绝对距离不进入它。
        """
        for truth in (FIELD_45, 50.0):
            inside = measure(
                "in",
                PROTOCOL_STRAIGHT,
                truth,
                {"L": straight_walk(truth * (1 + STRAIGHT_LINE_MAX_ERROR * 0.99))},
            )
            outside = measure(
                "out",
                PROTOCOL_STRAIGHT,
                truth,
                {"L": straight_walk(truth * (1 + STRAIGHT_LINE_MAX_ERROR * 1.01))},
            )
            assert StraightLineVerdict((inside,)).passed is True
            assert StraightLineVerdict((outside,)).passed is False

    def test_the_report_carries_the_floor_it_ran_under(self):
        """报告要说清按哪版判据算的 —— R3 加了一条约束，快照里就得看得见。"""
        trial = measure(
            "s", PROTOCOL_STRAIGHT, FIELD_45, {"L": straight_walk(FIELD_45)}
        )
        criterion = StraightLineVerdict((trial,)).snapshot()["criterion"]
        assert criterion["max_abs_error"] == STRAIGHT_LINE_MAX_ERROR
        assert criterion["min_distance_m"] == STRAIGHT_LINE_MIN_DISTANCE_M

    def test_the_closed_loop_criterion_is_stricter_than_the_straight_one(self):
        """闭环真值恒为零，读数里没有"路径长度量得准不准"这一项，它纯粹是航向漂移。"""
        assert CLOSED_LOOP_MAX_ERROR < STRAIGHT_LINE_MAX_ERROR

    def test_one_bad_trial_out_of_three_fails_the_straight_criterion(self):
        """判据写的是"已知距离直线误差 < 3%"，不是"典型误差 < 3%"。"""
        good = [
            measure(f"g{i}", PROTOCOL_STRAIGHT, 50.0, {"L": straight_walk(50.0)})
            for i in range(2)
        ]
        bad = measure("b", PROTOCOL_STRAIGHT, 50.0, {"L": straight_walk(56.0)})
        assert StraightLineVerdict(tuple(good)).passed is True
        assert StraightLineVerdict((*good, bad)).passed is False


class TestNoDataIsNotAPass:
    """空样本返回 None。"没数据"和"验过且通过"在报告里必须分得开。"""

    def test_an_empty_straight_verdict_is_none(self):
        assert StraightLineVerdict(()).passed is None

    def test_an_empty_loop_verdict_is_none(self):
        assert ClosedLoopVerdict(()).passed is None

    def test_consistency_with_only_one_protocol_present_is_none(self):
        straight = (measure("s", PROTOCOL_STRAIGHT, 50.0, {"L": straight_walk(50.0)}),)
        assert ProtocolConsistency(straight=straight, shuttle=()).bias is None
        assert ProtocolConsistency(straight=straight, shuttle=()).quantified is None
        assert ProtocolConsistency(straight=(), shuttle=straight).quantified is None

    def test_the_summary_says_unverified_not_failed_when_data_is_missing(self):
        """ "没采到"与"验过但没过"的下一步动作完全不同，报告必须分得开。"""
        report = summarize([])
        assert "未验" in report["decision"]
        assert report["straight_line"]["passed"] is None
        assert report["closed_loop"]["passed"] is None

    def test_a_complete_run_says_passed(self):
        trials = [
            measure("s1", PROTOCOL_STRAIGHT, 50.0, {"L": straight_walk(50.2)}),
            measure("l1", PROTOCOL_LOOP, 12.0, {"L": loop_walk(0.05)}),
            measure("t1", PROTOCOL_SHUTTLE, 4.0, {"L": shuttle_walk(4.0)}),
        ]
        report = summarize(trials)
        assert report["decision"].startswith("通过")


class TestProtocolConsistencyQuantifiesButDoesNotJudge:
    """判据三原文是"可量化并写入协议说明" —— 量化即达成，没有及格线。"""

    def build(self, straight_error: float, shuttle_error: float):
        straight = tuple(
            measure(
                f"s{i}",
                PROTOCOL_STRAIGHT,
                50.0,
                {"L": straight_walk(50.0 * (1 + straight_error))},
            )
            for i in range(3)
        )
        shuttle = tuple(
            measure(
                f"t{i}",
                PROTOCOL_SHUTTLE,
                4.0,
                {"L": shuttle_walk(4.0 * (1 + shuttle_error))},
            )
            for i in range(3)
        )
        return ProtocolConsistency(straight=straight, shuttle=shuttle)

    def test_the_bias_is_positive_when_the_shuttle_costs_more_per_metre(self):
        """符号有意义：正 = 往返每米积的误差多于长直线，那是转身要付的代价。"""
        consistency = self.build(straight_error=0.0, shuttle_error=0.02)
        assert consistency.bias is not None
        assert consistency.bias > 0.0

    def test_the_bias_is_negative_the_other_way_round(self):
        consistency = self.build(straight_error=0.02, shuttle_error=0.0)
        assert consistency.bias < 0.0

    def test_quantifying_it_is_the_whole_criterion(self):
        consistency = self.build(straight_error=0.0, shuttle_error=0.05)
        assert consistency.quantified is True

    def test_it_never_claims_a_pass_or_a_fail(self):
        """一个用户从未定过的门槛不能由本模块发明出来。"""
        huge = self.build(straight_error=0.0, shuttle_error=0.5)
        none = self.build(straight_error=0.0, shuttle_error=0.0)
        assert huge.passed is None
        assert none.passed is None
        assert huge.quantified is True


class TestTheSnapshotIsValidJson:
    def test_a_report_with_missing_data_serialises(self):
        """`json.dumps` 会把 nan 写成裸 `NaN`，那不是合法 JSON。"""
        text = json.dumps(summarize([]), ensure_ascii=False)
        assert "NaN" not in text

    def test_a_full_report_serialises_and_carries_its_criteria(self):
        trials = [
            measure("s1", PROTOCOL_STRAIGHT, 50.0, {"L": straight_walk(50.2)}),
            measure("l1", PROTOCOL_LOOP, 12.0, {"L": loop_walk(0.05)}),
            measure("t1", PROTOCOL_SHUTTLE, 4.0, {"L": shuttle_walk(4.0)}),
        ]
        report = summarize(trials)
        text = json.dumps(report, ensure_ascii=False)
        assert "NaN" not in text
        # 判据随结论一起落盘：读报告的人要能看出它按哪版门槛算的。
        assert (
            report["straight_line"]["criterion"]["max_abs_error"]
            == STRAIGHT_LINE_MAX_ERROR
        )
        assert report["closed_loop"]["criterion"]["max_error"] == CLOSED_LOOP_MAX_ERROR
        assert report["protocol_consistency"]["criterion"]["reporting_only"] is True


# ── 定长走廊的几何：落脚数 ↔ 距离 ────────────────────────────────────────────
#
# 这一组守的是一个**曾经只写在文档里、并且已经被抄错过一次**的换算。
# `evidence/ray-360/field-replay` 由 `45.148 ÷ 38` 得出「步长恒为 1.188 m」，
# 而同一批证据里 `ray-337/protocol-truth` 的几何推导给的是 1.2 m。两处互相矛盾了
# 好几天而无人发现 —— 因为两边都只是文字，没有任何东西会因此变红。

#: 每足落脚数。**受控量**：两种鞋型 × 六个速度档，12 趟全部一致（现场逐步计数）。
COURSE_PLACEMENTS = 38
#: 走廊的砖块总数。**逐块数的，三次起终点相同、三次都是 75。**
COURSE_BRICKS = 75


class TestTheCourseGeometryIsPinnedNotRetyped:
    """走廊几何有唯一执行点，且它与场地实测自洽。"""

    def test_the_placement_count_and_the_brick_count_lock_each_other(self):
        """`2N − 1 = 砖块数` —— **这是仅剩的独立自洽检验，也是最要紧的那条。**

        38 是现场**逐步数**出来的，75 是**逐块数**出来的（三次起终点相同、三次都是
        75）。两个计数来源完全无关，却被「并脚起步、并脚收尾」的几何扣死：
        N 次落脚推进 `2N − 1` 个单步。

        扣不上就说明协议形态被记错了 —— 而那会让**所有**距离判据的真值一起错，
        因为走廊长与步幅都是按这条几何从两个计数推出来的。

        它不含任何长度测量，所以走廊均不均匀都不影响它。**这一点现在很关键**：
        原先还有一条「砖距 × 落脚数 ⇒ 走廊长」的断言，在全程量完之后变成了循环
        （平均砖距就是 `L / 75` 算出来的），已经删掉 —— 恒真的断言守不住任何东西。
        """
        assert 2 * COURSE_PLACEMENTS - 1 == COURSE_BRICKS

    def test_the_two_spans_are_contiguous_and_cover_the_whole_course(self):
        """两段跨量首尾相接、正好覆盖 75 个砖距，其和即走廊长。

        `FIELD_45` 由两段读数**相加**得到而不是写死一个数，是为了让「它是量出来的、
        量了哪两段」在常量定义处就看得见。段 B 从段 A 结束处起量（同一块砖的同侧
        边缘），所以相加既不重叠也不留空隙。
        """
        assert COURSE_SPAN_A_PITCHES + COURSE_SPAN_B_PITCHES == COURSE_BRICKS
        assert FIELD_45 == pytest.approx(45.103, abs=5e-4)

    def test_the_corridor_is_not_uniform_so_a_partial_span_must_not_be_extrapolated(
        self,
    ):
        """**走廊不均匀 —— 这条守的是「不许再外推」。**

        前 40 块与后 35 块的砖距差 **1.014 mm/块（0.169%）**。单次读数 ±5 mm 下这是
        5.3σ，±10 mm 下仍有 2.7σ —— 不是噪声。

        代价是实打实的：只量 40 块再外推到 75，得 45.0675 m，比全程实测**短 35.5 mm**，
        那是读数误差（±9 mm）的**四倍** —— 非均匀性一直是主项，只是之前没量。

        这条断言存在，是为了让下一个想「量一段推全程」省事的人先看见这个数。
        """
        pitch_a = COURSE_SPAN_A_M / COURSE_SPAN_A_PITCHES
        pitch_b = COURSE_SPAN_B_M / COURSE_SPAN_B_PITCHES

        assert pitch_b - pitch_a == pytest.approx(0.001014, abs=2e-5)

        extrapolated = course_length_from_pitch(pitch_a, COURSE_PLACEMENTS)
        assert FIELD_45 - extrapolated == pytest.approx(0.0355, abs=5e-4)

    def test_the_stride_divisor_is_placements_minus_a_half_not_placements(self):
        """**这条测试守的正是那个被抄错的除法。**

        「每足 38 个步态周期」很自然被读成「38 个步幅」。但 38 是**落脚次数**，
        并脚起步、并脚收尾时两端各有半步，所以只跨 37.5 个步幅。
        """
        stride = stride_length_from_placements(FIELD_45, COURSE_PLACEMENTS)

        assert stride == pytest.approx(FIELD_45 / 37.5)
        # 步幅 = 两个**平均**砖距。走廊不均匀，逐步进距在 600.90~601.91 mm 之间，
        # 但「每步进一块砖」这条几何不变，所以均值口径下这个等式仍然精确。
        assert stride == pytest.approx(2 * FIELD_45 / COURSE_BRICKS, abs=1e-9)

        naive = FIELD_45 / COURSE_PLACEMENTS
        assert stride > naive
        # 差 1.3% —— 在 3% 的距离判据下吃掉近一半预算，且是系统性偏置
        assert (stride - naive) / stride == pytest.approx(0.0132, abs=5e-4)

    def test_the_two_conversions_are_inverses(self):
        """两个函数互为逆运算：同一条几何不该有两套算法。"""
        pitch = FIELD_45 / COURSE_BRICKS
        for placements in (2, 7, 38, 101):
            length = course_length_from_pitch(pitch, placements)
            stride = stride_length_from_placements(length, placements)
            assert stride == pytest.approx(2 * pitch)

    @pytest.mark.parametrize("placements", [0, -1, 2.5, True])
    def test_a_placement_count_that_is_not_a_positive_integer_is_refused(
        self, placements
    ):
        """落脚数是数出来的整数。`True` 也要挡 —— `bool` 是 `int` 的子类。"""
        with pytest.raises(ProtocolError):
            course_length_from_pitch(FIELD_45 / COURSE_BRICKS, placements)
        with pytest.raises(ProtocolError):
            stride_length_from_placements(FIELD_45, placements)

    @pytest.mark.parametrize("bad", [0.0, -1.0, float("nan"), float("inf")])
    def test_a_length_that_is_not_positive_and_finite_is_refused(self, bad):
        with pytest.raises(ProtocolError):
            course_length_from_pitch(bad, COURSE_PLACEMENTS)
        with pytest.raises(ProtocolError):
            stride_length_from_placements(bad, COURSE_PLACEMENTS)

    def test_the_field_constant_is_the_measured_course_not_the_nominal_one(self):
        """`FIELD_45` 是**实测**值，不是 75×600＋74×2 那个标称值。

        标称与实测差 80.5 mm。这条断言存在，是因为此前那个常量的注释**自称实测**
        而其实没量过 —— 一个自称实测的标称值比一个明说是标称的值更危险。
        """
        nominal = 75 * 0.600 + 74 * 0.002
        assert FIELD_45 != pytest.approx(nominal, abs=1e-4)
        assert nominal - FIELD_45 == pytest.approx(0.0450, abs=5e-4)
