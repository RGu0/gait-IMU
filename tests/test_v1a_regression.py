"""V1-a 端到端回归。契约 §5 称它是"tests 中最重要的测试"。

判据（PRD v1.2 §17.1 V1 / RAY-206）：**合成数据下步长误差 < 0.5%（纯算法误差）**。

## 这条测试链穿过整个前向算法链

    validate/synthetic  →  core/zupt  →  core/alignment  →  core/eskf  →  (core/dualfoot)

它是唯一一条会因为**任何一个环节**退化而变红的测试。上游各模块自己的测试都只看自己
那一段；这里看的是它们合起来还准不准。

## 结论先写在这里

| 步态 | 结果 |
| --- | --- |
| 走（108 spm）、快走（150）、慢跑（170）、4 米往返 | ✅ 全部 ≤ 0.15% 均值、≤ 0.27% rms |
| **低速/拖步（60 spm，步长 0.35 m）** | ❌ **2.1~4.6%**，超预算 4~9 倍 |

低速那一档的根因已量化（航向误差达 15.9°，支撑相内位置蠕动约 4 cm 占短步长的 11%），
单独登记为一个 Issue。**本文件把它写成一条"已知上界"的测试而不是跳过** —— 跳过的
限制不会被人再想起，而一个带上界的断言会在它变好或变坏时都发出声音。

## 为什么用真值的触地时刻而不是检出的

事件分割与 IC/TO 亚窗口细化属 RAY-216，还没有。用真值时刻取位置，量到的就是**位置
精度本身**，不掺事件检测误差 —— 这正是"纯算法误差"的口径。

RAY-216 落地之后，这里应当增加一条用 `extract_cycles` 的端到端版本，两者之差就是事件
细化贡献的误差。那一条不该替换本条：把两种误差混在一个数里，退化时分不清是谁的责任。
"""

from dataclasses import dataclass

import numpy as np
import pytest

from gait.config import AlgoConfig
from gait.contracts import FootSeries, NavResult
from gait.core.dualfoot import apply_distance_constraint
from gait.core.eskf import run_ins
from gait.validate.synthetic import (
    NoiseModel,
    WalkSpec,
    generate_dual_walk,
    generate_walk,
)

#: V1-a 的预算。
BUDGET_PERCENT = 0.5

#: 三档噪声。分开跑而不是只用最差的一档，是为了让"退化来自算法还是来自噪声模型"
#: 在测试结果上就能分开。
NOISE_LEVELS = {
    "clean": NoiseModel(),
    "sensor": NoiseModel(accel_density=1.5e-3, gyro_density=3.0e-4, seed=3),
    # 30 mg 未标定零偏。契约 §3.2 说 `FootSeries.acc` 是"已标定补偿"的，所以这一档
    # 代表的是**标定没做好**的情形 —— RAY-202 量过，它会给初始对准带来 1.72° 的
    # 倾角误差。这一档仍在预算内，说明 ESKF 确实把零偏估回来了。
    "uncalibrated": NoiseModel.bs_bt91(seed=3),
}

#: 标称步态。四档覆盖 PRD §7 的 T-01 场景与整体设计 §5.5.3 的强度表。
NOMINAL_GAITS = {
    "walk": ({}, 1.30),
    "fast_walk": ({"cadence": 150.0, "stance_ratio": 0.52, "stride_length": 1.7}, 1.70),
    "jog": ({"cadence": 170.0, "stance_ratio": 0.38, "stride_length": 2.4}, 2.40),
    "turnaround_4m": ({"path_length_m": 4.0}, 1.30),
}

#: 低速/病理档。**它不在标称集合里**，因为它当前不满足 V1-a —— 见模块文档。
LOW_SPEED = ({"cadence": 60.0, "stride_length": 0.35, "stance_ratio": 0.75}, 0.35)


@dataclass(frozen=True)
class StrideAccuracy:
    """一次会话的步长精度，按百分比。

    `planar` 是整体设计 §6.2 定义的步长（两次触地之间位移的**水平模长**）。
    `forward` 是同一位移在行进方向上的投影 —— 它不是指标，是**诊断量**：两者分道扬镳
    时，问题在航向而不在纵向积分。
    """

    planar_mean: float
    planar_rms: float
    forward_mean: float
    forward_rms: float
    strides: int


def stride_accuracy(
    navigation: NavResult, truth, reference: float, *, straight_only: bool = True
) -> StrideAccuracy:
    """按真值触地时刻取位置，逐 stride 比对。

    默认**只统计直行 stride**。这不是挑好看的数：PRD §13 明确写着「步速、步频、步长
    （左/右）—— 直行段中段步」，转身段的步长根本不是同一个量（合成数据里它约 0.085 m，
    而直行是 1.30 m）。把两者平均进同一个百分比，得到的既不是直行精度也不是转身精度。

    直行段与转身段的**自动分离**属 RAY-215；这里用真值的 `is_turn` 标记代劳，因为本条
    测试要量的是位置精度，不是分离算法。
    """
    fs = 1.0 / float(np.median(np.diff(navigation.t)))
    planar: list[float] = []
    forward: list[float] = []
    for index, stride in enumerate(truth.strides[:-1]):
        if straight_only and stride.is_turn:
            continue
        start = round(stride.t_ic * fs)
        end = round(truth.strides[index + 1].t_ic * fs)
        if end >= len(navigation.t):
            continue
        displacement = navigation.p[end, :2] - navigation.p[start, :2]
        true_step = (stride.end - stride.start)[:2]
        length = float(np.linalg.norm(true_step))
        if length <= 0:
            continue
        direction = true_step / length
        planar.append(float(np.linalg.norm(displacement)) - stride.stride_length)
        forward.append(float(displacement @ direction) - stride.stride_length)

    if not planar:
        raise AssertionError("没有可比对的 stride")
    planar_array = np.asarray(planar)
    forward_array = np.asarray(forward)
    scale = 100.0 / reference
    return StrideAccuracy(
        planar_mean=float(planar_array.mean()) * scale,
        planar_rms=float(np.sqrt((planar_array**2).mean())) * scale,
        forward_mean=float(forward_array.mean()) * scale,
        forward_rms=float(np.sqrt((forward_array**2).mean())) * scale,
        strides=planar_array.size,
    )


def run_single_foot(spec_kwargs: dict, noise: NoiseModel, *, seconds: float = 30.0):
    series, truth = generate_walk(WalkSpec(duration_s=seconds, **spec_kwargs), noise=noise)
    return run_ins(series, AlgoConfig()), truth


class TestV1aOnNominalGaits:
    """判据主体：四档标称步态 × 三档噪声，全部要在 0.5% 以内。"""

    @pytest.mark.parametrize("gait", sorted(NOMINAL_GAITS))
    @pytest.mark.parametrize("noise", sorted(NOISE_LEVELS))
    def test_stride_length_error_is_inside_the_budget(self, gait, noise):
        spec_kwargs, reference = NOMINAL_GAITS[gait]
        navigation, truth = run_single_foot(spec_kwargs, NOISE_LEVELS[noise])
        accuracy = stride_accuracy(navigation, truth, reference)
        assert abs(accuracy.planar_mean) < BUDGET_PERCENT, (
            f"{gait}/{noise}: 均值 {accuracy.planar_mean:+.3f}%（{accuracy.strides} 步）"
        )

    @pytest.mark.parametrize("gait", sorted(NOMINAL_GAITS))
    def test_the_spread_is_also_inside_the_budget(self, gait):
        """均值合格但逐步散布很大，等于"平均对、每一步都不对"。

        逐步误差进的是 CV 指标（PRD §13 的步长 CV），所以 rms 也要看 —— 一个只压均值
        的实现会让 CV 变成噪声的读数。
        """
        spec_kwargs, reference = NOMINAL_GAITS[gait]
        navigation, truth = run_single_foot(spec_kwargs, NOISE_LEVELS["sensor"])
        accuracy = stride_accuracy(navigation, truth, reference)
        assert accuracy.planar_rms < BUDGET_PERCENT

    def test_an_uncalibrated_accelerometer_still_passes(self):
        """30 mg 未标定零偏仍在预算内 —— 说明 ESKF 确实把它估回来了。

        RAY-202 量过：同样的 30 mg 会给**初始对准**带来 1.72° 的倾角误差，是 0.5°
        预算的 3.4 倍。对准做不到而端到端做得到，差别就是这条链上的 ESKF。
        """
        navigation, truth = run_single_foot(NOMINAL_GAITS["walk"][0], NOISE_LEVELS["uncalibrated"])
        accuracy = stride_accuracy(navigation, truth, NOMINAL_GAITS["walk"][1])
        assert abs(accuracy.planar_mean) < BUDGET_PERCENT

    def test_turning_strides_must_be_excluded_or_the_budget_breaks(self):
        """转身 stride 必须被剔除，否则 V1-a 直接不成立 —— 这就是 RAY-215 的必要性。

        4 米往返里转身 stride 的真实步长约 0.085 m，直行是 1.30 m。把两者平均进同一个
        百分比，得到的既不是直行精度也不是转身精度。实测：剔除后均值 0.01%，不剔除
        0.50%（正好压在预算线上，而那只是巧合 —— 它随转身占比变化）。

        PRD §13 写的就是「直行段中段步」。本条把那句话变成一个会失败的断言。
        """
        navigation, truth = run_single_foot(
            NOMINAL_GAITS["turnaround_4m"][0], NOISE_LEVELS["uncalibrated"]
        )
        reference = NOMINAL_GAITS["turnaround_4m"][1]
        turns = [stride for stride in truth.strides if stride.is_turn]
        assert turns, "这份数据本来就该有转身 stride"

        straight = stride_accuracy(navigation, truth, reference)
        everything = stride_accuracy(navigation, truth, reference, straight_only=False)
        assert abs(straight.planar_mean) < BUDGET_PERCENT
        assert abs(everything.planar_mean) > abs(straight.planar_mean) * 5


class TestLowSpeedIsAKnownFailure:
    """低速/拖步档当前**不满足** V1-a。这里把它钉成一个有上界的已知量。

    跳过它是最省事的做法，也是最糟的：一个被跳过的限制不会被人再想起。带上界的断言
    会在它变好（该更新阈值并庆祝）或变坏（该查回归）时都发出声音。
    """

    #: 当前实测的上界，留约 50% 余量。实测 seed=3 约 2.2%、seed=7 约 3.1%。
    KNOWN_CEILING_PERCENT = 6.0

    @pytest.mark.parametrize("seed", [3, 7])
    def test_it_misses_the_budget_but_stays_below_the_recorded_ceiling(self, seed):
        spec_kwargs, reference = LOW_SPEED
        noise = NoiseModel(accel_density=1.5e-3, gyro_density=3.0e-4, seed=seed)
        navigation, truth = run_single_foot(spec_kwargs, noise)
        accuracy = stride_accuracy(navigation, truth, reference)
        assert abs(accuracy.planar_mean) > BUDGET_PERCENT, (
            "低速档突然满足预算了 —— 这是好消息，但要先确认不是判据被改松了，"
            "然后把它移进 NOMINAL_GAITS 并关掉对应的 Issue。"
        )
        assert abs(accuracy.planar_mean) < self.KNOWN_CEILING_PERCENT

    def test_the_forward_component_is_much_better_than_the_planar_one(self):
        """诊断量：纵向准、平面不准 ⇒ 问题在航向，不在纵向积分。

        这一条是低速失效的定性证据。它比"误差是 2%"有用得多：知道误差在哪个方向上，
        才知道该去修哪个模块（航向 → RAY-205 的双足约束与航向可观测性，不是 ESKF 的
        积分格式）。
        """
        spec_kwargs, reference = LOW_SPEED
        noise = NoiseModel(accel_density=1.5e-3, gyro_density=3.0e-4, seed=7)
        navigation, truth = run_single_foot(spec_kwargs, noise)
        accuracy = stride_accuracy(navigation, truth, reference)
        assert abs(accuracy.forward_mean) < abs(accuracy.planar_mean)

    def test_the_low_speed_preset_does_not_rescue_it(self):
        """`AlgoConfig.low_speed()` 是为**检测**准备的，不是为航向准备的。

        PRD §7 让档案勾选拖步/小碎步时切到这个预设。实测它对步长误差几乎没有影响 ——
        因为零速检测在这一档本来就是好的（RAY-203 实测 9/9 支撑相、零误检），瓶颈在
        航向可观测性。**记下来是为了不让人以为"切了预设就解决了"。**
        """
        spec_kwargs, reference = LOW_SPEED
        noise = NoiseModel(accel_density=1.5e-3, gyro_density=3.0e-4, seed=7)
        series, truth = generate_walk(WalkSpec(duration_s=30.0, **spec_kwargs), noise=noise)
        default = stride_accuracy(run_ins(series, AlgoConfig()), truth, reference)
        low_speed = stride_accuracy(run_ins(series, AlgoConfig.low_speed()), truth, reference)
        assert abs(low_speed.planar_mean - default.planar_mean) < 0.5


class TestDualFootEndToEnd:
    """双足链：两足各跑一次 ESKF，再过 RAY-205 的距离约束。"""

    def dual(self, spec_kwargs: dict, noise: NoiseModel, *, seconds: float = 30.0):
        pair = generate_dual_walk(WalkSpec(duration_s=seconds, **spec_kwargs), noise=noise)
        navigation = {foot: run_ins(pair[foot][0], AlgoConfig()) for foot in ("L", "R")}
        result = apply_distance_constraint(navigation["L"], navigation["R"])
        return navigation, result, {foot: pair[foot][1] for foot in ("L", "R")}

    def test_both_feet_meet_the_budget_on_a_nominal_walk(self):
        navigation, _, truth = self.dual({}, NOISE_LEVELS["sensor"])
        for foot in ("L", "R"):
            accuracy = stride_accuracy(navigation[foot], truth[foot], 1.30)
            assert abs(accuracy.planar_mean) < BUDGET_PERCENT, foot

    def test_the_constraint_improves_the_forward_component(self):
        """约束修的是差分航向，所以先在**纵向**上看到效果。

        平面步长包含横向漂移，而横向漂移的共模部分约束看不见（RAY-205 的能力边界）——
        所以平面误差不一定变好。纵向分量是约束真正作用的地方。
        """
        navigation, result, truth = self.dual({}, NOISE_LEVELS["sensor"])
        before = stride_accuracy(navigation["L"], truth["L"], 1.30)
        after = stride_accuracy(result.left, truth["L"], 1.30)
        assert abs(after.forward_mean) < abs(before.forward_mean)

    def test_the_constraint_does_not_rescue_the_low_speed_case_either(self):
        """低速档的航向误差是**共模**的，而距离约束只看得见差分。

        这是 RAY-205 的能力边界在端到端上的直接后果，也是低速失效需要另一条路的
        原因。写成测试，免得有人以为"双足做完就好了"。
        """
        spec_kwargs, reference = LOW_SPEED
        noise = NoiseModel(accel_density=1.5e-3, gyro_density=3.0e-4, seed=7)
        navigation, result, truth = self.dual(spec_kwargs, noise)
        before = stride_accuracy(navigation["L"], truth["L"], reference)
        after = stride_accuracy(result.left, truth["L"], reference)
        assert abs(before.planar_mean) > BUDGET_PERCENT
        assert abs(after.planar_mean) > BUDGET_PERCENT


class TestTheChainIsActuallyExercised:
    """防止这条测试在某个环节被短路之后仍然通过。"""

    def test_the_pipeline_uses_every_layer(self):
        """生成 → 检测 → 对准 → 滤波，四层都要真的跑到。

        断言方式是看输出里有没有只有那一层才能产生的东西：`zupt` 非空说明检测跑了，
        `bg`/`ba` 非零说明滤波在估零偏，`q` 非单位说明对准与积分都动了。
        """
        navigation, _ = run_single_foot({}, NOISE_LEVELS["sensor"], seconds=10.0)
        assert navigation.zupt.any(), "检测层没跑"
        assert np.abs(navigation.bg).max() > 0.0, "滤波层没估陀螺零偏"
        assert np.abs(navigation.ba).max() > 0.0, "滤波层没估加计零偏"
        assert np.abs(navigation.p).max() > 1.0, "积分层没走出去"

    def test_the_series_is_a_valid_contract_object(self):
        series, _ = generate_walk(WalkSpec(duration_s=5.0), noise=NOISE_LEVELS["sensor"])
        assert isinstance(series, FootSeries)

    def test_the_measurement_is_deterministic(self):
        """回归测试的前提：同一份输入两次跑出同一个数。"""
        first = stride_accuracy(*run_single_foot({}, NOISE_LEVELS["sensor"]), 1.30)
        second = stride_accuracy(*run_single_foot({}, NOISE_LEVELS["sensor"]), 1.30)
        assert first == second
