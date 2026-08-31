"""物理对碰锚点：冲击峰检测、双侧配对与跨足偏移统计。契约 §1 的 `sync/anchor.py`（F3.1）。

**工程模式实验室工具，不进产品流程。** PRD v1.3 §8 已撤销全部物理锚点动作，
v1 默认同步机制是主机侧接收时刻（`timebase.py`，RAY-209）。本模块只为 V3′
（RAY-213）提供跨足时间**真值**：两个模块外壳对碰是同一个物理事件，它在左右
两条数据流里的冲击峰时刻之差，就是双足时基偏差的真值 —— 不需要任何额外硬件。

## 测量原理，以及"真值"到底是什么的真值

`timebase.build_timebase` 把每台设备映射到主机时基，但它的 offset 吸收了该设备
**不可观测的固有链路延迟** `latency_min`（见 `timebase.py` 模块文档）。两台设备的
`latency_min` 不必相同，其差直接成为跨足同步误差 —— 主机侧没有任何方法看到它。

对碰给了一个物理上的等时刻参照：冲击在两台设备上**同时**发生。把两侧冲击峰各自
换算到主机时基后相减：

    delta = t_host_L(冲击) − t_host_R(冲击)
          = (T + latency_min_L) − (T + latency_min_R)
          = latency_min_L − latency_min_R

物理事件时刻 T 消掉了，剩下的正是主机侧同步方案测不到的那一项。所以 `delta`
不是"这套工具的误差"，它**就是被测对象**：主机侧时基的跨足偏差。N 次对碰给出
它的分布（中位、离散度、随时间漂移），RAY-213 拿这个分布对照双支撑期等指标的
临床可辨差异，走 PRD §8 的三选一决策。

## 为什么在加速度模值上检测

对碰时模块姿态任意，冲击方向未知；模值 |a| 对姿态不变，静止基线恒为 1 g。
阈值因此是"绝对模值超过多少"，不需要先估重力方向 —— 这也让检测完全独立于
时基与下游算法。

## 亚采样周期插值，与它失效的两种情形

200 Hz 的采样周期是 5 ms，而 RAY-213 要分辨的效应在 ±10~30 ms 量级 —— 只按
样本取峰，量化误差最大就有半个周期（2.5 ms），叠加两侧就是 5 ms，太粗。对峰值
样本及其左右邻居做抛物线插值（冲击包络在峰附近近似二次），把峰时刻细化到亚
采样周期。

两种情形下插值不可用，各自有回退：

1. **削顶**。器件加速度满量程 ±16 g，对碰冲击很容易顶到。削顶把峰变成平台，
   抛物线在平台上无解（分母趋零）。回退：取削顶平台的**中点**。平台近似对称于
   真峰，中点误差有界（半个平台宽度）；`clipped` 标志让下游知道这一对的精度
   等级不同。
2. **峰在数据边界**，或邻居差分不构成极大（噪声、单样本尖刺）。回退：直接用
   峰值样本的整数索引，`interpolated=False`。

## 连击、回弹与轻碰

一次对碰在毫秒尺度上不是单脉冲：外壳回弹会产生间隔几十毫秒的次级冲击。它们是
**同一个物理事件**，分开计数会让两侧配对错乱 —— 所以间隔小于
`anchor_merge_window_s` 的超阈值区段合并成一个事件，取其中的全局最大峰。代价是
故意的、间隔小于该窗口的连击也会被合并；但两侧按同一规则合并，配对与 delta
不受影响，只是计数少一次。

轻碰可能只在一侧过阈值。配对因此必须容忍单侧漏检：按主机时基做非交叉最优
匹配（见 `pair_events`），超出 `anchor_pairing_window_s` 的峰归入 `unpaired_*`
而不是硬配 —— 硬配一对错误的峰，会把一个几百毫秒的假偏移混进 ±10~30 ms 的
分布里。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Final

import numpy as np

from gait.config import AlgoConfig
from gait.sync.timebase import SyncReport, build_timebase

#: 锚点报告的结构版本。RAY-213 的实验记录会引用这份报告。
ANCHOR_REPORT_VERSION: Final[str] = "1.0"

__all__ = [
    "ANCHOR_REPORT_VERSION",
    "AnchorError",
    "AnchorEvent",
    "AnchorPair",
    "AnchorReport",
    "FootSignal",
    "ImpactPeak",
    "coarse_alignment",
    "detect_impacts",
    "measure_offsets",
    "pair_events",
]


class AnchorError(ValueError):
    """锚点检测的输入非法。"""


@dataclass(frozen=True)
class ImpactPeak:
    """一次冲击的峰，时刻以**分数样本索引**表示。

    索引而不是秒：检测发生在等间隔的样本序列上，此时还没有任何时基。换算成
    设备时基或主机时基是调用方（`measure_offsets`）的事，检测本身与时基无关。
    """

    #: 分数样本索引。整数部分是峰值样本，小数部分来自抛物线插值。
    index: float
    #: 峰值样本的模值，m/s²。削顶时它是被削后的读数，真实峰值更高。
    magnitude: float
    #: 事件内有样本触及满量程。此时 `index` 是削顶平台的中点，精度降级。
    clipped: bool
    #: 抛物线插值是否成立。False 表示回退到了整数样本索引（或平台中点）。
    interpolated: bool
    #: 事件的样本跨度：首个到末个超阈值样本，**含**合并进来的回弹之间的
    #: 亚阈值间隙。诊断用：拖碰与带回弹的干脆对碰都会展宽它，只有结合峰形
    #: 才能区分 —— 它不是"超阈值样本计数"。
    width_samples: int


@dataclass(frozen=True)
class FootSignal:
    """一只脚的检测输入。

    `magnitude` 与 `arrival` 逐样本对应：前者用于找峰，后者交给
    `build_timebase` 构建该设备的主机时基。`clipped` 由调用方从原始计数值判定
    （|raw| 触及 int16 满量程）—— 模值层面看不出单轴削顶，原始计数值看得出。
    """

    #: (n,) 加速度模值，m/s²。
    magnitude: np.ndarray
    #: (n,) 逐样本主机接收时刻，s。与 `timebase.build_timebase` 的输入同义。
    arrival: np.ndarray
    #: (n,) 布尔，该样本是否有轴触及满量程。None 表示调用方无原始计数值可查。
    clipped: np.ndarray | None = None


@dataclass(frozen=True)
class AnchorEvent:
    """一侧的一次冲击，峰索引已换算成两种时刻。"""

    peak: ImpactPeak
    #: 主机时基下的峰时刻，s。`offset + index / fs_measured`。
    t_host: float
    #: 设备自身时基下的峰时刻，s。`index / fs_measured`，零点是该设备第 0 个样本。
    t_device: float

    def snapshot(self) -> dict[str, Any]:
        return {
            "t_host": self.t_host,
            "t_device": self.t_device,
            "index": self.peak.index,
            "magnitude": self.peak.magnitude,
            "clipped": self.peak.clipped,
            "interpolated": self.peak.interpolated,
            "width_samples": self.peak.width_samples,
        }


@dataclass(frozen=True)
class AnchorPair:
    """同一次对碰在左右数据流里的一对峰。"""

    left: AnchorEvent
    right: AnchorEvent

    @property
    def delta(self) -> float:
        """主机时基下的峰时刻差 `t_L − t_R`，s。

        物理事件时刻在差里消掉，剩下的是两台设备不可观测的固有延迟之差 ——
        即主机侧同步方案的跨足偏差真值。见模块文档。
        """
        return self.left.t_host - self.right.t_host

    @property
    def degraded(self) -> bool:
        """任一侧削顶或未能插值。这一对的精度低于其余对，统计时不剔除但要可见。"""
        return not (
            self.left.peak.interpolated
            and self.right.peak.interpolated
            and not self.left.peak.clipped
            and not self.right.peak.clipped
        )

    def snapshot(self) -> dict[str, Any]:
        return {
            "delta_s": self.delta,
            "degraded": self.degraded,
            "left": self.left.snapshot(),
            "right": self.right.snapshot(),
        }


@dataclass(frozen=True)
class AnchorReport:
    """一次对碰会话的全部产出。RAY-213 实验记录的直接输入。

    字段跟着《06 测试与验证方案》V3′ 的记录模板走：逐锚点的双侧时刻与偏移、
    分布统计（中位、90 分位、最大）、是否随时间漂移。
    """

    pairs: tuple[AnchorPair, ...]
    #: 只在一侧检出的峰。轻碰单侧漏检、或一侧削顶变形到没过阈值时出现。
    unpaired_left: tuple[AnchorEvent, ...]
    unpaired_right: tuple[AnchorEvent, ...]
    #: 两侧各自的时基质量。锚点结论的可信度上限由它们决定：时基不稳
    #: （`stable=False`），delta 的漂移就分不清是链路变差还是晶振问题。
    left_sync: SyncReport
    right_sync: SyncReport
    #: 非 None 表示配对前左侧时间轴被平移了这么多秒（粗对齐，见
    #: `coarse_alignment`）。此时所有 delta 与左侧 `t_host` 都已含这次平移：
    #: **均值按构造在零附近，不携带绝对偏移的信息**；散布与漂移不受影响。
    alignment_applied_s: float | None = None
    version: str = ANCHOR_REPORT_VERSION

    @property
    def deltas(self) -> np.ndarray:
        """(n,) 逐对的 `t_L − t_R`，s。"""
        return np.asarray([pair.delta for pair in self.pairs], dtype=np.float64)

    @property
    def offset_mean(self) -> float:
        return float(self.deltas.mean()) if self.pairs else float("nan")

    @property
    def offset_std(self) -> float:
        """样本标准差（ddof=1）。单对时无离散度可言，返回 nan 而不是 0 ——
        0 会被读成"完美重复"，而那是最误导的一种读法。"""
        if len(self.pairs) < 2:
            return float("nan")
        return float(self.deltas.std(ddof=1))

    @property
    def offset_median(self) -> float:
        return float(np.median(self.deltas)) if self.pairs else float("nan")

    @property
    def offset_p90_abs(self) -> float:
        """|delta| 的 90 分位。V3′ 模板要的"90 分位"按绝对值给：同步误差对
        指标的伤害不分方向。"""
        if not self.pairs:
            return float("nan")
        return float(np.percentile(np.abs(self.deltas), 90))

    @property
    def offset_max_abs(self) -> float:
        return float(np.abs(self.deltas).max()) if self.pairs else float("nan")

    @property
    def drift_s_per_min(self) -> float:
        """delta 对时间的线性趋势，s/min。回答 V3′ 模板的"是否随时间漂移"。

        对 (对碰时刻, delta) 做最小二乘。两对以下没有趋势可言，返回 nan。
        漂移不为零的第一嫌疑是两台设备晶振差（`fs_mismatch`）经整段时长的累积，
        其次才是链路状态变化 —— 报告把 `SyncReport` 一并带上就是为了这个对照。
        """
        if len(self.pairs) < 2:
            return float("nan")
        times = np.asarray([pair.left.t_host for pair in self.pairs])
        slope = np.polyfit(times, self.deltas, 1)[0]
        return float(slope * 60.0)

    def snapshot(self) -> dict[str, Any]:
        """写进报告文件的普通字典。与 `SyncReport.snapshot` 同一个理由：
        手写快照会漏字段，而漏掉的字段正是三个月后复现失败的原因。"""
        return {
            "version": self.version,
            "alignment_applied_s": self.alignment_applied_s,
            "pairs": [pair.snapshot() for pair in self.pairs],
            "unpaired_left": [event.snapshot() for event in self.unpaired_left],
            "unpaired_right": [event.snapshot() for event in self.unpaired_right],
            "offset": {
                "count": len(self.pairs),
                # 非有限值以 None 落盘：json.dumps 会把 nan 写成裸 `NaN`，
                # 那不是合法 JSON，非 Python 的读取方会在上面摔跤。
                "mean_s": _json_number(self.offset_mean),
                "std_s": _json_number(self.offset_std),
                "median_s": _json_number(self.offset_median),
                "p90_abs_s": _json_number(self.offset_p90_abs),
                "max_abs_s": _json_number(self.offset_max_abs),
                "drift_s_per_min": _json_number(self.drift_s_per_min),
                "degraded_pairs": sum(1 for pair in self.pairs if pair.degraded),
            },
            "left_sync": self.left_sync.snapshot(),
            "right_sync": self.right_sync.snapshot(),
        }


def _json_number(value: float) -> float | None:
    """快照里的数值：非有限值转 None。理由见 `AnchorReport.snapshot` 的注释。"""
    return value if math.isfinite(value) else None


def _merge_runs(above: np.ndarray, merge_gap: int) -> list[tuple[int, int]]:
    """超阈值样本的连续区段，间隔**小于** `merge_gap` 个样本的相邻区段合并。

    合并的对象是回弹：一次对碰的次级冲击与主峰隔几十毫秒，是同一个物理事件。
    分裂条件是 `diff >= merge_gap`（相邻超阈值样本的索引差恰为窗口时**不**合并），
    与配置文档"间隔小于它才并成一个事件"一致；`merge_gap` 因此必须 ≥ 2，
    否则连续样本（diff = 1）会被拆散。
    """
    indices = np.flatnonzero(above)
    if indices.size == 0:
        return []
    breaks = np.flatnonzero(np.diff(indices) >= merge_gap)
    starts = np.concatenate(([0], breaks + 1))
    stops = np.concatenate((breaks, [indices.size - 1]))
    return [(int(indices[a]), int(indices[b])) for a, b in zip(starts, stops, strict=True)]


def _refine_peak(magnitude: np.ndarray, peak: int) -> tuple[float, bool]:
    """抛物线插值细化峰索引。返回 (分数索引, 是否成立)。

    三点 (y₋, y₀, y₊) 过抛物线，顶点相对中点的偏移是
    `δ = (y₊ − y₋) / (2·(2·y₀ − y₋ − y₊))`。分母是负的二阶差分：真极大处它显著为正；
    趋零意味着平台（削顶）或纯噪声，此时顶点公式发散，回退而不是硬算 ——
    一个 |δ| > 0.5 的"细化"比不细化更坏，它把峰挪进了邻居样本的领地。
    """
    if peak <= 0 or peak >= magnitude.size - 1:
        return float(peak), False
    left, mid, right = (
        float(magnitude[peak - 1]),
        float(magnitude[peak]),
        float(magnitude[peak + 1]),
    )
    curvature = left - 2.0 * mid + right
    if curvature >= 0.0:
        return float(peak), False
    delta = 0.5 * (right - left) / (-curvature)
    if abs(delta) > 0.5:
        return float(peak), False
    return peak + delta, True


def detect_impacts(
    magnitude: np.ndarray,
    fs: float,
    cfg: AlgoConfig | None = None,
    *,
    clipped: np.ndarray | None = None,
) -> list[ImpactPeak]:
    """在加速度模值序列上找冲击峰。

    `fs` 只用来把合并窗口从秒换算成样本数 —— 用**标称**采样率即可，几百 ppm 的
    晶振偏差在几十毫秒的窗口上差不出一个样本。
    """
    cfg = cfg or AlgoConfig()
    series = np.asarray(magnitude, dtype=np.float64)
    if series.ndim != 1:
        raise AnchorError(f"magnitude 应为一维，收到 shape={series.shape}")
    if not fs > 0:
        raise AnchorError(f"fs 必须为正，收到 {fs}")
    if not np.all(np.isfinite(series)):
        raise AnchorError("magnitude 含 NaN/Inf。模值由 √(x²+y²+z²) 得来，不该出现非有限值。")
    flags = None
    if clipped is not None:
        flags = np.asarray(clipped, dtype=bool)
        if flags.shape != series.shape:
            raise AnchorError(
                f"clipped 与 magnitude 形状不一致：{flags.shape} vs {series.shape}"
            )

    threshold = cfg.anchor_threshold_m_s2
    merge_gap = max(2, round(cfg.anchor_merge_window_s * fs))
    peaks: list[ImpactPeak] = []
    for start, stop in _merge_runs(series >= threshold, merge_gap):
        segment = series[start : stop + 1]
        peak = start + int(np.argmax(segment))
        event_clipped = bool(flags[start : stop + 1].any()) if flags is not None else False
        if event_clipped and flags is not None and flags[peak]:
            # 峰值样本本身削顶：取**包含它的**连续削顶段的中点 —— 平台近似
            # 对称于真峰。只看这一段，不看整个事件：主峰与回弹都削顶时，
            # 事件内首尾削顶样本的中点会落在两个平台之间，偏差几十毫秒。
            run_start = peak
            while run_start > start and flags[run_start - 1]:
                run_start -= 1
            run_stop = peak
            while run_stop < stop and flags[run_stop + 1]:
                run_stop += 1
            index = (run_start + run_stop) / 2.0
            interpolated = False
        else:
            # 峰值样本未削顶（削顶的只是回弹等次级样本）：真峰仍可插值。
            # 但邻居削顶时抛物线用的是被压平的读数，回退整数索引。
            index, interpolated = _refine_peak(series, peak)
            if (
                event_clipped
                and flags is not None
                and interpolated
                and (flags[max(peak - 1, 0)] or flags[min(peak + 1, series.size - 1)])
            ):
                index, interpolated = float(peak), False
        peaks.append(
            ImpactPeak(
                index=index,
                magnitude=float(series[peak]),
                clipped=event_clipped,
                interpolated=interpolated,
                width_samples=stop - start + 1,
            )
        )
    return peaks


def pair_events(
    left: list[AnchorEvent],
    right: list[AnchorEvent],
    cfg: AlgoConfig | None = None,
) -> tuple[list[AnchorPair], list[AnchorEvent], list[AnchorEvent]]:
    """按主机时基做非交叉最优匹配。返回 (配对, 左侧落单, 右侧落单)。

    两侧事件都按时间有序，且是同一串物理事件 —— 最优匹配因此不交叉，可用
    O(L·R) 的动态规划**精确**求解：配对数最多者胜，同数取 |Δ| 总和最小。
    配对窗口 `anchor_pairing_window_s` 之外的组合不许配 —— 硬配一对错误的峰
    会把几百毫秒的假偏移混进 ±10~30 ms 的分布。

    不用就近贪心：贪心（含单步前瞻）在密集场景下会丢弃本可配对的峰 ——
    左 [0.00, 0.10]、右 [0.06, 0.09] 时，前瞻贪心只配出一对并制造两个假落单，
    而最优匹配是 (0.00↔0.06, 0.10↔0.09)。锚点会话的事件数在几十的量级，
    O(L·R) 不构成负担。
    """
    cfg = cfg or AlgoConfig()
    window = cfg.anchor_pairing_window_s
    n_left, n_right = len(left), len(right)
    # best[i][j] = 从 left[i:], right[j:] 起的最优 (−配对数, |Δ| 总和)。
    # 元组取 min：配对数多者优先，其次总时差小者。
    best: list[list[tuple[int, float]]] = [
        [(0, 0.0)] * (n_right + 1) for _ in range(n_left + 1)
    ]
    for i in range(n_left - 1, -1, -1):
        for j in range(n_right - 1, -1, -1):
            candidates = [best[i + 1][j], best[i][j + 1]]
            gap = abs(left[i].t_host - right[j].t_host)
            if gap <= window:
                matched, cost = best[i + 1][j + 1]
                candidates.append((matched - 1, cost + gap))
            best[i][j] = min(candidates)

    pairs: list[AnchorPair] = []
    lone_left: list[AnchorEvent] = []
    lone_right: list[AnchorEvent] = []
    i = j = 0
    while i < n_left and j < n_right:
        gap = abs(left[i].t_host - right[j].t_host)
        matched, cost = best[i + 1][j + 1]
        if gap <= window and best[i][j] == (matched - 1, cost + gap):
            pairs.append(AnchorPair(left=left[i], right=right[j]))
            i += 1
            j += 1
        elif best[i][j] == best[i + 1][j]:
            lone_left.append(left[i])
            i += 1
        else:
            lone_right.append(right[j])
            j += 1
    lone_left.extend(left[i:])
    lone_right.extend(right[j:])
    return pairs, lone_left, lone_right


def coarse_alignment(
    left: list[AnchorEvent],
    right: list[AnchorEvent],
    cfg: AlgoConfig | None = None,
) -> float | None:
    """从对碰序列本身估计两条时间轴的零点差。

    ## 它解决的问题

    RAY-198/200 的录制文件把 `t` 归零到**各自的第一段字节**，左右文件的零点差
    是一个未知常数（两台设备开流先后之差，可达秒级）。这个常数一旦超过配对窗口，
    就近配对整体失败 —— 而采集侧目前没有持久化各文件零点的绝对时刻（epoch）。

    对碰序列自己就是解药：两侧看到的是**同一串物理事件**，全体 `t_L − t_R` 候选
    差里，真配对的差聚成一簇（簇宽 = 同步误差 + 检测噪声，毫秒级），错配的差
    散在对碰间隔的尺度上（几百毫秒起）。最密的簇的中位数就是零点差。

    ## 使用它的代价

    对齐量本身来自这些锚点，所以对齐后的 delta 均值**按构造在零附近，不再携带
    绝对偏移的信息** —— 报告必须把这次平移记在 `alignment_applied_s` 里让人看见。
    散布、分位与漂移不受一个常数平移的影响，RAY-212 的验收量（标准差）依然成立。

    ## 失效模式

    对碰打成完美节拍器（严格等间隔）时，整体错位一个周期的簇只比真簇少一个成员，
    密度判据仍选中真簇，但裕量只有 1 —— 实验时对碰间隔自然的手工抖动（几百毫秒）
    会把假簇打散，这也是 CLI 文档建议"间隔随意、不要数拍子"的原因。
    两侧各只有一个峰时无所谓簇，直接对齐那唯一的候选。
    """
    cfg = cfg or AlgoConfig()
    if not left or not right:
        return None
    window = cfg.anchor_pairing_window_s
    # outer 差在单个 float64 缓冲里出全表：事件数被误用场景推到上千时
    # （整段步行录制配低阈值），逐元素的 Python 列表要秒级与上百 MB，
    # 向量化是毫秒级。正常锚点会话（几十事件）两者都无所谓。
    left_times = np.asarray([event.t_host for event in left], dtype=np.float64)
    right_times = np.asarray([event.t_host for event in right], dtype=np.float64)
    candidates = np.sort(np.subtract.outer(left_times, right_times).ravel())
    # 滑窗找最密的簇：对每个候选，数落在它 +2·window 内的候选个数。
    ends = np.searchsorted(candidates, candidates + 2.0 * window, side="right")
    counts = ends - np.arange(candidates.size)
    best = int(np.argmax(counts))
    cluster = candidates[best : ends[best]]
    return float(np.median(cluster))


def _events(signal: FootSignal, nominal_fs: float, cfg: AlgoConfig) -> tuple[list[AnchorEvent], SyncReport]:
    """一侧的完整检测：时基构建 + 找峰 + 峰索引换算成两种时刻。

    换算刻意用**实测**采样率（`report.fs`），不用手边现成的 `nominal_fs`：
    晶振几百 ppm 的偏差乘上几十秒的会话时长就是几十毫秒 —— 与被测效应同量级。
    找峰传 `nominal_fs` 则无妨，它只把合并窗口换算成样本数，几十毫秒的窗口
    差不出一个样本。
    """
    magnitude = np.asarray(signal.magnitude, dtype=np.float64)
    arrival = np.asarray(signal.arrival, dtype=np.float64)
    if magnitude.shape != arrival.shape:
        raise AnchorError(
            f"magnitude 与 arrival 形状不一致：{magnitude.shape} vs {arrival.shape}。"
            "两者必须逐样本对应，否则峰索引换算出的时刻属于别的样本。"
        )
    timebase = build_timebase(arrival, nominal_fs, cfg)
    peaks = detect_impacts(magnitude, nominal_fs, cfg, clipped=signal.clipped)
    report = timebase.report
    events = [
        AnchorEvent(
            peak=peak,
            t_host=report.offset + peak.index / report.fs,
            t_device=peak.index / report.fs,
        )
        for peak in peaks
    ]
    return events, report


def measure_offsets(
    left: FootSignal,
    right: FootSignal,
    nominal_fs: float,
    cfg: AlgoConfig | None = None,
    *,
    coarse_align: bool = False,
    window: tuple[float, float] | None = None,
) -> AnchorReport:
    """完整流水线：双侧时基构建 → 冲击峰检测 →（可选粗对齐）→ 配对 → 偏移统计。

    这是 CLI 与 RAY-213 的唯一入口。时基构建用与产品流程完全相同的
    `build_timebase` —— 锚点量的就是它的偏差，被测者与测量尺必须是同一把。

    `window`（主机时基下的 `(起, 止)`）把锚点候选限制在这段时间内。**采集里同时
    含对碰段与步行段时必须给**：足跟着地的冲击同样超过阈值（实测 3~6.7 g，与轻碰
    重叠），不限定窗口就会把左右两次不相关的着地硬配成一对 —— 实测产生过一个
    −223 ms 的假 Δ，而同一趟对碰段内的真 Δ 全在 ±8 ms。
    形状上两者可分（对碰宽 1~3 样本，着地 5~13），但那是启发式；调用方本来就知道
    对碰段是哪一段，用它比猜可靠。

    `coarse_align=True` 用于两侧时间轴零点不一致的输入（各自归零的录制文件）：
    先用 `coarse_alignment` 估出零点差并平移左侧，再配对。代价与含义见该函数
    文档 —— 此时报告的均值不再是绝对偏移。共钟输入（现场 `t_host`，或已按
    epoch 平移的录制）保持默认 False，均值才可读作绝对跨足偏差。
    """
    cfg = cfg or AlgoConfig()
    left_events, left_sync = _events(left, nominal_fs, cfg)
    right_events, right_sync = _events(right, nominal_fs, cfg)
    if window is not None:
        # 时基仍用**整段**数据拟合（样本越多回归越稳），只有锚点候选被限制在窗内。
        start, stop = window
        left_events = [e for e in left_events if start <= e.t_host <= stop]
        right_events = [e for e in right_events if start <= e.t_host <= stop]
    alignment: float | None = None
    if coarse_align:
        alignment = coarse_alignment(left_events, right_events, cfg)
        if alignment is not None:
            left_events = [
                AnchorEvent(
                    peak=event.peak,
                    t_host=event.t_host - alignment,
                    t_device=event.t_device,
                )
                for event in left_events
            ]
    pairs, lone_left, lone_right = pair_events(left_events, right_events, cfg)
    return AnchorReport(
        pairs=tuple(pairs),
        unpaired_left=tuple(lone_left),
        unpaired_right=tuple(lone_right),
        left_sync=left_sync,
        right_sync=right_sync,
        alignment_applied_s=alignment,
    )
