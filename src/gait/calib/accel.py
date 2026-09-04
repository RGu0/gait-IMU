"""加计多姿态出厂标定（RAY-207 R2，服务方工装）。

目标是把规格书量级的加计零偏（±20~40 mg）压到 2~5 mg。未标定的零偏会直接变成步长的
系统偏差：按 PRD 的估算是 1.75%~3.5%，它不会报错，只会让每一份报告都稳定地偏。

## 判据是**比力模长**，不是「这一面朝上」

静止时比力的模长必然是 1 g，无论模块朝哪儿。于是判据写成

    |A · a_meas + c| = g

它**完全不使用姿态朝向**。这一条是本模块最要紧的设计，理由是实测出来的：

R1 的原方案是六面法 —— 摆六个轴向面，假设每个面的真值恰为 `g·e_axis`，按方向做最小
二乘。真机（`F9:B3:4F:46:C9:31`）实测六个面的摆放倾斜 0.3°~2.8°、面内标准差仅
0.004 m/s²（摆得很稳）。但 `g·sin(2.8°) = 48 mg` —— **倾斜以一阶进入方向判据**，最小
二乘会把它当成器件误差吸收进矩阵。

拿一个**完美**传感器（零偏 0、标度 0、非对角 0），只带上那组实测倾斜跑原方案：

| 解出来的量 | 值 | 真值 |
| -- | -- | -- |
| 零偏 | 5.0 mg | 0 |
| 非对角项最大 | 22.84 ‰ | 0 |

全是倾斜的假象。22.8‰ 的交叉轴项拿去补偿，是往数据里**注入**误差而不是去掉误差 ——
比不标定更糟，且同样不报错。模长判据没有这个失效模式：它不关心模块朝哪儿，所以操作
员也**不需要**摆准，只需要摆稳。

## A 取**对称**矩阵，因为模长数据看不见旋转

模长判据对 `A` 与 `R·A`（R 为任意旋转）给出完全相同的值 —— 存在一个三维的规范自由度。
不约束它，解就在这三维里漂，而漂出来的那部分是**假的**。

约束成对称是有物理依据的，不是数学上的方便：`A` 与 `R·A` 的差别是一个纯旋转，而**纯
旋转不是加计误差**，它是模块相对外壳/足部的安装朝向 —— 那属 RAY-208 的 `walk.py`
（安装误差角）。让本模块去解它，就会与那边解出两个不一致的答案。

代价要写明：真实器件矩阵的**反对称部分本模块解不出来**。合成验证里真值的反对称部分
是 1.50‰，而按对称拟合后对**对称部分**的误差是 0.01‰ —— 也就是说误差全部、且仅仅是
那个看不见的部分。

参数因此是 6（对称 A）+ 3（c）= 9 个。

## 姿态要够多、够散，且这条要能被判出来

模长判据下**六个轴向姿态观测不到交叉轴项**：设计矩阵的交叉列只有对角列的 1.5%~8.8%，
而那点数值恰恰全部来自倾斜本身。合成实测（每姿态注入 0~3° 随机倾斜）：

| 姿态集 | 零偏误差 | 非对角项误差 |
| -- | -- | -- |
| 6 个轴向面 | 6.35 mg | 102.6 ‰（不可用） |
| 26 个分散姿态 | 0.00 mg | 1.35 ‰（即那个看不见的反对称部分） |

姿态数下限与条件数是**两道互补的闸，缺一不可**，这一点容易想当然：

* **条件数**抓「姿态挤成一团」：二十个几乎相同的姿态个数达标而信息量不达标，条件数
  会到 1e9 量级。
* **姿态数**抓「个数不足」，而这一条**条件数抓不到**。实测：六个轴向面的雅可比条件数
  只有 **12.7**，稳稳落在上限之内 —— 因为 6 个观测解 9 个参数是欠定的，`cond` 对一个
  6×9 矩阵只看得到 6 个奇异值，那三个**完全没有被约束**的方向根本不在它的视野里。
  也就是说，若只留条件数这一道闸，R1 的六面法会畅通无阻地通过，而它的矩阵误差是
  180‰。

先卡个数，才让条件数变得有意义（n > 参数个数之后 `cond` 才反映真实可辨性）。

## 为什么必须有留一交叉验证

R1 的六面法给 6 条方程解 6 个参数（3 标度 + 3 零偏），**恰定** —— 残差恒为 0，与标定
质量无关。一个任意坏的实现也能报 0 mg。那个数字不可能证伪任何东西。

≥20 个姿态才带来真正的冗余，此时残差才有意义；而**留一**比残差更进一步：它问的是
「这组参数对一个没参与拟合的姿态还准不准」，答的是过拟合。两个数都进快照。

## 不用 wt901 的 `calibrate_acceleration()`

它写 `Register.CALSW`，改**模块内部状态**，与 PRD **FR-03**（不写回模块寄存器、全部
上位机补偿、模块保持出厂原始态）直接冲突；而且 `CALSW` 是**只写**寄存器，重连后校准
状态「未知，也无从查询」，一次误调之后「这台模块还是不是出厂原始态」就永久失去根据。
能力上也不等价：它是单姿态动作，解不出矩阵。

这条由 `tools/check_calibration_channel.py` 守着（红线，进 `./dev lint`），不是只写在
这里 —— 误用它不报错、事后也查不出来，一条只活在文档里的约束挡不住一个看起来正确的
调用。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

import numpy as np

from gait.calib.still import CalibrationError

#: 标准重力加速度，m/s²（CGPM 定义值）。与 `wt901.protocol.units.STANDARD_GRAVITY`
#: 同值；这里不 import 它，因为 `gait.calib` 不该依赖设备层的包（分层红线）。
STANDARD_GRAVITY: Final[float] = 9.80665

#: 1 mg 对应的加速度，m/s²。规格书与验收都用 mg 说话，报告一律换算到它。
MILLI_G: Final[float] = STANDARD_GRAVITY / 1000.0

#: 待解参数个数：对称 A 的 6 个独立分量 + 改正偏移 c 的 3 个。
PARAMETER_COUNT: Final[int] = 9

#: 最少姿态数。9 个恰好可解但零冗余（残差必为 0，验收无法证伪）；20 个在合成验证里把
#: 雅可比条件数从 178 压到 21，且留一交叉验证才有意义。
MIN_ORIENTATIONS: Final[int] = 20

#: 每个姿态最少要有的样本数。200 Hz 下约 2 s。
MIN_SAMPLES_PER_ORIENTATION: Final[int] = 400

#: 姿态内静止判据：三轴标准差的最大值上限，m/s²。超过它说明这一段没静置好
#: （手还扶着、桌子在动），均值就不是这个姿态的真实读数。
MAX_ORIENTATION_STD: Final[float] = 0.05

#: 姿态内均值的模长允许偏离 1 g 的比例。偏太多说明这段根本不是静止的重力读数。
#: 给得比标定精度松得多是刻意的：**这一条不是精度判据**，它只排除「这段不是静止」。
MAX_MAGNITUDE_DEVIATION: Final[float] = 0.10

# 这里**没有**「两个姿态不许太接近」的下限，这不是疏漏。
#
# 第一版加了一条 10° 的最小间隔。它有两个毛病：随机摆二十个姿态时，有两个落在 10° 内
# 本来就很常见（生日问题），于是它会误拒正常采集；更要紧的是**它守的东西已经有人守了**
# —— 姿态散不散，雅可比条件数量的就是这个，而且量得更准（它直接回答「这九个参数可不可
# 辨」，而不是拿两两夹角去猜）。
#
# 而且近似重复的姿态在本方法里**本来就是无害的**：模长判据没有固定的姿态预算，重复一个
# 姿态只是多了一次测量，最小二乘照单处理。这与六面法不同 —— 那里重复一个面就意味着少
# 一个面，因为预算恰好是六。把六面法的直觉搬过来会加出一道守着空气的闸。
#
# `calib.still` 因为同一个理由删过一道重复的闸：同一件事有两处判据，它们迟早对不上。

#: 每个姿态的有效误差，mg。**实测值**：真机 24 姿态的残差 0.54 mg，按 n/(n-k) 修正
#: 得 0.68 mg。
#:
#: 注意它**不是白噪声**：姿态内标准差 0.0086 m/s²，600 个样本取均值后只有 0.036 mg ——
#: 相差 19 倍。主导误差是器件本身的非线性/滞后，所以**每个姿态多静置一会儿没有用**，
#: 只有增加姿态数才有用。这条直接决定了采集流程的形状。
DEFAULT_SIGMA_MG: Final[float] = 0.68

#: 预测零偏不确定度的目标，mg。达到它就可以停止采集。取 1.0 是因为验收要 2~5 mg，
#: 留一倍余量给「预测」与「实际」之间的偏差（实测两者相差 0~6%）。
TARGET_BIAS_SIGMA_MG: Final[float] = 1.0

#: 预测交叉轴不确定度的目标，千分比。
#:
#: **必须与零偏目标并列，只看零偏会漏掉一整类坏采集**：实测 22 个「只平放」的姿态，
#: 零偏 σ 有 0.49 mg（达标），交叉轴 σ 却有 5.97 ‰ —— 补 6 个斜姿态后降到 1.21 ‰。
#: 也就是说只看零偏的话，工装会对一个只会平放的操作员说「够了」，而 6 ‰ 的交叉耦合
#: 在 1 g 下就是 6 mg，比整个零偏预算还大。足部模块在步态里会经历各种朝向，交叉轴项
#: 定不准就直接进数据 —— 这正是 R2 要避开的那类失效，六面法的 180 ‰ 只是它的极端版。
TARGET_CROSS_SIGMA_PPT: Final[float] = 2.0

#: 雅可比条件数上限。分散良好时实测 15~22；显著高于它说明姿态挤在一起，
#: 此时解对噪声极度敏感 —— **而它不会报错，只会给出一组很离谱的参数**。
MAX_CONDITION_NUMBER: Final[float] = 60.0

#: 高斯牛顿的迭代上限与收敛判据。正常数据一两步就收敛；跑满上限是数据有问题的信号，
#: 由 `_fit` 报错而不是把没收敛的参数当结果返回。
MAX_ITERATIONS: Final[int] = 200
CONVERGENCE_STEP: Final[float] = 1e-14

#: 对称 A 的六个独立分量在参数向量里的位置。
_SYMMETRIC_INDEX: Final[tuple[tuple[int, int], ...]] = (
    (0, 0),
    (1, 1),
    (2, 2),
    (0, 1),
    (0, 2),
    (1, 2),
)

__all__ = [
    "MAX_CONDITION_NUMBER",
    "MAX_ITERATIONS",
    "MAX_ORIENTATION_STD",
    "MILLI_G",
    "MIN_ORIENTATIONS",
    "MIN_SAMPLES_PER_ORIENTATION",
    "PARAMETER_COUNT",
    "STANDARD_GRAVITY",
    "TARGET_BIAS_SIGMA_MG",
    "TARGET_CROSS_SIGMA_PPT",
    "AccelCalibration",
    "Observability",
    "OrientationObservation",
    "observability",
    "observe_orientation",
    "solve_orientations",
]


@dataclass(frozen=True, slots=True)
class OrientationObservation:
    """一个静置姿态的观测。

    **不记「这是哪个面」** —— 模长判据不使用朝向，记一个用不到的标签只会让读者以为
    它参与了计算。存均值与标准差：拟合只用均值，标准差是这一段摆得稳不稳的依据。
    """

    #: 姿态内均值（标称 SI，m/s²）。
    mean: np.ndarray
    #: 姿态内三轴标准差，m/s²。
    std: np.ndarray
    samples: int

    @property
    def direction(self) -> np.ndarray:
        """比力方向的单位向量。**不进拟合** —— 模长判据不使用朝向。

        供采集工装给操作员提示「这个姿态和刚才那个差不多，换个方向」用。那是**建议**，
        不是判据：姿态散不散由 `solve_orientations` 的雅可比条件数判定。
        """
        magnitude = float(np.linalg.norm(self.mean))
        if magnitude <= 0:
            raise CalibrationError("姿态均值的模长为零 —— 这段不是静止的比力")
        return self.mean / magnitude

    def snapshot(self) -> dict[str, Any]:
        return {
            "mean": [float(v) for v in self.mean],
            "std": [float(v) for v in self.std],
            "samples": self.samples,
        }


def observe_orientation(acc: np.ndarray) -> OrientationObservation:
    """把一段静置样本收成一个姿态观测，并验它确实是静止的重力读数。

    `acc` 是 `(n,3)` 的**标称 SI** 比力（原始码值经器件标称量程换算，即
    `wt901.protocol.units.accel_to_m_s2` 的结果）。

    在标称 SI 上做而不是在码值上做，与契约 §3.1「补偿在码值上做」并不矛盾：标称换算
    是一个固定的线性缩放，`A·(k·raw) + c` 与直接从码值出发的单次线性映射完全等价，
    没有任何信息损失。选它是因为这样 A 是**无量纲**的，接近单位阵，偏离量就是标度与
    交叉轴误差本身 —— 而从码值出发的 A 会把 1/32768·16·g 这个常数吸进去，看不出对错。

    **不检查摆放角度。** 模长判据不使用朝向，任何稳定姿态都是合法的（这正是 R2 换掉
    六面法的原因）。这里只排除「没静置好」与「这段不是重力」。
    """
    acc = np.asarray(acc, dtype=np.float64)
    if acc.ndim != 2 or acc.shape[1] != 3:
        raise CalibrationError(f"acc 应为 (n,3)，收到 {acc.shape}")
    if acc.shape[0] < MIN_SAMPLES_PER_ORIENTATION:
        raise CalibrationError(
            f"这个姿态只有 {acc.shape[0]} 个样本，少于 {MIN_SAMPLES_PER_ORIENTATION}。"
            "样本不够时姿态内均值压不住噪声，而标定精度全建立在均值上。"
        )

    mean = acc.mean(axis=0)
    std = acc.std(axis=0)

    worst_std = float(np.max(std))
    if worst_std > MAX_ORIENTATION_STD:
        raise CalibrationError(
            f"这个姿态没有静置好（三轴标准差最大 {worst_std:.3f} m/s²，"
            f"上限 {MAX_ORIENTATION_STD}）。请把模块放稳、手离开后再采。"
        )

    magnitude = float(np.linalg.norm(mean))
    deviation = abs(magnitude - STANDARD_GRAVITY) / STANDARD_GRAVITY
    if deviation > MAX_MAGNITUDE_DEVIATION:
        raise CalibrationError(
            f"比力模长 {magnitude:.3f} m/s² 偏离 1 g 达 {deviation:.1%}，"
            "这段不像静止的重力读数。"
        )
    return OrientationObservation(mean=mean, std=std, samples=int(acc.shape[0]))


def _matrix_from(parameters: np.ndarray) -> np.ndarray:
    matrix = np.zeros((3, 3), dtype=np.float64)
    for slot, (row, column) in enumerate(_SYMMETRIC_INDEX):
        matrix[row, column] = matrix[column, row] = parameters[slot]
    return matrix


def _jacobian(measured: np.ndarray, matrix: np.ndarray, offset: np.ndarray) -> np.ndarray:
    """`d|A·m + c| / d(参数)`，形状 `(n, 9)`。

    对称分量的偏导要把 `A[i,j]` 与 `A[j,i]` 两处一起算 —— 它们是**同一个**参数。
    漏掉一半不会报错，只会让非对角方向的收敛慢一半、条件数看起来比实际好。
    """
    corrected = measured @ matrix.T + offset
    norms = np.linalg.norm(corrected, axis=1)
    unit = corrected / norms[:, None]
    jacobian = np.zeros((measured.shape[0], PARAMETER_COUNT), dtype=np.float64)
    for slot, (row, column) in enumerate(_SYMMETRIC_INDEX):
        if row == column:
            jacobian[:, slot] = unit[:, row] * measured[:, column]
        else:
            jacobian[:, slot] = (
                unit[:, row] * measured[:, column] + unit[:, column] * measured[:, row]
            )
    jacobian[:, 6:] = unit
    return jacobian


def _fit(measured: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """高斯牛顿解 `|A·m + c| = g`，A 对称。返回 (A, c, 收敛处的雅可比)。

    从 A = I、c = 0 起步。这个初值在物理上就是「假设器件是理想的」，而真实器件离理想
    只有千分之几，所以一两步就收敛 —— 不需要更聪明的初值，也不该用随机初值：模长判据
    有一个全局的镜像解（A → −A），随机初值会有一半的机会掉进去。
    """
    parameters = np.array([1.0, 1.0, 1.0, 0.0, 0.0, 0.0], dtype=np.float64)
    offset = np.zeros(3, dtype=np.float64)
    jacobian = np.zeros((measured.shape[0], PARAMETER_COUNT))
    converged = False
    for _ in range(MAX_ITERATIONS):
        matrix = _matrix_from(parameters)
        corrected = measured @ matrix.T + offset
        norms = np.linalg.norm(corrected, axis=1)
        if not np.all(np.isfinite(norms)) or np.any(norms <= 0):
            raise CalibrationError("迭代发散 —— 姿态数据里可能有非静止段")
        jacobian = _jacobian(measured, matrix, offset)
        step, *_ = np.linalg.lstsq(jacobian, -(norms - STANDARD_GRAVITY), rcond=None)
        parameters = parameters + step[:6]
        offset = offset + step[6:]
        if float(np.linalg.norm(step)) < CONVERGENCE_STEP:
            converged = True
            break

    # **没收敛必须报错，不能把手上这组参数当结果返回。** 迭代用尽与收敛在返回值上
    # 长得一模一样，而一组没收敛的参数看起来完全正常 —— 量纲对、量级也对，只是错的。
    # 正常数据从 A=I 起步一两步就收敛，跑满上限意味着数据有问题，那件事该说出来。
    if not converged:
        raise CalibrationError(
            f"高斯牛顿在 {MAX_ITERATIONS} 次迭代内没有收敛。这批姿态里可能混了非静止段，"
            "或者姿态方向几乎共面。"
        )
    return _matrix_from(parameters), offset, jacobian


def _residual_mg(measured: np.ndarray, matrix: np.ndarray, offset: np.ndarray) -> float:
    corrected = measured @ matrix.T + offset
    error = np.linalg.norm(corrected, axis=1) - STANDARD_GRAVITY
    return float(np.sqrt(np.mean(error**2)) / MILLI_G)


@dataclass(frozen=True, slots=True)
class AccelCalibration:
    """一台设备的加计标定结果。

    形状对齐 `calib.still.StillCalibration`：frozen dataclass + `snapshot()` 出 `dict`
    进 `SessionMeta.calib_snapshot`。**不另造一套标定接口。**
    """

    device: str
    #: 改正矩阵 A（无量纲、**对称**，接近单位阵）：`a_true = A · a_meas + c`。
    matrix: np.ndarray
    #: 改正偏移 c，m/s²。**它不是器件零偏** —— 器件零偏见 `bias`。
    offset: np.ndarray
    #: 拟合残差 RMS（模长偏离 1 g），mg。
    residual_mg: float
    #: 留一交叉验证 RMS，mg。回答的是「对没参与拟合的姿态还准不准」。
    loo_mg: float
    #: 雅可比条件数。姿态分散良好时 15~22。
    condition_number: float
    orientations: tuple[OrientationObservation, ...]

    @property
    def bias(self) -> np.ndarray:
        """器件零偏 b，m/s²：由改正式还原，`b = −A⁻¹c`。

        规格书的「±20~40 mg」说的是它。拿 `offset` 去比规格书会得到一个看起来对、
        实际无关的数 —— `offset` 的量级取决于 A。
        """
        return -np.linalg.solve(self.matrix, self.offset)

    @property
    def bias_mg(self) -> np.ndarray:
        return self.bias / MILLI_G

    @property
    def bias_magnitude_mg(self) -> float:
        return float(np.linalg.norm(self.bias) / MILLI_G)

    @property
    def scale_error_ppt(self) -> np.ndarray:
        """三轴标度误差，千分比（A 对角线偏离 1 的量 × 1000）。"""
        return (np.diag(self.matrix) - 1.0) * 1000.0

    @property
    def cross_axis_ppt(self) -> float:
        """最大交叉轴项，千分比。"""
        off_diagonal = self.matrix[~np.eye(3, dtype=bool)]
        return float(np.max(np.abs(off_diagonal)) * 1000.0)

    def apply(self, acc: np.ndarray) -> np.ndarray:
        """把标称 SI 的比力改正到标定后的值。`(n,3) -> (n,3)`，也接受 `(3,)`。

        这是 `RawFrame → FootSeries` 那一段的单位换算里、紧接 `accel_to_m_s2` 的一步
        （RAY-360）。本模块**只提供**它，不负责接线。
        """
        acc = np.asarray(acc, dtype=np.float64)
        single = acc.ndim == 1
        if single:
            acc = acc[None, :]
        if acc.ndim != 2 or acc.shape[1] != 3:
            raise CalibrationError(f"acc 应为 (n,3) 或 (3,)，收到 {acc.shape}")
        corrected = acc @ self.matrix.T + self.offset
        return corrected[0] if single else corrected

    def snapshot(self) -> dict[str, Any]:
        """进 `SessionMeta.calib_snapshot`（PRD §6.1 强制字段）。"""
        return {
            "device": self.device,
            "method": "multi-orientation-magnitude",
            "matrix": [[float(v) for v in row] for row in self.matrix],
            "offset": [float(v) for v in self.offset],
            "bias_mg": [float(v) for v in self.bias_mg],
            "bias_magnitude_mg": self.bias_magnitude_mg,
            "scale_error_ppt": [float(v) for v in self.scale_error_ppt],
            "cross_axis_ppt": self.cross_axis_ppt,
            "residual_mg": self.residual_mg,
            "loo_mg": self.loo_mg,
            "condition_number": self.condition_number,
            "orientations": [item.snapshot() for item in self.orientations],
        }


def solve_orientations(
    device: str,
    observations: list[OrientationObservation] | tuple[OrientationObservation, ...],
) -> AccelCalibration:
    """从多个静置姿态解出对称改正矩阵与改正偏移（模长判据）。

    每个姿态给一条方程 `|A·m + c| = g`。9 个参数，因此姿态数必须显著多于 9 才有冗余，
    而冗余正是残差与留一能说话的前提（见模块文档）。
    """
    observations = tuple(observations)
    if len(observations) < MIN_ORIENTATIONS:
        raise CalibrationError(
            f"只有 {len(observations)} 个姿态，少于 {MIN_ORIENTATIONS}。"
            f"待解参数有 {PARAMETER_COUNT} 个；姿态数接近它时残差必然为 0，"
            "那个 0 说明不了标定质量。"
        )

    measured = np.array([item.mean for item in observations], dtype=np.float64)
    matrix, offset, jacobian = _fit(measured)

    condition = float(np.linalg.cond(jacobian))
    if not np.isfinite(condition) or condition > MAX_CONDITION_NUMBER:
        raise CalibrationError(
            f"姿态分布不足以定出这 {PARAMETER_COUNT} 个参数"
            f"（雅可比条件数 {condition:.0f}，上限 {MAX_CONDITION_NUMBER}）。"
            "请让姿态的朝向更分散 —— 只在少数几个方向附近打转时，解对噪声极度敏感，"
            "而它不会报错。"
        )

    # 留一：每次留出一个姿态、用其余的拟合，再看留出的那个模长错多少。
    # 它比残差多回答一件事 —— 这组参数对**没参与拟合**的姿态还准不准。
    errors = []
    failed = 0
    for index in range(len(observations)):
        rest = np.delete(measured, index, axis=0)
        try:
            fold_matrix, fold_offset, _ = _fit(rest)
        except CalibrationError:
            failed += 1
            continue
        errors.append(_residual_mg(measured[index : index + 1], fold_matrix, fold_offset))

    # **一个折叠都不该失败。** 留出一个之后仍有 ≥19 个姿态，远多于 9 个参数；此时解不
    # 出来说明这批数据本身有问题。第一版这里是 `continue` 了事，于是「一半折叠失败」
    # 与「全部成功」会给出同样健康的 loo —— 幸存者平均出来的数看不出任何异常。
    if failed:
        raise CalibrationError(
            f"留一交叉验证有 {failed}/{len(observations)} 个折叠解不出来。"
            "留出一个后仍有足够姿态，解不出说明这批数据有问题，不能只拿剩下的算。"
        )
    loo = float(np.sqrt(np.mean(np.square(errors))))

    return AccelCalibration(
        device=device,
        matrix=matrix,
        offset=offset,
        residual_mg=_residual_mg(measured, matrix, offset),
        loo_mg=loo,
        condition_number=condition,
        orientations=observations,
    )


@dataclass(frozen=True, slots=True)
class Observability:
    """当前这批姿态把九个参数定到了什么程度。

    供采集工装**边采边判**：达标就停，不达标就告诉操作员还缺什么。没有它，采集端只能
    数个数，而「二十个姿态」既可能绰绰有余、也可能远远不够，取决于摆得散不散 ——
    实测随机 24 姿态在不同运气下零偏误差在 0.43~1.10 mg 之间，相差 2.6 倍。
    """

    count: int
    condition_number: float
    #: 预测的零偏不确定度，mg。由 `σ²(JᵀJ)⁻¹` 的零偏块算出。
    bias_sigma_mg: float
    #: 预测的交叉轴项不确定度，千分比。
    cross_sigma_ppt: float
    sufficient: bool
    advice: str


def observability(
    observations: list[OrientationObservation] | tuple[OrientationObservation, ...],
    *,
    sigma_mg: float = DEFAULT_SIGMA_MG,
) -> Observability:
    """从已采到的姿态预测标定精度，并给出下一步建议。

    `σ²(JᵀJ)⁻¹` 是最小二乘的标准协方差近似。它在这里**可信**，不是纸上推导 ——
    与蒙特卡洛实测对照过六组姿态集，预测与实测相差 0~6%（见 scope 证据）。

    停止判据取四条并列：姿态数够（让条件数有意义）、条件数够低、预测**零偏**不确定度
    达标、预测**交叉轴**不确定度达标。后两条才是真正想要的东西，前两条是让它们算得
    出来、算得准的前提。

    零偏与交叉轴必须**分开判**：只看零偏时，一批「只平放」的姿态会被判为够了，而它的
    交叉轴项其实差得远（见 `TARGET_CROSS_SIGMA_PPT` 的实测数据）。
    """
    observations = tuple(observations)
    count = len(observations)
    if count < PARAMETER_COUNT:
        return Observability(
            count=count,
            condition_number=float("inf"),
            bias_sigma_mg=float("inf"),
            cross_sigma_ppt=float("inf"),
            sufficient=False,
            advice=f"还差 {PARAMETER_COUNT - count} 个姿态才够解出参数，继续采。",
        )

    measured = np.array([item.mean for item in observations], dtype=np.float64)
    try:
        _matrix, _offset, jacobian = _fit(measured)
    except CalibrationError:
        return Observability(
            count=count,
            condition_number=float("inf"),
            bias_sigma_mg=float("inf"),
            cross_sigma_ppt=float("inf"),
            sufficient=False,
            advice="这批姿态还解不出来，换个方向再采几个。",
        )

    condition = float(np.linalg.cond(jacobian))
    sigma = sigma_mg * MILLI_G
    try:
        covariance = np.linalg.inv(jacobian.T @ jacobian) * sigma**2
    except np.linalg.LinAlgError:
        covariance = np.full((PARAMETER_COUNT, PARAMETER_COUNT), np.inf)

    bias_sigma = float(np.sqrt(np.trace(covariance[6:, 6:])) / MILLI_G)
    # 交叉轴项是参数向量的第 4~6 个（`_SYMMETRIC_INDEX` 的后三个）。
    cross_sigma = float(np.sqrt(np.trace(covariance[3:6, 3:6])) * 1000.0)

    sufficient = (
        count >= MIN_ORIENTATIONS
        and condition <= MAX_CONDITION_NUMBER
        and bias_sigma <= TARGET_BIAS_SIGMA_MG
        and cross_sigma <= TARGET_CROSS_SIGMA_PPT
    )

    if sufficient:
        advice = "够了，可以停。"
    elif count < MIN_ORIENTATIONS:
        advice = f"继续采，至少还要 {MIN_ORIENTATIONS - count} 个。"
    else:
        # 交叉轴项的不确定度相对最大时，缺的是**倾斜**姿态 —— 轴向姿态定不出它们
        # （六个轴向面的矩阵误差 180‰ 就是这么来的）。
        if cross_sigma > TARGET_CROSS_SIGMA_PPT:
            advice = (
                "把模块**斜着**放几个 —— 让两根轴同时分到重力（垫高一角、靠着书）。"
                "只平放的话，交叉轴项定不出来。"
            )
        else:
            coverage = np.abs(np.array([item.direction for item in observations])).sum(axis=0)
            axis = "XYZ"[int(np.argmin(coverage))]
            advice = f"多摆几个让 {axis} 轴方向受力的姿态 —— 目前那个方向的姿态最少。"

    return Observability(
        count=count,
        condition_number=condition,
        bias_sigma_mg=bias_sigma,
        cross_sigma_ppt=cross_sigma,
        sufficient=sufficient,
        advice=advice,
    )
