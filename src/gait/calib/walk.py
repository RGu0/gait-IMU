"""直线走 10 步：安装误差角与坐标系重排。

## 它解的是什么

模块绑在鞋上，绑成什么姿态是每次佩戴时的偶然。而契约要求 `FootSeries.acc` 是
「已重排到**足部系**」的。本模块产出那个重排：一个从**模块体系**到**足部系**的旋转。

足部系的三轴（与合成器 `validate/synthetic.py` 构造数据时用的一致，实测确认）：

| 轴 | 方向 | 怎么定出来 |
|---|---|---|
| z | 向上 | 静立标定的重力方向（`still-calibration` 已给，不重算） |
| x | 前向 | 行走段水平加速度的**主成分** |
| y | 内外侧 | `z × x`，右手系 |

## 主成分给的是轴，不是方向

这是本模块唯一真正棘手的地方。协方差的主特征向量确定一条**直线**，前与后在它看来
完全一样 —— 特征向量取 `+v` 还是 `-v` 只是数值库的实现细节。

不处理这个歧义，**大约一半的会话会把足部前向装反 180°**。后果是步长与航向全错，
而且**不会报错**：数据看起来完全正常，只是走反了。

### 消歧用的是角速度峰值的不对称

行走时足部绕内外侧轴的转动是不对称的：摆动期向前甩的峰值明显大于回摆。实测三种
条件（直行、含转身、慢走）下正峰与负峰之比稳定在 1.4 左右。

试过但**没用**的：`a_forward` 的偏度。实测只有 0.01–0.15，噪声一来就淹了 —— 一个
量级这么小的判据，等于把消歧交给运气。这条记在这里，免得有人再走一遍。

用分位数而不是极值：单个尖峰可能来自一次磕碰，而 98/2 分位对它不敏感。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

import numpy as np

from gait.calib.still import CalibrationError, StillCalibration
from gait.core import quaternion

#: 主成分的**主导程度**下限：最大特征值 / 次大特征值。
#:
#: 低于它说明水平面上的运动没有一个明确的主方向 —— 受试者不是在直线走（原地踏步、
#: 转圈、或者根本没动）。这时给出的前向轴是噪声的方向，而**一个由噪声定出来的前向轴
#: 会让整场会话的步长与航向都错**，所以宁可判不通过。
MIN_PRINCIPAL_RATIO: Final[float] = 3.0

#: 消歧所需的峰值不对称度下限（正峰 / |负峰|，或其倒数）。
#:
#: 实测行走数据稳定在 1.4 左右。低于这个比值说明两侧峰值差不多，方向判据不可信 ——
#: 此时**不猜**，判不通过。猜错的代价是整场数据前后颠倒且不会报错。
MIN_PEAK_ASYMMETRY: Final[float] = 1.15

#: 分位数：用它而不是极值，因为单个尖峰可能来自一次磕碰。
PEAK_PERCENTILE: Final[float] = 98.0


@dataclass(frozen=True, slots=True)
class MountingCalibration:
    """一次安装误差角标定。**`rotation` 是这个模块存在的全部理由。**"""

    foot: str
    #: (3,3) 模块体系 → 足部系。`foot_vector = rotation @ module_vector`。
    rotation: np.ndarray
    #: 同一个旋转的四元数形式，便于与 `core/quaternion` 的其余部分拼接。
    q: np.ndarray
    #: 主成分的主导程度（最大 / 次大特征值）。越大说明「直线走」这件事做得越干净。
    principal_ratio: float
    #: 角速度峰值的不对称度。消歧的依据，越大越可信。
    peak_asymmetry: float
    samples: int

    @property
    def mounting_angles_deg(self) -> tuple[float, float, float]:
        """安装误差角 (roll, pitch, yaw)，度。**给人看的，不参与计算。**

        计算一律用 `rotation` —— 欧拉角在接近奇异时不唯一，而旋转矩阵没有这个问题。
        这里给出来是为了让操作员与排障的人有一个能说出口的数。
        """
        matrix = self.rotation
        pitch = float(np.degrees(np.arcsin(-np.clip(matrix[2, 0], -1.0, 1.0))))
        roll = float(np.degrees(np.arctan2(matrix[2, 1], matrix[2, 2])))
        yaw = float(np.degrees(np.arctan2(matrix[1, 0], matrix[0, 0])))
        return roll, pitch, yaw

    def apply(self, acc: np.ndarray, gyr: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """把模块体系的量重排到足部系。这是 `FootSeries` 那句「已重排」的落点。"""
        rotation = self.rotation
        return np.asarray(acc) @ rotation.T, np.asarray(gyr) @ rotation.T

    def snapshot(self) -> dict[str, Any]:
        roll, pitch, yaw = self.mounting_angles_deg
        return {
            "foot": self.foot,
            "rotation": [[float(v) for v in row] for row in self.rotation],
            "q": [float(v) for v in self.q],
            "mounting_roll_deg": roll,
            "mounting_pitch_deg": pitch,
            "mounting_yaw_deg": yaw,
            "principal_ratio": self.principal_ratio,
            "peak_asymmetry": self.peak_asymmetry,
            "samples": self.samples,
        }


def _principal_horizontal_axis(horizontal: np.ndarray) -> tuple[np.ndarray, float]:
    """水平加速度的主方向，以及它有多主导。

    用协方差的特征分解而不是 SVD：矩阵是 3×3 对称的，`eigh` 走对称路径，稳且便宜。
    """
    covariance = np.cov(horizontal, rowvar=False)
    values, vectors = np.linalg.eigh(covariance)
    order = np.argsort(values)[::-1]
    values, vectors = values[order], vectors[:, order]
    if values[0] <= 0:
        raise CalibrationError("水平加速度没有任何变化，这段数据不是行走")

    # 次大特征值可以是 0，而那**不是错误** —— 它表示横向运动为零，也就是主方向
    # 极度主导。第一版把它当错误抛了出去，逻辑正好反了：比值越大越好。
    #
    # 用相对量判「可忽略」：横向方差比前向小 12 个数量级时，比值已经没有数值意义，
    # 直接报 inf 比让它变成一个由舍入噪声决定的大数诚实。
    negligible = values[0] * 1e-12
    if values[1] <= negligible:
        return vectors[:, 0], float("inf")
    return vectors[:, 0], float(values[0] / values[1])


def estimate_mounting(
    still: StillCalibration,
    acc: np.ndarray,
    gyr: np.ndarray,
    *,
    min_principal_ratio: float = MIN_PRINCIPAL_RATIO,
    min_peak_asymmetry: float = MIN_PEAK_ASYMMETRY,
) -> MountingCalibration:
    """从直线行走段解出安装误差角。

    `still` 提供向上方向 —— 重力法的 roll/pitch 就在它里面，本模块不重算
    （`still-calibration` 已经用 RAY-202 的对准做过一次；做第二次就有两个答案）。
    """
    acc = np.asarray(acc, dtype=np.float64)
    gyr = np.asarray(gyr, dtype=np.float64)
    if acc.ndim != 2 or acc.shape[1] != 3 or acc.shape != gyr.shape:
        raise CalibrationError(f"acc/gyr 应为等长的 (n,3)，收到 {acc.shape} 与 {gyr.shape}")
    if acc.shape[0] < 100:
        raise CalibrationError(f"行走段只有 {acc.shape[0]} 个样本，不足以解出主方向")

    up = np.asarray(still.gravity_direction, dtype=np.float64)

    # 去掉沿重力的分量：重力是水平面上最大的一个常量，留着它会让主成分指向天上。
    centred = acc - acc.mean(axis=0)
    horizontal = centred - np.outer(centred @ up, up)

    forward, ratio = _principal_horizontal_axis(horizontal)
    if ratio < min_principal_ratio:
        raise CalibrationError(
            f"水平运动没有明确的主方向（主导度 {ratio:.1f} < {min_principal_ratio}）。"
            "这一段不像直线行走 —— 由噪声定出来的前向轴会让整场会话的步长与航向都错。"
        )

    # 正交化：主成分理论上已经在水平面内，但去重力那步的数值残差会留一点点分量。
    forward = forward - (forward @ up) * up
    forward /= np.linalg.norm(forward)

    # ── 消歧：主成分给的是轴，不是方向（见模块文档）──────────────────────
    medio_lateral = np.cross(up, forward)
    about_ml = gyr @ medio_lateral
    positive = float(np.percentile(about_ml, PEAK_PERCENTILE))
    negative = float(abs(np.percentile(about_ml, 100.0 - PEAK_PERCENTILE)))
    if positive <= 0 or negative <= 0:
        raise CalibrationError("绕内外侧轴的角速度没有双向峰值，这一段不是行走")

    asymmetry = max(positive, negative) / min(positive, negative)
    if asymmetry < min_peak_asymmetry:
        # **不猜。** 猜错的代价是整场数据前后颠倒，且不会报错。
        raise CalibrationError(
            f"无法判定足部前向（峰值不对称度 {asymmetry:.2f} < {min_peak_asymmetry}）。"
            "请让受试者沿直线正常行走 10 步后重试。"
        )
    if positive < negative:
        # 正峰应当更大（摆动期向前甩强于回摆）。反了就说明前向取反了。
        forward = -forward
        medio_lateral = np.cross(up, forward)

    rotation = np.vstack([forward, medio_lateral, up])
    return MountingCalibration(
        foot=still.foot,
        rotation=rotation,
        q=quaternion.from_matrix(rotation),
        principal_ratio=ratio,
        peak_asymmetry=asymmetry,
        samples=int(acc.shape[0]),
    )
