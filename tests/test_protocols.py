"""`gait.validate.protocols` 的协议精度验证。

RAY-230 需求修订 R2 的交付物。判据三条（50 m 直线 < 3%、闭环 < 1.5%、4 米往返与
长直线一致性可量化），这里逐条守住它们的**执行点**，外加两条贯穿性质：

1. **空样本返回 `None`，不返回"合格"。** "没数据"不是"合格" —— 一条没跑过的判据
   和一条跑过且通过的判据在报告里必须长得不一样。
2. **判据是冻结常量。** 门槛只能来自 `STRAIGHT_LINE_MAX_ERROR` /
   `CLOSED_LOOP_MAX_ERROR`，不能散在判断里；否则"跑完之后有没有人动过判据"
   在 git 历史里查不出来。

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
    ClosedLoopVerdict,
    ProtocolConsistency,
    ProtocolError,
    StraightLineVerdict,
    TrialGeometry,
    evaluate_trial,
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

    def test_the_closed_loop_criterion_is_stricter_than_the_straight_one(self):
        """闭环真值恒为零，读数里没有"路径长度量得准不准"这一项，它纯粹是航向漂移。"""
        assert CLOSED_LOOP_MAX_ERROR < STRAIGHT_LINE_MAX_ERROR

    def test_one_bad_trial_out_of_three_fails_the_straight_criterion(self):
        """判据写的是"50 m 直线误差 < 3%"，不是"典型误差 < 3%"。"""
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
