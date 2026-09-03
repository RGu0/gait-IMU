"""静立 5 s 的会话标定：陀螺零偏、标定基准、松动检测。

## 它与 `core/alignment.py` 的分工

RAY-202 已经交付了重力对准（`align_to_gravity` / `find_still_window` /
`initial_alignment`），本模块**不重做**它，而是调用它。

`Alignment` 给的是「这一刻的姿态与它有多可信」；本模块要的是另外两样：

1. **陀螺零偏** —— `Alignment` 里没有这个字段，因为对准只看比力；
2. **一份能存下来、事后拿来对比的基准** —— 松动检测的定义就是「现在的重力方向
   相对**标定时**的重力方向变了多少」。没有基准，这句话无从谈起。

## 松动检测为什么比较重力方向而不是姿态角

姿态角（roll/pitch）在接近 ±90° 时对同样大小的方向变化会给出很不一样的角度差
（万向节附近的坐标奇异）。而**两个单位向量之间的夹角没有这个问题**，它在整个球面
上是均匀的。判据说的是「重力方向突变 > 5°」，那就直接量方向之间的夹角。

## 为什么这里**没有**零偏的合理性判据

第一版写了一个 `MAX_GYRO_BIAS_RAD_S` 上限，超过就判不通过。写完去触发它，才发现
它够不着：`find_still_window` 用 `core/zupt.detect_stance` 选窗口，而那个检测器在
**每轴零偏约 0.02 rad/s**（合成数据实测）就已经判「这段不静止」并抛错 —— 比任何我会
设的上限都严得多。

也就是说那条检查是一段死代码，而且是**看起来在保护什么**的死代码。更要紧的是它违反
了 `find_still_window` 自己文档里的那句话：

> 用 `core/zupt.py` 的检测器而不是另写一套静止判据。同一件事有两处判据时，它们
> 迟早对不上。

「零偏大不大」与「这段静不静止」在静立标定里是同一个问题的两种问法。所以判定归
`detect_stance`，本模块只**测量并报告**零偏 —— 它要进 `calib_snapshot`，也要被后续
的 ESKF 用掉，但它不是一道闸。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

import numpy as np

from gait.config import AlgoConfig
from gait.core.alignment import Alignment, initial_alignment

#: 松动判据：静止段重力方向相对标定值的夹角超过它即阻断（PRD §6.1）。
LOOSENESS_LIMIT_DEG: Final[float] = 5.0

#: 静立段要求的最短时长。PRD 写的是 5 s；短于它的样本不足以把噪声压下去。
MIN_STILL_SECONDS: Final[float] = 5.0

class CalibrationError(ValueError):
    """标定无从进行 —— 样本不够、形状不对。"""


@dataclass(frozen=True, slots=True)
class StillCalibration:
    """一次静立标定的结果。**它会被存下来，供后续会话比对。**

    存的是**方向向量**而不是 roll/pitch：松动检测要量的是两个方向之间的夹角，
    而角度在坐标奇异附近不能直接相减（见模块文档）。
    """

    foot: str
    #: 标定时刻的重力方向（单位向量，足部系）。松动检测的基准。
    gravity_direction: np.ndarray
    #: 陀螺零偏，rad/s。
    gyro_bias: np.ndarray
    #: 用到的样本数与时长。
    samples: int
    seconds: float
    #: 复用 RAY-202 的对准结果，姿态与它的质量指标都在里面。
    alignment: Alignment

    @property
    def gyro_bias_magnitude(self) -> float:
        return float(np.linalg.norm(self.gyro_bias))

    def snapshot(self) -> dict[str, Any]:
        """进 `SessionMeta.calib_snapshot`（PRD §6.1 强制字段）。"""
        return {
            "foot": self.foot,
            "gravity_direction": [float(v) for v in self.gravity_direction],
            "gyro_bias": [float(v) for v in self.gyro_bias],
            "gyro_bias_magnitude": self.gyro_bias_magnitude,
            "samples": self.samples,
            "seconds": self.seconds,
            "roll": self.alignment.roll,
            "pitch": self.alignment.pitch,
            "gravity_residual": self.alignment.gravity_residual,
            "tilt_sigma": self.alignment.tilt_sigma,
        }


def _unit(vector: np.ndarray) -> np.ndarray:
    norm = float(np.linalg.norm(vector))
    if norm <= 0:
        raise CalibrationError("重力方向的模为零 —— 这段样本不是静止的比力")
    return np.asarray(vector, dtype=np.float64) / norm


def calibrate_still(
    foot: str,
    acc: np.ndarray,
    gyr: np.ndarray,
    fs: float,
    cfg: AlgoConfig | None = None,
) -> StillCalibration:
    """从一段静立样本解出标定基准与陀螺零偏。

    静止窗口的选取交给 RAY-202 的 `initial_alignment` —— 它已经拿 `AlgoConfig` 的
    零速判据做这件事，这里再写一遍就会有两套「什么算静止」，而两套迟早对不上。
    """
    acc = np.asarray(acc, dtype=np.float64)
    gyr = np.asarray(gyr, dtype=np.float64)
    if acc.ndim != 2 or acc.shape[1] != 3 or acc.shape != gyr.shape:
        raise CalibrationError(f"acc/gyr 应为等长的 (n,3)，收到 {acc.shape} 与 {gyr.shape}")
    if fs <= 0:
        raise CalibrationError(f"采样率必须为正，收到 {fs}")

    seconds = acc.shape[0] / fs
    if seconds < MIN_STILL_SECONDS:
        raise CalibrationError(
            f"静立段只有 {seconds:.1f} s，不足 {MIN_STILL_SECONDS:.0f} s。"
            "样本不够时噪声压不下去，标定出来的零偏会把噪声当成偏置带进整场会话。"
        )

    alignment = initial_alignment(acc, gyr, fs, cfg, minimum_seconds=MIN_STILL_SECONDS)
    start, end = alignment.window
    window_acc = acc[start:end]
    window_gyr = gyr[start:end]

    return StillCalibration(
        foot=foot,
        gravity_direction=_unit(window_acc.mean(axis=0)),
        # 静止时陀螺的输出就是零偏本身 —— 没有真实角速度混在里面。
        gyro_bias=window_gyr.mean(axis=0),
        samples=int(window_acc.shape[0]),
        seconds=float(window_acc.shape[0] / fs),
        alignment=alignment,
    )


@dataclass(frozen=True, slots=True)
class LoosenessCheck:
    """一次松动检查的结论。

    `deviation_deg` 无论过不过都带出来 —— 一个「通过了但偏了 4.8°」的会话与一个
    「通过了且偏了 0.2°」的会话不一样，而两者在只有布尔的世界里长得一模一样。
    """

    foot: str
    deviation_deg: float
    limit_deg: float
    loose: bool

    @property
    def message(self) -> str:
        """给操作员的一句话。**动作语言，不提算法**（PRD §6.1 / UI 设计 §7）。"""
        side = "左脚" if self.foot == "L" else "右脚"
        if self.loose:
            return f"{side}的模块有些松动，请绑紧后重试。"
        return f"{side}佩戴稳定。"

    def snapshot(self) -> dict[str, Any]:
        return {
            "foot": self.foot,
            "deviation_deg": self.deviation_deg,
            "limit_deg": self.limit_deg,
            "loose": self.loose,
        }


def check_looseness(
    reference: StillCalibration,
    acc: np.ndarray,
    *,
    limit_deg: float = LOOSENESS_LIMIT_DEG,
) -> LoosenessCheck:
    """把当前静止段的重力方向与标定基准比一比。

    `acc` 是**当前**这一段已知静止的比力。方向之间的夹角用点积求，因为它在整个球面
    上均匀 —— 直接相减 roll/pitch 会在坐标奇异附近给出错误的差值（见模块文档）。
    """
    current = _unit(np.asarray(acc, dtype=np.float64).mean(axis=0))
    cosine = float(np.clip(np.dot(current, reference.gravity_direction), -1.0, 1.0))
    deviation = float(np.degrees(np.arccos(cosine)))
    return LoosenessCheck(
        foot=reference.foot,
        deviation_deg=deviation,
        limit_deg=limit_deg,
        loose=deviation > limit_deg,
    )


@dataclass(frozen=True, slots=True)
class CalibrationVerdict:
    """标定这一步过不过，以及为什么。

    `reasons` 用**动作语言**，供 P-07 直接显示（PRD §6.1：失败提示用动作语言）。
    """

    passed: bool
    reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.passed and self.reasons:
            raise CalibrationError("通过时不应带 reasons —— 那会让调用方两头猜")

    def snapshot(self) -> dict[str, Any]:
        return {"passed": self.passed, "reasons": list(self.reasons)}


def verdict(
    calibrations: dict[str, StillCalibration],
    *,
    looseness: dict[str, LoosenessCheck] | None = None,
) -> CalibrationVerdict:
    """双足静立标定的通过判定。

    两只脚都必须有 —— 少一只不是「那只脚没问题」，是这次标定没覆盖它。这条与
    `device/orchestration.preflight_battery` 的口径一致，不另立一套。
    """
    missing = sorted({"L", "R"} - set(calibrations))
    if missing:
        raise CalibrationError(
            f"缺少这些脚的标定：{missing}。少一只不是「那只脚没问题」，"
            "是这次标定没覆盖它。"
        )

    reasons: list[str] = []
    for label in ("L", "R"):
        # 零偏不在这里判 —— 「这段静不静止」已经由 detect_stance 在选窗口时回答过，
        # 而它比任何零偏上限都严（见模块文档）。
        check = (looseness or {}).get(label)
        if check is not None and check.loose:
            reasons.append(check.message)

    return CalibrationVerdict(passed=not reasons, reasons=tuple(reasons))
