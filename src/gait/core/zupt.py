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

    @property
    def hard(self) -> np.ndarray:
        """硬检测得到的零速。`zupt & ~degraded`。"""
        return self.zupt & ~self.degraded


def lowpass(x: np.ndarray, fs: float, cutoff_hz: float, *, max_taps: int = _MAX_TAPS) -> np.ndarray:
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


def _window_sums(x: np.ndarray, window: int) -> np.ndarray:
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


def _runs(mask: np.ndarray) -> list[tuple[int, int]]:
    """布尔掩码里的连续 True 区间，半开。"""
    if not mask.any():
        return []
    padded = np.concatenate(([False], mask, [False]))
    edges = np.flatnonzero(padded[1:] != padded[:-1])
    return [(int(start), int(end)) for start, end in zip(edges[::2], edges[1::2], strict=True)]


def _confidence(score: np.ndarray, threshold: float) -> np.ndarray:
    """GLRT 统计量 → 0~1 的置信度。

    映射取 `1 - T/γ5`，在判据边界处为 0、在完全静止处趋近 1。

    **这个数的绝对值没有校准过，它的序才有意义。** 说清楚是因为它会以
    `zupt_quality` 的名义出现在报告里（RAY-218），而一个看起来像概率的数很容易被
    当成概率。真正需要概率解释时要做的是在真机数据上拟合，而不是换一个更像概率的
    公式 —— 后者只会让它更容易被误信。
    """
    return np.clip(1.0 - score / threshold, 0.0, 1.0)


def detect_stance(
    acc: np.ndarray,
    gyr: np.ndarray,
    fs: float,
    cfg: AlgoConfig | None = None,
    *,
    gravity: float = GRAVITY_STANDARD,
) -> StanceDetection:
    """检测零速区间。`acc` 是比力（m/s²），`gyr` 是角速度（rad/s），单个连续段。

    `cfg` 就是预设切换的接口 —— 换一个 `AlgoConfig` 即可，本模块**没有任何模块级
    状态**。`AlgoConfig.low_speed()` 与默认预设可以在同一个进程里对同一段数据反复
    交替调用而互不影响，测试直接断言这件事。
    """
    cfg = cfg or AlgoConfig()
    specific_force = np.asarray(acc, dtype=np.float64)
    omega = np.asarray(gyr, dtype=np.float64)
    for name, value in (("acc", specific_force), ("gyr", omega)):
        if value.ndim != 2 or value.shape[1] != 3:
            raise ZuptError(f"{name} 应为 (n, 3)，收到 shape={value.shape}")
    if specific_force.shape != omega.shape:
        raise ZuptError(f"acc 与 gyr 的样本数必须一致：{specific_force.shape} vs {omega.shape}")
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

    acc_sum = _window_sums(detection_acc, window)
    acc_square_sum = _window_sums(np.sum(detection_acc**2, axis=1), window)[:, 0]
    gyr_sum = _window_sums(omega, window)
    gyr_square_sum = _window_sums(np.sum(omega**2, axis=1), window)[:, 0]

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
    residual = acc_square_sum - 2.0 * gravity * window * acc_mean_norm + window * gravity**2
    # 浮点相减可能给出一个极小的负数，而它只表示"完全静止"。截到 0，好让 score 保持
    # "越小越静止、下界为 0"这个可以被下游依赖的性质。
    score = np.maximum(
        (residual / cfg.zupt_sigma_acc**2 + gyr_square_sum / cfg.zupt_sigma_gyr**2) / window,
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

    degraded = _soft_stance(score, hard, cfg)
    zupt = hard | degraded

    confidence = np.where(zupt, _confidence(score, cfg.zupt_glrt_threshold), 0.0)
    # 降级样本的置信度另外压一档：它们是"这一步一定发生了"推出来的，不是测出来的。
    # 压制系数与 ESKF 放大 R 的倍数无关 —— 那是滤波器的事，这里只表达"更不可信"。
    confidence = np.where(degraded, np.minimum(confidence, 0.25), confidence)

    return StanceDetection(
        zupt=zupt,
        zaru=zaru,
        degraded=degraded,
        stances=_runs(zupt),
        score=score,
        confidence=confidence,
    )


def _drop_short_runs(mask: np.ndarray, minimum: int) -> np.ndarray:
    """丢掉短于 `minimum` 的连续 True 段。

    走路支撑相约 600–800 ms（整体设计 §5.5.3），50 ms 的碎片只可能是噪声或摆动相里
    的瞬时巧合。而一个碎片注入 ESKF 的是一次**错误的**零速观测 —— 见模块文档里那条
    不对称：误检毁轨迹，漏检只损失一步。
    """
    cleaned = mask.copy()
    for start, end in _runs(mask):
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
    runs = _runs(hard)
    if len(runs) < 2:
        # 一个硬支撑相都没有，或只有一个：没有"两次之间"，也就无从判断中间漏了几步。
        # 这种情形应当由上层当作检测失败处理，而不是靠软零速把它填成看起来正常。
        return soft

    half = cfg.soft_zupt_span_samples // 2
    for (_, gap_start), (gap_end, _) in pairwise(runs):
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
        soft[max(search_start, centre - half) : min(search_end, centre + half + 1)] = True
    return soft
