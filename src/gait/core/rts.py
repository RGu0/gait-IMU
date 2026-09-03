"""RTS 后向平滑。整体设计 §5.8 第 1 条，PRD §6.1 云端完整报告。

## 为什么误差状态的 RTS 与教科书形式不同

教科书的 RTS 递推是

    x̂_{k|N} = x̂_{k|k} + C_k (x̂_{k+1|N} − x̂_{k+1|k})
    C_k      = P_{k|k} Φ_{k+1}ᵀ (P_{k+1|k})⁻¹

它假定滤波器把**状态本身**存了下来。ESKF 没有：每一步更新之后误差被注入名义状态并
**清零**，于是任何时刻的"滤波后误差"恒等于 0，直接套上式会得到 `δ ≡ 0` —— 平滑器
什么也不做，而且不报错。

正确的做法是认识到：清零不是信息消失，而是信息**搬进了名义轨迹**。样本 k+1 上注入的
修正量 `d_{k+1}` 正是"名义轨迹在这一步被移动了多少"。以更新前的名义为参考系，
`x̂_{k+1|N} − x̂_{k+1|k}` 在误差坐标下就是 `d_{k+1} + δ_{k+1|N}`。代回去，且
`δ_{k|k} ≡ 0`：

    δ_{k|N} = C_k (d_{k+1} + δ_{k+1|N})

递推从段末的 `δ_{N|N} = 0` 起算 —— 最后一个样本没有"未来"，平滑值等于滤波值。

这就是本模块存在的全部技术内容。它很短，但那个 `+ d_{k+1}` 是整件事的关键：漏掉它
平滑器会静默地退化成恒等变换，而"轨迹没变"看起来跟"轨迹本来就很准"一模一样。
`test_dropping_the_injected_correction_makes_the_smoother_a_no_op` 把这件事钉住。

## 注入方向

姿态修正右乘 `q ⊗ exp(δθ)`，与 `eskf._update` 的注入、与
`quaternion.integrate_angular_rate` 一致。误差是**局部（足部系）**表述，左乘会把修正
加到相反的方向上 —— 而那不报错，只是让结果变差。

## 段边界

逐段平滑，段与段之间不传递。理由与 `eskf.run_ins` 不跨段积分相同：空洞跨越的时间未知，
两侧状态之间没有可信的动力学联系，而 RTS 的整个推导建立在"存在一个已知的 Φ 把两个
时刻联系起来"之上。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

import numpy as np

from gait.contracts import NavResult
from gait.core import quaternion as quat
from gait.core.eskf import (
    ACCEL_BIAS,
    GYRO_BIAS,
    POSITION,
    STATE_DIM,
    THETA,
    VELOCITY,
    FilterHistory,
)

#: 平滑增益求解失败时的对角抖动，相对于 P 的迹。见 `_smoother_gain`。
_JITTER_SCALE: Final[float] = 1e-12


class SmoothError(ValueError):
    """平滑输入非法。"""


@dataclass(frozen=True)
class SmoothReport:
    """平滑改动了多少。用于质量标注与"平滑是否真的发生了"的自检。

    `max_position_shift` 为零意味着平滑器没有起作用 —— 那要么是数据本身没有可平滑的
    余量（极短会话），要么是接错了线。两种都值得看见，所以它进报告而不是只进日志。
    """

    #: 平滑前后位置差的最大模长，m。
    max_position_shift: float
    #: 平滑前后位置差的中位模长，m。
    median_position_shift: float
    #: 平滑前后姿态差的最大角度，deg。
    max_attitude_shift_deg: float
    #: 航向（偏航）修正的最大绝对值，deg。低速档的误差主要在这一项上。
    max_yaw_shift_deg: float
    #: 求解平滑增益时退化到抖动正则的样本数。非零说明协方差接近奇异。
    regularized_steps: int
    #: 参与平滑的样本数。
    samples: int

    def snapshot(self) -> dict[str, float | int]:
        return {
            "max_position_shift": self.max_position_shift,
            "median_position_shift": self.median_position_shift,
            "max_attitude_shift_deg": self.max_attitude_shift_deg,
            "max_yaw_shift_deg": self.max_yaw_shift_deg,
            "regularized_steps": self.regularized_steps,
            "samples": self.samples,
        }


@dataclass(frozen=True)
class SmoothResult:
    """平滑后的导航结果与改动报告。"""

    navigation: NavResult
    report: SmoothReport


def _smoother_gain(
    covariance: np.ndarray, transition: np.ndarray, process_noise: np.ndarray
) -> tuple[np.ndarray, bool]:
    """`C_k = P_{k|k} Φᵀ (Φ P_{k|k} Φᵀ + Q)⁻¹`，返回增益与"是否用了正则"。

    不显式求逆：`C_kᵀ = P_pred⁻¹ (Φ P_{k|k})`，与 `eskf._update` 用 `solve` 而不是
    `inv` 是同一个理由 —— 15×15 的显式逆在 P 病态时误差比解线性方程组大一到两个数量级。

    Q 的位置块恒为零（位置不是独立的随机过程），所以 P_pred 在原理上可以接近奇异。
    真奇异时退回到"加一点对角抖动再解"而不是 `pinv`：抖动的物理含义清楚（给状态一点
    人为的不确定度），而伪逆在这里等于悄悄换了一个不同的估计量。退化次数进报告。
    """
    predicted = transition @ covariance @ transition.T + process_noise
    predicted = 0.5 * (predicted + predicted.T)
    right = transition @ covariance
    try:
        return np.linalg.solve(predicted, right).T, False
    except np.linalg.LinAlgError:
        jitter = _JITTER_SCALE * float(np.trace(predicted)) / STATE_DIM
        regularized = predicted + np.eye(STATE_DIM) * max(jitter, _JITTER_SCALE)
        return np.linalg.solve(regularized, right).T, True


def _smooth_segment(history_phi: np.ndarray, history_p: np.ndarray, history_d: np.ndarray,
                    process_noise: np.ndarray) -> tuple[np.ndarray, int]:
    """一段的后向递推，返回逐样本的平滑误差 `(m, 15)` 与退化次数。"""
    m = len(history_p)
    delta = np.zeros((m, STATE_DIM))
    regularized = 0
    for index in range(m - 2, -1, -1):
        gain, fell_back = _smoother_gain(
            history_p[index], history_phi[index + 1], process_noise
        )
        regularized += int(fell_back)
        # 关键的一行：括号里是 `d_{k+1} + δ_{k+1|N}`，不是 `δ_{k+1|N}`。见模块文档。
        delta[index] = gain @ (history_d[index + 1] + delta[index + 1])
    return delta, regularized


def smooth(navigation: NavResult, history: FilterHistory) -> SmoothResult:
    """对前向结果做 RTS 后向平滑。云端精算链专用。

    `history` 必须来自**同一次** `eskf.run_ins_with_history` 调用：递推里的 Φ 与 P 是
    在那次前向滤波的名义轨迹上线性化的，配上另一条轨迹的名义值没有意义。样本数不一致
    时拒绝而不是截断 —— 长度对得上但内容对不上的情况本模块查不出来，能查出来的这一种
    就必须查。
    """
    if not isinstance(navigation, NavResult):
        raise SmoothError(f"navigation 必须是 NavResult，收到 {type(navigation).__name__}")
    if not isinstance(history, FilterHistory):
        raise SmoothError(f"history 必须是 FilterHistory，收到 {type(history).__name__}")

    n = len(navigation.t)
    if history.samples != n:
        raise SmoothError(
            f"history 覆盖 {history.samples} 个采样，navigation 有 {n} 个 —— "
            "两者必须来自同一次 run_ins_with_history 调用。"
        )

    # `delta` 初值为零，而**被跳过的段（`history.skipped`）不在下面的循环里** ——
    # 它们的修正量因此保持零，前向值原样留下。那是对的：那段没跑滤波，没有 `Φ` 与
    # `P` 可回传，凭空给它一个修正就是编造。
    #
    # 覆盖检查（上面那道）算的是含跳过段的总覆盖，所以它仍然能抓住"history 与
    # navigation 来自不同调用"，同时不再把一个 8 采样的碎段误判成不同调用（RAY-357）。
    delta = np.zeros((n, STATE_DIM))
    regularized = 0
    for segment in history.segments:
        segment_delta, segment_regularized = _smooth_segment(
            segment.phi, segment.covariance, segment.correction, segment.process_noise
        )
        delta[segment.start : segment.end] = segment_delta
        regularized += segment_regularized

    # 注入。姿态右乘（局部误差），其余直接相加 —— 与 eskf._update 的注入一字不差。
    attitude = quat.multiply(navigation.q, quat.from_rotation_vector(delta[:, THETA]))
    attitude = quat.normalize(attitude)
    velocity = navigation.v + delta[:, VELOCITY]
    position = navigation.p + delta[:, POSITION]
    gyro_bias = navigation.bg + delta[:, GYRO_BIAS]
    accel_bias = navigation.ba + delta[:, ACCEL_BIAS]

    smoothed = NavResult(
        t=navigation.t,
        q=attitude,
        v=velocity,
        p=position,
        bg=gyro_bias,
        ba=accel_bias,
        zupt=navigation.zupt,
        stances=list(navigation.stances),
        degraded=navigation.degraded,
        score=navigation.score,
    )
    return SmoothResult(smoothed, _report(navigation, smoothed, regularized))


def _report(before: NavResult, after: NavResult, regularized: int) -> SmoothReport:
    shift = np.linalg.norm(after.p - before.p, axis=1)
    attitude_shift = np.degrees(quat.angle_between(before.q, after.q))
    _, _, yaw_before = quat.to_euler(before.q)
    _, _, yaw_after = quat.to_euler(after.q)
    yaw_shift = np.degrees(np.abs(np.arctan2(np.sin(yaw_after - yaw_before),
                                             np.cos(yaw_after - yaw_before))))
    return SmoothReport(
        max_position_shift=float(np.max(shift)) if shift.size else 0.0,
        median_position_shift=float(np.median(shift)) if shift.size else 0.0,
        max_attitude_shift_deg=float(np.max(attitude_shift)) if attitude_shift.size else 0.0,
        max_yaw_shift_deg=float(np.max(yaw_shift)) if yaw_shift.size else 0.0,
        regularized_steps=regularized,
        samples=len(before.t),
    )
