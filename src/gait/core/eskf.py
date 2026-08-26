"""15 维误差状态卡尔曼滤波。契约 §1 的 `core/eskf.py`（F4.3）。整体设计 §5.6。

## 为什么是误差状态而不是直接 EKF

姿态若作为状态直接进滤波器，四元数的单位模约束会与线性更新冲突：一次卡尔曼修正会把
它推离单位球面，只能事后强行归一化，而那不是一个有统计含义的操作。

误差状态把姿态误差表示成一个**三维小角度**，它没有约束、接近线性、数值稳定；每次更新
之后把误差注入名义状态并清零，四元数始终由指数映射生成，天然在单位球面上。

## 状态与顺序

误差状态 15 维，顺序 `[δθ, δv, δp, δbg, δba]`：

| 块 | 含义 | 参考系 |
| --- | --- | --- |
| `δθ` | 姿态误差，小角度 | **足部系**（局部误差表述） |
| `δv` | 速度误差 | 导航系 ENU |
| `δp` | 位置误差 | 导航系 ENU |
| `δbg` | 陀螺零偏误差 | 足部系，rad/s |
| `δba` | 加计零偏误差 | 足部系，m/s² |

局部（足部系）而非全局姿态误差：注入时是右乘 `q ⊗ exp(δθ)`，与
`quaternion.integrate_angular_rate` 的右乘一致 —— 两处若一个左乘一个右乘，误差会被
注到相反的方向上，而那不报错。

## 三种观测

| 观测 | 触发 | 残差 | 作用 |
| --- | --- | --- | --- |
| ZUPT | 检测到零速 | `0 - v̂` | 主约束，抑制速度/位置/零偏 |
| ZARU | 检测到零角速 | `0 - (ω_m - b̂g)` | **直接**约束陀螺零偏 |
| 高度 | 支撑相 | `z_ref - p̂_z` | 平地场景抑制高度漂移 |

磁航向观测**不实现** —— 本硬件在 200 Hz 下磁力计不可读（选型对比 v0.2 §3.2）。
双足距离约束属 RAY-205，是 6 轴下唯一能约束航向的机制。

高度约束在**上下楼/坡道必须关闭**（`eskf_enable_height_constraint`）。默认开是因为
T-01 是平地行走，但这个默认在任何非平地场景下都是错的，而错的方式是安静地把真实的
高度变化压掉 —— 楼梯会看起来像平地。

## 航向是**弱可观测**的，不是完全不可观测

一个直觉的说法是"6 轴下航向没有任何观测约束"。实测下来这话不准确，值得写清楚。

支撑相里比力约等于重力，`R[f]×` 只在垂直方向退化，所以 ZUPT 在**支撑相**确实不约束
航向。但摆动相里比力方向变化很大（合成行走下峰值约 38 m/s²，方向随足部俯仰扫过一个
大角度），姿态-速度的耦合因此在各个方向上都非零 —— 航向于是被间接约束了一点。

量出来是这样（合成行走，导航系下的 1σ）：

| 时长 | 倾角 y | 航向 |
| --- | --- | --- |
| 先验 | 1.0° | 30° |
| 5 s | 0.058° | 4.5° |
| 30 s | 0.028° | 2.7° |
| 60 s | 0.024° | 2.3° |

结论是**它掉下来一次然后就卡住了**：30 s → 60 s 倾角还在收敛 43%，航向只收敛 13%。
2° 的航向不确定度在 4 米往返里就是几厘米的横向误差，且随距离线性放大。

所以这不是"有观测所以没问题"，而是"有一点观测、远远不够"。**双足距离约束（RAY-205）
仍然是 6 轴下唯一真正约束航向的机制**，整体设计 v0.2 把它从差异化增益升级为必需项
正是这个道理。测试断言的是那个卡住的行为，不是"航向方差不变"。

## 与 `core/ins.py` 的关系，以及那里的第二份实现

名义状态的推进与 `ins.mechanize` 是同一套方程，但这里用**旋转矩阵**而不是四元数在
循环里传递：矩阵形式下姿态推进、比力转换与雅可比 `R[f]×` 共用同一个 `R`，省掉每步
两次四元数↔矩阵的往返。36000 采样 × 两足下这不是空谈。

代价是 `_exp_so3` 成了指数映射的第二份实现。**它由一条测试与 `quaternion` 模块钉在
一起**（随机旋转矢量下与 `to_matrix(from_rotation_vector(rv))` 逐元素相等到机器精度）。
第二份实现在这里是可接受的，因为有东西在持续证明两份是同一个东西；没有那条测试就
不可接受。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Final

import numpy as np

from gait.config import AlgoConfig
from gait.contracts import FootSeries, NavResult
from gait.core import quaternion as quat
from gait.core.alignment import Alignment, initial_alignment
from gait.core.ins import GRAVITY_STANDARD, gravity_vector
from gait.core.zupt import StanceDetection, detect_stance

#: 误差状态维数与各块的位置。写成常量而不是散在切片里 —— 顺序一旦在某处写反，
#: 症状是"滤波器收敛到一个错误但自洽的状态"，不会报错。
STATE_DIM: Final[int] = 15
THETA: Final[slice] = slice(0, 3)
VELOCITY: Final[slice] = slice(3, 6)
POSITION: Final[slice] = slice(6, 9)
GYRO_BIAS: Final[slice] = slice(9, 12)
ACCEL_BIAS: Final[slice] = slice(12, 15)

_EYE3: Final[np.ndarray] = np.eye(3)
_SMALL_ANGLE: Final[float] = 1e-8


class EskfError(ValueError):
    """滤波输入非法。"""


def _skew(v: np.ndarray) -> np.ndarray:
    """反对称矩阵，满足 `_skew(a) @ b == cross(a, b)`。"""
    x, y, z = v
    return np.array([[0.0, -z, y], [z, 0.0, -x], [-y, x, 0.0]])


def _exp_so3(rotation_vector: np.ndarray) -> np.ndarray:
    """旋转矢量 → 旋转矩阵。Rodrigues 闭式。

    这是指数映射在本仓库里的**第二份实现**（第一份是
    `quaternion.from_rotation_vector` + `to_matrix`）。存在的理由见模块文档；
    `test_exp_so3_agrees_with_the_quaternion_path` 把两份钉在一起。
    """
    theta = float(np.linalg.norm(rotation_vector))
    if theta < _SMALL_ANGLE:
        # sin θ/θ → 1、(1-cos θ)/θ² → 1/2。展开到二阶，避免 0/0。
        return _EYE3 + _skew(rotation_vector) + 0.5 * _skew(rotation_vector) @ _skew(rotation_vector)
    axis = _skew(rotation_vector / theta)
    return _EYE3 + np.sin(theta) * axis + (1.0 - np.cos(theta)) * (axis @ axis)


@dataclass(frozen=True)
class FilterState:
    """名义状态与误差协方差。

    frozen：每一步返回新状态。可变状态会让"这是哪一步的值"变成需要追踪的问题，而
    ESKF 的每一步都要同时用到推进前与推进后的量。
    """

    #: 足部系 → 导航系的旋转矩阵。循环内用矩阵，输出时才转四元数（见模块文档）。
    rotation: np.ndarray
    velocity: np.ndarray
    position: np.ndarray
    gyro_bias: np.ndarray
    accel_bias: np.ndarray
    covariance: np.ndarray

    @classmethod
    def initial(cls, alignment: Alignment, cfg: AlgoConfig) -> FilterState:
        """由初始对准与配置构造。位置取原点，速度取零（对准段本来就是静止的）。"""
        covariance = np.diag(
            np.concatenate(
                [
                    # 倾角两轴用倾角方差，航向轴单独给 —— 它不可观测，见模块文档。
                    np.array(
                        [
                            cfg.eskf_initial_tilt_sigma**2,
                            cfg.eskf_initial_tilt_sigma**2,
                            cfg.eskf_initial_yaw_sigma**2,
                        ]
                    ),
                    np.full(3, cfg.eskf_initial_velocity_sigma**2),
                    np.full(3, cfg.eskf_initial_position_sigma**2),
                    np.full(3, cfg.eskf_initial_gyro_bias_sigma**2),
                    np.full(3, cfg.eskf_initial_accel_bias_sigma**2),
                ]
            )
        )
        return cls(
            rotation=quat.to_matrix(alignment.q),
            velocity=np.zeros(3),
            position=np.zeros(3),
            gyro_bias=np.zeros(3),
            accel_bias=np.zeros(3),
            covariance=covariance,
        )


@dataclass(frozen=True)
class SegmentHistory:
    """一个连续段的前向滤波逐样本历史。RTS 平滑（`core/rts.py`）的输入。

    只存 `Φ` 与后验 `P`，不存先验 `P⁻`：段内 `dt` 恒定使 Q 是常量，后向递推里
    `P⁻_{k+1} = Φ_{k+1} P_k Φ_{k+1}ᵀ + Q` 重算一遍即可 —— 少存三分之一的内存
    （15×15×8 字节 × 每样本），且保证两处用的先验永远是同一个数。
    """

    #: 该段在整个序列里的半开区间。
    start: int
    end: int
    #: `(m, 15, 15)`。`phi[k]` 把样本 `k-1` 的误差推进到样本 `k`；`phi[0]` 恒为单位阵，
    #: 后向递推不会用到它。
    phi: np.ndarray
    #: `(m, 15, 15)`。每个样本**更新与重置之后**的后验协方差。
    covariance: np.ndarray
    #: `(m, 15)`。样本 `k` 上注入名义状态的修正量；无观测的样本为零。
    #: 后向平滑要把"晚到的修正"回传给更早的样本，这正是被回传的那个量。
    correction: np.ndarray
    #: 该段的离散过程噪声 Q（段内 dt 恒定，Q 是常量）。
    process_noise: np.ndarray


@dataclass(frozen=True)
class FilterHistory:
    """整个会话的前向滤波历史，按段组织。段与段之间不平滑 —— 与 `run_ins` 的
    跨段语义一致：空洞两侧的状态没有可信的动力学联系。"""

    segments: tuple[SegmentHistory, ...]

    @property
    def samples(self) -> int:
        return sum(item.end - item.start for item in self.segments)


def _process_noise(cfg: AlgoConfig, dt: float) -> np.ndarray:
    """离散过程噪声 Q。整体设计 §5.6.3。

    零偏项按一阶马尔可夫过程：`2σ²/τ · Δt`。位置项为零 —— 位置不是独立的随机过程，
    它的不确定度全部来自速度的积分，Q 里再加一份等于把同一个噪声算两遍。
    """
    return np.diag(
        np.concatenate(
            [
                np.full(3, cfg.eskf_gyro_noise_density**2 * dt),
                np.full(3, cfg.eskf_accel_noise_density**2 * dt),
                np.zeros(3),
                np.full(3, 2.0 * cfg.eskf_gyro_bias_instability**2 / cfg.eskf_gyro_bias_tau_s * dt),
                np.full(3, 2.0 * cfg.eskf_accel_bias_instability**2 / cfg.eskf_accel_bias_tau_s * dt),
            ]
        )
    )


@dataclass(frozen=True)
class _Workspace:
    """整段滤波里不变的量。

    `dt` 在一个连续段内是常数，于是 Q、单位阵、三种观测的雅可比与噪声矩阵也都是常数。
    把它们提到循环外不是微优化：10 分钟静置的验收算例要跑 120001 步，逐步重建这些
    矩阵占掉的时间比滤波本身还多。

    观测雅可比按 `(zupt, zaru)` 组合预先堆好并缓存 —— 它们只有四种可能，而每种在
    整段里一字不变。
    """

    cfg: AlgoConfig
    dt: float
    gravity: np.ndarray
    process_noise: np.ndarray
    identity: np.ndarray
    jacobians: dict[tuple[bool, bool], np.ndarray]
    noises: dict[tuple[bool, bool, bool], np.ndarray]

    @classmethod
    def build(cls, cfg: AlgoConfig, dt: float, gravity: np.ndarray) -> _Workspace:
        zupt = np.zeros((3, STATE_DIM))
        zupt[:, VELOCITY] = _EYE3
        zaru = np.zeros((3, STATE_DIM))
        # 估计的角速度是 ω_m - b̂g；真值取 0 时残差对 δbg 的偏导是 -I。
        zaru[:, GYRO_BIAS] = -_EYE3
        height = np.zeros((1, STATE_DIM))
        height[0, POSITION.start + 2] = 1.0
        use_height = cfg.eskf_enable_height_constraint

        jacobians: dict[tuple[bool, bool], np.ndarray] = {}
        noises: dict[tuple[bool, bool, bool], np.ndarray] = {}
        for has_zupt in (False, True):
            for has_zaru in (False, True):
                rows: list[np.ndarray] = []
                variances: list[float] = []
                if has_zupt:
                    rows.append(zupt)
                    variances.extend([cfg.eskf_zupt_sigma**2] * 3)
                if has_zaru:
                    rows.append(zaru)
                    variances.extend([cfg.eskf_zaru_sigma**2] * 3)
                if has_zupt and use_height:
                    rows.append(height)
                    variances.append(cfg.eskf_height_sigma**2)
                if not rows:
                    continue
                jacobians[(has_zupt, has_zaru)] = np.vstack(rows)
                base = np.array(variances)
                noises[(has_zupt, has_zaru, False)] = np.diag(base)
                noises[(has_zupt, has_zaru, True)] = np.diag(base * cfg.eskf_degraded_r_scale)

        return cls(
            cfg=cfg,
            dt=dt,
            gravity=gravity,
            process_noise=_process_noise(cfg, dt),
            identity=np.eye(STATE_DIM),
            jacobians=jacobians,
            noises=noises,
        )


def _transition(rotation: np.ndarray, omega: np.ndarray, force: np.ndarray, cfg: AlgoConfig, dt: float) -> np.ndarray:
    """误差状态转移矩阵 `Φ = I + F·Δt`。

    一阶离散化。200 Hz 下 `‖F‖·Δt` 的量级是 1e-2，二阶项 1e-4 —— 远小于 Q 本身的
    不确定度，做二阶只会增加出错面而不增加精度。
    """
    f = np.zeros((STATE_DIM, STATE_DIM))
    f[THETA, THETA] = -_skew(omega)
    f[THETA, GYRO_BIAS] = -_EYE3
    f[VELOCITY, THETA] = -rotation @ _skew(force)
    f[VELOCITY, ACCEL_BIAS] = -rotation
    f[POSITION, VELOCITY] = _EYE3
    f[GYRO_BIAS, GYRO_BIAS] = -_EYE3 / cfg.eskf_gyro_bias_tau_s
    f[ACCEL_BIAS, ACCEL_BIAS] = -_EYE3 / cfg.eskf_accel_bias_tau_s
    return np.eye(STATE_DIM) + f * dt


def _predict(
    state: FilterState, acc: np.ndarray, gyr: np.ndarray, workspace: _Workspace
) -> tuple[FilterState, np.ndarray]:
    """名义状态与协方差各推进一步。`acc`/`gyr` 是该区间起点的**原始**测量。

    同时返回本步的转移矩阵 `Φ`：RTS 的后向递推需要它，而它在这里本来就已算出，
    记录之外重算一遍等于把线性化点选错的机会开两次。
    """
    dt = workspace.dt
    omega = gyr - state.gyro_bias
    force = acc - state.accel_bias

    rotation_vector = omega * dt
    half = _exp_so3(0.5 * rotation_vector)
    # 同轴旋转可以直接平方，省掉第二次指数映射。
    rotation_next = state.rotation @ (half @ half)
    # 比力用**区间中点**姿态转换（与 `ins.propagate` 同一格式，见那里的文档）。
    acceleration = state.rotation @ half @ force + workspace.gravity

    transition = _transition(state.rotation, omega, force, workspace.cfg, dt)
    covariance = transition @ state.covariance @ transition.T + workspace.process_noise
    return (
        FilterState(
            rotation=rotation_next,
            velocity=state.velocity + acceleration * dt,
            position=state.position + state.velocity * dt + 0.5 * acceleration * dt * dt,
            gyro_bias=state.gyro_bias,
            accel_bias=state.accel_bias,
            covariance=covariance,
        ),
        transition,
    )


def _update(
    state: FilterState,
    observation: np.ndarray,
    jacobian: np.ndarray,
    noise: np.ndarray,
    identity: np.ndarray,
) -> FilterState:
    """`_update_with_correction` 的便捷形态，只要更新后的状态。"""
    updated, _ = _update_with_correction(state, observation, jacobian, noise, identity)
    return updated


def _update_with_correction(
    state: FilterState,
    observation: np.ndarray,
    jacobian: np.ndarray,
    noise: np.ndarray,
    identity: np.ndarray,
) -> tuple[FilterState, np.ndarray]:
    """一次卡尔曼更新 + 误差注入 + 重置，同时返回注入的修正量。

    修正量单独返回是给 RTS 平滑用的：注入之后误差状态清零，"滤波器在这一步学到了
    什么"只剩这个向量还记得。

    协方差用 Joseph 形式 `(I-KH)P(I-KH)ᵀ + KRKᵀ`。标准形式 `(I-KH)P` 在数值上会让 P
    慢慢失去对称与正定性，而 ZUPT 在 180 s 里要更新上万次 —— 那正是失对称会累积成
    发散的场景。Joseph 形式贵一次矩阵乘法，换来的是不用去查"滤波器为什么突然炸了"。
    """
    innovation_covariance = jacobian @ state.covariance @ jacobian.T + noise
    gain = np.linalg.solve(innovation_covariance, jacobian @ state.covariance).T
    correction = gain @ observation

    factor = identity - gain @ jacobian
    covariance = factor @ state.covariance @ factor.T + gain @ noise @ gain.T

    # 注入。姿态用右乘（局部误差表述，与 quaternion.integrate_angular_rate 一致）。
    delta_theta = correction[THETA]
    rotation = state.rotation @ _exp_so3(delta_theta)

    # 重置雅可比。误差被清零之后，剩余不确定度所在的坐标系跟着转了 δθ/2。
    # 这一项常被略去（δθ 通常 < 1e-3 rad），但它是三行代码，而略去它意味着协方差
    # 从此与状态所在的坐标系差一个小旋转 —— 一个不会报错、只会慢慢变糟的差。
    reset = identity.copy()
    reset[THETA, THETA] = _EYE3 - _skew(0.5 * delta_theta)
    covariance = reset @ covariance @ reset.T

    return (
        FilterState(
            rotation=rotation,
            velocity=state.velocity + correction[VELOCITY],
            position=state.position + correction[POSITION],
            gyro_bias=state.gyro_bias + correction[GYRO_BIAS],
            accel_bias=state.accel_bias + correction[ACCEL_BIAS],
            covariance=0.5 * (covariance + covariance.T),
        ),
        correction,
    )


def _residual(
    state: FilterState,
    gyr: np.ndarray,
    has_zupt: bool,
    has_zaru: bool,
    height_reference: float,
    workspace: _Workspace,
) -> np.ndarray:
    """该样本上成立的那几个观测的残差，顺序与 `_Workspace.jacobians` 一致。

    三个观测堆成一次更新而不是顺序做三次：顺序更新在数学上等价（噪声独立），但每次
    都要重新注入与重置一遍姿态，三次注入的小角度近似误差会叠起来。一次更新只注入一次。
    """
    parts: list[np.ndarray] = []
    if has_zupt:
        parts.append(-state.velocity)
    if has_zaru:
        parts.append(-(gyr - state.gyro_bias))
    if has_zupt and workspace.cfg.eskf_enable_height_constraint:
        parts.append(np.array([height_reference - state.position[2]]))
    return np.concatenate(parts)


def _run_segment(
    acc: np.ndarray,
    gyr: np.ndarray,
    dt: float,
    detection: StanceDetection,
    state: FilterState,
    cfg: AlgoConfig,
    gravity: np.ndarray,
    record: bool = False,
) -> tuple[dict[str, np.ndarray], FilterState]:
    """一个连续段的前向滤波。返回逐样本的名义状态与段末状态。

    `record=True` 时额外记录 RTS 平滑所需的历史（`phi`/`p_post`/`d` 三个键）。
    默认关闭：本地基础链不做平滑，逐样本存两个 15×15 矩阵（约 3.6 KB/样本）在
    3 分钟会话上是 130 MB 量级 —— 那是云端重算才付得起、也才值得付的账。
    """
    n = len(acc)
    rotations = np.empty((n, 3, 3))
    velocity = np.empty((n, 3))
    position = np.empty((n, 3))
    gyro_bias = np.empty((n, 3))
    accel_bias = np.empty((n, 3))
    phi_history = np.empty((n, STATE_DIM, STATE_DIM)) if record else None
    covariance_history = np.empty((n, STATE_DIM, STATE_DIM)) if record else None
    correction_history = np.zeros((n, STATE_DIM)) if record else None

    height_reference = float(state.position[2])
    stance_heights: list[float] = []
    was_stance = False

    workspace = _Workspace.build(cfg, dt, gravity)
    zupt_flags = detection.zupt
    zaru_flags = detection.zaru
    degraded_flags = detection.degraded

    for index in range(n):
        if index > 0:
            state, transition = _predict(state, acc[index - 1], gyr[index - 1], workspace)
            if phi_history is not None:
                phi_history[index] = transition
        elif phi_history is not None:
            phi_history[0] = workspace.identity

        is_stance = bool(zupt_flags[index])
        if is_stance and not was_stance:
            stance_heights = []
        if not is_stance and was_stance and stance_heights:
            # 支撑相结束：把这一相的平均高度作为下一相的参考。
            # 用"上一相的高度"而不是"始终为 0"，是为了容忍缓坡 —— 平地假设只需要
            # 相邻两相等高，不需要全程等高。
            height_reference = float(np.mean(stance_heights))
        was_stance = is_stance

        has_zaru = bool(zaru_flags[index])
        jacobian = workspace.jacobians.get((is_stance, has_zaru))
        if jacobian is not None:
            state, correction = _update_with_correction(
                state,
                _residual(state, gyr[index], is_stance, has_zaru, height_reference, workspace),
                jacobian,
                workspace.noises[(is_stance, has_zaru, bool(degraded_flags[index]))],
                workspace.identity,
            )
            if correction_history is not None:
                correction_history[index] = correction
        if is_stance:
            stance_heights.append(float(state.position[2]))

        rotations[index] = state.rotation
        velocity[index] = state.velocity
        position[index] = state.position
        gyro_bias[index] = state.gyro_bias
        accel_bias[index] = state.accel_bias
        if covariance_history is not None:
            covariance_history[index] = state.covariance

    output = {
        "rotation": rotations,
        "v": velocity,
        "p": position,
        "bg": gyro_bias,
        "ba": accel_bias,
    }
    if record:
        output["phi"] = phi_history
        output["p_post"] = covariance_history
        output["d"] = correction_history
    return output, state


def run_ins(
    series: FootSeries,
    cfg: AlgoConfig | None = None,
    *,
    alignment: Alignment | None = None,
    gravity: float = GRAVITY_STANDARD,
) -> NavResult:
    """契约 §4 的入口。整段会话前向滤波，返回 `NavResult`。

    ## 与云端精算链的关系

    本地基础链到此为止。云端完整链（RAY-227）从 `run_ins_with_history` 拿到同一次
    前向滤波外加逐样本历史，再做 RTS 后向平滑（`core/rts.py`）—— 两条链共用这个
    内核（整体设计 §0.2 的第 2 条取舍），差别只在是否保留历史与后续处理。

    ## 分段

    `series.segments` 的每一段独立滤波。段与段之间**不积分** —— 空洞跨越的时间未知，
    强行续算会产出一段看似连续、实际凭空发明的轨迹（PRD §6.1：空洞 > 3 样本切分数据段，
    不插值续算）。

    跨段携带的是姿态与两个零偏（它们随时间缓变，一个空洞改不了多少），**速度归零、
    速度与位置的协方差恢复到初值**：空洞之后受试者的速度是未知的，假装知道会让第一个
    ZUPT 观测被一个过分自信的先验压住。位置从上一段的末值继续，但那只是为了让轨迹
    数组连得上，**不构成对连续性的声明** —— 报告层要把段边界显式画出来。

    要求 `segments` 覆盖整个序列。有样本不属于任何段时拒绝而不是猜：那种情况下该填
    什么由 RAY-210 定义，在它定义之前，发明一个填法比停下来更糟。

    ## 只做前向

    PRD §6.1 的本地基础报告走的就是前向链；后向平滑只在云端链里发生，且发生在
    `core/rts.py`，不在这里。
    """
    navigation, _ = _run(series, cfg, alignment=alignment, gravity=gravity, record=False)
    return navigation


def run_ins_with_history(
    series: FootSeries,
    cfg: AlgoConfig | None = None,
    *,
    alignment: Alignment | None = None,
    gravity: float = GRAVITY_STANDARD,
) -> tuple[NavResult, FilterHistory]:
    """与 `run_ins` 同一次前向滤波，额外返回 RTS 平滑所需的逐样本历史。

    云端精算链（RAY-227）的入口。**不改变前向结果**：`NavResult` 与 `run_ins`
    在数值上逐位相同 —— 记录历史是旁路，不是分叉。分叉意味着两条链的"前向部分"
    可以悄悄漂开，那正是端云同构红线要防的事。

    内存量级：每样本约 3.7 KB（两个 15×15 float64 + 一个 15 向量），200 Hz 下
    3 分钟单足约 130 MB。这是云端进程的账，不要在采集端调它。
    """
    navigation, history = _run(series, cfg, alignment=alignment, gravity=gravity, record=True)
    assert history is not None
    return navigation, history


def _run(
    series: FootSeries,
    cfg: AlgoConfig | None,
    *,
    alignment: Alignment | None,
    gravity: float,
    record: bool,
) -> tuple[NavResult, FilterHistory | None]:
    cfg = cfg or AlgoConfig()
    if not isinstance(series, FootSeries):
        raise EskfError(f"series 必须是 FootSeries，收到 {type(series).__name__}")

    n = len(series.t)
    covered = sum(end - start for start, end in series.segments)
    if covered != n or (series.segments and series.segments[0][0] != 0):
        raise EskfError(
            f"segments 必须覆盖整个序列：{n} 个采样，段内共 {covered} 个。"
            "有样本不属于任何有效段时，该填什么由 RAY-210（空洞切分）定义 —— "
            "在它定义之前，发明一个填法比停下来更糟。"
        )

    dt = 1.0 / series.fs
    gravity_n = gravity_vector(gravity)

    rotations = np.empty((n, 3, 3))
    velocity = np.empty((n, 3))
    position = np.empty((n, 3))
    gyro_bias = np.empty((n, 3))
    accel_bias = np.empty((n, 3))
    zupt = np.zeros(n, dtype=bool)
    degraded = np.zeros(n, dtype=bool)
    score = np.zeros(n)

    state: FilterState | None = None
    initial_covariance: np.ndarray | None = None
    histories: list[SegmentHistory] = []
    for start, end in series.segments:
        acc = series.acc[start:end]
        gyr = series.gyr[start:end]
        detection = detect_stance(acc, gyr, series.fs, cfg, gravity=gravity)

        if state is None:
            resolved = alignment or initial_alignment(acc, gyr, series.fs, cfg, gravity=gravity)
            state = FilterState.initial(resolved, cfg)
            initial_covariance = state.covariance
        else:
            # 跨段：姿态与零偏留下，速度归零，速度/位置协方差恢复到初值。
            assert initial_covariance is not None
            covariance = state.covariance.copy()
            covariance[VELOCITY, VELOCITY] = initial_covariance[VELOCITY, VELOCITY]
            covariance[POSITION, POSITION] = initial_covariance[POSITION, POSITION]
            state = replace(state, velocity=np.zeros(3), covariance=covariance)

        segment, state = _run_segment(acc, gyr, dt, detection, state, cfg, gravity_n, record=record)
        rotations[start:end] = segment["rotation"]
        velocity[start:end] = segment["v"]
        position[start:end] = segment["p"]
        gyro_bias[start:end] = segment["bg"]
        accel_bias[start:end] = segment["ba"]
        zupt[start:end] = detection.zupt
        degraded[start:end] = detection.degraded
        score[start:end] = detection.score
        if record:
            histories.append(
                SegmentHistory(
                    start=start,
                    end=end,
                    phi=segment["phi"],
                    covariance=segment["p_post"],
                    correction=segment["d"],
                    process_noise=_process_noise(cfg, dt),
                )
            )

    # 循环里传的是旋转矩阵，到这里才一次性转成契约要求的四元数 —— 批量转换是
    # 向量化的，逐步转换不是。
    attitude = quat.from_matrix(rotations)
    navigation = NavResult(
        t=np.asarray(series.t, dtype=np.float64),
        q=attitude,
        v=velocity,
        p=position,
        bg=gyro_bias,
        ba=accel_bias,
        zupt=zupt,
        stances=_runs(zupt),
        degraded=degraded,
        score=score,
    )
    return navigation, FilterHistory(segments=tuple(histories)) if record else None


def _runs(mask: np.ndarray) -> list[tuple[int, int]]:
    """布尔掩码里的连续 True 区间，半开。与 `core/zupt.py` 的同名函数同形。"""
    if not mask.any():
        return []
    padded = np.concatenate(([False], mask, [False]))
    edges = np.flatnonzero(padded[1:] != padded[:-1])
    return [(int(start), int(end)) for start, end in zip(edges[::2], edges[1::2], strict=True)]
