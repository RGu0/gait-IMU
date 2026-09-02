"""零速检测。契约 §1 的 `core/zupt.py`（F4.2/4.6）。整体设计 §5.5。

## 为什么这是核心难点

足绑惯导的全部精度来源于"每一步支撑相都有一个瞬间足部真实静止"。此时惯导估计的速度
与真值 0 之差就是可观测的误差，能反推出陀螺零偏、姿态误差、速度误差。

两种失效方式**不对称**，这决定了整个设计的偏向：

* **漏检**：误差在这一步不受约束地累积。代价是一步的精度。
* **误检**（把运动判成静止）：向滤波器注入一个错误的零速观测。代价是**毁掉整条轨迹**，
  而且不会报错 —— 滤波器会满怀信心地收敛到错误的状态。

所以判据是"联合与"而不是"任一命中"：C1∧C2∧C3∧C4 全过才算候选，GLRT 再终判一次。
宁可漏，不可错。

## 检测用信号与积分用信号必须分开

整体设计 §5.2 第 3 条："仅在**零速检测器输入端**用截止 5–10 Hz 的低通去冲击噪声；
送入 INS 积分的加速度必须是未额外滤波的原始值。这个'检测用信号'与'积分用信号'分离的
设计，是很多实现踩坑的地方。"

本模块因此**只**返回标志与统计量，从不返回滤波后的信号。滤波结果连出口都没有，
下游拿不到，也就没法误用。`AlgoConfig.detection_lowpass_hz` 的名字里带 `detection_`
是同一个用意。

陀螺不做额外低通（芯片内置抗混叠已足够）：对陀螺过度滤波会造成姿态积分的相位滞后，
直接表现为轨迹弯曲。

## 判据

整体设计 §5.5.2 的五条，窗口 W 为中心对齐的滑窗：

| 判据 | 表达式 | 物理含义 |
| --- | --- | --- |
| C1 | `abs(‖ā‖ - g) < γ1` | 静止时只受重力 |
| C2 | `var(a) < γ2` | 无冲击、无振动 |
| C3 | `‖ω̄‖ < γ3` | 无转动 |
| C4 | `var(ω) < γ4` | 转动平稳为零 |
| C5 | GLRT `T < γ5` | Skog 等提出的广义似然比检验 |

C1 与 C2 分工不同，两条都要：一个匀速平移的足部能骗过 C1（比力仍等于重力）但骗不过
C2。反过来一次平稳的持续加速能骗过 C2 但骗不过 C1。

整体设计 §5.5.2 说"粗筛通过后才算 GLRT（省算力）"。**本实现不这么做，而是全程
计算**：在向量化的 numpy 里，按掩码选择性计算比直接算全部更贵（要做一次布尔索引、
一次散回），所谓的省算力只在逐样本循环的实现里成立。分工本身没变 —— 判定仍然是
`C1∧C2∧C3∧C4 ∧ (T < γ5)`。

全程计算还有一个实打实的好处：软零速降级需要在"一条都没通过粗筛"的区间里挑出最像
静止的那一刻。若统计量只在粗筛通过处存在，那个区间里就无从比较，只能任取一个位置。

## 软零速降级

整体设计 §5.5.3：高速下支撑相可能短于一个窗口，甚至不存在真零速。此时不硬撑，
而是在"这一步显然发生了但没检到"的间隔里挑统计量最小的时刻，标一个**降级**的零速。

本模块只负责标出 `degraded`；把观测噪声 R 放大 10–50 倍是 RAY-204 的 ESKF 该做的事。
分工的理由是 R 属于滤波器的噪声模型，而检测器不该知道滤波器怎么用它的输出。

## ZARU 单独输出

零角速率比零速更鲁棒 —— 即使足部有平动，支撑中期的角速度通常仍接近零。因此
`zaru` 是一条独立的掩码而不是 `zupt` 的子集：低速/病理预设下（`force_zaru`）它会在
零速判据没过的地方仍然给出可用的约束，那正是 PRD §7 要求强制 ZARU 的场景。

## 输入必须是连续段

`acc` / `gyr` 要来自 `FootSeries` 的**单个** `segments` 区间。跨空洞调用会让滑窗把
空洞两侧的样本混进同一个窗口，而那两侧之间隔着未知长的时间。空洞切分是 RAY-210 的
职责，本模块不重复判断 —— 它没有时间轴，判断不了。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from itertools import pairwise
from typing import Final

import numpy as np

from gait.config import AlgoConfig
from gait.core.ins import GRAVITY_STANDARD

#: 低通用的 FIR 阶数上限。截止越低需要的抽头越多，而抽头长到与支撑相同量级时，
#: 滤波器会把支撑相的边界抹掉 —— 那正是要检测的东西。
_MAX_TAPS: Final[int] = 201


class ZuptError(ValueError):
    """零速检测的输入非法。"""


@dataclass(frozen=True)
class PeriodReport:
    """步态周期的估计，以及"这几个独立估计彼此同意吗"。

    它**必须**能被调用方读到。用户选定的降级路径是"周期不一致时标记并降级，不丢段"，
    而"标记"只有在下游能看见标记时才有意义 —— 一个只影响 `confidence` 数值、不说明
    原因的降级，读报告的人无从分辨"这一段本来就难"与"检测器出问题了"。
    """

    #: 采用的周期（各估计的中位数），样本。
    period_samples: float
    #: 周期数。**由 `round(时长/周期)` 定死，不由检测决定** —— 这是全设计的要害：
    #: "两周期并成一个"或"一周期劈成两个"在结构上不可能发生。
    cycles: int
    #: 各法给出的周期（样本）。落进报告供人直接核对，不是调试残留。
    #:
    #: 名字就是来源：`autocorrelation` / `swing` / `impact` 来自本脚自己的信号，
    #: `crosscorrelation` 来自双脚互相关的外部先验（RAY-328 L1）。哪一票在场是可读
    #: 的事实 —— 同一段数据用不用双脚，估计池就不一样，而报告必须说得出这件事。
    estimates: tuple[tuple[str, float], ...]
    #: 各估计的 max/min。实测一致时 1.005~1.058，致命失效时 2.0。
    ratio: float
    #: 一致性闸是否通过。False 不代表结果不可用，代表它已被降级标记。
    consistent: bool
    #: 周期边界，半开、升序，恰好 `cycles` 个。
    bounds: tuple[tuple[int, int], ...]
    #: 网格**之前**与**之后**还剩多少数据，以周期计。
    #:
    #: 网格只铺在首个到末个摆动峰之间（见 `_lay_grid`），两头总还剩一截 —— 那截里
    #: 通常正好有**一步**，只是它没有完整的两个边界，所以不进 `cycles`。
    #:
    #: **不要把它加进 `cycles`。** `cycles` 的定义是"观测跨度内的完整周期"，那是个
    #: 稳定、可复核的量；而"这一趟一共迈了几步"是另一个问题，答案要看两头那两截算不算。
    #: 报出来让调用方自己决定 —— `spanned_cycles` 是其中一种决定。
    head_truncated: float = 0.0
    tail_truncated: float = 0.0

    @property
    def truncated(self) -> bool:
        """两头合起来还剩至少半个周期 —— 也就是"至少有一步没被数进来"。"""
        return self.head_truncated + self.tail_truncated >= 0.5

    @property
    def spanned_cycles(self) -> int:
        """**数据跨度**里一共有几个周期，把两头那两截算进来。

        与 `cycles` 是两个量，谁也不替代谁：`cycles` 数的是完整的格子，这个数的是
        这段数据实际盖住了多少个周期。做一次加法再取整，而不是两头各取整再相加 ——
        两头各 0.5 个周期合起来是一整个，分开取整会把它整个丢掉。

        实测（RAY-339，24 格，真值 38）：`cycles` 的偏差是 **−1.04**，本属性是
        **+0.33**、RMSE 0.96。作为对照，T-230-03 那种定长走廊可以直接 `cycles + 1`
        （偏差 −0.04），但那个 1 来自"首尾恰好各半步"这个**协议性质**，自由行走没有 ——
        本属性不需要它。
        """
        return round(self.cycles + self.head_truncated + self.tail_truncated)


@dataclass(frozen=True)
class StanceDetection:
    """检测结果。

    与契约 §4 的 `detect_stance(...) -> tuple[np.ndarray, list, np.ndarray]` 相比多出
    `zaru`、`degraded` 与 `confidence` 三项。三项都不是可选的装饰：

    * `degraded` 是契约 §3.3 `NavResult.degraded` 的来源，PRD §13 的质量标注要靠它；
    * `zaru` 是 PRD §7 强制 ZARU 的载体，它与 `zupt` 不是包含关系；
    * `confidence` 是 RAY-218 `zupt_quality` 的输入。

    三元组装不下它们，而用元组硬塞会让调用点变成一串靠位置记忆的解包。
    """

    #: (n,) 最终零速标志，含软降级得到的那些。
    zupt: np.ndarray
    #: (n,) 零角速率标志。**不是 `zupt` 的子集**。
    zaru: np.ndarray
    #: (n,) 该样本的零速来自软降级而非硬检测。
    degraded: np.ndarray
    #: 零速区间，与契约 `NavResult.stances` 同形（升序、不重叠）。
    stances: list[tuple[int, int]]
    #: (n,) GLRT 统计量，**全程有限**。越小越像静止。
    #: 它不受粗筛门控，理由见模块文档：向量化下选择性计算并不省，而软零速降级需要在
    #: 粗筛全不通过的区间里仍然能比较。
    score: np.ndarray
    #: (n,) 0~1 的置信度，非零速样本处为 0。见 `_confidence` 的文档。
    confidence: np.ndarray
    #: 周期分段的结果。None 表示这一段没有可辨认的步态（静立、或太短），此时零速
    #: 全部来自阈值判据与既有的软零速降级。
    period: PeriodReport | None = None

    @property
    def hard(self) -> np.ndarray:
        """硬检测得到的零速。`zupt & ~degraded`。"""
        return self.zupt & ~self.degraded


def lowpass(
    x: np.ndarray, fs: float, cutoff_hz: float, *, max_taps: int = _MAX_TAPS
) -> np.ndarray:
    """零相位低通，窗口化 sinc FIR。**只供检测使用。**

    ## 为什么自己写而不是用 scipy

    本仓库的运行时依赖只有 numpy 与 wt901（`pyproject.toml`）。为了一个低通引入 scipy
    会把一个 30 MB 的依赖装进采集端的 Electron 打包（RAY-250），而这里需要的只是
    一条二十行的对称 FIR。

    ## 零相位靠对称抽头，不靠前后各滤一遍

    对称 FIR 的群延迟是常数 `(taps-1)/2`，居中卷积即可抵消。前后各滤一遍（filtfilt）
    也能零相位，但会把有效阶数翻倍，边界瞬态也翻倍 —— 而这里的信号短、边界多
    （每个数据段都是一次边界）。

    边界用**边缘复制**填充。补零会在序列首尾造出一个虚假的阶跃，而序列首尾正是
    静止段最可能出现的地方（PRD §7 的测试从静立开始）。

    ## `max_taps` 不是性能参数，是正确性参数

    一个对称 FIR 会把每个跳变向两侧各抹开 `(taps-1)/2` 个样本。支撑相与摆动相的
    边界正是跳变，所以抽头越长，检测到的支撑相被削得越短 —— 慢走时无所谓（支撑相
    600–800 ms），慢跑时支撑相只有 200–300 ms，一个 255 ms 的滤波器会把它整个抹平。

    调用方（`detect_stance`）因此把 `max_taps` 钉在检测窗口上：**滤波器不得抹过
    检测窗口的尺度**，否则窗口的含义就被滤波器先毁掉了。代价是在低截止频率下达不到
    理想的陡峭度 —— 实际得到的是一个温和的平滑器，而不是一个 8 Hz 的砖墙。这是
    有意的取舍：这里要挡的是触地冲击那种宽带尖峰，温和就够；而把支撑相削没了是
    不可接受的。
    """
    signal = np.asarray(x, dtype=np.float64)
    if signal.ndim != 2 or signal.shape[1] != 3:
        raise ZuptError(f"待滤波信号应为 (n, 3)，收到 shape={signal.shape}")
    if not fs > 0:
        raise ZuptError(f"fs 必须为正，收到 {fs}")
    nyquist = 0.5 * fs
    if not 0 < cutoff_hz < nyquist:
        raise ZuptError(
            f"截止频率应在 (0, {nyquist}) 内，收到 {cutoff_hz}。"
            "高于 Nyquist 的截止意味着不滤波，那与「检测用信号要低通」的设计相矛盾。"
        )

    normalised = cutoff_hz / nyquist
    # 过渡带宽约 4/taps（Hann 窗）。取 taps ≈ 4/normalised 让过渡带与截止同量级。
    if max_taps < 3:
        raise ZuptError(f"max_taps 至少为 3，收到 {max_taps}")
    taps = int(4.0 / normalised)
    # 奇数才有中心抽头，居中卷积才能真正零相位。
    taps = max(3, min(_MAX_TAPS, max_taps | 1, taps | 1))
    if taps >= len(signal):
        # 序列比滤波器还短：此时"滤波"只是取全段均值，没有意义也没有危害。
        # 但它会把整段抹平，从而让任何段都看起来像静止 —— 那是危险的，所以拒绝。
        raise ZuptError(
            f"序列长度 {len(signal)} 不足以支撑 {taps} 抽头的低通。"
            "把这么短的段整体抹平会让它看起来像静止段，那正是最不该误判的方向。"
        )

    half = (taps - 1) // 2
    index = np.arange(taps) - half
    kernel = normalised * np.sinc(normalised * index) * np.hanning(taps)
    kernel /= kernel.sum()

    padded = np.pad(signal, ((half, half), (0, 0)), mode="edge")
    return np.stack(
        [np.convolve(padded[:, axis], kernel, mode="valid") for axis in range(3)],
        axis=-1,
    )


def window_sums(x: np.ndarray, window: int) -> np.ndarray:
    """中心对齐滑窗的和，输出与输入等长。边界用边缘复制。

    用累积和而不是 `np.convolve`：O(n) 而不是 O(n·W)，而 180 s × 200 Hz × 两足下
    这个差别是实打实的。
    """
    array = np.atleast_2d(np.asarray(x, dtype=np.float64).T).T
    left = window // 2
    right = window - 1 - left
    padded = np.pad(array, ((left, right), (0, 0)), mode="edge")
    cumulative = np.concatenate(
        [np.zeros((1, padded.shape[1])), np.cumsum(padded, axis=0)], axis=0
    )
    return cumulative[window:] - cumulative[:-window]


def runs(mask: np.ndarray) -> list[tuple[int, int]]:
    """布尔掩码里的连续 True 区间，半开。"""
    if not mask.any():
        return []
    padded = np.concatenate(([False], mask, [False]))
    edges = np.flatnonzero(padded[1:] != padded[:-1])
    return [
        (int(start), int(end))
        for start, end in zip(edges[::2], edges[1::2], strict=True)
    ]


def _confidence(score: np.ndarray, threshold: float) -> np.ndarray:
    """GLRT 统计量 → 0~1 的置信度。

    映射取 `1 - T/γ5`，在判据边界处为 0、在完全静止处趋近 1。

    **这个数的绝对值没有校准过，它的序才有意义。** 说清楚是因为它会以
    `zupt_quality` 的名义出现在报告里（RAY-218），而一个看起来像概率的数很容易被
    当成概率。真正需要概率解释时要做的是在真机数据上拟合，而不是换一个更像概率的
    公式 —— 后者只会让它更容易被误信。
    """
    return np.clip(1.0 - score / threshold, 0.0, 1.0)


def _local_peaks(signal: np.ndarray, min_distance: int) -> np.ndarray:
    """幅值降序的贪心峰选，保证两两间距 ≥ `min_distance`。

    先取**严格**局部极大，否则一个平顶上的每个样本都会成为候选，而贪心会把它们
    当成一串挨着的峰。相等的一侧用 `>=`、另一侧用 `>`，平台只留最左那个。

    `min_distance` 由估出的周期给（0.5×周期），**不是固定参数**。固定间距会全面
    失败：慢走周期 2.5 s、快走 1.0 s，实测 sep=0.3 s 给出 120 个峰、sep=0.5 s 给出
    97 个，而真值 38（T-230-04 追加一）。
    """
    min_distance = max(1, min_distance)
    if signal.size < 3:
        return np.zeros(0, dtype=int)
    interior = (
        np.flatnonzero((signal[1:-1] >= signal[:-2]) & (signal[1:-1] > signal[2:])) + 1
    )
    if interior.size == 0:
        return interior.astype(int)
    # 占位数组 + 定长切片，而不是"与已选的每一个比距离"。噪声让 ‖ω‖ 上出现几千个
    # 局部极大（真正的摆动峰只有几十个），逐对比较是 O(k²) —— 实测 36000 样本上要跑
    # 到分钟量级，而这是每段数据都要走的路径。切片检查是 O(k·min_distance)，两者在
    # 结果上**完全等价**：都在问"容差内有没有已选的峰"。
    taken = np.zeros(signal.size, dtype=bool)
    chosen: list[int] = []
    for candidate in interior[np.argsort(signal[interior], kind="stable")[::-1]]:
        index = int(candidate)
        if taken[max(0, index - min_distance + 1) : index + min_distance].any():
            continue
        taken[index] = True
        chosen.append(index)
    return np.array(sorted(chosen), dtype=int)


def _autocorrelation_period(signal: np.ndarray, low: int, high: int) -> float | None:
    """自相关的峰所在滞后，样本。估不出时 None。

    它**不依赖任何特征检测**，因此在一致性闸里是一票独立的证据：三个特征都锁错了
    同一个错误事件时，特征之间仍然彼此一致，只有自相关会不同意。
    """
    n = signal.size
    high = min(high, n // 2)
    if high <= low or low < 1:
        return None
    centred = signal - signal.mean()
    energy = float(centred @ centred)
    if energy <= 0.0:
        # 常量信号：没有周期可言。这不是失败，是"这段没有步态"——
        # 静立、或足部被固定。调用方应当回落到阈值判据。
        return None
    # 经 FFT 算自相关。`np.correlate(x, x, "full")` 走的是直接卷积，O(n²)：实测一趟
    # 慢速档 23000 样本要 5×10⁸ 次乘加，单趟就跑掉好几分钟，而真机记录只会更长。
    # 它还把 23000 个滞后全算了出来，可这里只用得上 `high + 1` 个。
    #
    # 补零到 ≥ 2n 再变换，是为了让循环相关等于线性相关 —— 不补零的话尾部会绕回来
    # 加到头部的滞后上，那正是周期估计最敏感的一段。
    size = 1 << math.ceil(math.log2(2 * n))
    spectrum = np.fft.rfft(centred, size)
    correlation = np.fft.irfft(spectrum * np.conj(spectrum), size)
    window = correlation[low : high + 1]
    if window.size == 0 or not np.isfinite(window).all():
        return None
    # 取**最小**的近极大滞后，而不是全局 argmax。周期为 T 的信号在 2T、3T 上同样有
    # 强峰，而 argmax 落在哪一个取决于包络衰减这种与步态无关的因素。基频选错成 2T
    # 会让周期数正好少一半 —— 那正是本设计要在结构上排除的失效，不能让它从种子进来。
    peak = float(window.max())
    if peak <= 0.0:
        return None
    return float(low + int(np.flatnonzero(window >= 0.9 * peak)[0]))


def _fold_harmonic(value: float, seed: float) -> float:
    """把 ×2 / ×3 / ÷2 / ÷3 的谐波折回基频。

    峰选器锁到"每周期两次冲击"里的另一次时，给出的周期正好是真周期的一半 ——
    实测 `walk` 上摆动峰给出 112 而真值 222。这不是"估歪了一点"，是**锁到了谐波**，
    而谐波是量化的：只能是整数倍或整数分之一。

    折叠只允许这几个精确因子，所以它修不好一个真正不一致的估计 —— 那样的估计折完
    仍然离种子很远，一致性闸照样拦得住。换句话说，折叠拿走的是"谐波"这一种已知的
    歧义，没有拿走闸门的鉴别力。

    **代价要说清楚**：折叠以自相关种子为锚，因此闸门对"种子自己错了整整一倍"这种
    情形不再敏感。这是 `_autocorrelation_period` 偏向最小近极大滞后的原因 ——
    那一条把种子锁在基频上，是本折叠成立的前提，两者必须一起读。
    """
    best = value
    for factor in (1.0, 2.0, 3.0, 0.5, 1.0 / 3.0):
        candidate = value * factor
        if abs(math.log(candidate / seed)) < abs(math.log(best / seed)):
            best = candidate
    return best


def _estimate_period(
    swing: np.ndarray,
    impact: np.ndarray,
    fs: float,
    cfg: AlgoConfig,
    prior_samples: float | None = None,
) -> PeriodReport | None:
    """估步态周期并划出周期边界。不是步态时返回 None。

    四个独立估计（自相关 + 三特征里的两个可算的）互相校验。校验放在**周期域**而不是
    事件域，理由是失效模式本身是量化的：两周期并成一个 → ×2，一周期劈成两个 → ×0.5，
    而一致时实测只差 1.005~1.058。要分的两类东西差了一个数量级，所以这道闸有近 20 倍
    余量（T-230-04 追加二）。

    事件域没有这样的分离：A–B 事件间隔的 IQR 跨趟差 30 倍，"这两个事件相差 80 ms
    算不算同一个"需要一个精细容差，而那个容差同样面临太严/太松。

    `prior_samples` 是**外部**算好的周期先验，样本（RAY-328 L1：双脚 swing 互相关的
    峰间距 T_x）。它由调用方算好传进来，本模块不去取 —— 互相关要两只脚的信号落在同
    一条时间轴上，而对齐是 `sync` 层的事，`gait.core` 不得 import 它（分层红线）。

    它进的是**估计池**，不是特权通道：与另外三个估计一样折谐波、一样做范围检查、
    一样参与一致性闸与中位数。理由是它并不比别的估计更可信 —— 实测 T_x 与单脚中位
    周期差 0.5%~9.3%，而它自己也可能锁到谐波上。给它特权就等于把"两只脚一起错"这
    种失效变成不可检出的。
    """
    low = max(1, round(cfg.stance_period_min_s * fs))
    high = round(cfg.stance_period_max_s * fs)
    seed = _autocorrelation_period(swing, low, high)
    if seed is None:
        return None

    separation = max(1, round(0.5 * seed))
    estimates: list[tuple[str, float]] = [("autocorrelation", seed)]
    if prior_samples is not None and prior_samples > 0.0:
        prior = _fold_harmonic(float(prior_samples), seed)
        if low <= prior <= high:
            estimates.append(("crosscorrelation", prior))
    for name, signal in (("swing", swing), ("impact", impact)):
        peaks = _local_peaks(signal, separation)
        if peaks.size >= 2:
            interval = _fold_harmonic(float(np.median(np.diff(peaks))), seed)
            if low <= interval <= high:
                estimates.append((name, interval))

    values = [value for _, value in estimates]
    ratio = max(values) / min(values)
    consistent = ratio < cfg.stance_period_consistency_max
    # 一致时取中位数（几个估计差不到 6%，取哪个都一样）；**不一致时退回自相关**，
    # 因为它是唯一不依赖峰选的估计 —— 用户选定的降级路径是"用最可信的单一特征并
    # 标注"，而不是把一个已知跑掉的估计拌进中位数里。实测 `shuffle`（拖步样步态，
    # 摆动峰本就微弱）正落在这一支：中位数会给出 259 而真值 400。
    period = float(np.median(values)) if consistent else seed
    # **这不再是最后一句话。** 网格铺完之后 `detect_stance` 会拿标出来的支撑相回头
    # 再问一次周期（`_refine_from_events`，RAY-339），而那一票通常会取代这里的取值。
    # 上面这条一致/不一致的分支因此只决定**第一遍**用什么，以及精修不采纳时保留什么。

    bounds = _lay_grid(swing, period, cfg)
    if bounds is None:
        return None
    head, tail = _truncation(swing.size, bounds, period)
    return PeriodReport(
        period_samples=period,
        cycles=len(bounds),
        estimates=tuple(estimates),
        ratio=ratio,
        consistent=consistent,
        bounds=bounds,
        head_truncated=head,
        tail_truncated=tail,
    )


def _truncation(
    samples: int, bounds: tuple[tuple[int, int], ...], period: float
) -> tuple[float, float]:
    """网格两头各剩多少数据，以周期计。

    这是个**观测**，不是修正：它只说"格子外面还有这么多"，不替调用方决定那些算不算
    一步。判据 4 的原话是"显式报出，不补进计数" —— 因为"首尾恰好各半步"是定长走廊
    协议才有的性质，自由行走没有，拿算法去补会在自由行走上反过来多数一个。
    """
    if not bounds or period <= 0.0:
        return 0.0, 0.0
    return bounds[0][0] / period, (samples - bounds[-1][1]) / period


def _lay_grid(
    swing: np.ndarray, period: float, cfg: AlgoConfig
) -> tuple[tuple[int, int], ...] | None:
    """周期定了"每格多宽"，这里定"格子从哪切"。铺不出合法网格时返回 None。

    **相位必须让边界落在摆动相里**：边界一旦切进支撑相，那一步就被劈给相邻两个周期，
    两边的 `argmin` 各自跑到隔壁步上去，那一步谁也不标 —— 实测就是这样漏掉的
    （边界 932/1154 分别落在支撑相 867–1001 与 1089–1223 里）。

    不能拿摆动峰当锚点。`‖ω‖` 的最大值出现在蹬离/触地，那是**紧挨着支撑相**的位置，
    不是摆动中段；按峰对齐恰好把边界推到支撑相边上。改为直接搜相位：在 `[0, period)`
    里取让**各边界处 `‖ω‖` 平均值最大**的那一个。这是把"边界要落在脚动得最快的地方"
    直接写成目标函数，不经过任何峰选。两类相位的得分差一个数量级，选起来没有悬念。

    网格的**范围**由摆动峰定：第一个峰到最后一个峰之间必定是走路，之前和之后可能是
    起步前的静立。范围放到整条记录上会把静立段也切成周期并在那里标零速 —— 脚确实是
    静的，不算误检，但那些跨度会混进步相事件里，把左右配对算坏（`generate_dual_walk`
    的 `still_lead_s` 就是这么暴露出来的）。

    抽成独立函数是因为 RAY-339 的事件域精修要**重铺一次**网格：换了周期就得重铺，
    而重铺必须与第一次走同一套规则，否则两次的边界含义不同，比较也就没有意义。
    """
    swing_peaks = _local_peaks(swing, max(1, round(0.5 * period)))
    if swing_peaks.size < 2:
        return None
    span_start, span_end = int(swing_peaks[0]), int(swing_peaks[-1])
    if span_end - span_start <= 0:
        return None

    # 必须把**整整一个周期**的相位都试到。按 `max(0, span_start - period)` 截断看着无害，
    # 实则当第一个峰离记录开头不足一个周期时会砍掉一整段相位，最优解恰好落在被砍掉的
    # 那段里时，选出来的网格是反相的 —— 每条边界都落进支撑相。相位是模周期的量，越界
    # 的起点往后挪一个周期即可，落在同一个相位上。
    span = round(period)
    best_score, best_edges = -1.0, None
    for offset in range(span):
        start = span_start - offset
        while start < 0:
            start += span
        count = int((span_end - start) // period)
        if count < cfg.stance_min_cycles:
            continue
        edges = start + np.round(np.arange(count + 1) * period).astype(int)
        score = float(swing[edges].mean())
        if score > best_score:
            best_score, best_edges = score, edges
    if best_edges is None:
        return None

    cycles = best_edges.size - 1
    bounds = tuple(
        (int(best_edges[index]), int(best_edges[index + 1]))
        for index in range(cycles)
        if best_edges[index + 1] > best_edges[index]
    )
    if len(bounds) < cfg.stance_min_cycles:
        return None
    return bounds


def _refine_from_events(
    swing: np.ndarray,
    stances: list[tuple[int, int]],
    period: PeriodReport,
    fs: float,
    cfg: AlgoConfig,
) -> PeriodReport | None:
    """网格铺完之后，拿标出来的支撑相**回头再问一次周期**。不该采纳时返回 None。

    ## 为什么这一步能赢过铺网格用的那个估计

    铺网格的三票（自相关、摆动峰、冲击峰）都是**整段**统计量，一趟里只要信号的节律或
    质量在变，它们就都被拉向那个变化的平均，而不是任何一处的真值。这里的量不一样：
    `_period_stance` 在每个格子里取 `argmin ‖ω‖`，那个位置是**数据定的**，不是格子定的，
    所以相邻两个标记之间的间隔量到的是真实的 stride，哪怕格子本身偏宽。

    实测（RAY-339，24 格真值受控已知）：网格 T 的 RMS **6.4%**、最差一格 **+23.9%**；
    事件域的 RMS **2.2%**、几乎无偏。采纳之后整条管线是 RMS **2.3%**、最差 **+4.6%**。

    **它必须被采纳，不能只是进池投票。** 实测把它当第四票喂进 `_estimate_period` 的
    估计池，RMS 只从 4.1% 动到 4.0% —— 中位数把它稀释掉了。

    ## 一个负结果：合成数据里找不到那个偏差

    最差那一格（`flat/slow-a/L`）的趟内步频漂移也是 24 格里最大的（后 1/3 的中位周期
    比前 1/3 短 15.1%），所以漂移显然有份。但**单靠漂移不足以造成它**：在合成行走上把
    步频拉快 35%，网格仍然只偏 1.9%，事件域给出的值与它逐比特相同。另一半来自真机上
    摆动峰本身的微弱与不规则，而合成器给出的峰干净得多。

    写在这里是为了让下一个人不必再去合成数据里找那个偏差 —— 找不到，而
    `tests/test_zupt.py::test_synthetic_drift_alone_does_not_move_the_estimate`
    把这条钉住了。本函数的精度结论只在真机验收里成立。

    ## 三道闸，都不是"准不准"

    1. **支持度**：像一个 stride 的间隔少于 `period_refine_min_intervals` 就不采纳。
       事件域的软肋是支撑相检出本身不规则（实测最差一格 37 个间隔里只有 11 个可用），
       而那时中位数已经不是中位数了。
    2. **谐波**：折回自相关那一支所在的基频。这就是"自相关退成谐波守卫"的意思 ——
       它不再决定周期是多少，只决定周期在**哪一个八度**上。
    3. **范围**：折完仍在 `stance_period_*` 之外就整个作废。

    三道闸拦的都是"这个估计没有资格参与"，没有一道在问"它准不准" —— 准不准正是它比
    网格强的地方，用一道闸去怀疑它等于把改进撤销。
    """
    starts = np.array([start for start, _ in stances], dtype=np.float64)
    if starts.size < cfg.period_refine_min_intervals + 1:
        return None
    intervals = np.diff(starts)
    if intervals.size == 0:
        return None
    seed = float(np.median(intervals))
    if seed <= 0.0:
        return None
    kept = intervals[
        (intervals > cfg.period_refine_low * seed)
        & (intervals < cfg.period_refine_high * seed)
    ]
    if kept.size < cfg.period_refine_min_intervals:
        return None

    # 谐波守卫：拿铺网格时那个估计当锚。它是整段统计量、可能偏，但**不会偏到另一个
    # 八度上**（`_autocorrelation_period` 偏向最小近极大滞后正是为了这条）。
    value = _fold_harmonic(float(np.median(kept)), period.period_samples)
    if not (cfg.stance_period_min_s * fs <= value <= cfg.stance_period_max_s * fs):
        return None
    bounds = _lay_grid(swing, value, cfg)
    if bounds is None:
        return None

    estimates = (*period.estimates, ("events", value))
    values = [item for _, item in estimates]
    ratio = max(values) / min(values)
    head, tail = _truncation(swing.size, bounds, value)
    return PeriodReport(
        period_samples=value,
        cycles=len(bounds),
        estimates=estimates,
        ratio=ratio,
        # 一致性闸从**开关**降级为**读数**。它原本决定"取中位数还是退回自相关"，而
        # 现在周期由事件域直接给出 —— 那个二选一没有了。它仍然报出来，因为"几个估计
        # 彼此差多少"是读报告的人要看的事实，只是它不再左右结果。
        consistent=ratio < cfg.stance_period_consistency_max,
        bounds=bounds,
        head_truncated=head,
        tail_truncated=tail,
    )


def _flat_foot_reference(unit_acc: np.ndarray, coarse: np.ndarray) -> np.ndarray | None:
    """静立姿态的参考方向：足底平放时的重力方向。

    取**粗筛通过处**的中位方向。那些样本按定义是"只受重力、不抖、不转"的窗口 ——
    实测粗筛自己放过 30~38%，作判据毫无区分力（那正是本 Issue 的根因之一），但作
    "哪些时刻的加计方向可以当重力用"的取样却恰到好处：它挡掉的正是线加速度大的
    摆动相，而加计方向只有在线加速度足够小时才代表重力方向。

    用中位数而不是均值：一个混进来的摆动相样本能把均值拽走，拽不动中位数。
    """
    if not coarse.any():
        return None
    reference = np.median(unit_acc[coarse], axis=0)
    norm = float(np.linalg.norm(reference))
    if norm <= 0.0:
        return None
    return reference / norm


def _period_stance(
    swing: np.ndarray,
    hard: np.ndarray,
    unit_acc: np.ndarray,
    reference: np.ndarray | None,
    period: PeriodReport,
    cfg: AlgoConfig,
) -> np.ndarray:
    """每个周期里标一个零速时刻：`argmin ‖ω‖`。**一周期一个，由构造保证。**

    没有阈值。三种信号的绝对幅值都随速度与鞋型变（实测周期内最低 `‖ω‖` 为
    1.08~11.59 °/s），所以任何固定阈值必然在某一档失配；而"周期内哪一点最低"
    不随速度变 —— 问题不在选哪个信号，在于"用阈值"这件事本身。

    取 `argmin ‖ω‖` 而不是姿态角最小点：姿态最平的那一刻实测 `‖ω‖` 已达 68~97 °/s
    （快速档），**脚是平的，但正在快速滚过去**。ZUPT 要的是速度为零，不是姿态回正。

    硬检测已经覆盖的周期不重复标记：那里的脚**真的**静止到过阈，是更强的证据。
    """
    soft = np.zeros_like(hard)
    half = cfg.soft_zupt_span_samples // 2
    limit = math.cos(math.radians(cfg.stance_attitude_tolerance_deg))
    for start, end in period.bounds:
        # 这个周期**只要有**硬检测就整格让开。周期路径是用来救"一个硬检测都没有"的
        # 周期的（实测快速档 87% 的周期就是这样），不是用来给已经检出的那一步加注的。
        #
        # 判据不能收窄成"最低点这一刻是否已被硬检测覆盖"。`‖ω‖` 的真实最低点常常落在
        # 硬检测跨度**边界外几个样本**——硬路径的粗筛用的是窗口统计量，它的边界系统性
        # 地向内缩（`test_events` 正是守着这条偏置）。于是每个周期都会在真支撑相紧邻处
        # 多标出一小截，把一个支撑相裂成两个跨度：实测 20 步变成 40 个跨度，左右配对
        # 因此全线算错。
        #
        # 边界现在落在摆动相里，一个支撑相不会被劈给两个周期，所以整格让开不会漏掉
        # 被劈开的那一步 —— 那正是当初收窄这条判据的理由，现在它不成立了。
        if hard[start:end].any():
            continue
        # 姿态校验（结构第 5 步）先**筛候选**，再在候选里取 `argmin`，而不是取全局
        # `argmin` 之后再否决它。周期数定死了"有几步"，姿态回答的是"这一步在哪"——
        # 它是个定位量，用作事后一票否决就浪费了：某一步被噪声搅乱时，全局最低点会
        # 跑到相邻摆动相的某个偶然安静样本上，否决掉它就等于连**这一步本来落在哪**
        # 一起丢掉。先筛后取则仍然把它定位回那一步里，只是置信度低。
        #
        # 整个周期无一刻脚是平的，才真的没有可信的零速时刻，此时才丢：漏检只损失
        # 一步，误检毁掉整条轨迹。
        candidates = np.arange(start, end)
        if reference is not None:
            candidates = candidates[unit_acc[candidates] @ reference >= limit]
        if candidates.size == 0:
            continue
        centre = int(candidates[np.argmin(swing[candidates])])
        # 展开的宽度由**数据**定，不由固定的 `span` 定。固定宽度在候选落到支撑相边缘时
        # 必然探进摆动相：实测就漏出去一个样本（真值支撑相始于 2600，标到了 2599）。
        #
        # 判据是"与最低点相比还分辨不出差别"——比最低点高出不到静止脚本身的噪声底
        # （`stance_still_reference_rad_s`）就还算同一个零速平台。这不是又一个绝对阈值：
        # 参照量是**这一周期自己测出的最低值**，随速度与鞋型一起漂。支撑相边缘 `‖ω‖`
        # 是成 rad/s 地往上窜的，展开在那里自己就停住了。
        level = float(swing[centre]) + cfg.stance_still_reference_rad_s
        head = centre
        while head > max(start, centre - half) and swing[head - 1] <= level:
            head -= 1
        tail = centre
        while tail + 1 < min(end, centre + half + 1) and swing[tail + 1] <= level:
            tail += 1
        window = np.arange(head, tail + 1)
        # 逐样本再验一次姿态：零速时刻本身通过校验，不代表它两侧都还在支撑相里。
        if reference is not None:
            window = window[unit_acc[window] @ reference >= limit]
        soft[window] = True
    return soft


def _period_confidence(
    swing: np.ndarray, soft: np.ndarray, period: PeriodReport, cfg: AlgoConfig
) -> np.ndarray:
    """由**实测的**周期内最低 `‖ω‖` 定权重，而不是二值接受/拒绝。

    现行做法是"不够静就整步丢掉"，实测后果是快速档丢掉全部（87% 漏检）。而最低
    `‖ω‖` 是连续量，天然适合做观测权重：2 °/s 的周期给得紧，11 °/s 的给得松。

    映射取 `ref / (ref + ω_min)`，在 ω_min = ref 处给半权、单调下降、恒为正。
    整体再压到 `≤ 0.25`：这些零速是"这一周期一定发生过一步"推出来的，不是测出来的，
    与既有软零速同一档。周期不一致时再折半 —— 那是降级里的降级。

    **这个数的绝对值没有校准过，它的序才有意义**（与 `_confidence` 同一条声明）。
    把它接到 ESKF 的观测协方差上是 RAY-204 的事，本模块只表达"多可信"。
    """
    confidence = np.zeros(swing.shape, dtype=np.float64)
    ceiling = 0.25 if period.consistent else 0.125
    reference = cfg.stance_still_reference_rad_s
    for start, end in period.bounds:
        marked = soft[start:end]
        if not marked.any():
            continue
        # 取**被标中的**样本里的最低值，而不是整个周期的最低值。姿态校验否决掉全局
        # 最低点时（那一刻脚不平，多半在摆动相），用它算权重就等于拿一个**没有采用的**
        # 时刻去给采用的时刻背书，报出来的置信度会高于实际依据。两者只在姿态否决时
        # 不同，而那恰恰是最该压低置信度的情形。
        minimum = float(swing[start:end][marked].min())
        confidence[start:end] = np.where(
            marked, ceiling * reference / (reference + minimum), 0.0
        )
    return confidence


def detect_stance(
    acc: np.ndarray,
    gyr: np.ndarray,
    fs: float,
    cfg: AlgoConfig | None = None,
    *,
    gravity: float = GRAVITY_STANDARD,
    period_prior_samples: float | None = None,
) -> StanceDetection:
    """检测零速区间。`acc` 是比力（m/s²），`gyr` 是角速度（rad/s），单个连续段。

    `cfg` 就是预设切换的接口 —— 换一个 `AlgoConfig` 即可，本模块**没有任何模块级
    状态**。`AlgoConfig.low_speed()` 与默认预设可以在同一个进程里对同一段数据反复
    交替调用而互不影响，测试直接断言这件事。

    `period_prior_samples` 是可选的外部周期先验（RAY-328 L1 的双脚互相关 T_x），
    以**样本**计。不传时本函数的行为与它存在之前逐比特相同 —— 这是刻意的：单脚路径
    仍然是完整可用的，双脚只是在有条件时多给一票。见 `_estimate_period`。
    """
    cfg = cfg or AlgoConfig()
    specific_force = np.asarray(acc, dtype=np.float64)
    omega = np.asarray(gyr, dtype=np.float64)
    for name, value in (("acc", specific_force), ("gyr", omega)):
        if value.ndim != 2 or value.shape[1] != 3:
            raise ZuptError(f"{name} 应为 (n, 3)，收到 shape={value.shape}")
    if specific_force.shape != omega.shape:
        raise ZuptError(
            f"acc 与 gyr 的样本数必须一致：{specific_force.shape} vs {omega.shape}"
        )
    n = specific_force.shape[0]
    window = cfg.zupt_window_samples
    if n < window:
        raise ZuptError(
            f"序列只有 {n} 个采样，短于检测窗口 {window}。"
            "空洞切分（RAY-210）可能切出这样的碎段；它们没有可判断的支撑相，"
            "调用方应当整段跳过而不是把窗口缩小 —— 缩小窗口会让判据的含义随段长变化。"
        )

    # 检测用信号：低通去冲击。**它不出这个函数。**
    #
    # 抽头长度钉在检测窗口上：滤波器把每个跳变向两侧各抹开 (taps-1)/2 个样本，
    # 而支撑相与摆动相的边界正是跳变。抹得比窗口还宽，窗口的含义就被先毁掉了。
    detection_acc = lowpass(
        specific_force, fs, cfg.detection_lowpass_hz, max_taps=window
    )

    acc_sum = window_sums(detection_acc, window)
    acc_square_sum = window_sums(np.sum(detection_acc**2, axis=1), window)[:, 0]
    gyr_sum = window_sums(omega, window)
    gyr_square_sum = window_sums(np.sum(omega**2, axis=1), window)[:, 0]

    acc_mean = acc_sum / window
    gyr_mean = gyr_sum / window
    acc_mean_norm = np.linalg.norm(acc_mean, axis=1)
    gyr_mean_norm = np.linalg.norm(gyr_mean, axis=1)
    # 三轴合计的方差：E[‖x‖²] - ‖E[x]‖²。逐轴求再相加是同一个数，这样少写一次循环。
    acc_variance = np.maximum(acc_square_sum / window - acc_mean_norm**2, 0.0)
    gyr_variance = np.maximum(gyr_square_sum / window - gyr_mean_norm**2, 0.0)

    coarse = (
        (np.abs(acc_mean_norm - gravity) < cfg.zupt_acc_threshold)
        & (acc_variance < cfg.zupt_acc_variance_threshold)
        & (gyr_mean_norm < cfg.zupt_gyr_threshold)
        & (gyr_variance < cfg.zupt_gyr_variance_threshold)
    )

    # GLRT。展开 Σ‖a_i - g·û‖²（û 是窗口均值方向）后只剩窗口和，不必回到逐样本：
    #     Σ‖a_i - g·û‖² = Σ‖a_i‖² - 2·g·W·‖ā‖ + W·g²
    # 因为 û·Σa_i 恰好是 ‖Σa_i‖。
    residual = (
        acc_square_sum - 2.0 * gravity * window * acc_mean_norm + window * gravity**2
    )
    # 浮点相减可能给出一个极小的负数，而它只表示"完全静止"。截到 0，好让 score 保持
    # "越小越静止、下界为 0"这个可以被下游依赖的性质。
    score = np.maximum(
        (residual / cfg.zupt_sigma_acc**2 + gyr_square_sum / cfg.zupt_sigma_gyr**2)
        / window,
        0.0,
    )

    hard = coarse & (score < cfg.zupt_glrt_threshold)
    hard = _drop_short_runs(hard, cfg.min_stance_samples)

    # ZARU：只看角速度。足部有平动时它仍然成立，比零速鲁棒（整体设计 §5.6.2）。
    zaru = (gyr_mean_norm < cfg.zupt_gyr_threshold) & (
        gyr_variance < cfg.zupt_gyr_variance_threshold
    )
    if not cfg.force_zaru:
        # 默认预设下只在已判定的支撑相内使用 ZARU：它此时是零速观测的补充，
        # 而不是独立的约束来源。低速/病理预设（PRD §7）才让它自己站住。
        zaru = zaru & hard

    # 周期分段。摆动峰用**逐样本**的 ‖ω‖ 而不是窗口均值：摆动峰是 200~600 °/s 的
    # 尖锐事件，滑窗平均会把它削矮并向两侧抹开，而这里要的正是它的位置。
    swing = np.linalg.norm(omega, axis=1)
    impact = np.abs(np.linalg.norm(detection_acc, axis=1) - gravity)
    period = _estimate_period(swing, impact, fs, cfg, period_prior_samples)

    if period is None:
        # 没有可辨认的步态：静立、足部被固定，或段太短。阈值判据在这里是对的 ——
        # 真正静止的脚**确实**满足 GLRT，失效的只是"走路时也要求它"。
        degraded = _soft_stance(score, hard, cfg)
        zupt = hard | degraded
        confidence = np.where(zupt, _confidence(score, cfg.zupt_glrt_threshold), 0.0)
        confidence = np.where(degraded, np.minimum(confidence, 0.25), confidence)
    else:
        unit_acc = acc_mean / np.maximum(acc_mean_norm[:, None], 1e-9)
        reference = _flat_foot_reference(unit_acc, coarse)
        degraded = _period_stance(swing, hard, unit_acc, reference, period, cfg)
        zupt = hard | degraded

        # 精修**恰好一次**，不迭代到收敛。第二遍的标记会给出又一个事件域估计，第三遍
        # 再给一个 —— 但那之后的每一步都在拿自己的输出喂自己，收敛到哪里由初值决定，
        # 而不是由数据决定。一次精修是"用数据定的位置回头修一次格子宽度"，再多就是
        # 让格子和标记互相说服。
        refined = _refine_from_events(swing, runs(zupt), period, fs, cfg)
        if refined is not None:
            period = refined
            degraded = _period_stance(swing, hard, unit_acc, reference, period, cfg)
            zupt = hard | degraded

        confidence = np.where(hard, _confidence(score, cfg.zupt_glrt_threshold), 0.0)
        # 周期零速的置信度来自**实测的**周期内最低 ‖ω‖，不来自 GLRT：GLRT 在这些
        # 样本上按定义是超阈的（那正是本 Issue 的根因），拿它算出来的置信度恒为 0，
        # 而一个恒为 0 的置信度会让下游把每一步都当成不可信。
        confidence = np.where(
            degraded, _period_confidence(swing, degraded, period, cfg), confidence
        )

    return StanceDetection(
        zupt=zupt,
        zaru=zaru,
        degraded=degraded,
        stances=runs(zupt),
        score=score,
        confidence=confidence,
        period=period,
    )


def _drop_short_runs(mask: np.ndarray, minimum: int) -> np.ndarray:
    """丢掉短于 `minimum` 的连续 True 段。

    走路支撑相约 600–800 ms（整体设计 §5.5.3），50 ms 的碎片只可能是噪声或摆动相里
    的瞬时巧合。而一个碎片注入 ESKF 的是一次**错误的**零速观测 —— 见模块文档里那条
    不对称：误检毁轨迹，漏检只损失一步。
    """
    cleaned = mask.copy()
    for start, end in runs(mask):
        if end - start < minimum:
            cleaned[start:end] = False
    return cleaned


def _soft_stance(score: np.ndarray, hard: np.ndarray, cfg: AlgoConfig) -> np.ndarray:
    """在硬检测的空档里补软零速。整体设计 §5.5.3 的降级策略。

    只在**间隔过长**处补：间隔超过 `soft_zupt_gap_samples` 意味着这段时间里一定发生
    过至少一步，而一步一定有支撑相。补的位置是该间隔内统计量最小的样本 —— 也就是
    "最像静止的那一刻"，对应设计文档说的"支撑相中速度模值最小的那一刻"。

    序列首尾的空档**不补**。开头与结尾的截断只说明记录在这里停了，不说明这中间有
    一步没检到；在那里硬塞一个零速观测是凭空发明数据。
    """
    soft = np.zeros_like(hard)
    hard_runs = runs(hard)
    if len(hard_runs) < 2:
        # 一个硬支撑相都没有，或只有一个：没有"两次之间"，也就无从判断中间漏了几步。
        # 这种情形应当由上层当作检测失败处理，而不是靠软零速把它填成看起来正常。
        return soft

    half = cfg.soft_zupt_span_samples // 2
    for (_, gap_start), (gap_end, _) in pairwise(hard_runs):
        if gap_end - gap_start <= cfg.soft_zupt_gap_samples:
            continue
        # 搜索范围从空档两端各让开一个检测窗口。
        #
        # 不让开的话，argmin 多半落在紧挨着上一个支撑相的位置：那里的足部**确实**还
        # 静止着（检出边界是被滑窗与低通向内削出来的，见 detect_stance 的保守性），
        # 统计量自然最低。但在那里补一个零速观测几乎是重复已有的约束，而真正缺观测
        # 的是空档中段 —— 也就是那一步实际发生的地方。
        margin = cfg.zupt_window_samples
        search_start = gap_start + margin
        search_end = gap_end - margin
        if search_end - search_start < cfg.soft_zupt_span_samples:
            continue
        centre = search_start + int(np.argmin(score[search_start:search_end]))
        soft[max(search_start, centre - half) : min(search_end, centre + half + 1)] = (
            True
        )
    return soft
