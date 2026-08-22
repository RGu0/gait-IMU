"""`gait.core.zupt` 的零速检测。

验收标准是"合成数据下常速检出率与误检率达标；预设切换接口可热换"。这里把"达标"
写成三条可断言的性质，因为一个笼统的检出率能被两种完全相反的失效方式满足：

1. **每一个完整的真实支撑相都被检到**（漏检 = 这一步的误差不受约束）。
2. **零误检**：检出的样本全部落在真实支撑相内（误检 = 毁掉整条轨迹）。
3. **保守**：检出区间是真实支撑相的**子集**，边界向内收。

第 2、3 条比第 1 条重要。模块文档里的那条不对称就是这个意思：漏检损失一步，误检
毁掉全部。逐样本召回率因此**不该**被推到 1.0 —— 那只能靠让判据变松来达成，而那正是
误检的来路。
"""

from dataclasses import replace

import numpy as np
import pytest

from gait.config import AlgoConfig
from gait.core.ins import GRAVITY_STANDARD
from gait.core.zupt import StanceDetection, ZuptError, detect_stance, lowpass
from gait.validate.synthetic import NoiseModel, WalkSpec, generate_walk

FS = 200.0

#: 各档步态。慢跑那一档的支撑相只有约 270 ms，是检测窗口与低通抽头都会被挤压的地方。
GAIT_CASES = {
    "walk": WalkSpec(duration_s=20.0),
    "fast_walk": WalkSpec(duration_s=20.0, cadence=150.0, stance_ratio=0.52, stride_length=1.7),
    "jog": WalkSpec(duration_s=20.0, cadence=170.0, stance_ratio=0.38, stride_length=2.4),
    "shuffle": WalkSpec(duration_s=20.0, cadence=60.0, stride_length=0.35, stance_ratio=0.75),
}


def truth_mask(length: int, stance: list[tuple[int, int]]) -> np.ndarray:
    mask = np.zeros(length, dtype=bool)
    for start, end in stance:
        mask[start:end] = True
    return mask


def detected_and_truth(spec: WalkSpec, cfg: AlgoConfig | None = None, noise: NoiseModel | None = None):
    series, truth = generate_walk(spec, noise=noise or NoiseModel.bs_bt91())
    detection = detect_stance(series.acc, series.gyr, series.fs, cfg or AlgoConfig())
    return detection, truth, truth_mask(len(series.t), truth.stance)


def _hard_runs(detection) -> list[tuple[int, int]]:
    """只由硬检测构成的区间，供"软零速有没有补在两端之外"这类断言使用。"""
    runs: list[tuple[int, int]] = []
    mask = detection.hard
    padded = np.concatenate(([False], mask, [False]))
    edges = np.flatnonzero(padded[1:] != padded[:-1])
    for start, end in zip(edges[::2], edges[1::2], strict=True):
        runs.append((int(start), int(end)))
    return runs


def still_signal(n: int, *, acc=(0.0, 0.0, GRAVITY_STANDARD), gyr=(0.0, 0.0, 0.0)):
    return np.tile(acc, (n, 1)), np.tile(gyr, (n, 1))


class TestDetectionOnSyntheticGait:
    """验收标准的主体。四档步态，每档都断言同样的三条性质。"""

    @pytest.mark.parametrize("name", sorted(GAIT_CASES))
    def test_every_complete_stance_is_detected(self, name):
        detection, truth, _ = detected_and_truth(GAIT_CASES[name])
        # 末尾被记录长度截断的那一段不算：它不是漏检，是记录在那里停了。
        complete = [(a, b) for a, b in truth.stance if b < len(detection.zupt)]
        missed = [(a, b) for a, b in complete if not detection.zupt[a:b].any()]
        assert not missed, f"{name}: 漏检 {len(missed)}/{len(complete)} 个支撑相：{missed[:3]}"

    @pytest.mark.parametrize("name", sorted(GAIT_CASES))
    def test_there_are_no_false_positives(self, name):
        """检出的样本必须全部落在真实支撑相内。这是三条里最重要的一条。"""
        detection, _, mask = detected_and_truth(GAIT_CASES[name])
        false_positives = int((detection.zupt & ~mask).sum())
        assert false_positives == 0, f"{name}: {false_positives} 个误检样本"

    @pytest.mark.parametrize("name", sorted(GAIT_CASES))
    def test_detection_is_conservative_but_not_empty(self, name):
        """检出区间向内收，但不能收到没有。

        向内收是中心滑窗 + 低通抽头的必然结果：靠近边界的样本，其窗口有一部分落在
        摆动相里。这**不是缺陷** —— 它换来的正是上一条的零误检。但边界的系统性内缩
        意味着**不能拿检出区间的端点当 IC/TO**（整体设计 §6.1 也这么说），事件细化是
        RAY-216 的事。
        """
        detection, _, mask = detected_and_truth(GAIT_CASES[name])
        recall = float((detection.zupt & mask).sum() / mask.sum())
        assert 0.5 < recall < 1.0, f"{name}: 逐样本召回率 {recall:.3f}"

    def test_noiseless_data_behaves_the_same(self):
        """无噪声不该让结论变好也不该让它变坏 —— 否则说明判据吃的是噪声不是信号。"""
        clean, _, clean_mask = detected_and_truth(GAIT_CASES["walk"], noise=NoiseModel())
        noisy, _, _ = detected_and_truth(GAIT_CASES["walk"])
        assert int((clean.zupt & ~clean_mask).sum()) == 0
        assert abs(clean.zupt.sum() - noisy.zupt.sum()) < 0.05 * clean.zupt.sum()


class TestEachCriterionIsLoadBearing:
    """五条判据都必须被真正读取。收紧任何一条都应当让检出变少。

    这类测试的价值在于抓"参数加了但没接上"—— 那种缺陷不会报错，只会让某个旋钮
    转起来毫无反应，而排查它要先怀疑到"是不是根本没生效"。
    """

    @pytest.mark.parametrize(
        ("field", "tightened"),
        [
            ("zupt_acc_threshold", 1e-4),
            ("zupt_gyr_threshold", 1e-5),
            ("zupt_acc_variance_threshold", 1e-8),
            ("zupt_gyr_variance_threshold", 1e-10),
            ("zupt_glrt_threshold", 1e-3),
        ],
    )
    def test_tightening_one_threshold_reduces_detection(self, field, tightened):
        baseline, _, _ = detected_and_truth(GAIT_CASES["walk"])
        strict, _, _ = detected_and_truth(
            GAIT_CASES["walk"], cfg=replace(AlgoConfig(), **{field: tightened})
        )
        assert strict.zupt.sum() < baseline.zupt.sum(), field

    def test_glrt_is_the_binding_criterion_at_the_current_defaults(self):
        """把 C1–C4 全部放宽十倍，结果一字不变；只有动 γ5 才有效果。

        这不是缺陷 —— 整体设计 §5.5.2 规定的分工就是"C1–C4 粗筛、GLRT 终判"。但它
        值得钉成一条测试，因为它有一个反直觉的后果：**只调 C1–C4 的阈值而不动 γ5，
        检测行为不会变。** 一个照着判据表逐条调参的人会以为自己在调检测器，实际什么
        也没发生。

        σ 值来自 Allan 方差标定之后（RAY-207）这个平衡可能改变，届时这条测试会失败，
        而那正是应该重新审视分工的时刻。
        """
        series, _ = generate_walk(GAIT_CASES["walk"], noise=NoiseModel.bs_bt91())
        base = AlgoConfig()
        loose_coarse = replace(
            base,
            zupt_acc_threshold=base.zupt_acc_threshold * 10,
            zupt_gyr_threshold=base.zupt_gyr_threshold * 10,
            zupt_acc_variance_threshold=base.zupt_acc_variance_threshold * 10,
            zupt_gyr_variance_threshold=base.zupt_gyr_variance_threshold * 10,
        )
        unchanged = detect_stance(series.acc, series.gyr, series.fs, loose_coarse)
        baseline = detect_stance(series.acc, series.gyr, series.fs, base)
        assert np.array_equal(unchanged.zupt, baseline.zupt)

        tighter_glrt = detect_stance(
            series.acc, series.gyr, series.fs, replace(base, zupt_glrt_threshold=0.4)
        )
        assert tighter_glrt.zupt.sum() < baseline.zupt.sum()


class TestKnownBlindSpot:
    def test_a_steady_horizontal_acceleration_reads_as_stance(self):
        """匀加速平移 + 零转动会被判成静止。这是加速度计的物理歧义，不是实现缺陷。

        加速度计测的是比力，无法把"倾斜一点"和"水平加速一点"分开。当前默认值下的
        边界：水平加速度小于约 1.76 m/s² 且完全不转动时，整段读作静止。

        这条测试钉住那个边界。它不表达"这样是对的"，而是表达"这样是已知的" ——
        阈值或 σ 一旦改动，边界会移动，而移动的方向需要有人看见。

        真实步态里救命的是 C3/C4：摆动的足部一定在转。因此这个盲区在实践中要靠
        角速度判据挡，而不是靠加速度判据。
        """
        detector = AlgoConfig()
        n = 1000
        below = detect_stance(*still_signal(n, acc=(1.5, 0.0, GRAVITY_STANDARD)), FS, detector)
        above = detect_stance(*still_signal(n, acc=(2.0, 0.0, GRAVITY_STANDARD)), FS, detector)
        assert below.zupt.all(), "1.5 m/s² 落在盲区内 —— 这是当前的已知行为"
        assert not above.zupt.any(), "2.0 m/s² 应当被拒"

    def test_rotation_rescues_the_blind_spot(self):
        """同样的加速度加上摆动量级的转动，立刻被拒。"""
        detector = AlgoConfig()
        n = 1000
        rescued = detect_stance(
            *still_signal(n, acc=(1.5, 0.0, GRAVITY_STANDARD), gyr=(0.0, 1.0, 0.0)),
            FS,
            detector,
        )
        assert not rescued.zupt.any()


class TestZaru:
    def test_by_default_zaru_only_supports_a_detected_stance(self):
        """默认预设下 ZARU 是零速观测的补充，不是独立的约束来源。"""
        detection, _, _ = detected_and_truth(GAIT_CASES["walk"])
        assert np.array_equal(detection.zaru, detection.zaru & detection.hard)

    def test_the_low_speed_preset_lets_zaru_stand_on_its_own(self):
        """PRD §7：低速/病理预设强制 ZARU。

        场景就是"足部有平动但不转"—— 拖步的典型形态。默认预设在这里什么约束都给不出，
        低速预设仍然给得出 ZARU。整体设计 §5.6.2 说这条比零速更鲁棒，这是它的落点。
        """
        n = 1000
        translating = still_signal(n, acc=(3.0, 0.0, GRAVITY_STANDARD))
        default = detect_stance(*translating, FS, AlgoConfig())
        low_speed = detect_stance(*translating, FS, AlgoConfig.low_speed())
        assert not default.zupt.any() and not default.zaru.any()
        assert not low_speed.zupt.any(), "零速判据仍然不该通过 —— 足部确实在加速"
        assert low_speed.zaru.all(), "但角速度是零，ZARU 该成立"


class TestSoftZupt:
    """整体设计 §5.5.3 的降级策略。"""

    def build_walk_with_one_ruined_stance(self):
        """把第 4 个支撑相搅乱，模拟"这一步没检到"。

        直接改 `acc`/`gyr` 而不改真值轨迹：这里要考的是检测器在缺一步时怎么办，
        不是生成一段物理自洽的数据。测试文档写清楚，免得它被当成合成数据的用法示例。
        """
        series, truth = generate_walk(GAIT_CASES["walk"], noise=NoiseModel.bs_bt91())
        acc = series.acc.copy()
        gyr = series.gyr.copy()
        start, end = truth.stance[4]
        rng = np.random.default_rng(11)
        acc[start:end] += rng.normal(scale=2.0, size=(end - start, 3))
        gyr[start:end] += rng.normal(scale=1.0, size=(end - start, 3))
        return acc, gyr, series.fs, (start, end)

    def test_a_missed_stance_is_filled_by_a_degraded_one(self):
        acc, gyr, fs, (start, end) = self.build_walk_with_one_ruined_stance()
        cfg = replace(AlgoConfig(), soft_zupt_gap_samples=150)
        detection = detect_stance(acc, gyr, fs, cfg)
        assert detection.degraded.any(), "缺了一步，软零速应当补上"
        # 补的位置应当落在被搅乱的那个支撑相附近，而不是随便一个摆动相里。
        filled = np.flatnonzero(detection.degraded)
        centre = 0.5 * (start + end)
        assert abs(float(filled.mean()) - centre) < (end - start)

    def test_degraded_samples_carry_a_capped_confidence(self):
        """降级的零速是"这一步一定发生了"推出来的，不是测出来的。"""
        acc, gyr, fs, _ = self.build_walk_with_one_ruined_stance()
        detection = detect_stance(acc, gyr, fs, replace(AlgoConfig(), soft_zupt_gap_samples=150))
        assert np.all(detection.confidence[detection.degraded] <= 0.25)

    def test_hard_and_degraded_are_disjoint(self):
        acc, gyr, fs, _ = self.build_walk_with_one_ruined_stance()
        detection = detect_stance(acc, gyr, fs, replace(AlgoConfig(), soft_zupt_gap_samples=150))
        assert not (detection.hard & detection.degraded).any()
        assert np.array_equal(detection.zupt, detection.hard | detection.degraded)

    def test_nothing_is_invented_at_the_ends_of_the_record(self):
        """序列首尾的空档不补。

        开头与结尾的截断只说明记录在这里停了，不说明这中间漏了一步。在那里硬塞一个
        零速观测是凭空发明数据 —— 而 ESKF 会毫不怀疑地接受它。

        用"缺了一步"的那份数据来验，否则 `degraded` 全空，这条测试等于什么都没测。
        """
        acc, gyr, fs, _ = self.build_walk_with_one_ruined_stance()
        detection = detect_stance(acc, gyr, fs, replace(AlgoConfig(), soft_zupt_gap_samples=150))
        assert detection.degraded.any()
        hard_runs = _hard_runs(detection)
        assert not detection.degraded[: hard_runs[0][0]].any()
        assert not detection.degraded[hard_runs[-1][1] :].any()

    def test_a_single_hard_stance_yields_no_soft_ones(self):
        """一个支撑相都没有或只有一个时，无从判断中间漏了几步 —— 不猜。"""
        n = 600
        acc, gyr = still_signal(n)
        acc[200:] += np.array([8.0, 0.0, 0.0])
        detection = detect_stance(acc, gyr, FS, AlgoConfig())
        assert not detection.degraded.any()


class TestPresetHotSwap:
    """验收标准第二句：预设切换接口可热换。"""

    def test_switching_presets_is_pure(self):
        """反复交替调用与各自单独调用必须给出同一个结果 —— 模块内不得有状态。"""
        series, _ = generate_walk(GAIT_CASES["walk"], noise=NoiseModel.bs_bt91())
        default = AlgoConfig()
        low = AlgoConfig.low_speed()
        first = detect_stance(series.acc, series.gyr, series.fs, default)
        second = detect_stance(series.acc, series.gyr, series.fs, low)
        for _ in range(3):
            assert np.array_equal(
                detect_stance(series.acc, series.gyr, series.fs, default).zupt, first.zupt
            )
            assert np.array_equal(
                detect_stance(series.acc, series.gyr, series.fs, low).zupt, second.zupt
            )

    def test_the_two_presets_actually_differ(self):
        """换了没区别就不算接口 —— 那只是一个被忽略的参数。"""
        series, _ = generate_walk(GAIT_CASES["walk"], noise=NoiseModel.bs_bt91())
        default = detect_stance(series.acc, series.gyr, series.fs, AlgoConfig())
        low = detect_stance(series.acc, series.gyr, series.fs, AlgoConfig.low_speed())
        assert not np.array_equal(default.zupt, low.zupt)

    def test_omitting_the_config_uses_the_default_preset(self):
        series, _ = generate_walk(GAIT_CASES["walk"], noise=NoiseModel.bs_bt91())
        implicit = detect_stance(series.acc, series.gyr, series.fs)
        explicit = detect_stance(series.acc, series.gyr, series.fs, AlgoConfig())
        assert np.array_equal(implicit.zupt, explicit.zupt)


class TestSignalSeparation:
    """整体设计 §5.2 第 3 条：检测用信号低通，积分用信号原始。"""

    def test_the_filtered_signal_never_leaves_the_detector(self):
        """滤波结果连出口都没有，下游拿不到，也就没法误用。"""
        fields = set(StanceDetection.__dataclass_fields__)
        assert fields == {"zupt", "zaru", "degraded", "stances", "score", "confidence"}

    def test_the_lowpass_is_zero_phase(self):
        """对称输入进去，对称输出出来。相位滞后会让检出的支撑相整体后移。

        用对称性而不是"阶跃中点是半幅"来验：后者对**任何**对称 FIR 都不成立，
        阶跃响应在跳变处是 `0.5 + h[中心]/2`。对称性才是零相位的定义。
        """
        n = 400
        centre = n // 2
        signal = np.zeros((n, 3))
        signal[centre - 5 : centre + 6, 0] = 1.0
        filtered = lowpass(signal, FS, 8.0)[:, 0]
        offsets = np.arange(1, 60)
        assert np.allclose(filtered[centre - offsets], filtered[centre + offsets], atol=1e-12)
        assert int(np.argmax(filtered)) == centre

    def test_the_lowpass_preserves_a_constant(self):
        """静止段是常量。若低通改变它的幅值，C1 会整体偏移。"""
        constant = np.tile([0.3, -1.2, GRAVITY_STANDARD], (500, 1))
        assert np.allclose(lowpass(constant, FS, 8.0), constant)

    def test_taps_are_capped_so_short_stances_survive(self):
        """抽头受检测窗口约束。不受约束的话，慢跑那 270 ms 的支撑相会被整个抹平。"""
        step = np.zeros((600, 3))
        step[300:, 0] = 1.0
        wide = lowpass(step, FS, 2.0)
        narrow = lowpass(step, FS, 2.0, max_taps=21)
        # 抹开的宽度用「离开 [0.02, 0.98] 区间的样本数」量。
        def smear(x):
            return int(((x[:, 0] > 0.02) & (x[:, 0] < 0.98)).sum())

        assert smear(narrow) < smear(wide)

    def test_a_cutoff_at_or_above_nyquist_is_refused(self):
        with pytest.raises(ZuptError, match="Nyquist"):
            lowpass(np.zeros((100, 3)), FS, FS)

    def test_a_sequence_shorter_than_the_filter_is_refused(self):
        """把太短的段整体抹平会让它看起来像静止段 —— 最不该误判的方向。"""
        with pytest.raises(ZuptError, match="看起来像静止段"):
            lowpass(np.zeros((5, 3)), FS, 2.0)


class TestScoreSemantics:
    def test_the_statistic_is_finite_everywhere_and_non_negative(self):
        """统计量不受粗筛门控。

        软零速降级需要在"一条都没通过粗筛"的区间里挑出最像静止的那一刻；若统计量在
        那里不存在，就只能任取一个位置 —— 而那个位置多半落在摆动相里。
        """
        detection, _, mask = detected_and_truth(GAIT_CASES["walk"])
        assert np.all(np.isfinite(detection.score))
        assert np.all(detection.score >= 0.0)
        # 序必须成立：支撑相里的统计量显著低于摆动相。
        assert np.median(detection.score[mask]) < 0.01 * np.median(detection.score[~mask])

    def test_confidence_is_zero_off_stance_and_positive_on_it(self):
        detection, _, _ = detected_and_truth(GAIT_CASES["walk"])
        assert np.all(detection.confidence[~detection.zupt] == 0.0)
        assert np.all(detection.confidence[detection.zupt] > 0.0)
        assert np.all(detection.confidence <= 1.0)

    def test_a_quieter_window_scores_lower(self):
        """置信度的绝对值没有校准过，但它的**序**必须成立。"""
        n = 1000
        quiet = detect_stance(*still_signal(n), FS, AlgoConfig())
        rng = np.random.default_rng(3)
        acc, gyr = still_signal(n)
        noisy = detect_stance(
            acc + rng.normal(scale=0.05, size=(n, 3)),
            gyr + rng.normal(scale=0.005, size=(n, 3)),
            FS,
            AlgoConfig(),
        )
        assert np.median(quiet.score) < np.median(noisy.score)
        assert np.median(quiet.confidence) > np.median(noisy.confidence)


class TestContractShape:
    def test_stances_match_the_zupt_mask(self):
        detection, _, _ = detected_and_truth(GAIT_CASES["walk"])
        rebuilt = np.zeros_like(detection.zupt)
        for start, end in detection.stances:
            rebuilt[start:end] = True
        assert np.array_equal(rebuilt, detection.zupt)

    def test_stances_are_ordered_and_disjoint(self):
        """契约 `_check_segments` 的要求：升序、不重叠、落在范围内。"""
        detection, _, _ = detected_and_truth(GAIT_CASES["walk"])
        previous = 0
        for start, end in detection.stances:
            assert 0 <= start < end <= len(detection.zupt)
            assert start >= previous
            previous = end

    def test_short_fragments_are_dropped(self):
        """50 ms 的碎片只可能是噪声或摆动相里的瞬时巧合，而它注入的是一次误检。"""
        detection, _, _ = detected_and_truth(GAIT_CASES["walk"])
        minimum = AlgoConfig().min_stance_samples
        hard_runs = [
            (start, end)
            for start, end in detection.stances
            if not detection.degraded[start:end].any()
        ]
        assert all(end - start >= minimum for start, end in hard_runs)


class TestRejections:
    def test_wrong_shapes(self):
        with pytest.raises(ZuptError):
            detect_stance(np.zeros((100, 2)), np.zeros((100, 2)), FS)

    def test_mismatched_lengths(self):
        with pytest.raises(ZuptError, match="样本数必须一致"):
            detect_stance(np.zeros((100, 3)), np.zeros((90, 3)), FS)

    def test_a_segment_shorter_than_the_window_is_refused(self):
        """空洞切分会切出这样的碎段。缩小窗口会让判据的含义随段长变化，所以拒绝。"""
        cfg = AlgoConfig()
        acc, gyr = still_signal(cfg.zupt_window_samples - 1)
        with pytest.raises(ZuptError, match="短于检测窗口"):
            detect_stance(acc, gyr, FS, cfg)
