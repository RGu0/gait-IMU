"""主机接收时刻时基构建。契约 §1 的 `sync/timebase.py`（F3.2）。PRD v1.2 §8。

## 它替代的是被撤销的物理锚点方案

契约 §4 写的签名是 `build_timebase(anchors, n_samples, nominal_fs)` —— 那是**跺脚物理
锚点**方案的形状。PRD v1.2 §8 已经撤销全部物理锚点动作（"物理锚点把技术负担转嫁给人，
违反原则 11；且同步需求的真实强度尚未被数据证明"），v1 的默认机制改成主机侧接收时刻。

本模块按 PRD 重写签名，契约文档同步修订至 v1.4。

## 要解决的问题

器件不提供时间戳也不提供序号（《BS-BT91 硬件适配》发现 3）。主机能拿到的只有
**BLE 通知到达的时刻**，而它与真正的采样时刻之间隔着一段**单边为正**的延迟：

    t_arrival[k] = t_true[k] + latency[k],   latency[k] ≥ latency_min > 0

`latency_min`（协议栈与链路的固有延迟）**不可观测** —— 它对所有样本一样，会被整个
吸收进 offset。可观测的只有它上面的**抖动**（排队、重传、主机调度）。

这个不可观测的常数正是跨足同步误差的来源：两台设备的 `latency_min` 不必相同，而没有
任何主机侧的方法能把它们分开。PRD §8 因此预期跨足同步误差 ±10~30 ms，并把量化它的
任务交给 RAY-213 的物理锚点实验。**本模块不假装能消除它，只把能消除的那部分消除掉，
并在报告里说明剩下的是什么。**

## 三步

### 1. 包聚簇与逐样本名义时刻回推

一次 BLE 通知里可能有多个样本。`wt901` 在**逐帧解析时**取 `time.monotonic()`，所以
同一次通知里的样本拿到的时刻只差几微秒 —— 它们在时间轴上是一簇。

一簇 m 个样本里，**最后一个**才是刚采到的；前面的是更早采的、攒在一起发出来。所以
第 j 个样本的名义到达时刻要回推 `(m-1-j)/fs_nominal`。不回推的话，一簇样本会被当成
"同一时刻采到的 m 个样本"，回归的斜率随每次通知里的样本数变化。

### 2. 滑动窗口最小值滤波

延迟是单边为正的，所以**每个窗口里到达最早的那个样本最接近真值**。取它作锚点，把
排队抖动整段丢掉。

**它救的是 offset，不是斜率。** 这一点与直觉不符，值得写下来 —— 实测（模拟 BLE 到达，
真实固有延迟 12 ms）：

| 情形 | 最小值滤波的 offset | 朴素最小二乘的 offset | 两者的采样率误差 |
| --- | --- | --- | --- |
| 平稳链路 | 12.1 ms | 24.4 ms | 都是 −0.00005% 量级 |
| 抖动大 3 倍 | 12.4 ms | 33.7 ms | 都是 −0.0001% 量级 |
| 每包 12 样本 | 12.4 ms | 44.2 ms | 0.00001% vs 0.00026% |

斜率上两者几乎一样 —— 独立同分布的噪声不偏最小二乘的斜率，不管它的分布多长尾。
但**均值把抖动的期望整个加进了 offset**：朴素法给出的 offset 比真值大 12~32 ms。

而 offset 误差**就是**跨足同步误差：两台设备的抖动分布不同，偏差也就不同，两者之差
直接落进双支撑期这类跨足指标。PRD §8 预期的 ±10~30 ms 正是这个量级 —— 也就是说，
不做最小值滤波，那个预期误差会**由主机侧算法自己制造出来**。

### 3. 线性回归

对锚点做 `(样本序号, 名义到达时刻)` 的最小二乘，得到 offset 与斜率。斜率的倒数就是
**实测采样率** —— 它与标称的 200 Hz 不同，因为器件晶振有几百 ppm 的偏差，而用标称值
会让所有时间参数系统性偏移。

## 一条单直线拟合治不了的失效，以及它恰好被验收标准挡住

**链路在会话中途变差**（拥塞、受试者走远、干扰）会让延迟出现一个台阶，而单条直线
分不开"台阶"与"晶振偏快"。实测：

| 情形 | 采样率误差 | 分窗离散度 | `stable` |
| --- | --- | --- | --- |
| 平稳 | −0.00005% | 0.0023% | True |
| 中途 +10 ms | −0.0084% | 0.076% | True |
| 中途 +40 ms | −0.033% | 0.30% | **False** |
| 中途 +150 ms | −0.125% | 1.12% | **False** |

最小值滤波在这里帮不上忙（台阶是所有样本共有的，不是抖动）。

但本 Issue 的验收标准 —— "实测采样率估计稳定，相邻窗口差 < 0.1%" —— **恰好是这个
失效的检测器**：它放过 +10 ms（采样率误差 0.008%，可忽略），挡住 +40 ms 起。
`SyncReport.stable` 因此不是一个装饰性的布尔值，它是"这段数据能不能用一条直线描述"
的判断。

**修不了但看得见**，这已经是这个方案能做到的上限；真要修需要分段拟合，而分段的
边界靠什么定又是另一个问题（RAY-210 的空洞切分可能提供一部分）。

## 输出的时间轴是"主机时基下的采样时刻"

`t[k] = offset + k/fs_measured`。它**不是**到达时刻，也不是器件的内部时刻，而是"若延迟
恒为 latency_min，这个样本会在主机的什么时刻到达"。两台设备各自解出自己的一条线，因为
它们共用同一个主机时钟，两条线天然可比 —— 这就是 PRD §8 说的"双足映射到同一主机时基"。

映射本身是恒等的；有内容的是**它的不确定度**，见 `cross_foot_uncertainty`。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

import numpy as np

from gait.config import AlgoConfig

#: `sync_report` 的结构版本。它进 `SessionMeta.sync_report`，因而进每一份历史会话。
SYNC_REPORT_VERSION: Final[str] = "1.0"


class TimebaseError(ValueError):
    """时基构建的输入非法。"""


@dataclass(frozen=True)
class SyncReport:
    """一台设备的时基质量。进 `SessionMeta.sync_report`（PRD §6.1 强制字段）。

    字段选择的原则：**能让三个月后的人判断这次会话的时间轴可不可信**。所以既有结果
    （offset、实测采样率），也有过程（锚点数、残差分布），还有稳定性（分窗采样率的
    离散度 —— 那正是本 Issue 的验收标准）。
    """

    #: 拟合结果。`t[k] = offset + k / fs`。
    offset: float
    fs: float
    #: 标称采样率，用于回推包内时刻。留在报告里是因为它影响结果。
    nominal_fs: float

    #: 样本数、识别出的包数、每包样本数的中位数。
    samples: int
    packets: int
    samples_per_packet: float

    #: 最小值滤波留下的锚点数。太少意味着回归的自由度不足。
    anchors: int

    #: 名义到达时刻相对拟合直线的残差，s。**它就是 BLE 抖动**。
    #: 只统计正侧：最小值滤波之后残差应当全部 ≥ 0（锚点落在下包络上）。
    residual_rms: float
    residual_p95: float
    residual_max: float

    #: 分窗估计采样率的相对离散度（最大相对偏差）。本 Issue 的验收标准是 < 0.1%。
    fs_window_spread: float
    fs_windows: int

    version: str = SYNC_REPORT_VERSION

    @property
    def fs_deviation_ppm(self) -> float:
        """实测采样率相对标称的偏差，ppm。器件晶振偏差的直接读数。"""
        return 1e6 * (self.fs - self.nominal_fs) / self.nominal_fs

    @property
    def stable(self) -> bool:
        """采样率估计是否稳定。验收标准：相邻窗口差 < 0.1%。"""
        return self.fs_window_spread < 1e-3

    def snapshot(self) -> dict[str, Any]:
        """写入 `SessionMeta.sync_report` 的普通字典。

        与 `config.snapshot()` 同一个理由：手写的快照可以漏字段，而漏掉的那个字段
        正是三个月后复现失败的原因。
        """
        return {
            "offset": self.offset,
            "fs": self.fs,
            "nominal_fs": self.nominal_fs,
            "fs_deviation_ppm": self.fs_deviation_ppm,
            "samples": self.samples,
            "packets": self.packets,
            "samples_per_packet": self.samples_per_packet,
            "anchors": self.anchors,
            "residual_rms": self.residual_rms,
            "residual_p95": self.residual_p95,
            "residual_max": self.residual_max,
            "fs_window_spread": self.fs_window_spread,
            "fs_windows": self.fs_windows,
            "stable": self.stable,
            "version": self.version,
        }


@dataclass(frozen=True)
class Timebase:
    """一台设备的时间轴与它的报告。"""

    #: (n,) 主机时基下的采样时刻，s。它**不是**到达时刻，见模块文档。
    t: np.ndarray
    report: SyncReport


@dataclass(frozen=True)
class CrossFootSync:
    """双足映射的不确定度。RAY-211 的自检与 RAY-213 的量化都从这里取输入。

    **映射本身是恒等的** —— 两台设备共用同一个主机时钟，各自解出的直线天然可比。
    有内容的是这个结构里的数：它们说明"天然可比"到什么程度。
    """

    #: 两足实测采样率之差的相对值。器件晶振各自偏差，这个数不为零是正常的。
    fs_mismatch: float
    #: 两足残差 p95 之和，s。它是**可观测**的抖动对跨足误差的贡献上界。
    observable_jitter: float
    #: 两足在 t=0 上的 offset 之差，s。**它同时包含真实的启动时差与两台设备各自
    #: 不可观测的固有延迟之差**，因此不能被解读成"同步误差"。
    offset_difference: float

    @property
    def caveat(self) -> str:
        """这个结构最重要的字段是一句话。

        写成属性而不是注释，是为了让它能被打印进报告、被测试断言 —— PRD §8 要求
        跨足时序指标"输出时强制附同步质量标注"，而标注里必须包含这句话的含义。
        """
        return (
            "固有链路延迟不可观测：两台设备的 latency_min 之差整体落进 offset_difference，"
            "主机侧无法把它与真实的启动时差分开。跨足同步误差的真值需要物理锚点实验"
            "（RAY-213）来定；本报告只给出可观测的抖动部分。"
        )


def _packet_boundaries(arrival: np.ndarray, nominal_fs: float, gap_fraction: float) -> np.ndarray:
    """每个样本所属包的编号。

    同一次 BLE 通知里的样本，其 `t_host` 只差几微秒（`wt901` 逐帧取
    `time.monotonic()`）。相邻样本的到达时刻差超过 `gap_fraction / fs` 就算新的一包。

    阈值取采样周期的一个分数而不是一个绝对时间：200 Hz 与 100 Hz 下"几微秒"是同一件事，
    但"一个采样周期"不是。
    """
    if arrival.size == 0:
        return np.zeros(0, dtype=np.int64)
    threshold = gap_fraction / nominal_fs
    new_packet = np.concatenate(([True], np.diff(arrival) > threshold))
    return np.cumsum(new_packet) - 1


def _nominal_arrival(arrival: np.ndarray, packet: np.ndarray, nominal_fs: float) -> np.ndarray:
    """按包内位置把到达时刻回推成逐样本的名义到达时刻。

    一簇 m 个样本里最后一个才是刚采到的，前面的是更早采的、攒在一起发出来。所以第 j 个
    样本回推 `(m-1-j)/fs_nominal`。

    不回推会怎样：一簇样本被当成"同一时刻采到的 m 个"，回归的斜率随每次通知里的样本数
    变化 —— 而那个数由 BLE 连接间隔决定，是会变的。
    """
    counts = np.bincount(packet)
    # 每个样本在自己包内的序号。
    starts = np.concatenate(([0], np.cumsum(counts)[:-1]))
    within = np.arange(arrival.size) - starts[packet]
    return arrival - (counts[packet] - 1 - within) / nominal_fs


def _minimum_filter_anchors(residual: np.ndarray, window: int) -> np.ndarray:
    """每个窗口里残差最小的样本索引。

    延迟单边为正，所以窗口里到达最早的那个最接近真值。用最小值而不是均值：BLE 的延迟
    分布是长尾的（一次重传几十毫秒），均值会被拖走。
    """
    anchors: list[int] = []
    for start in range(0, residual.size, window):
        stop = min(start + window, residual.size)
        anchors.append(start + int(np.argmin(residual[start:stop])))
    return np.asarray(anchors, dtype=np.int64)


def _fit(index: np.ndarray, time: np.ndarray) -> tuple[float, float]:
    """最小二乘直线 `time ≈ offset + slope · index`。"""
    matrix = np.stack([np.ones_like(index, dtype=np.float64), index.astype(np.float64)], axis=1)
    solution, *_ = np.linalg.lstsq(matrix, time, rcond=None)
    return float(solution[0]), float(solution[1])


def build_timebase(
    arrival: np.ndarray, nominal_fs: float, cfg: AlgoConfig | None = None
) -> Timebase:
    """由逐样本的主机接收时刻构建时间轴。PRD §8 的 v1 默认同步机制。

    `arrival` 是 `wt901.ImuSample.t_host` 按样本顺序排成的数组，单调不减。

    ## 与契约 §4 的签名差异

    契约写的是 `build_timebase(anchors, n_samples, nominal_fs)` —— 那是**物理锚点**
    方案的形状，而 PRD v1.2 §8 已经撤销全部物理锚点动作。本函数按 PRD 重写；契约文档
    同步修订至 v1.4。

    ## 为什么要求单调不减

    `time.monotonic()` 保证单调，所以逆序只可能来自两种情况：两台设备的样本被混进了
    同一个数组，或者调用方自己重排过。两者都会让回归得到一个无意义的斜率，而斜率
    错了不会报错 —— 它只会让整段会话的时间轴被拉伸或压缩。
    """
    cfg = cfg or AlgoConfig()
    times = np.asarray(arrival, dtype=np.float64)
    if times.ndim != 1:
        raise TimebaseError(f"arrival 应为一维，收到 shape={times.shape}")
    if not nominal_fs > 0:
        raise TimebaseError(f"nominal_fs 必须为正，收到 {nominal_fs}")

    window = cfg.sync_minfilter_window_samples
    if times.size < 2 * window:
        raise TimebaseError(
            f"只有 {times.size} 个样本，不足两个最小值滤波窗口（{window}）。"
            "回归至少需要两个锚点，否则解出的斜率没有任何约束 —— 而一个错的斜率"
            "会把整段会话的时间轴拉伸或压缩，且不报错。"
        )
    if np.any(np.diff(times) < 0):
        bad = int(np.argmin(np.diff(times)))
        raise TimebaseError(
            f"到达时刻必须单调不减：arrival[{bad}]={times[bad]} > arrival[{bad + 1}]="
            f"{times[bad + 1]}。`time.monotonic()` 本身单调，逆序只可能来自两台设备的"
            "样本被混进同一个数组，或调用方重排过。"
        )

    packet = _packet_boundaries(times, nominal_fs, cfg.sync_packet_gap_fraction)
    nominal = _nominal_arrival(times, packet, nominal_fs)

    index = np.arange(times.size, dtype=np.float64)
    # 先用标称斜率把趋势去掉，最小值滤波才是在比较"延迟"而不是"时间"。
    residual = nominal - index / nominal_fs
    anchors = _minimum_filter_anchors(residual, window)
    offset, slope = _fit(index[anchors], nominal[anchors])
    if slope <= 0:
        raise TimebaseError(
            f"回归得到非正的采样间隔 {slope}。数据多半不是同一台设备的连续采集。"
        )

    fitted = offset + slope * index
    final_residual = nominal - fitted
    fs = 1.0 / slope

    spread, windows = _fs_stability(index, nominal, float(nominal_fs), cfg)
    report = SyncReport(
        offset=offset,
        fs=fs,
        nominal_fs=float(nominal_fs),
        samples=int(times.size),
        packets=int(packet[-1] + 1),
        samples_per_packet=float(np.median(np.bincount(packet))),
        anchors=int(anchors.size),
        residual_rms=float(np.sqrt(np.mean(final_residual**2))),
        residual_p95=float(np.percentile(final_residual, 95)),
        residual_max=float(final_residual.max()),
        fs_window_spread=spread,
        fs_windows=windows,
    )
    return Timebase(t=fitted, report=report)


def _fs_stability(
    index: np.ndarray, nominal: np.ndarray, nominal_fs: float, cfg: AlgoConfig
) -> tuple[float, int]:
    """把序列切成若干大窗口各拟合一次，返回采样率估计的相对离散度与窗口数。

    这是本 Issue 验收标准（"实测采样率估计稳定，相邻窗口差 < 0.1%"）的直接度量。

    它不参与时基构建 —— 时基用的是整段拟合。这里只回答"这次的估计有多可信"。分窗
    估计比整段拟合噪声大得多，所以它是一个**保守**的稳定性读数：分窗都稳，整段必然更稳。
    """
    span = cfg.sync_stability_window_samples
    windows = int(index.size // span)
    if windows < 2:
        # 窗口不足两个就没有"相邻窗口差"可言。返回 0 而不是 nan：nan 会一路流进
        # 质量标注，在报告里变成一个没人能解释的空白格。窗口数一并返回，让调用方
        # 能区分"稳"与"没得比"。
        return 0.0, windows

    estimates: list[float] = []
    inner = cfg.sync_minfilter_window_samples
    for number in range(windows):
        piece = slice(number * span, (number + 1) * span)
        residual = nominal[piece] - index[piece] / nominal_fs
        anchors = _minimum_filter_anchors(residual, inner)
        if anchors.size < 2:
            continue
        _, slope = _fit(index[piece][anchors], nominal[piece][anchors])
        if slope > 0:
            estimates.append(1.0 / slope)
    if len(estimates) < 2:
        return 0.0, len(estimates)
    values = np.asarray(estimates)
    return float((values.max() - values.min()) / values.mean()), len(estimates)


def cross_foot_uncertainty(left: SyncReport, right: SyncReport) -> CrossFootSync:
    """双足映射的不确定度。

    **映射本身是恒等的**：两台设备共用同一个主机时钟（`time.monotonic()`），各自解出的
    直线天然落在同一条时间轴上。所以这个函数不做任何变换 —— 它只把"天然可比到什么程度"
    量出来。

    `offset_difference` 特别容易被误读。它包含两部分：受试者两只脚开始记录的真实时差，
    以及两台设备**不可观测的固有链路延迟之差**。主机侧没有任何办法把它们分开，所以
    这个数**不能**被当成同步误差。`caveat` 属性写着这句话。
    """
    return CrossFootSync(
        fs_mismatch=abs(left.fs - right.fs) / (0.5 * (left.fs + right.fs)),
        observable_jitter=left.residual_p95 + right.residual_p95,
        offset_difference=left.offset - right.offset,
    )
