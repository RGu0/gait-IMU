"""`gait.core.dualfoot` 的双足联合约束。

这个文件里有一条测试与其说在验证代码，不如说在**记录一个证明**：
`test_the_position_method_measures_its_own_assumption` 断言"把左右两路数据对调之后
重算，两次结果之和精确等于步宽"。那个恒等式说明整体设计 §5.7 的位置法在本仓库的
数据流下判断不了左右 —— 它测的是自己的假设。

把证明写成测试而不是只写进文档：文档会被绕过，测试不会。将来若有人"修好"这个方法，
那条测试会失败并逼他解释他到底改了什么前提。
"""

from dataclasses import replace

import numpy as np
import pytest

from gait.config import AlgoConfig
from gait.contracts import NavResult
from gait.core import quaternion as quat
from gait.core.dualfoot import (
    DEFAULT_STEP_WIDTH,
    DualFootError,
    _distances,
    apply_distance_constraint,
    check_alternating_stance,
    inversion_signature,
    lateral_separation,
    swapped,
)
from gait.core.eskf import run_ins
from gait.validate.synthetic import NoiseModel, WalkSpec, generate_dual_walk

SENSOR_NOISE = NoiseModel(accel_density=1.5e-3, gyro_density=3.0e-4, seed=3)


def dual_navigation(seconds: float = 30.0, *, seed: int = 3, **spec_kwargs):
    """跑一次完整的双足会话，返回两足的 `NavResult` 与真值。"""
    pair = generate_dual_walk(
        WalkSpec(duration_s=seconds, **spec_kwargs),
        noise=NoiseModel(accel_density=1.5e-3, gyro_density=3.0e-4, seed=seed),
    )
    navigation = {foot: run_ins(pair[foot][0]) for foot in ("L", "R")}
    truth = {foot: pair[foot][1] for foot in ("L", "R")}
    return navigation, truth


def heading_error_degrees(navigation: NavResult, truth) -> float:
    """估计轨迹相对真值的整体航向偏差，deg。"""
    reference = truth.p[-1, :2] - truth.p[0, :2]
    estimate = navigation.p[-1, :2] - navigation.p[0, :2]
    return float(
        np.degrees(
            np.arctan2(estimate[1], estimate[0]) - np.arctan2(reference[1], reference[0])
        )
    )


def constant_result(n: int, *, roll: float, stances: list[tuple[int, int]]) -> NavResult:
    """一条只有横滚有内容的合成 `NavResult`，用来单独验证内外翻线索的算法。"""
    zupt = np.zeros(n, dtype=bool)
    for start, end in stances:
        zupt[start:end] = True
    return NavResult(
        t=np.arange(n) / 200.0,
        q=np.tile(quat.from_euler(roll, 0.0, 0.0), (n, 1)),
        v=np.zeros((n, 3)),
        p=np.zeros((n, 3)),
        bg=np.zeros((n, 3)),
        ba=np.zeros((n, 3)),
        zupt=zupt,
        stances=stances,
        degraded=np.zeros(n, dtype=bool),
        score=np.zeros(n),
    )


class TestDistanceConstraint:
    """验收标准之一：开启约束后航向漂移显著下降。"""

    def test_violations_are_eliminated(self):
        navigation, _ = dual_navigation()
        result = apply_distance_constraint(navigation["L"], navigation["R"])
        assert result.report.violation_fraction_before > 0.05, "这份数据本来就该越界"
        assert result.report.violation_fraction_after == 0.0
        assert result.report.peak_distance_after <= result.report.max_distance * 1.001

    def test_the_differential_heading_error_shrinks(self):
        """约束修的是**差分**航向，所以量的也是差分。

        共模航向不在这里衡量，因为它本来就没有意义 —— RAY-202 定的产品边界是"输出
        定义在会话坐标系、yaw = 0"，整段一起转多少度是坐标系的定义问题。
        """
        navigation, truth = dual_navigation()
        before = heading_error_degrees(navigation["L"], truth["L"]) - heading_error_degrees(
            navigation["R"], truth["R"]
        )
        result = apply_distance_constraint(navigation["L"], navigation["R"])
        after = heading_error_degrees(result.left, truth["L"]) - heading_error_degrees(
            result.right, truth["R"]
        )
        assert abs(after) < 0.6 * abs(before), f"差分航向 {before:.2f}° → {after:.2f}°"

    def test_the_correction_is_split_symmetrically(self):
        """§5.7 的"对称分配修正量"：左足转 +θ/2、右足转 −θ/2。"""
        navigation, _ = dual_navigation()
        result = apply_distance_constraint(navigation["L"], navigation["R"])
        angle = 0.5 * result.report.differential_yaw_rate * navigation["L"].t[-1]
        for original, corrected, sign in (
            (navigation["L"], result.left, +1.0),
            (navigation["R"], result.right, -1.0),
        ):
            before = np.arctan2(original.p[-1, 1], original.p[-1, 0])
            after = np.arctan2(corrected.p[-1, 1], corrected.p[-1, 0])
            assert np.isclose(after - before, sign * angle, atol=1e-9)

    def test_nothing_is_corrected_when_nothing_violates(self):
        """不等式约束：没越界就不该有任何修正。"""
        navigation, _ = dual_navigation(6.0)
        loose = replace(AlgoConfig(), dualfoot_max_distance_m=50.0)
        result = apply_distance_constraint(navigation["L"], navigation["R"], loose)
        assert result.report.violation_fraction_before == 0.0
        assert result.report.differential_yaw_rate == 0.0
        assert np.array_equal(result.left.p, navigation["L"].p)

    def test_only_the_differential_heading_is_observable(self):
        """把两足连同偏置一起转同一个角度，所有足间距一字不变。

        这是本模块能力边界的数学表述：距离只看得见航向的**差**。它同时解释了为什么
        共模航向修不了，以及为什么那不要紧。
        """
        rng = np.random.default_rng(0)
        n = 500
        t = np.arange(n) / 200.0
        left = rng.normal(size=(n, 3))
        right = rng.normal(size=(n, 3))
        offset = np.array([0.0, -DEFAULT_STEP_WIDTH, 0.0])
        base = _distances(left, right, offset, t, 0.001)

        phi = 0.4
        rotation = np.array(
            [[np.cos(phi), -np.sin(phi), 0.0], [np.sin(phi), np.cos(phi), 0.0], [0.0, 0.0, 1.0]]
        )
        rotated = _distances(
            left @ rotation.T, right @ rotation.T, rotation @ offset, t, 0.001
        )
        assert np.allclose(base, rotated)

    def test_mismatched_inputs_are_refused(self):
        navigation, _ = dual_navigation(6.0)
        shorter = run_ins(
            generate_dual_walk(WalkSpec(duration_s=4.0), noise=SENSOR_NOISE)["R"][0]
        )
        with pytest.raises(DualFootError, match="采样数不一致"):
            apply_distance_constraint(navigation["L"], shorter)

    def test_a_different_time_axis_is_refused(self):
        """对齐属同步层（RAY-209）。在这里凑合会把同步误差伪装成航向误差。"""
        navigation, _ = dual_navigation(6.0)
        shifted = navigation["R"]
        shifted = NavResult(
            t=navigation["R"].t + 0.5,
            q=shifted.q,
            v=shifted.v,
            p=shifted.p,
            bg=shifted.bg,
            ba=shifted.ba,
            zupt=shifted.zupt,
            stances=shifted.stances,
            degraded=shifted.degraded,
            score=shifted.score,
        )
        with pytest.raises(DualFootError, match="时间轴不同"):
            apply_distance_constraint(navigation["L"], shifted)


class TestAlternatingStance:
    def test_a_normal_walk_is_not_flagged(self):
        navigation, _ = dual_navigation()
        report = check_alternating_stance(navigation["L"], navigation["R"])
        assert report.walking_samples > 0
        assert not report.suspicious

    def test_the_leading_standing_period_is_excluded(self):
        """PRD §7 的流程开头有静立段。不排除的话每一次会话都会报可疑。

        这一条是本模块第一版的真实缺陷：静立 1 s 被算成一段 1.62 s 的双支撑期，
        超过 1.0 s 的阈值，于是每次都报。一个永远报警的检查等于没有检查。
        """
        navigation, _ = dual_navigation(20.0, still_lead_s=3.0)
        report = check_alternating_stance(navigation["L"], navigation["R"])
        assert report.longest_double_support_s < 1.0
        assert not report.suspicious

    def test_a_long_simultaneous_stance_inside_the_walk_is_flagged(self):
        """走到一半站住了 —— 那是检测器或受试者出了状况，要打标注。"""
        navigation, _ = dual_navigation(20.0)
        cfg = AlgoConfig()
        middle = len(navigation["L"].t) // 2
        span = round(2.0 * 200.0)
        for foot in ("L", "R"):
            navigation[foot].zupt[middle : middle + span] = True
        report = check_alternating_stance(navigation["L"], navigation["R"], cfg)
        assert report.longest_double_support_s > cfg.dualfoot_double_support_max_s
        assert report.suspicious

    def test_the_reported_double_support_fraction_underestimates_the_truth(self):
        """由 ZUPT 边界算出的双支撑期占比**系统性偏低**，不能直接当指标用。

        走路的真实双支撑期占 10~25%（整体设计 §6.2），实测这里只有约 2%。原因不是
        检测器坏了，而是它**保守**：中心滑窗与低通把每个支撑相的两端各削掉约 18 个
        采样（见 `core/zupt.py` 的保守性说明），两只脚各削一次，重叠区几乎被削光。

        双支撑期占比是 PRD §13 的 v1 输出指标，所以这件事必须被记下来：**它要由
        细化后的 IC/TO 事件算（RAY-216），不能由 ZUPT 区间算。** 本模块的
        `double_support_fraction` 只用于"有没有严重异常"的粗判。
        """
        navigation, _ = dual_navigation()
        report = check_alternating_stance(navigation["L"], navigation["R"])
        assert report.double_support_fraction < 0.10

    def test_a_session_that_never_moves_reports_no_walking_rather_than_nan(self):
        """整段没动过时返回零并把 `walking_samples` 置零。

        第一版在这里对空切片取均值，安静地返回了 nan —— 而 nan 会一路流进质量标注，
        在报告里变成一个空白格，没有任何地方说明它为什么是空的。
        """
        n = 400
        still = constant_result(n, roll=0.0, stances=[(0, n)])
        report = check_alternating_stance(still, still)
        assert report.walking_samples == 0
        assert report.double_support_fraction == 0.0
        assert not report.suspicious


class TestLateralSeparationCannotIdentifyFeet:
    """整体设计 §5.7 的位置法在本仓库的数据流下判断不了左右。这里是证明。"""

    @pytest.mark.parametrize("seed", [1, 2, 3])
    @pytest.mark.parametrize("step_width", [0.08, 0.12, 0.20])
    def test_the_position_method_measures_its_own_assumption(self, seed, step_width):
        """把左右对调后重算，两次结果之和**精确等于步宽**。

        因为 `nominal_left_lateral = 假设步宽/2 + 估计发散量/2`，而对调只翻转第二项
        的符号。真实的左右身份一个字也没进这个式子。

        这条恒等式是本 scope 最重要的产出：它把"这个方法不可用"从一个判断变成一个
        可以复核的事实。
        """
        navigation, _ = dual_navigation(12.0, seed=seed, step_width=step_width)
        normal = lateral_separation(navigation["L"], navigation["R"], step_width=step_width)
        reversed_ = lateral_separation(
            *swapped(navigation["L"], navigation["R"]), step_width=step_width
        )
        assert normal.nominal_left_lateral + reversed_.nominal_left_lateral == pytest.approx(
            step_width, abs=1e-3
        )

    def test_it_reports_that_it_is_not_identifiable(self):
        """漂移超过半个步宽时，符号由漂移决定而不是由左右决定。"""
        navigation, _ = dual_navigation(30.0)
        separation = lateral_separation(navigation["L"], navigation["R"])
        assert abs(separation.estimated_divergence) > 0.5 * separation.assumed_step_width
        assert not separation.identifiable

    def test_the_divergence_is_what_the_constraint_removes(self):
        """约束之后发散量应当显著变小 —— 这正是它在做的事。"""
        navigation, _ = dual_navigation()
        before = lateral_separation(navigation["L"], navigation["R"]).estimated_divergence
        result = apply_distance_constraint(navigation["L"], navigation["R"])
        after = lateral_separation(result.left, result.right).estimated_divergence
        assert abs(after) < abs(before)

    def test_too_few_strides_are_refused(self):
        """步数不足时提示重走，而不是给一个没有余量的结论。"""
        single = constant_result(400, roll=0.0, stances=[(0, 120)])
        with pytest.raises(DualFootError, match="支撑相可用"):
            lateral_separation(single, single)

    def test_walking_in_place_is_refused(self):
        """行进方向定不下来时不给结论 —— 转身段与原地踏步都属于这种。"""
        n = 2000
        stances = [(i * 200, i * 200 + 120) for i in range(9)]
        still = constant_result(n, roll=0.0, stances=stances)
        with pytest.raises(DualFootError, match="不足一个步宽"):
            lateral_separation(still, still)


class TestInversionSignature:
    """§5.7 第 3 条的另一半线索。算法可验证，符号与左右的对应关系不可验证。"""

    def test_it_recovers_an_injected_roll_difference(self):
        n = 2000
        stances = [(i * 200, i * 200 + 120) for i in range(9)]
        left = constant_result(n, roll=0.10, stances=stances)
        right = constant_result(n, roll=-0.06, stances=stances)
        signature = inversion_signature(left, right)
        assert signature.difference == pytest.approx(0.16, abs=1e-6)
        assert signature.strides_used == len(stances) - 1

    def test_identical_feet_give_no_signal(self):
        n = 2000
        stances = [(i * 200, i * 200 + 120) for i in range(9)]
        same = constant_result(n, roll=0.05, stances=stances)
        assert inversion_signature(same, same).difference == pytest.approx(0.0, abs=1e-12)

    def test_on_synthetic_gait_it_is_not_decisive(self):
        """RAY-206 的模型只有俯仰、没有横滚 —— 那是它已声明的限制之一。

        所以本 scope 只能验证这个量"算得对"，验证不了它"判得准"。判准需要真机数据
        （RAY-230）或一次专门的佩戴实验来标定符号与左右的对应关系。
        """
        navigation, _ = dual_navigation()
        signature = inversion_signature(navigation["L"], navigation["R"])
        assert not signature.decisive

    def test_too_few_strides_are_refused(self):
        single = constant_result(400, roll=0.0, stances=[(0, 120)])
        with pytest.raises(DualFootError, match="支撑相可用"):
            inversion_signature(single, single)


class TestSwap:
    def test_swapping_is_its_own_inverse(self):
        navigation, _ = dual_navigation(6.0)
        once = swapped(navigation["L"], navigation["R"])
        twice = swapped(*once)
        assert twice[0] is navigation["L"]
        assert twice[1] is navigation["R"]
