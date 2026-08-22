"""`gait.sync.timebase` 的主机侧时基。

验收标准两条：**回放数据可复现同一时基**；**实测采样率估计稳定（相邻窗口差 < 0.1%）**。

这个文件里除了那两条，还有一组测试专门回答"最小值滤波到底值不值" —— 因为实测下来
它救的是 offset 而不是斜率，与直觉相反。而 offset 误差**就是**跨足同步误差，所以
那一组测试守的是 PRD §8 里 ±10~30 ms 那个预期不要由主机侧算法自己制造出来。
"""

from dataclasses import replace

import numpy as np
import pytest

from gait.config import AlgoConfig
from gait.sync.timebase import (
    SYNC_REPORT_VERSION,
    TimebaseError,
    build_timebase,
    cross_foot_uncertainty,
)

#: 真实采样率。刻意不是 200.0 —— 器件晶振有几百 ppm 偏差，用标称值会让所有时间参数
#: 系统性偏移，而本模块存在的一半理由就是把这个偏差量出来。
FS_TRUE = 200.3
NOMINAL_FS = 200.0
#: 固有链路延迟。它**不可观测**（对所有样本一样），会整体落进 offset。
BASE_LATENCY = 0.012


def simulate_arrival(
    *,
    n: int = 36000,
    fs_true: float = FS_TRUE,
    per_packet: int = 4,
    jitter_mean: float = 0.004,
    retransmit_rate: float = 0.01,
    retransmit_cost: float = 0.05,
    congestion: tuple[float, float] | None = None,
    seed: int = 0,
) -> np.ndarray:
    """模拟一台设备的 BLE 到达时刻。

    模型的三条性质是真实的，测试全都建立在它们上面：

    1. **一包 m 个样本整包到达**，包内逐帧解析只差几微秒（`wt901` 的实际行为）。
    2. **延迟单边为正**，服从长尾分布（指数 + 偶发重传）。
    3. **有序交付**：一次重传把它自己和后面的都往后推，不会造成乱序（L2CAP 是有序的）。

    第 3 条要紧 —— 第一版的模拟没有它，产生了乱序数据，被 `build_timebase` 的单调性
    检查当场拒掉。那次拒绝是对的：真实链路不会那样。

    `congestion` = `(起始比例, 额外延迟)`，模拟链路在会话中途变差。
    """
    rng = np.random.default_rng(seed)
    true_time = np.arange(n) / fs_true
    arrival = np.empty(n)
    previous = -np.inf
    for start in range(0, n, per_packet):
        stop = min(start + per_packet, n)
        extra = congestion[1] if congestion is not None and start / n >= congestion[0] else 0.0
        latency = BASE_LATENCY + extra + rng.exponential(jitter_mean)
        if rng.random() < retransmit_rate:
            latency += retransmit_cost
        moment = max(true_time[stop - 1] + latency, previous + 1e-4)
        previous = moment
        arrival[start:stop] = moment + 1e-6 * np.arange(stop - start)
    return arrival


def naive_fit(arrival: np.ndarray) -> tuple[float, float]:
    """不做最小值滤波的朴素最小二乘，作对照。返回 (offset, fs)。"""
    index = np.arange(arrival.size, dtype=np.float64)
    matrix = np.stack([np.ones_like(index), index], axis=1)
    solution, *_ = np.linalg.lstsq(matrix, arrival, rcond=None)
    return float(solution[0]), 1.0 / float(solution[1])


def fs_error_percent(fs: float, fs_true: float = FS_TRUE) -> float:
    return 100.0 * (fs - fs_true) / fs_true


class TestAcceptance:
    def test_the_measured_sampling_rate_is_recovered(self):
        """标称 200 Hz、实际 200.3 Hz。用标称值会让所有时间参数偏 0.15%。"""
        report = build_timebase(simulate_arrival(), NOMINAL_FS).report
        assert abs(fs_error_percent(report.fs)) < 0.01
        assert report.fs_deviation_ppm == pytest.approx(1500.0, rel=0.05)

    def test_the_rate_estimate_is_stable_across_windows(self):
        """验收标准：相邻窗口差 < 0.1%。"""
        report = build_timebase(simulate_arrival(), NOMINAL_FS).report
        assert report.fs_windows >= 2
        assert report.fs_window_spread < 1e-3
        assert report.stable

    def test_replaying_the_same_arrivals_reproduces_the_same_timebase(self):
        """验收标准：回放数据可复现同一时基。逐 bit 相同，不是"接近"。"""
        arrival = simulate_arrival(n=8000)
        first = build_timebase(arrival, NOMINAL_FS)
        second = build_timebase(arrival, NOMINAL_FS)
        assert np.array_equal(first.t, second.t)
        assert first.report == second.report

    def test_the_timebase_is_a_straight_line_through_the_samples(self):
        report_and_t = build_timebase(simulate_arrival(n=8000), NOMINAL_FS)
        steps = np.diff(report_and_t.t)
        assert np.allclose(steps, steps[0])
        assert steps[0] == pytest.approx(1.0 / report_and_t.report.fs)


class TestWhatTheMinimumFilterActuallyBuys:
    """实测下来它救的是 **offset**，不是斜率 —— 与直觉相反，所以单独一组。"""

    @pytest.mark.parametrize(
        ("label", "kwargs"),
        [
            ("平稳", {}),
            ("抖动大 3 倍", {"jitter_mean": 0.012}),
            ("每包 12 样本", {"per_packet": 12}),
        ],
    )
    def test_the_offset_lands_on_the_true_link_latency(self, label, kwargs):
        """最小值滤波给出的 offset 应当贴着**固有**延迟，而不是平均延迟。"""
        arrival = simulate_arrival(**kwargs)
        report = build_timebase(arrival, NOMINAL_FS).report
        naive_offset, _ = naive_fit(arrival)
        assert abs(report.offset - BASE_LATENCY) < 0.002, label
        assert abs(naive_offset - BASE_LATENCY) > 0.010, label

    def test_the_slope_is_barely_different_from_naive_least_squares(self):
        """斜率上两者几乎一样 —— 独立同分布的噪声不偏最小二乘的斜率。

        这条测试存在是为了**不让人以为最小值滤波是为了采样率**。它不是。把它去掉，
        采样率照样准，而跨足同步误差会凭空多出十几毫秒。
        """
        arrival = simulate_arrival()
        report = build_timebase(arrival, NOMINAL_FS).report
        _, naive = naive_fit(arrival)
        assert abs(fs_error_percent(report.fs) - fs_error_percent(naive)) < 0.001

    def test_an_offset_bias_is_exactly_a_cross_foot_sync_error(self):
        """两台设备抖动分布不同 → 朴素法的 offset 偏差不同 → 直接落进跨足指标。

        PRD §8 预期的跨足误差是 ±10~30 ms。这条测试说明：不做最小值滤波，那个预期
        **会由主机侧算法自己制造出来**。
        """
        left = simulate_arrival(jitter_mean=0.004, seed=0)
        right = simulate_arrival(jitter_mean=0.012, per_packet=8, seed=1)
        filtered = abs(
            build_timebase(left, NOMINAL_FS).report.offset
            - build_timebase(right, NOMINAL_FS).report.offset
        )
        naive = abs(naive_fit(left)[0] - naive_fit(right)[0])
        assert filtered < 0.003
        assert naive > 0.010
        assert naive > 3.0 * filtered


class TestMidSessionCongestion:
    """单直线拟合治不了链路中途变差，但验收标准恰好挡得住它。"""

    @pytest.mark.parametrize(
        ("extra", "expect_stable"),
        [(0.010, True), (0.040, False), (0.150, False)],
    )
    def test_the_stability_flag_detects_what_the_fit_cannot_fix(self, extra, expect_stable):
        """`stable` 不是装饰性的布尔值，是"这段数据能不能用一条直线描述"的判断。

        实测：+10 ms 的台阶只让采样率偏 0.008%（可忽略），+40 ms 起偏到 0.03% 以上。
        0.1% 这条线正好落在两者之间 —— 验收标准的阈值恰好是一个工作的检测器。
        """
        report = build_timebase(
            simulate_arrival(congestion=(0.5, extra)), NOMINAL_FS
        ).report
        assert report.stable is expect_stable

    def test_a_flagged_session_really_does_have_a_biased_rate(self):
        """反过来验一次：被标记不稳定的那一档，采样率确实偏了。

        没有这一条，`stable` 可能只是一个恰好在报警的指标。
        """
        calm = build_timebase(simulate_arrival(), NOMINAL_FS).report
        congested = build_timebase(
            simulate_arrival(congestion=(0.5, 0.150)), NOMINAL_FS
        ).report
        assert abs(fs_error_percent(congested.fs)) > 20.0 * abs(fs_error_percent(calm.fs))


class TestPacketReconstruction:
    def test_packets_are_recognised_from_the_arrival_clustering(self):
        """包数**接近**但不精确等于 n/m，而那是对的。

        一次重传之后积压会成串涌出，几包挤在几十微秒内到达 —— 那在时间轴上确实就是
        一簇，检测器把它们并成一包是如实反映。实测 12000/6 = 2000 包里并掉约 1%。

        断言写成"接近"而不是"相等"，是因为**相等要求的是模拟器没有积压**，而真实
        链路有。一个要求相等的断言会逼着后来的人去把模拟改得不真实。
        """
        arrival = simulate_arrival(n=12000, per_packet=6)
        report = build_timebase(arrival, NOMINAL_FS).report
        assert report.samples_per_packet == pytest.approx(6.0)
        assert report.packets == pytest.approx(12000 // 6, rel=0.05)

    @pytest.mark.parametrize("per_packet", [1, 2, 4, 8, 16])
    def test_the_rate_estimate_does_not_depend_on_packet_size(self, per_packet):
        """不回推包内时刻的话，斜率会随每次通知里的样本数变化。

        而那个数由 BLE 连接间隔决定，是会变的 —— 于是同一台设备在不同时段会解出
        不同的采样率，且没有任何东西会报错。
        """
        report = build_timebase(
            simulate_arrival(n=12000, per_packet=per_packet), NOMINAL_FS
        ).report
        assert abs(fs_error_percent(report.fs)) < 0.02, per_packet

    def test_without_the_back_out_the_rate_would_be_wrong(self):
        """把回推关掉（等价于把包大小误认成 1）会怎样 —— 用朴素拟合逼近这个情形。

        每包 16 个样本时，朴素法把一簇 16 个样本当成同一时刻采到的，斜率因此偏掉。
        """
        arrival = simulate_arrival(n=12000, per_packet=16)
        report = build_timebase(arrival, NOMINAL_FS).report
        _, naive = naive_fit(arrival)
        assert abs(fs_error_percent(report.fs)) < abs(fs_error_percent(naive))


class TestReport:
    def test_the_snapshot_carries_everything_needed_to_judge_the_session(self):
        """它进 `SessionMeta.sync_report`（PRD §6.1 强制字段）。"""
        report = build_timebase(simulate_arrival(n=12000), NOMINAL_FS).report
        snapshot = report.snapshot()
        for key in (
            "offset",
            "fs",
            "nominal_fs",
            "fs_deviation_ppm",
            "samples",
            "packets",
            "samples_per_packet",
            "anchors",
            "residual_rms",
            "residual_p95",
            "residual_max",
            "fs_window_spread",
            "fs_windows",
            "stable",
            "version",
        ):
            assert key in snapshot
        assert snapshot["version"] == SYNC_REPORT_VERSION

    def test_the_residual_is_the_ble_jitter(self):
        """残差就是抖动。它应当与模拟里注入的量级一致，且**单边为正**。

        单边为正是最小值滤波成立的前提：锚点落在下包络上，所有样本都在它上面。
        """
        report = build_timebase(simulate_arrival(jitter_mean=0.004), NOMINAL_FS).report
        assert report.residual_p95 > 0.0
        assert report.residual_max > report.residual_p95
        assert 0.001 < report.residual_rms < 0.05

    def test_the_anchor_count_follows_the_window_length(self):
        cfg = replace(AlgoConfig(), sync_minfilter_window_samples=50)
        report = build_timebase(simulate_arrival(n=10000), NOMINAL_FS, cfg).report
        assert report.anchors == 10000 // 50


class TestCrossFoot:
    def test_the_mapping_is_identity_and_the_report_says_what_is_left(self):
        """两台设备共用同一个主机时钟，映射本身是恒等的。有内容的是不确定度。"""
        left = build_timebase(simulate_arrival(seed=0), NOMINAL_FS).report
        right = build_timebase(
            simulate_arrival(fs_true=199.7, per_packet=6, seed=1), NOMINAL_FS
        ).report
        cross = cross_foot_uncertainty(left, right)
        assert cross.fs_mismatch == pytest.approx(0.003, rel=0.2)
        assert cross.observable_jitter > 0.0

    def test_the_caveat_is_a_field_not_a_comment(self):
        """PRD §8 要求跨足时序指标"输出时强制附同步质量标注"。

        那条标注必须包含"固有延迟不可观测"这件事，所以它得是一个能被打印、能被断言
        的字段，而不是源码里的一句注释。
        """
        left = build_timebase(simulate_arrival(n=8000, seed=0), NOMINAL_FS).report
        right = build_timebase(simulate_arrival(n=8000, seed=1), NOMINAL_FS).report
        caveat = cross_foot_uncertainty(left, right).caveat
        assert "不可观测" in caveat
        assert "RAY-213" in caveat


class TestRejections:
    def test_out_of_order_arrivals_are_refused(self):
        """`time.monotonic()` 本身单调，逆序只可能来自两台设备的样本被混在一起。"""
        arrival = simulate_arrival(n=4000)
        arrival[1000] = arrival[900]
        with pytest.raises(TimebaseError, match="单调不减"):
            build_timebase(arrival, NOMINAL_FS)

    def test_too_few_samples_are_refused(self):
        """回归至少要两个锚点，否则斜率没有任何约束 —— 而错的斜率不报错。"""
        with pytest.raises(TimebaseError, match="不足两个最小值滤波窗口"):
            build_timebase(simulate_arrival(n=150), NOMINAL_FS)

    def test_a_non_positive_nominal_rate_is_refused(self):
        with pytest.raises(TimebaseError):
            build_timebase(simulate_arrival(n=4000), 0.0)

    def test_a_two_dimensional_input_is_refused(self):
        with pytest.raises(TimebaseError):
            build_timebase(np.zeros((100, 2)), NOMINAL_FS)

    def test_a_short_session_reports_no_stability_windows_rather_than_nan(self):
        """窗口不足两个就没有"相邻窗口差"可言。返回 0 并把窗口数一并给出。

        返回 nan 会一路流进 `sync_report`，在报告里变成一个没人能解释的空白格。
        """
        report = build_timebase(simulate_arrival(n=3000), NOMINAL_FS).report
        assert report.fs_windows < 2
        assert report.fs_window_spread == 0.0
