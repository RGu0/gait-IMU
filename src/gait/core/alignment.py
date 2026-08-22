"""初始对准。契约 §1 的 `core/alignment.py`（F4.5）。整体设计 §5.3。

## 它解决的是一个只做一次、却影响全程的问题

惯导积分从一个初始姿态出发。这个姿态错 1°，重力就有 `g·sin(1°) ≈ 0.17 m/s²` 分量落进
水平方向，被当作真实加速度积分两次 —— 1 秒后是 8.5 cm 的位置误差。ZUPT 会在每个支撑相
把速度拉回零，所以误差不会无限累积，但**每一个摆动相内的轨迹形态都被这个初值歪着**。

因此验收标准是 0.5°，而不是"看起来对"。

## 与整体设计 §5.3 的公式不同，这是有意的

§5.3 写的是：

    roll  = arctan2(-ā_y, -ā_z)
    pitch = arctan2(ā_x, sqrt(ā_y² + ā_z²))

那两行隐含"加速度计读数指向重力方向"的符号约定。本仓库采用的是**比力**约定：静止时
模块读 `(0, 0, +g)` 而不是 `(0, 0, -g)`（见 `core/ins.py` 的模块文档）。照抄会得到一个
**相差 180°** 的对准 —— 而 180° 的姿态误差不会让程序报错，只会让轨迹整个翻过来。

这条出入在 RAY-201 交付时就已登记，此处重新推导：

设 `û` 是静止段平均比力的单位向量（足部系）。要找的姿态 `q` 满足 `rotate(q, û) = ẑ`，
且 yaw = 0。在内旋 ZYX 约定下 yaw = 0 时 `R = Ry(pitch)·Rx(roll)`，于是

    û = Rᵀ·ẑ = (-sin(pitch), sin(roll)·cos(pitch), cos(roll)·cos(pitch))

反解：

    pitch = arcsin(-û_x)
    roll  = arctan2(û_y, û_z)

单位姿态代入得 `û = (0, 0, 1)`、`roll = pitch = 0`，与"模块平放"一致 —— §5.3 的公式
代入同一个 `û` 会给出 `roll = π`。

## yaw = 0 是产品边界，不是缺省值

6 轴配置下航向**无法绝对确定**，这是物理限制而不是算法缺陷：重力只约束两个自由度。
本硬件在 200 Hz 下磁力计不可读（选型对比 v0.2 §3.2），所以连 9 轴的旁路也没有。

处理方式是把所有输出定义在**会话坐标系**：以起步方向为 x 轴的相对系。对步长、步速、
轨迹形态的分析完全够用，但"朝向正北"这类问题答不了。`HEADING_REFERENCE` 是这件事的
可断言声明，报告层要把它印出来（PRD §12）。

## 不做的事

会话级标定的其余部分 —— 零偏刷新、坐标系重排、安装误差角、戴反与松动检测 —— 属
RAY-208 的 `calib/` 层。本模块只回答一个问题：给定一段已知静止的样本，初始姿态是多少。

陀螺零偏也不在这里估。它确实可以由同一段静止样本的均值给出，但它属于**标定参数**，
要随 `calib_snapshot` 存档并跨会话复用；姿态则是每次会话重算的。混在一个函数里返回，
两个生命周期不同的东西会被同一个调用点一起处理掉。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

import numpy as np

from gait.config import AlgoConfig
from gait.core import quaternion as quat
from gait.core.ins import GRAVITY_STANDARD
from gait.core.zupt import detect_stance

#: 航向基准的可断言声明。见模块文档"yaw = 0 是产品边界"。
HEADING_REFERENCE: Final[str] = "session_relative_yaw_zero"

#: 静止段平均比力的模值允许偏离重力多少（相对值）。超过就拒绝对准。
#:
#: 10% 是一个**很宽**的门：静止模块的偏差只来自零偏与噪声，30 mg 的加计零偏也才
#: 0.3%。它挡的不是"标定得准不准"，而是"这段根本不是静止段" —— 那种情况下解出来的
#: 角度毫无意义，而它会被后续每一步默默使用。
MAX_GRAVITY_MISMATCH: Final[float] = 0.10


class AlignmentError(ValueError):
    """初始对准失败。"""


@dataclass(frozen=True)
class Alignment:
    """一次初始对准的结果与它的可信程度。

    质量指标与姿态一起返回，而不是让调用方事后自己算：对准是"只做一次、影响全程"的
    动作，一个没有质量指标的对准结果无法被质量标注（RAY-218）使用，也就无法在报告里
    说明这次会话的轨迹有多可信。
    """

    #: (4,) 足部系 → 导航系，yaw = 0。
    q: np.ndarray
    roll: float
    pitch: float
    #: 用到的样本数与它们在原序列中的位置。
    samples: int
    window: tuple[int, int]
    #: `abs(‖f̄‖ - g)`，m/s²。反映零偏与"这段是否真的静止"。
    gravity_residual: float
    #: 倾角的 1σ 不确定度，rad。由窗口内比力的横向抖动推出，见 `align_to_gravity`。
    tilt_sigma: float

    @property
    def heading_reference(self) -> str:
        return HEADING_REFERENCE


def align_to_gravity(
    acc: np.ndarray,
    *,
    gravity: float = GRAVITY_STANDARD,
    window: tuple[int, int] = (0, 0),
    max_mismatch: float = MAX_GRAVITY_MISMATCH,
) -> Alignment:
    """由一段**已知静止**的比力样本解出初始姿态。

    `acc` 是足部系比力，(n, 3)，m/s²。静止判定不在这里做 —— 传进来的样本必须已经
    被判定为静止（`initial_alignment` 负责选窗口）。

    ## 为什么先平均再归一化，而不是逐样本解角再平均

    逐样本解角在数值上等价，但角度的平均在跨越 ±180° 时会崩（arctan2 的值域边界）。
    比力是向量，向量平均没有这个问题。窗口内的噪声也因此按 1/√n 压下去，而角度平均
    压不下同样多。

    ## `tilt_sigma`

    倾角误差主要来自比力横向分量的噪声：`δθ ≈ δa_horizontal / g`。用窗口内比力去掉
    均值后的横向散布除以 `g·√n` 估计它。这是一个**量级估计**，不是严格的置信区间 ——
    它假设噪声白且各向同性，而真实的零偏是常值、不随 n 变小。所以它回答的是"这段样本
    够不够长"，不是"对准有多准"。
    """
    samples = np.asarray(acc, dtype=np.float64)
    if samples.ndim != 2 or samples.shape[1] != 3:
        raise AlignmentError(f"acc 应为 (n, 3)，收到 shape={samples.shape}")
    if samples.shape[0] == 0:
        raise AlignmentError("空窗口无法对准")

    mean = samples.mean(axis=0)
    magnitude = float(np.linalg.norm(mean))
    residual = abs(magnitude - gravity)
    if magnitude <= 0 or residual > max_mismatch * gravity:
        raise AlignmentError(
            f"静止段的平均比力模值是 {magnitude:.4f} m/s²，与重力 {gravity:.4f} 相差 "
            f"{residual:.4f}（超过 {max_mismatch:.0%}）。这一段多半根本不是静止段 —— "
            "在它上面解出的角度毫无意义，而后续每一步都会默默使用那个角度。"
        )

    unit = mean / magnitude
    # 见模块文档的推导。**不要**照抄整体设计 §5.3 的那两行：它们的符号约定与本仓库
    # 的比力约定相反，照抄会得到相差 180° 的对准，而 180° 不会报错。
    pitch = float(np.arcsin(np.clip(-unit[0], -1.0, 1.0)))
    roll = float(np.arctan2(unit[1], unit[2]))
    attitude = quat.from_euler(roll, pitch, 0.0)

    horizontal = samples - mean
    tilt_sigma = float(
        np.linalg.norm(horizontal.std(axis=0)[:2]) / (gravity * np.sqrt(len(samples)))
    )
    return Alignment(
        q=attitude,
        roll=roll,
        pitch=pitch,
        samples=len(samples),
        window=window,
        gravity_residual=residual,
        tilt_sigma=tilt_sigma,
    )


def find_still_window(
    acc: np.ndarray,
    gyr: np.ndarray,
    fs: float,
    cfg: AlgoConfig | None = None,
    *,
    minimum_seconds: float = 0.5,
) -> tuple[int, int]:
    """挑出用于对准的静止窗口：序列**开头**的第一段静止。

    用 `core/zupt.py` 的检测器而不是另写一套静止判据。同一件事有两处判据时，它们迟早
    对不上，而对不上的表现是"对准用了一段检测器认为在动的样本"—— 没人会去查这件事。

    取**开头第一段**而不是最长的一段：PRD §7 的流程是静立 5 s 之后开始走，那一段就是
    为对准准备的。取最长的一段会在受试者中途长时间站立时选到它，而那时模块可能已经
    因为走动而相对足部松动了（松动检测属 RAY-208，但选窗口时不该主动往坑里走）。
    """
    detection = detect_stance(acc, gyr, fs, cfg)
    minimum = round(minimum_seconds * fs)
    for start, end in detection.stances:
        if end - start >= minimum:
            return start, end
    raise AlignmentError(
        f"序列开头找不到长度 ≥ {minimum} 样本（{minimum_seconds} s）的静止段。"
        "PRD §7 的流程要求静立后开始；没有静止段就没有可信的初始姿态，"
        "此时应当提示重新开始而不是硬着头皮对准。"
    )


def initial_alignment(
    acc: np.ndarray,
    gyr: np.ndarray,
    fs: float,
    cfg: AlgoConfig | None = None,
    *,
    gravity: float = GRAVITY_STANDARD,
    minimum_seconds: float = 0.5,
) -> Alignment:
    """选窗口 + 对准，一次调用。`acc`/`gyr` 是单个连续段。"""
    start, end = find_still_window(acc, gyr, fs, cfg, minimum_seconds=minimum_seconds)
    return align_to_gravity(
        np.asarray(acc, dtype=np.float64)[start:end], gravity=gravity, window=(start, end)
    )
