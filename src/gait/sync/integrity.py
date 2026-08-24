"""到达率监控与数据空洞切分。契约 §1 的 `sync/integrity.py`（F3.3/3.5）。

## 无序号硬件下只能检测，不能定位

《BS-BT91 硬件适配》发现 3：数据包既无时间戳也无序号。`wt901.ImuSample.seq` 是**主机
侧收到第几个样本**的计数，重连归零 —— 它记的是"收到了多少"，不是"设备发了多少"。

所以丢包在数据里留下的唯一痕迹是**时间**：两个相邻样本的到达时刻之间多出了一段
装不下的空白。能算出丢了大约几个，**算不出丢的是哪几个** —— 那正是 PRD §6.1 说
"绝不插值续算"的原因：连补在哪里都不知道，补出来的一定是编造的。

## 为什么必须切分，而不是插值

惯导积分对虚假数据极度敏感。一个插值出来的样本会被 ESKF 当作真实测量，而它带来的
速度误差会一直积到下一个支撑相才被 ZUPT 拉回 —— 那一步的步长就此报废，且**没有任何
东西会报错**。切分之后，段与段之间不积分（`core/eskf.py` 的 `run_ins` 逐段处理），
误差被关在段内。

PRD §6.1 定的阈值是 **3 个样本**。

## 顺序：先切分，再拟时基

RAY-209 的时基把 `(样本序号, 到达时刻)` 拟成一条直线。丢包打断了序号与真实采样时刻的
对应，于是它量到的是**到达率**而不是器件的采样率 —— 实测：

| 丢包率 | 采样率误差 | RAY-209 的 `stable` |
| --- | --- | --- |
| 0 | −0.00003% | True |
| 0.1% | −0.137% | False |
| 1% | −0.994% | False |
| 5% | −5.138% | False |

所以本模块的切分**只能建在到达时刻上，不能建在时基上** —— 时基是切分之后才有的东西。
（好消息是 RAY-209 为拥塞做的 `stable` 标志在每一档丢包上都报了 False：它实际上是
"这段拟合不可信"的通用指示器。）

## 空洞检测为什么不能只看相邻间隔

直觉做法是"相邻到达时刻差超过 N 个采样周期就算空洞"。它在这里不成立，两个原因：

1. **包结构**。一次通知里的样本几乎同时到达，包与包之间隔着一整包的时间。相邻样本的
   间隔因此在"几微秒"与"m/fs"之间跳，而 m 由 BLE 连接间隔决定、会变。
2. **抖动与阈值同量级**。PRD 定的阈值是 3 个样本 = 15 ms @200 Hz，而 BLE 的排队抖动
   本来就是十几毫秒。按相邻间隔判，抖动会被当成丢包。

真正的区别在于**持久性**：一次迟到的通知会让这个间隔变大、下一个变小（积压随即排空），
残差 `到达时刻 − 序号/fs` 只是抖一下；而一次真实的丢包会让残差**永久台阶式上移**，
上移量正是丢失的样本数除以采样率。

所以判据建在残差的**台阶**上，且台阶的两侧各取一个窗口的**最小值**来比 —— 与 RAY-209
用最小值滤波是同一个道理：延迟单边为正，下包络才是可比的基准。

## 逐秒到达率不能用来分级

PRD §6.1 要求"到达率逐秒监控"，本模块照做。但那个数**含抖动**，不是纯粹的丢包读数：
一次 50 ms 的重传会把约 10 个样本推过秒边界，让那一秒读作 0.95 —— 而一个样本都没丢。
实测无丢包时它最低掉到 **0.94**。

而空洞检测器是**精确**的：零误报，丢失数一个不差（注入 4/12/20 个样本的丢包，估计值
分别是 4/12/20）。

所以：**逐秒到达率用于定位（哪一秒链路忙），分级建在实测丢失上。** 两者都进报告，
但它们回答的不是同一个问题。
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import pairwise
from typing import Any, Final

import numpy as np

from gait.config import AlgoConfig

#: `integrity_report` 的结构版本。它进 `SessionMeta.integrity_report`（PRD §6.1 强制）。
INTEGRITY_REPORT_VERSION: Final[str] = "1.0"

#: 到达率的分级。`normal` / `degraded` 的边界见 `AlgoConfig.integrity_rate_*`。
GRADES: Final[tuple[str, ...]] = ("normal", "degraded", "unusable")


class IntegrityError(ValueError):
    """完整性检查的输入非法。"""


@dataclass(frozen=True)
class Gap:
    """一处数据空洞。

    `before` / `after` 是空洞两侧**实际收到**的样本索引。中间没有索引 —— 丢失的样本
    在数组里根本不存在，这也是"只能检测不能定位"的直接体现。
    """

    before: int
    after: int
    #: 两侧到达时刻之差，s。
    elapsed: float
    #: 估计丢失的样本数。**是估计**：它由时间差除以采样周期算出，带一个采样的量化误差。
    estimated_lost: int

    @property
    def duration(self) -> float:
        """空洞的时间跨度，s。报告里用它，因为"丢了多久"比"丢了几个"更好理解。"""
        return self.elapsed


@dataclass(frozen=True)
class IntegrityReport:
    """一台设备的数据完整性。进 `SessionMeta.integrity_report`（PRD §6.1 强制字段）。"""

    nominal_fs: float
    samples: int
    duration: float

    #: 按时长算的期望样本数，与实收数、总体到达率。
    #:
    #: **`overall_rate` 可以大于 1，那不是 bug。** `expected` 按**标称**采样率算，而器件
    #: 晶振有几百 ppm 的偏差 —— 标称 200 Hz 的设备实跑 200.3 Hz，一分钟就多出 18 个
    #: 样本，到达率读作 1.0016。这里不做截断：截断会把真实的晶振偏差藏起来，而那个
    #: 偏差正是 RAY-209 的时基要量的东西。分级不看这个数（它看实测丢失）。
    expected: int
    received: int
    overall_rate: float

    #: 逐秒到达率（PRD §6.1「到达率逐秒监控」）。长度为整秒数。
    #:
    #: **它含抖动，不是纯粹的丢包读数。** 一次 50 ms 的重传会把约 10 个样本推过秒
    #: 边界，让那一秒读作 0.95 —— 而一个样本都没丢。实测无丢包时它最低到 0.94。
    #: 所以它用于**定位**（哪一秒链路忙），不用于分级。
    per_second_rate: np.ndarray
    #: 逐秒**丢失样本数**，由空洞检测得出。它不含抖动，是精确的。分级建在这上面。
    per_second_loss: np.ndarray
    worst_second_rate: float
    #: 丢失最多的那一秒，丢了多少个样本。
    worst_second_loss: int
    seconds_below_warn: int
    seconds_below_unusable: int

    gaps: list[Gap]
    #: 切分之后的连续数据段，可直接作为 `FootSeries.segments`。
    segments: list[tuple[int, int]]
    #: 最长一段占全部样本的比例。它比"段数"更能说明这次采集还剩多少可用的连续数据。
    longest_segment_fraction: float

    grade: str
    version: str = INTEGRITY_REPORT_VERSION

    @property
    def lost_samples(self) -> int:
        return sum(gap.estimated_lost for gap in self.gaps)

    def snapshot(self) -> dict[str, Any]:
        """写入 `SessionMeta.integrity_report` 的普通字典。

        逐秒数组也写进去 —— PRD §6.1 要求的是"逐秒监控"，只存一个总体到达率等于把
        那句话做掉一半：一次集中在 5 秒里的丢包与均匀分布的同样多丢包，对轨迹的
        影响完全不同。
        """
        return {
            "nominal_fs": self.nominal_fs,
            "samples": self.samples,
            "duration": self.duration,
            "expected": self.expected,
            "received": self.received,
            "overall_rate": self.overall_rate,
            "per_second_rate": [float(value) for value in self.per_second_rate],
            "per_second_loss": [int(value) for value in self.per_second_loss],
            "worst_second_rate": self.worst_second_rate,
            "worst_second_loss": self.worst_second_loss,
            "seconds_below_warn": self.seconds_below_warn,
            "seconds_below_unusable": self.seconds_below_unusable,
            "gaps": [
                {
                    "before": gap.before,
                    "after": gap.after,
                    "elapsed": gap.elapsed,
                    "estimated_lost": gap.estimated_lost,
                }
                for gap in self.gaps
            ],
            "lost_samples": self.lost_samples,
            "segments": [list(segment) for segment in self.segments],
            "longest_segment_fraction": self.longest_segment_fraction,
            "grade": self.grade,
            "version": self.version,
        }


def _packet_boundaries(arrival: np.ndarray, nominal_fs: float, gap_fraction: float) -> np.ndarray:
    """新包的起始索引。与 `sync/timebase.py` 的聚簇判据一致。"""
    threshold = gap_fraction / nominal_fs
    return np.flatnonzero(np.diff(arrival) > threshold) + 1


def _window_minimum(values: np.ndarray, start: int, stop: int) -> float:
    """`values[start:stop]` 的最小值；空区间返回 `inf` 好让比较自然失败。"""
    if stop <= start:
        return float("inf")
    return float(values[max(start, 0) : stop].min())


def find_gaps(
    arrival: np.ndarray, nominal_fs: float, cfg: AlgoConfig | None = None
) -> list[Gap]:
    """检测数据空洞。判据建在残差的**台阶**上，不是相邻间隔上。

    残差 `c[k] = arrival[k] − k / fs_nominal`。一次迟到的通知让 `c` 抖一下（下一个
    间隔随即变小，积压排空）；一次真实的丢包让 `c` **永久台阶式上移**，上移量正是
    丢失样本数除以采样率。

    台阶两侧各取一个窗口的**最小值**来比 —— 延迟单边为正，下包络才是可比的基准。
    这与 RAY-209 的最小值滤波是同一个道理。

    只在**包边界**上找：丢的是整个通知，不会丢半包。
    """
    cfg = cfg or AlgoConfig()
    times = np.asarray(arrival, dtype=np.float64)
    boundaries = _packet_boundaries(times, nominal_fs, cfg.sync_packet_gap_fraction)
    if boundaries.size == 0:
        return []

    # 采样周期用**稳健估计**而不是标称值：器件晶振有几百 ppm 偏差，而残差是按周期
    # 累积的 —— 用标称值会让残差整段单调漂移，把台阶判据的基线一起带走。
    period = _robust_period(times, boundaries, nominal_fs)
    residual = times - np.arange(times.size) * period
    window = cfg.sync_minfilter_window_samples
    minimum_step = cfg.integrity_gap_samples * period

    gaps: list[Gap] = []
    scan_from = 0
    for boundary in boundaries:
        # 上一处空洞之后要让开一个窗口再继续找。
        #
        # 不让开会把同一处空洞报几十遍：台阶是**永久**的，所以在它之后的一整个窗口
        # 里，`before` 窗口仍然含着台阶之前的低残差，步长看起来始终为正。第一版就是
        # 这样 —— 一个丢了 4 个样本的包被报成 19 处空洞、估计丢 76 个。
        if boundary < scan_from:
            continue
        # 便宜的预筛：连阈值那么大的空白都装不下就不必再看。
        if times[boundary] - times[boundary - 1] < minimum_step:
            continue
        before = _window_minimum(residual, boundary - window, boundary)
        after = _window_minimum(residual, boundary, boundary + window)
        step = after - before
        if step < minimum_step:
            continue
        lost = round(step / period)
        if lost <= cfg.integrity_gap_samples:
            continue
        gaps.append(
            Gap(
                before=int(boundary - 1),
                after=int(boundary),
                elapsed=float(times[boundary] - times[boundary - 1]),
                estimated_lost=lost,
            )
        )
        scan_from = int(boundary) + window
    return gaps


#: 估计周期时一个块里放多少个包。见 `_robust_period` 的说明。
#:
#: 真机实测（RAY-200，WT901BLE67 @200 Hz）：包平均 25 个/s，成簇效应在几个包内
#: 就排空（间隔 p99 = 91 ms）。25 个包 ≈ 1 s，足以把成簇抹平；同时一轮 30 分钟
#: 有 400+ 个块，中位数拒绝含丢包的块仍然有效。
PERIOD_BLOCK_PACKETS: Final[int] = 25


def _robust_period(arrival: np.ndarray, boundaries: np.ndarray, nominal_fs: float) -> float:
    """采样周期的稳健估计，s。

    ## 为什么不能取逐包比值的中位数

    第一版取逐包 `(包间到达时差) / (包内样本数)` 的**中位数**，理由是丢包只影响
    少数包、中位数看不见它们。那个理由本身没错，但它默认了「包间隔的中位数≈均值」
    —— 而真机不是这样。

    RAY-200 真机实测（WT901BLE67，200 Hz，8 帧/包）：包间隔**严重右偏**，
    中位数 30.7 ms 而均值 40.5 ms（p99 91 ms，max 331 ms）—— BLE 通知成簇到达，
    几个挤在一起，然后等一等。逐包比值的中位数因此系统性偏小 **24%**：

    | 量 | 值 |
    | --- | --- |
    | 中位数法估计周期 | 3.85 ms |
    | 真实平均周期 | 5.07 ms |
    | 残差 851 s 内累计漂移 | 205 s |
    | `find_gaps` 报出的缺失率 | **24.00%** |
    | 按到达率反推的真实缺口 | **1.41%** |

    周期偏小 24% 会让残差 `t − k·period` 随样本数**单调爬升**，于是**每一个包边界
    都是正台阶**，累出来的假丢包恰好等于周期的偏差量。这不是精度问题，是把结论
    整个翻转的量级问题：未修复时 RAY-200 会报「200 Hz 丢包 24%、不可行」。

    模拟数据抓不到它 —— 模拟的包间隔规整，中位数等于均值，偏差为零。只有真机的
    成簇到达才触发。

    ## 分块：块内取比值之和，块间取中位数

    偏差来自「对**比值**取中位数」。改成：把包分成块，**块内**用
    `Σ时差 / Σ样本数`（比值之和，不是和的比值 —— 成簇在块内自然抹平，无偏），
    **块间**取中位数（含丢包的块被拒绝，保住稳健性）。两个性质各由一层负责。

    块数不足时退回块内同一个无偏估计（整段的 `Σ时差/Σ样本数`）：那种长度下
    本来也没什么可稳健的，而无偏比有偏重要。包数不足时退回标称值。
    """
    if boundaries.size < 3:
        return 1.0 / nominal_fs
    # 每包的最后一个样本：boundaries-1 是前一包的末尾，序列末尾补上最后一包的。
    ends = np.concatenate((boundaries - 1, [arrival.size - 1]))
    elapsed = np.diff(arrival[ends])
    # elapsed[i] 是第 i 包末尾到第 i+1 包末尾的时间，其间到达的是第 i+1 包的样本。
    sizes = np.diff(np.concatenate(([0], boundaries, [arrival.size])))
    counts = sizes[1:]
    usable = counts > 0
    elapsed, counts = elapsed[usable], counts[usable]
    if elapsed.size == 0:
        return 1.0 / nominal_fs

    block = PERIOD_BLOCK_PACKETS
    blocks = elapsed.size // block
    if blocks < 3:
        total = float(counts.sum())
        return float(elapsed.sum() / total) if total else 1.0 / nominal_fs
    trimmed = blocks * block
    block_elapsed = elapsed[:trimmed].reshape(blocks, block).sum(axis=1)
    block_counts = counts[:trimmed].reshape(blocks, block).sum(axis=1)
    return float(np.median(block_elapsed / block_counts))


def estimate_period(
    arrival: np.ndarray, nominal_fs: float, cfg: AlgoConfig | None = None
) -> float:
    """器件的实测采样周期，s。`1/它` 就是器件**实发**速率。

    为什么需要它：器件晶振偏差是真实且不小的。wt901 在真机上逐档实测，200 Hz
    档（编码 `0x0B`）实际跑 198.43 Hz —— 比标称低 **0.8%**。RAY-200 实测两台
    WT901BLE67 为 197.8 Hz。而 PRD §17.1 V2 的判据是「缺失率 < 0.5%」：光晶振
    偏差就吃掉了全部预算，按标称算的话一条完美链路也永远不达标。

    **判据要问的是「器件发出来的，链路丢了多少」**，分母因此必须是器件实发数。

    局限，用之前必须知道：本估计对**成片**的丢包稳健（块间中位数拒绝含丢包的
    块），但若丢包**均匀散布在每一个块**里，中位数块本身也含丢包，估计出的就
    退化为**到达**速率而非器件速率，缺失率随之被低估。交叉验证的办法是看
    `find_gaps` 给出的丢失数与「按本速率算的期望数 − 实收数」是否相符。
    """
    cfg = cfg or AlgoConfig()
    times = np.asarray(arrival, dtype=np.float64)
    boundaries = _packet_boundaries(times, nominal_fs, cfg.sync_packet_gap_fraction)
    return _robust_period(times, boundaries, nominal_fs)


def split_segments(samples: int, gaps: list[Gap]) -> list[tuple[int, int]]:
    """按空洞把 `[0, samples)` 切成连续段。

    段是**半开**区间且**首尾相接**（`[0,a) [a,b) …`），与契约 `FootSeries.segments` 的
    要求一致，也与 `core/eskf.py` 的"segments 必须覆盖整个序列"一致。

    切分不丢样本：空洞里的样本本来就不存在（它们没被收到），所以段的并集就是全部
    收到的样本。段与段之间断掉的是**时间连续性**，不是数组。
    """
    if samples <= 0:
        return []
    boundaries = sorted({gap.after for gap in gaps} | {0, samples})
    return [
        (start, stop)
        for start, stop in pairwise(boundaries)
        if stop > start
    ]


def per_second_rate(arrival: np.ndarray, nominal_fs: float) -> np.ndarray:
    """逐秒到达率（实收 / 期望）。PRD §6.1「到达率逐秒监控」。

    按**到达时刻**分桶而不是按样本序号：序号在丢包之后已经不代表时间了，而这个指标
    要回答的正是"哪一秒出了问题"。

    最后一个不满一秒的尾巴不计入 —— 它的分母不是 `fs`，算出来的比率会假性偏低。
    """
    times = np.asarray(arrival, dtype=np.float64)
    if times.size == 0:
        return np.zeros(0)
    seconds = int((times[-1] - times[0]) // 1.0)
    if seconds < 1:
        return np.zeros(0)
    bucket = np.minimum(((times - times[0]) // 1.0).astype(np.int64), seconds - 1)
    counts = np.bincount(bucket, minlength=seconds)[:seconds]
    return counts / nominal_fs


def per_second_loss(
    arrival: np.ndarray, gaps: list[Gap], nominal_fs: float, seconds: int
) -> np.ndarray:
    """逐秒丢失的样本数，由空洞检测得出。

    与 `per_second_rate` 的区别是**它不含抖动**：抖动只把样本在时间上挪一挪，不改变
    它们存不存在；而空洞检测量的正是"不存在"。分级因此建在这个量上。

    空洞被记在它**开始**的那一秒。跨秒的长空洞不拆分 —— 拆分需要知道丢失样本的时刻，
    而那正是无序号硬件给不出来的东西。
    """
    losses = np.zeros(max(seconds, 0), dtype=np.int64)
    if losses.size == 0 or len(arrival) == 0:
        return losses
    origin = float(arrival[0])
    for gap in gaps:
        bucket = int((float(arrival[gap.before]) - origin) // 1.0)
        losses[min(max(bucket, 0), losses.size - 1)] += gap.estimated_lost
    return losses


def _grade(overall_loss_rate: float, losses: np.ndarray, nominal_fs: float, cfg: AlgoConfig) -> str:
    """分级。PRD §6.1「分级告警」。

    **建在实测丢失上，不建在到达率上。** 逐秒到达率含抖动：实测无丢包时它会掉到
    0.94（一次 50 ms 重传推 10 个样本过秒边界），按 0.98 判会把干净的会话判成
    degraded。空洞检测器则是精确的 —— 零误报、丢失数一个不差。

    **两条线都看总体与逐秒**：一次集中在几秒里的丢包与均匀分布的同样多丢包，总体
    丢失率一样，但对轨迹的影响完全不同 —— 前者毁掉那几秒里的每一步，后者可能一步
    都没毁。只看总体会把前者放过去。
    """
    # 比的是**接收率**而不是丢失率，因为阈值本身就是按接收率写的（0.98 / 0.90）。
    # 写成 `丢失率 > 1 - 阈值` 会在边界上被浮点表示翻转：1.0 - 0.90 在 float 里是
    # 0.09999999999999998，于是"正好丢 10%"会判成 unusable。实测撞上过。
    worst = 1.0 - float(losses.max()) / nominal_fs if losses.size else 1.0
    overall = 1.0 - overall_loss_rate
    if overall < cfg.integrity_rate_unusable or worst < cfg.integrity_rate_unusable:
        return "unusable"
    if overall < cfg.integrity_rate_warn or worst < cfg.integrity_rate_warn:
        return "degraded"
    return "normal"


def assess(
    arrival: np.ndarray, nominal_fs: float, cfg: AlgoConfig | None = None
) -> IntegrityReport:
    """一台设备的完整性评估。`arrival` 是逐样本的主机接收时刻，单调不减。

    输出的 `segments` 可以直接交给 `FootSeries`，再由 `core/eskf.run_ins` 逐段滤波。
    """
    cfg = cfg or AlgoConfig()
    times = np.asarray(arrival, dtype=np.float64)
    if times.ndim != 1:
        raise IntegrityError(f"arrival 应为一维，收到 shape={times.shape}")
    if times.size < 2:
        raise IntegrityError(f"至少需要 2 个样本才能谈到达率，收到 {times.size} 个")
    if not nominal_fs > 0:
        raise IntegrityError(f"nominal_fs 必须为正，收到 {nominal_fs}")
    if np.any(np.diff(times) < 0):
        raise IntegrityError(
            "到达时刻必须单调不减 —— 逆序只可能来自两台设备的样本被混进同一个数组。"
        )

    duration = float(times[-1] - times[0])
    expected = round(duration * nominal_fs) + 1
    received = int(times.size)
    rates = per_second_rate(times, nominal_fs)

    gaps = find_gaps(times, nominal_fs, cfg)
    segments = split_segments(received, gaps)
    longest = max((stop - start for start, stop in segments), default=0)
    losses = per_second_loss(times, gaps, nominal_fs, rates.size)
    lost = sum(gap.estimated_lost for gap in gaps)
    loss_rate = lost / (received + lost) if received + lost else 0.0

    return IntegrityReport(
        nominal_fs=float(nominal_fs),
        samples=received,
        duration=duration,
        expected=expected,
        received=received,
        overall_rate=received / expected if expected else 0.0,
        per_second_rate=rates,
        per_second_loss=losses,
        worst_second_rate=float(rates.min()) if rates.size else 1.0,
        worst_second_loss=int(losses.max()) if losses.size else 0,
        seconds_below_warn=int(np.sum(1.0 - losses / nominal_fs < cfg.integrity_rate_warn)),
        seconds_below_unusable=int(
            np.sum(1.0 - losses / nominal_fs < cfg.integrity_rate_unusable)
        ),
        gaps=gaps,
        segments=segments,
        longest_segment_fraction=longest / received if received else 0.0,
        grade=_grade(loss_rate, losses, float(nominal_fs), cfg),
    )


def spans_gap(start: int, stop: int, segments: list[tuple[int, int]]) -> bool:
    """`[start, stop)` 是否跨越了段边界。

    PRD §6.1：「空洞跨越的步态周期标记 invalid」。步态周期本身属 RAY-216，本函数是
    它需要的那个判断 —— 放在这里，是因为"什么算跨越"取决于段是怎么切的，而那是本
    模块的知识。

    半开区间的边界情形值得说清楚：一个恰好在段边界处结束的周期（`stop == 段的终点`）
    **不算**跨越 —— 它的每一个样本都在同一段里。
    """
    if stop <= start:
        raise IntegrityError(f"区间必须非空：[{start}, {stop})")
    for segment_start, segment_stop in segments:
        if segment_start <= start and stop <= segment_stop:
            return False
    return True
