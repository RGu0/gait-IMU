"""加计六面法出厂标定（RAY-207，服务方工装）。

目标是把规格书量级的加计零偏（±20~40 mg）压到 2~5 mg。未标定的零偏会直接变成步长
的系统偏差：1 mg ≈ 0.0098 m/s²，在一个步态周期的两次积分里累出的位移误差，按 PRD
的估算是 1.75%~3.5% 的步长系统偏差 —— 它不会报错，只会让每一份报告都稳定地偏。

## 为什么**不**用 wt901 的 `calibrate_acceleration()`

这不是「那个 API 不好用」。它做的是另一件事，而那件事与本项目的方案冲突：

* 它的实现是 `registers.write(Register.CALSW, CalibrationMode.ACCELERATION)`，改的是
  **模块内部状态**。PRD **FR-03** 明定标定参数不写回模块寄存器、全部补偿在上位机
  完成、**模块保持出厂原始态** —— 调用它，这个前提就不再成立，而此前所有会话的
  标定快照都建立在这个前提上。
* 而且**查不回来**：wt901 的模块文档写明 `CALSW`（`0x01`）是只写寄存器，重连后
  设备的校准状态「未知，也无从查询」。一次误调之后，「这台模块还是不是出厂原始
  态」就永久失去了根据。
* 能力上也不等价：它是**单姿态**（仅水平）的一次性动作，只能处理该姿态下的零位。
  六面法要六个独立重力方向，才能最小二乘解出 3×3 矩阵 + 零偏向量共 12 个参数。
  单姿态在数学上解不出那个矩阵。

这条约束由 `tools/check_calibration_channel.py` 守着（红线，进 `./dev lint`），不是
只写在这里 —— 因为误用它不报错，事后也查不出来，一条只活在文档里的约束挡不住一个
看起来正确的调用。

## 参数化：存的是**改正式**，不是器件式

器件的误差模型通常写成

    a_meas = M · a_true + b          （M 含标度与非正交，b 是零偏）

但真正要被反复执行的是它的**逆**。所以本模块直接拟合并存储改正式：

    a_true = A · a_meas + c          （A = M⁻¹，c = −M⁻¹b）

两个理由：`apply()` 是每一帧都要跑的热路径，存 A 就不必每次求逆；而且改正式对参数
是**线性**的，最小二乘一步到位，不必迭代。

器件式随时可以还原（`M = A⁻¹`、`b = −A⁻¹c`），`bias_mg` 报的正是还原出来的 `b`，
因为规格书的「±20~40 mg」说的是它，不是 `c`。**两者不可混用**：`c` 的量级取决于
A，拿它去比规格书会得到一个看起来对但其实无关的数。

## 六面法为什么对「桌面不够平」相当宽容

工装是在桌上摆六个面，谁也保证不了桌面绝对水平、模块外壳绝对方正。但倾斜进入结果
的方式对零偏与标度**不同**，而且都比直觉温和：

* **零偏**取自 ±一对面的**和**：`(m₊ + m₋)/2`。真实的 ±g 在相加时抵消，剩下的就是
  零偏，与这一对面共同的倾斜无关（一阶）。
* **标度**取自同一对面的**差**：`(m₊ − m₋)/2`，倾斜以 `cos θ` 进入 —— 是**二阶**的。
  θ = 3° 时 `cos θ = 0.9986`，折合约 1.4 mg；θ = 10° 才到 15 mg。

所以 `MAX_TILT_RATIO` 给得比较松（约 14°）：卡得过严只会让操作员反复重摆，而收益是
二阶的。真正要卡死的是**每个面是否静止**与**六个面是否都摆到了** —— 那两条卡不住
才会让结果整个错掉，见下。

## 判据放在哪里

静止判定**复用** `core/zupt` 那一套（经 `calib.still` 的同一条路径），不另写一套。
`calib.still` 已经因为「同一件事有两处判据，迟早对不上」删过一道重复的闸；这里不重蹈。
本模块只判它自己独有的三件事：六个面是否齐、每个面是否真的落在某个面上、拟合是否
病态（条件数）。
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

#: 六个面的标签。`+Z` 表示模块的 +Z 轴朝上 —— 静止时加计沿 +Z 读到 +g。
FACES: Final[tuple[str, ...]] = ("+X", "-X", "+Y", "-Y", "+Z", "-Z")

#: 每个面最少要有的样本数。200 Hz 下约 2 s。少于它，面内均值压不下噪声，
#: 而六面法的整个精度都建立在「每个面的均值足够干净」上。
MIN_SAMPLES_PER_FACE: Final[int] = 400

#: 面内静止判据：三轴标准差的最大值上限，m/s²。超过它说明这一面没静置好
#: （手还扶着、桌子在动），均值就不是那个面的真实读数。
MAX_FACE_STD: Final[float] = 0.05

#: 面内均值的模长允许偏离 1 g 的比例。偏太多说明这段根本不是静止的重力读数。
MAX_MAGNITUDE_DEVIATION: Final[float] = 0.10

#: 离轴分量与模长之比的上限，约合 14°。给得松是因为倾斜对结果的影响是二阶的
#: （见模块文档）；它要挡的是「摆错面 / 立在棱上」这种定性错误，不是几度的不平。
MAX_TILT_RATIO: Final[float] = 0.25

#: 设计矩阵条件数上限。六个面摆齐时实测约 2~3；显著高于它说明有面重复或缺失，
#: 此时解会对噪声极度敏感 —— **而它不会报错，只会给出一组很离谱的参数**。
MAX_CONDITION_NUMBER: Final[float] = 20.0

__all__ = [
    "FACES",
    "MAX_CONDITION_NUMBER",
    "MAX_FACE_STD",
    "MAX_TILT_RATIO",
    "MILLI_G",
    "MIN_SAMPLES_PER_FACE",
    "AccelCalibration",
    "FaceObservation",
    "identify_face",
    "observe_face",
    "solve_six_face",
]


@dataclass(frozen=True, slots=True)
class FaceObservation:
    """一个面的静置观测。

    存均值与标准差而不是原始样本：拟合只用得到均值，而标准差是判断这一面摆得好不好
    的依据，两者都要进快照供事后复核。原始样本留在会话目录里，不进标定参数。
    """

    face: str
    #: 面内均值（标称 SI，m/s²）。
    mean: np.ndarray
    #: 面内三轴标准差，m/s²。
    std: np.ndarray
    samples: int

    @property
    def expected(self) -> np.ndarray:
        """该面静止时的**真值**比力：沿朝上的那根轴的 +1 g。"""
        return _face_vector(self.face) * STANDARD_GRAVITY

    @property
    def tilt_ratio(self) -> float:
        """离轴分量占模长的比例。约等于摆放倾角的正弦。"""
        magnitude = float(np.linalg.norm(self.mean))
        if magnitude <= 0:
            raise CalibrationError(f"{self.face} 面的均值模长为零 —— 这段不是静止的比力")
        axis = int(np.argmax(np.abs(self.mean)))
        off_axis = np.delete(self.mean, axis)
        return float(np.linalg.norm(off_axis) / magnitude)

    def snapshot(self) -> dict[str, Any]:
        return {
            "face": self.face,
            "mean": [float(v) for v in self.mean],
            "std": [float(v) for v in self.std],
            "samples": self.samples,
            "tilt_deg": float(np.degrees(np.arcsin(min(1.0, self.tilt_ratio)))),
        }


def _face_vector(face: str) -> np.ndarray:
    """面标签 → 朝上的单位向量（模块体系）。"""
    if face not in FACES:
        raise CalibrationError(f"未知的面 {face!r}，应为 {list(FACES)} 之一")
    axis = "XYZ".index(face[1])
    vector = np.zeros(3, dtype=np.float64)
    vector[axis] = 1.0 if face[0] == "+" else -1.0
    return vector


def identify_face(mean: np.ndarray) -> str:
    """从一段静置的均值判断这是哪个面朝上。

    取绝对值最大的那根轴及其符号。**不接受调用方自报面别** —— 操作员按提示摆六个面
    时最容易犯的错就是摆错顺序或漏摆，而自报会让那个错静默地进到拟合里：拟合会照单
    全收，给出一组解，只是解是错的。让数据自己说它是哪个面，漏摆与重复才暴露得出来。
    """
    mean = np.asarray(mean, dtype=np.float64)
    if mean.shape != (3,):
        raise CalibrationError(f"均值应为 (3,)，收到 {mean.shape}")
    axis = int(np.argmax(np.abs(mean)))
    return f"{'+' if mean[axis] >= 0 else '-'}{'XYZ'[axis]}"


def observe_face(acc: np.ndarray) -> FaceObservation:
    """把一段静置样本收成一个 `FaceObservation`，并验它确实落在某个面上。

    `acc` 是 `(n,3)` 的**标称 SI** 比力（原始码值经器件标称量程换算，即
    `wt901.protocol.units.accel_to_m_s2` 的结果）。

    在标称 SI 上做而不是在码值上做，与契约 §3.1「补偿在码值上做」并不矛盾：标称换算
    是一个固定的线性缩放，`A·(k·raw) + c` 与直接从码值出发的单次线性映射完全等价，
    没有任何信息损失。选它是因为这样 A 是**无量纲**的，接近单位阵，偏离量就是标度与
    交叉轴误差本身 —— 而从码值出发的 A 会把 1/32768·16·g 这个常数吸进去，看不出对错。
    """
    acc = np.asarray(acc, dtype=np.float64)
    if acc.ndim != 2 or acc.shape[1] != 3:
        raise CalibrationError(f"acc 应为 (n,3)，收到 {acc.shape}")
    if acc.shape[0] < MIN_SAMPLES_PER_FACE:
        raise CalibrationError(
            f"这一面只有 {acc.shape[0]} 个样本，少于 {MIN_SAMPLES_PER_FACE}。"
            "样本不够时面内均值压不住噪声，而六面法的精度全建立在均值上。"
        )

    mean = acc.mean(axis=0)
    std = acc.std(axis=0)
    face = identify_face(mean)
    observation = FaceObservation(face=face, mean=mean, std=std, samples=int(acc.shape[0]))

    worst_std = float(np.max(std))
    if worst_std > MAX_FACE_STD:
        raise CalibrationError(
            f"{face} 面没有静置好（三轴标准差最大 {worst_std:.3f} m/s²，"
            f"上限 {MAX_FACE_STD}）。请把模块放稳、手离开后再采。"
        )

    magnitude = float(np.linalg.norm(mean))
    deviation = abs(magnitude - STANDARD_GRAVITY) / STANDARD_GRAVITY
    if deviation > MAX_MAGNITUDE_DEVIATION:
        raise CalibrationError(
            f"{face} 面的比力模长 {magnitude:.3f} m/s² 偏离 1 g 达 {deviation:.1%}，"
            "这段不像静止的重力读数。"
        )

    if observation.tilt_ratio > MAX_TILT_RATIO:
        tilt = np.degrees(np.arcsin(min(1.0, observation.tilt_ratio)))
        raise CalibrationError(
            f"{face} 面倾斜约 {tilt:.0f}°，超过上限。模块可能没有平放在这个面上"
            "（立在棱上，或摆成了别的面）。"
        )
    return observation


@dataclass(frozen=True, slots=True)
class AccelCalibration:
    """一台设备的加计六面法标定结果。

    形状对齐 `calib.still.StillCalibration`：frozen dataclass + `snapshot()` 出 `dict`
    进 `SessionMeta.calib_snapshot`。**不另造一套标定接口。**
    """

    device: str
    #: 改正矩阵 A（无量纲，接近单位阵）：`a_true = A · a_meas + c`。
    matrix: np.ndarray
    #: 改正偏移 c，m/s²。**它不是器件零偏** —— 器件零偏见 `bias`。
    offset: np.ndarray
    #: 拟合后各面的残差 RMS，换算成 mg。这是验收里的「标定后」那个数。
    residual_mg: float
    #: 设计矩阵条件数。六面摆齐时约 2~3。
    condition_number: float
    faces: tuple[FaceObservation, ...]

    @property
    def bias(self) -> np.ndarray:
        """器件零偏 b，m/s²：由改正式还原，`b = −A⁻¹c`。

        规格书的「±20~40 mg」说的是它。拿 `offset` 去比规格书会得到一个看起来对、
        实际无关的数 —— `offset` 的量级取决于 A。
        """
        return -np.linalg.solve(self.matrix, self.offset)

    @property
    def bias_mg(self) -> np.ndarray:
        """器件零偏，逐轴，单位 mg。"""
        return self.bias / MILLI_G

    @property
    def bias_magnitude_mg(self) -> float:
        return float(np.linalg.norm(self.bias) / MILLI_G)

    @property
    def scale_error_ppt(self) -> np.ndarray:
        """三轴标度误差，千分比（A 对角线偏离 1 的量 × 1000）。"""
        return (np.diag(self.matrix) - 1.0) * 1000.0

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
            "matrix": [[float(v) for v in row] for row in self.matrix],
            "offset": [float(v) for v in self.offset],
            "bias_mg": [float(v) for v in self.bias_mg],
            "bias_magnitude_mg": self.bias_magnitude_mg,
            "scale_error_ppt": [float(v) for v in self.scale_error_ppt],
            "residual_mg": self.residual_mg,
            "condition_number": self.condition_number,
            "faces": [face.snapshot() for face in self.faces],
        }


def solve_six_face(
    device: str, observations: list[FaceObservation] | tuple[FaceObservation, ...]
) -> AccelCalibration:
    """最小二乘解出 3×3 改正矩阵与改正偏移。

    每个面给一行方程 `A · m_face + c = g · u_face`。六个面共 6 行，未知数每个输出轴
    4 个（A 的一行 3 个 + c 的 1 个），6 > 4，超定，`lstsq` 一步解出。

    **按面均值拟合而不是按原始样本**：那样每个面的权重与它静置了多久成正比，而静置
    时长是操作员的随手行为，不该影响结果。面内的噪声已经在求均值时压过一遍了。
    """
    observations = tuple(observations)
    present = [item.face for item in observations]
    missing = [face for face in FACES if face not in present]
    if missing:
        raise CalibrationError(
            f"缺少这些面：{missing}。六面法要六个独立的重力方向才能解出 3×3 矩阵；"
            "少一个面，方程组就撑不起 12 个参数。"
        )
    duplicated = sorted({face for face in present if present.count(face) > 1})
    if duplicated:
        raise CalibrationError(
            f"这些面采了不止一次：{duplicated}。重复的面不提供新的重力方向，"
            "而它会让人误以为六个方向都齐了。"
        )

    measured = np.array([item.mean for item in observations], dtype=np.float64)
    target = np.array([item.expected for item in observations], dtype=np.float64)
    design = np.hstack([measured, np.ones((measured.shape[0], 1))])

    condition = float(np.linalg.cond(design))
    if not np.isfinite(condition) or condition > MAX_CONDITION_NUMBER:
        raise CalibrationError(
            f"六面拟合病态（条件数 {condition:.1f}，上限 {MAX_CONDITION_NUMBER}）。"
            "多半是某两个面摆成了同一个方向。病态的解不会报错，只会很离谱。"
        )

    # solution 是 (4,3)：前三行是 Aᵀ，最后一行是 c。
    solution, *_ = np.linalg.lstsq(design, target, rcond=None)
    matrix = np.ascontiguousarray(solution[:3].T)
    offset = np.ascontiguousarray(solution[3])

    residual = design @ solution - target
    residual_mg = float(np.sqrt(np.mean(np.sum(residual**2, axis=1))) / MILLI_G)

    return AccelCalibration(
        device=device,
        matrix=matrix,
        offset=offset,
        residual_mg=residual_mg,
        condition_number=condition,
        faces=observations,
    )
