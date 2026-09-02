"""双脚协同的周期规划入口。`analysis/planning.py`（RAY-328 `dual-foot-qc-windowing`）。

它把两件独立的事合成一份可落盘的读数：

* **宽闸与净窗**（`sync/planning.py`）—— 这一趟的哪些时间段两只脚都完整，覆盖率多少；
* **跨脚周期校验**（`core/dualfoot.check_cross_foot_period`）—— 两只脚的周期对得上吗。

合成放在 `analysis` 层而不是任一边，是分层红线逼出来的，而红线本身是对的：
`gait.core` **不得 import** `gait.sync`（`gait.CORE_FORBIDDEN_IMPORTS`，lint 强制），
因为 core 要能在 CLI、Windows 采集端、云端重算三个宿主里跑同一份代码，也要能把外部
数据集直接喂进来 —— 一旦它认识了同步层，这个性质就悄悄没了。所以跨脚信息**全部经
参数传入 core**：`check_cross_foot_period` 只收两个已经算好的 `PeriodReport`，它不知道
也不需要知道那两个周期是从哪条时间轴上来的。

## 降级是一个戳，不是一道闸

`PeriodPlan.degraded` 为真时，`window` 与两脚的周期估计**一个字都不变**。跨脚校验
只加票、不否决，理由见 `CrossFootPeriod` 的文档：同一个超阈的比值，既可能是估计跑
掉了，也可能是这个人两脚周期真的不同，而没有数据能分开这两者 —— 目标人群恰恰是后
一种人。

`plannable` 与 `degraded` 因此是两个正交的量：前者说"这份数据够不够做规划"（宽闸的
结论），后者说"规划出来的结果要不要打折看"（跨脚的一票）。一份 `plannable=True,
degraded=True` 的报告是完全正常的输出，不是矛盾。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np

from gait.config import AlgoConfig
from gait.core.dualfoot import (
    AlternationDecoding,
    CrossFootPeriod,
    check_cross_foot_period,
    decode_alternation,
)
from gait.core.zupt import PeriodReport, StanceDetection, detect_stance
from gait.sync.integrity import IntegrityReport
from gait.sync.planning import DualNetWindow, cycle_is_net, plan_dual_net_window
from gait.sync.selfcheck import stance_spans

__all__ = [
    "CrossFootPhase",
    "DualFootPeriodPlan",
    "FootPlanInput",
    "FootSeriesInput",
    "PeriodPlan",
    "cross_foot_phase",
    "plan_dual_foot_periods",
    "plan_periods",
]


@dataclass(frozen=True)
class FootPlanInput:
    """一只脚送进规划的东西。

    `fs` 是这只脚的**实测**采样率（`SyncReport.fs`），不是标称值：`PeriodReport` 以
    样本计周期，而两脚的实测 fs 实测最大差 1.1%，用标称值换算等于把这个差记到跨脚
    比值上去。
    """

    #: 主机侧到达时刻，s，升序。与另一只脚**共钟**。
    arrival: np.ndarray
    #: 这只脚的周期估计。`None` 表示这一段没有可辨认的步态。
    period: PeriodReport | None
    #: 实测采样率，Hz。
    fs: float
    #: 已经算好的完整性报告。不传就由 `sync.planning` 自己算。
    integrity: IntegrityReport | None = None


@dataclass(frozen=True)
class PeriodPlan:
    """一趟的周期规划前提。"""

    window: DualNetWindow
    #: 跨脚校验。`None` 表示至少一只脚没有周期 —— 这一票**弃权**，不是赞成。
    cross_foot: CrossFootPeriod | None

    @property
    def plannable(self) -> bool:
        """宽闸的结论：这份数据够不够做周期规划。"""
        return self.window.plannable

    @property
    def degraded(self) -> bool:
        """跨脚的一票：两脚周期对不上。**不影响 `plannable`。**"""
        return self.cross_foot is not None and not self.cross_foot.agrees

    def snapshot(self) -> dict[str, Any]:
        return {
            "window": self.window.snapshot(),
            "cross_foot": self.cross_foot.snapshot() if self.cross_foot else None,
            "plannable": self.plannable,
            "degraded": self.degraded,
            "coverage": self.window.coverage,
        }


def plan_periods(
    left: FootPlanInput,
    right: FootPlanInput,
    nominal_fs: float,
    cfg: AlgoConfig | None = None,
) -> PeriodPlan:
    """算出这一趟的净窗、覆盖率与跨脚校验结论。

    两问分开算、都算完：宽闸拒了也照样出跨脚结论，跨脚弃权也照样出净窗。把其中一个
    的失败变成另一个的短路，会让报告里少掉的那一半看起来像"没这个问题"。
    """
    cfg = cfg or AlgoConfig()
    window = plan_dual_net_window(
        left.arrival,
        right.arrival,
        nominal_fs,
        cfg,
        left_report=left.integrity,
        right_report=right.integrity,
    )
    cross_foot = check_cross_foot_period(left.period, left.fs, right.period, right.fs, cfg)
    return PeriodPlan(window=window, cross_foot=cross_foot)


# ── 双脚互相关：周期先验与反相自检（RAY-328 L1）────────────────────────────


@dataclass(frozen=True)
class CrossFootPhase:
    """两脚 swing 信号互相关给出的周期与相位差。

    互相关一次给出两个量，而它们的可信度**完全不同**：

    * **峰间距 T_x** 是周期。它由相关函数上一串峰的间隔取中位数得到，不依赖任何单个
      峰的绝对位置，因此对两条时间轴的公共偏移免疫 —— 哪怕对齐整体错了 200 ms，峰
      与峰的间隔一个字不变。实测它与单脚中位周期差 0.5%~9.3%。
    * **最近正峰 φ** 是相位差。它**直接读绝对位置**，所以对齐错多少它就错多少。

    所以 T_x 可以进周期估计池，φ 只用来做一次粗判（反相还是不反相）。把 φ 拿去算
    "左右相位差是多少毫秒"这种量，量到的会是对齐误差加上真实相位差，两者分不开。
    """

    #: 互相关峰间距，s。峰不足两个时为 None。
    period_s: float | None
    #: 最近正峰的滞后，s。一个峰都没有时为 None。
    phase_s: float | None
    #: φ 对周期取模后的比值，落在 [0, 1)。
    phase_fraction: float | None
    #: 判定用的反相带。**它对称于 0.5**，因此把左右两只脚对调（φ/T 变成 1 − φ/T）
    #: 不会改变判定 —— 反相是两只脚之间的关系，不该取决于谁被叫做"左"。
    band: tuple[float, float]

    @property
    def in_antiphase(self) -> bool | None:
        """φ/T 是否落在反相带内。`None` 表示没算出相位，不是"不反相"。"""
        if self.phase_fraction is None:
            return None
        return self.band[0] <= self.phase_fraction <= self.band[1]

    def snapshot(self) -> dict[str, Any]:
        return {
            "period_s": self.period_s,
            "phase_s": self.phase_s,
            "phase_fraction": self.phase_fraction,
            "band": list(self.band),
            "in_antiphase": self.in_antiphase,
        }


def _greedy_peaks(signal: np.ndarray, min_separation: int, floor: float = 0.25) -> np.ndarray:
    """幅值降序贪心峰选，滤掉低于最大值 `floor` 倍的噪声峰。

    与 `core.zupt._local_peaks` 是同一族做法，但**不共用**：那一个跑在 `‖ω‖` 上，
    这一个跑在相关函数上，后者的峰按定义比原信号平滑得多，而且这里要额外滤掉一个
    幅值地板 —— 相关函数的尾部有一串低矮的伪峰，它们会把峰间距的中位数拉散。
    把两个需求塞进一个函数会让参数表长过函数体，而 core 那一个还不能 import 这里。
    """
    if signal.size < 3:
        return np.zeros(0, dtype=int)
    interior = (
        np.flatnonzero((signal[1:-1] >= signal[:-2]) & (signal[1:-1] > signal[2:])) + 1
    )
    if interior.size == 0:
        return interior.astype(int)
    interior = interior[signal[interior] >= floor * float(signal[interior].max())]
    if interior.size == 0:
        return interior.astype(int)
    separation = max(1, min_separation)
    taken = np.zeros(signal.size, dtype=bool)
    chosen: list[int] = []
    for candidate in interior[np.argsort(signal[interior], kind="stable")[::-1]]:
        index = int(candidate)
        if taken[max(0, index - separation + 1) : index + separation].any():
            continue
        taken[index] = True
        chosen.append(index)
    return np.array(sorted(chosen), dtype=int)


def _positive_lag_correlation(left: np.ndarray, right: np.ndarray, high: int) -> np.ndarray:
    """线性互相关的正滞后段 `c[k] = Σ left[t+k]·right[t]`。

    补零到 ≥ 2n 再变换，与 `core.zupt._autocorrelation_period` 同一个理由：不补零的
    循环相关会把尾部绕回来加到头部的滞后上，而那正是相位最敏感的一段。
    """
    n = left.size
    size = 1 << math.ceil(math.log2(2 * n))
    spectrum = np.fft.rfft(left, size) * np.conj(np.fft.rfft(right, size))
    return np.fft.irfft(spectrum, size)[: min(high, n)]


def cross_foot_phase(
    left_swing: np.ndarray,
    left_t: np.ndarray,
    right_swing: np.ndarray,
    right_t: np.ndarray,
    seed_s: float,
    cfg: AlgoConfig | None = None,
    *,
    grid_fs: float | None = None,
) -> CrossFootPhase | None:
    """两脚 swing（`‖ω‖`）的互相关。`*_t` 是**共钟**的时刻，s。

    两只脚的样本各有各的到达时刻，互相关却要求等间隔的公共栅格，所以先按 `grid_fs`
    重采样到两脚共同跨度上。重采样用线性插值：swing 峰宽在 100 ms 量级，而栅格步长
    是 5 ms，插值误差远小于要测的量。

    `seed_s` 是周期的量级先验（取两脚单脚估计的中位数即可）。它只用来定搜索范围与
    峰的最小间距 —— 与 `core.zupt._local_peaks` 里 `min_distance` 由周期给、而不是
    定死的，是同一个理由：慢走 2.5 s 与快走 1.0 s 差 2.5 倍，一个固定间距两头都错。

    **两条时刻必须升序**（`np.interp` 的前提）。到达时刻按构造就是升序的 —— 乱序只
    可能来自两台设备的样本被混进同一个数组，而 `sync.integrity.assess` 会拒绝那种
    输入，本函数因此不重复检查。

    **已知的近似**：栅格是在整段上铺的，空洞处由 `np.interp` 拉一条直线补上。这条
    直线是**造出来的**信号。它只会稀释相关（多出一段平缓的贡献），不会造出峰，所以
    峰的位置不受影响 —— 而峰的位置正是这里唯一要的东西。实测双净覆盖 95%~100%，
    被造出来的那一小段占比在 5% 以内。真要把它去掉，得把相关拆到 `DualNetWindow.net`
    的每一段上分别算再合并，那是有代价的改动，目前的证据不支持先付这个代价。

    返回 `None` 表示这段数据算不出互相关（跨度不足四个周期，或没有峰）。
    """
    cfg = cfg or AlgoConfig()
    if not seed_s > 0.0:
        raise ValueError(f"seed_s 必须为正，收到 {seed_s}")
    left_t = np.asarray(left_t, dtype=np.float64)
    right_t = np.asarray(right_t, dtype=np.float64)
    left_swing = np.asarray(left_swing, dtype=np.float64)
    right_swing = np.asarray(right_swing, dtype=np.float64)
    for name, times, signal in (
        ("left", left_t, left_swing),
        ("right", right_t, right_swing),
    ):
        if times.shape != signal.shape:
            raise ValueError(
                f"{name} 的时刻与信号长度必须一致：{times.shape} vs {signal.shape}"
            )
        if times.size < 2:
            return None

    fs = float(grid_fs) if grid_fs else 1.0 / float(np.median(np.diff(left_t)))
    start = max(float(left_t[0]), float(right_t[0]))
    stop = min(float(left_t[-1]), float(right_t[-1]))
    if stop - start < 4.0 * seed_s:
        # 少于四个周期的跨度上，相关函数的峰间距是噪声。这不是"精度差"，是那个量
        # 还没成形 —— 与 `stance_min_cycles` 同一个道理。
        return None
    grid = np.arange(start, stop, 1.0 / fs)
    left_grid = np.interp(grid, left_t, left_swing)
    right_grid = np.interp(grid, right_t, right_swing)

    high = int(3.2 * seed_s * fs)
    min_lag = int(0.2 * seed_s * fs)
    correlation = _positive_lag_correlation(
        left_grid - left_grid.mean(), right_grid - right_grid.mean(), high
    )
    if correlation.size <= min_lag + 3:
        return None
    # 从 0.2×T 起找峰：滞后 0 附近永远有一个自相关意义上的大峰（两只脚在走同一段路，
    # 信号本来就相似），它与"左右相位差"无关，不排掉会把 φ 读成 0。
    peaks = _greedy_peaks(correlation[min_lag:], max(1, int(0.55 * seed_s * fs))) + min_lag
    if peaks.size == 0:
        return None
    period_s = float(np.median(np.diff(peaks)) / fs) if peaks.size >= 2 else None
    phase_s = float(peaks[0] / fs)
    base = period_s or seed_s
    return CrossFootPhase(
        period_s=period_s,
        phase_s=phase_s,
        phase_fraction=(phase_s % base) / base,
        band=(float(cfg.xcorr_antiphase_min), float(cfg.xcorr_antiphase_max)),
    )


# ── 双脚周期规划的完整两遍（RAY-328 L1）────────────────────────────────────


@dataclass(frozen=True)
class FootSeriesInput:
    """一只脚的原始输入。一个连续段，不跨空洞。"""

    #: 主机侧到达时刻，s，升序，与另一只脚**共钟**。
    arrival: np.ndarray
    #: 比力，(n, 3)，m/s²。
    accel: np.ndarray
    #: 角速度，(n, 3)，rad/s。
    gyro: np.ndarray
    #: 实测采样率，Hz（`SyncReport.fs`）。
    fs: float
    integrity: IntegrityReport | None = None


@dataclass(frozen=True)
class DualFootPeriodPlan:
    """两遍跑完之后的全部结果。"""

    left: StanceDetection
    right: StanceDetection
    phase: CrossFootPhase | None
    plan: PeriodPlan
    #: 双净窗内的 L,R 交替解码。`None` 表示没有可用的 stride（两脚都没估出周期），
    #: 此时"这个间隔算几步"问不出来，解码不成立。
    alternation: AlternationDecoding | None = None

    @property
    def seeded(self) -> bool:
        """第二遍是否真的用上了互相关先验。

        它可以为假而一切正常：跨度不足四个周期、相关函数没有峰、或先验折完谐波仍在
        `stance_period_*` 的范围外。此时结果与单脚路径逐比特相同，而**报告必须说得出
        这件事** —— 否则"双脚版本"与"单脚版本"给出同一个数时，读的人无从知道是双脚
        没帮上忙，还是双脚压根没参与。
        """
        return any(
            name == "crosscorrelation"
            for detection in (self.left, self.right)
            if detection.period is not None
            for name, _ in detection.period.estimates
        )

    def snapshot(self) -> dict[str, Any]:
        return {
            "phase": self.phase.snapshot() if self.phase else None,
            "seeded": self.seeded,
            "cycles_left": self.left.period.cycles if self.left.period else None,
            "cycles_right": self.right.period.cycles if self.right.period else None,
            "alternation": self.alternation.snapshot() if self.alternation else None,
            **self.plan.snapshot(),
        }


def _median_period_s(left: StanceDetection, right: StanceDetection, left_fs: float, right_fs: float) -> float | None:
    values = [
        detection.period.period_samples / fs
        for detection, fs in ((left, left_fs), (right, right_fs))
        if detection.period is not None
    ]
    return float(np.median(values)) if values else None


def plan_dual_foot_periods(
    left: FootSeriesInput,
    right: FootSeriesInput,
    nominal_fs: float,
    cfg: AlgoConfig | None = None,
) -> DualFootPeriodPlan:
    """双脚周期规划：**两遍**。

    1. 各自单脚检测，得到量级先验；
    2. 用它把两脚 swing 重采样到公共栅格上做互相关，取峰间距 T_x 与相位差 φ；
    3. 把 T_x 作为周期先验**再检测一遍**，最后跑宽闸与跨脚校验。

    为什么必须两遍：互相关要先知道周期的量级才能定搜索范围与峰间距（慢走 2.5 s 与
    快走 1.0 s 差 2.5 倍，一个定死的范围两头都错），而周期的量级只能先由单脚估出来。
    这不是迭代求精 —— 只有两遍，第二遍不会再喂回第一遍。

    第一遍算不出周期时**不报错**：那说明这一段没有可辨认的步态（静立、段太短），
    此时第二遍与第一遍相同，`seeded` 为假。
    """
    cfg = cfg or AlgoConfig()
    first = {
        foot: detect_stance(series.accel, series.gyro, series.fs, cfg)
        for foot, series in (("L", left), ("R", right))
    }
    seed_s = _median_period_s(first["L"], first["R"], left.fs, right.fs)

    phase: CrossFootPhase | None = None
    if seed_s is not None:
        phase = cross_foot_phase(
            np.linalg.norm(np.asarray(left.gyro, dtype=np.float64), axis=1),
            np.asarray(left.arrival, dtype=np.float64),
            np.linalg.norm(np.asarray(right.gyro, dtype=np.float64), axis=1),
            np.asarray(right.arrival, dtype=np.float64),
            seed_s,
            cfg,
            grid_fs=nominal_fs,
        )

    prior_s = phase.period_s if phase is not None else None
    second = {
        foot: (
            first[foot]
            if prior_s is None
            else detect_stance(
                series.accel,
                series.gyro,
                series.fs,
                cfg,
                period_prior_samples=prior_s * series.fs,
            )
        )
        for foot, series in (("L", left), ("R", right))
    }

    plan = plan_periods(
        FootPlanInput(
            arrival=left.arrival,
            period=second["L"].period,
            fs=left.fs,
            integrity=left.integrity,
        ),
        FootPlanInput(
            arrival=right.arrival,
            period=second["R"].period,
            fs=right.fs,
            integrity=right.integrity,
        ),
        nominal_fs,
        cfg,
    )
    stride_s = _median_period_s(second["L"], second["R"], left.fs, right.fs)
    alternation = None
    if stride_s is not None:
        alternation = decode_alternation(
            _net_stance_spans(left, second["L"], plan.window),
            _net_stance_spans(right, second["R"], plan.window),
            stride_s,
            cfg,
        )
    return DualFootPeriodPlan(
        left=second["L"],
        right=second["R"],
        phase=phase,
        plan=plan,
        alternation=alternation,
    )


def _net_stance_spans(
    series: FootSeriesInput, detection: StanceDetection, window: DualNetWindow
) -> list[tuple[float, float]]:
    """这只脚落在双净窗内的支撑相，换成时刻区间。

    只留整个落在净窗内的：跨过空洞的间隔里，"另一只脚漏检了几次"与"这段时间根本
    没有数据"看起来一模一样，而前者该补槽、后者补了就是编造。
    """
    spans = stance_spans(np.asarray(series.arrival, dtype=np.float64), detection.stances)
    return [span for span in spans if cycle_is_net(span[0], span[1], window)]
