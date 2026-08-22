"""捷联惯导机械编排。契约 §1 的 `core/ins.py`（F4.1）。整体设计 §5.4。

## 这个模块单独用是没有意义的，这正是它值得单独存在的原因

纯 INS 积分的误差随时间三次方发散 —— 整体设计 §5.4 的原话是"1 秒内位置误差就能到
米级"。所以本模块的输出**永远不是最终结果**，它是 ESKF（RAY-204）每一步的预测项。

把它与滤波器分开写，是为了让"积分本身对不对"能被独立回答。两者混在一个函数里时，
一个错误的结果有两个可能的来源，而排查滤波器比排查积分器贵得多。这里的测试因此对着
**解析解**断言：常角速度 + 常体轴比力的组合有闭式积分，误差可以量化到具体数量级，
而不是"看起来合理"。

## 三个约定，错一个都不报错

1. **`acc` 是比力（specific force），不是加速度。**
   加速度计测的是 `f = a - g`。静止时 `a = 0`，于是 ENU 下 `f_n = (0, 0, +g)`：
   一个静止平放的模块，z 轴读数是 **+9.8 而不是 0，更不是 -9.8**。
   导航方程因此是 `a_n = C_f^n · f_f + g_n`，其中 `g_n = (0, 0, -g)`。
   这两个符号里任何一个反了，静止的模块都会以 2g 的加速度"掉下去"或"飞上天"——
   数值大得一眼能看出来。真正危险的是**只有一个反**在某些标定组合下部分抵消，
   表现为轨迹缓慢下沉。所以有一条测试专门断言"静止 10 秒不动"。

2. **`gyr` 是 rad/s**（契约 R2，见 `contracts.FIELD_UNITS`）。deg/s 差 57.3 倍。

3. **导航系是 ENU**（整体设计 §1.3），z 轴向上。`gravity` 参数传的是**重力大小**
   （正数），不是重力矢量 —— 让调用方构造矢量就是把上面那个符号问题交给每个调用点
   各自去错一次。

## 忽略了什么，为什么

地球自转（15°/h）与哥氏项在步态场景下的量级远低于传感器噪声：BS-BT91 的陀螺零偏
不稳定性就已经是它的若干倍，加计零偏 ±20~40 mg 更是首要误差源（见《BS-BT91 硬件
适配》发现 1）。补偿一个比噪声小两个数量级的项，只会增加出错面。

不做圆锥/划桨补偿（等效旋转矢量二子样以上）：整体设计 §5.4 明确"行走场景一阶即可"。
跑步场景需要时，替换的是 `quaternion.integrate_angular_rate` 一个函数，不是这里的
结构。

## 数值积分的阶

* 姿态：区间内 `ω` 恒定的前提下**精确**（旋转矢量取指数，不是一阶近似）。
* 速度/位置：用**区间中点姿态**转换比力，即中点法，全局二阶。
  代价只是每步多算一次半角指数。测试不只断言绝对误差，还断言 **dt 减半误差降到
  约 1/4** —— 一个绝对误差阈值可以被任何巧合满足，收敛阶不行。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

import numpy as np

from gait.core import quaternion as quat

#: 标准重力，m/s²。CGPM 定义值。
GRAVITY_STANDARD: Final[float] = 9.80665

#: WGS-84 椭球上的正常重力公式（Somigliana）系数。
_SOMIGLIANA_EQUATOR: Final[float] = 9.7803253359
_SOMIGLIANA_K: Final[float] = 0.00193185265241
_SOMIGLIANA_E2: Final[float] = 0.00669437999013

#: 自由空气梯度，m/s² 每米高度。海拔每升高 1 km，重力小约 3.1e-3 m/s²。
_FREE_AIR_GRADIENT: Final[float] = 3.086e-6


class InsError(ValueError):
    """机械编排的输入非法。"""


def gravity_magnitude(latitude_deg: float, altitude_m: float = 0.0) -> float:
    """按纬度与海拔给出当地重力大小，m/s²。

    整体设计 §5.4："重力值用当地实测或按纬度模型取，误差 0.1% 即引入约 0.01 m/s²
    的速度积分漂移。" 0.1% 听起来无关紧要，但赤道与极地之间的实际差异就是 0.5%，
    直接用 9.80665 在低纬度会引入约 0.026 m/s² 的系统性偏差 —— 那不是随机噪声，
    ZUPT 也压不掉它在支撑相之间的累积。

    这个函数不猜纬度。没有它就用 `GRAVITY_STANDARD`，并且这件事会随
    `algo_params` 进会话元数据，三个月后能查清当时用的是哪个值。
    """
    if not -90.0 <= latitude_deg <= 90.0:
        raise InsError(f"纬度应在 [-90, 90] 内，收到 {latitude_deg}")
    sin_squared = np.sin(np.radians(latitude_deg)) ** 2
    normal = (
        _SOMIGLIANA_EQUATOR
        * (1.0 + _SOMIGLIANA_K * sin_squared)
        / np.sqrt(1.0 - _SOMIGLIANA_E2 * sin_squared)
    )
    return float(normal - _FREE_AIR_GRADIENT * altitude_m)


def gravity_vector(magnitude: float = GRAVITY_STANDARD) -> np.ndarray:
    """ENU 导航系下的重力矢量：`(0, 0, -g)`。

    只有这一处构造它。见模块文档第 3 条。
    """
    if not magnitude > 0:
        raise InsError(f"重力大小必须为正，收到 {magnitude}")
    return np.array([0.0, 0.0, -float(magnitude)])


def intervals_from_time(t: np.ndarray) -> np.ndarray:
    """由时间轴给出 `(n-1,)` 的采样间隔，并要求严格递增。

    `FootSeries.t` 来自同步层构建的统一时基，理论上必然递增。但空洞切分、时基
    回归、以及未来的会话拼接都有让它退化的可能，而一个非正的 `dt` 会让积分沿时间
    倒着走 —— 那不会报错，只会产生一段方向相反的轨迹。在入口处拒绝比在下游猜便宜。
    """
    time = np.asarray(t, dtype=np.float64)
    if time.ndim != 1:
        raise InsError(f"t 应为一维，收到 shape={time.shape}")
    if time.size < 2:
        raise InsError(f"t 至少需要 2 个采样才能构成积分区间，收到 {time.size} 个")
    dt = np.diff(time)
    if not np.all(dt > 0):
        bad = int(np.argmin(dt))
        raise InsError(
            f"时间轴必须严格递增：t[{bad}]={time[bad]} 与 t[{bad + 1}]={time[bad + 1]} "
            f"给出 dt={dt[bad]}。非正的 dt 会让积分沿时间倒着走，且不会报错。"
        )
    return dt


@dataclass(frozen=True)
class InsState:
    """某一时刻的导航状态。

    frozen：`propagate` 返回新状态而不是就地修改。ESKF 需要保留上一步的状态做误差
    注入，而一个可变的状态对象会让"这是哪一步的值"变成一个需要追踪的问题。
    """

    q: np.ndarray  # (4,) 足部系 → 导航系
    v: np.ndarray  # (3,) m/s，导航系
    p: np.ndarray  # (3,) m，导航系

    def __post_init__(self) -> None:
        if np.asarray(self.q).shape != (4,):
            raise InsError(f"q 应为 (4,)，收到 shape={np.asarray(self.q).shape}")
        for name in ("v", "p"):
            value = np.asarray(getattr(self, name))
            if value.shape != (3,):
                raise InsError(f"{name} 应为 (3,)，收到 shape={value.shape}")


def propagate(
    state: InsState,
    acc: np.ndarray,
    gyr: np.ndarray,
    dt: float,
    gravity: np.ndarray,
) -> InsState:
    """把状态推进一个采样区间。`acc`/`gyr` 是该区间**起点**采样的测量值。

    `gravity` 是导航系重力矢量，由 `gravity_vector()` 给出 —— 传大小还是矢量在这一
    层不再有选择余地，因为 ESKF 会以同样的形式反复调用它。
    """
    if not dt > 0:
        raise InsError(f"dt 必须为正，收到 {dt}")
    specific_force = np.asarray(acc, dtype=np.float64)
    omega = np.asarray(gyr, dtype=np.float64)

    rotation_vector = omega * dt
    q_next = quat.normalize(quat.multiply(state.q, quat.from_rotation_vector(rotation_vector)))
    # 中点姿态：把比力转到导航系时用区间中点的姿态，而不是起点的。见模块文档。
    q_mid = quat.normalize(quat.multiply(state.q, quat.from_rotation_vector(0.5 * rotation_vector)))

    acceleration = quat.rotate(q_mid, specific_force) + gravity
    v_next = state.v + acceleration * dt
    # 位置用 p + v·dt + ½·a·dt²，不是 p + v_next·dt。后者在恒定加速度下有
    # ½·a·dt² 的每步偏差，200 Hz 下看着很小，36000 步之后不是。
    p_next = state.p + state.v * dt + 0.5 * acceleration * dt * dt
    return InsState(q=q_next, v=v_next, p=p_next)


def mechanize(
    acc: np.ndarray,
    gyr: np.ndarray,
    dt: float | np.ndarray,
    *,
    q0: np.ndarray,
    v0: np.ndarray | None = None,
    p0: np.ndarray | None = None,
    gravity: float = GRAVITY_STANDARD,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """整段序列的机械编排，返回 `(q, v, p)`，形状 `(n,4)`、`(n,3)`、`(n,3)`。

    ## 输出与采样一一对应，这是刻意的

    `q[k]` 是**第 k 个采样时刻**的姿态，`q[0] = q0`。第 k 步的状态由第 k-1 个采样的
    测量值推进而来，因此**最后一个采样的 `acc`/`gyr` 不参与任何计算** —— 它之后没有
    区间。这不是遗漏：契约 §3.3 的 `NavResult` 各数组与 `FootSeries.t` 等长，若把状态
    定义成"积分完第 k 个测量之后"，输出就会比时间轴长一个，或者要凭空丢掉一个采样。

    ## `dt`

    标量表示等间隔；也可传 `(n-1,)` 数组以支持不等间隔（空洞切分之后各段内部仍然
    等间隔，但段与段之间不是）。由 `FootSeries.t` 取间隔请用 `intervals_from_time`。

    ## 不接受 `FootSeries`

    参数是裸数组而不是契约结构。`run_ins(series, cfg) -> NavResult`（契约 §4）属于
    RAY-204 的 ESKF：那一层才知道零速、协方差与降级标记，才填得满 `NavResult`。
    本函数若提前吃下 `FootSeries`，就得返回一个大半字段为空的 `NavResult`，而一个
    半空的契约结构比裸数组更容易被误用。
    """
    specific_force = np.asarray(acc, dtype=np.float64)
    omega = np.asarray(gyr, dtype=np.float64)
    for name, value in (("acc", specific_force), ("gyr", omega)):
        if value.ndim != 2 or value.shape[1] != 3:
            raise InsError(f"{name} 应为 (n, 3)，收到 shape={value.shape}")
    if specific_force.shape != omega.shape:
        raise InsError(
            f"acc 与 gyr 的样本数必须一致：{specific_force.shape} vs {omega.shape}"
        )
    n = specific_force.shape[0]
    if n == 0:
        raise InsError("空序列无法积分")

    steps = _as_steps(dt, n)
    g = gravity_vector(gravity)

    initial_attitude = np.asarray(q0, dtype=np.float64)
    if initial_attitude.shape != (4,):
        raise InsError(f"q0 应为 (4,)，收到 shape={initial_attitude.shape}")
    q = np.empty((n, 4), dtype=np.float64)
    q[0] = quat.normalize(initial_attitude)

    # 姿态是唯一无法整体向量化的部分：四元数乘积前后有依赖。
    #
    # 但循环体不必是一次四元数乘法。`multiply(q, d)` 对 q 是线性的，所以对固定的 d
    # 存在一个 4×4 矩阵 M(d) 使 `M(d) @ q == multiply(q, d)`，而 M 的第 i 列就是
    # `multiply(e_i, d)`。于是所有 M 可以一次性向量化算出，循环里只剩一次矩阵乘向量
    # —— 36000 个采样下比逐步调用 `multiply` 快近一个数量级。
    #
    # 关键在于 **M 是从 `multiply` 推出来的，不是照公式重写一遍**。Hamilton 乘积在
    # 本仓库里只有一处实现；这里若抄一份，模块文档开头警告的那种"约定悄悄分叉"就
    # 从这里开始。
    rotation_vectors = omega[:-1] * steps[:, None]
    deltas = quat.from_rotation_vector(rotation_vectors)
    half_deltas = quat.from_rotation_vector(0.5 * rotation_vectors)
    basis = np.eye(4)
    right_multiply = np.stack([quat.multiply(basis[i], deltas) for i in range(4)], axis=-1)
    for k in range(n - 1):
        # 每步归一化（整体设计 §5.4 明确要求）。M 在 d 为单位四元数时是正交阵，
        # 理论上范数自守，但 36000 步的舍入不该靠"理论上"来管。
        advanced = right_multiply[k] @ q[k]
        q[k + 1] = advanced / np.sqrt(advanced @ advanced)

    # 剩下的全部可以向量化：中点姿态只依赖已经算完的 q[k]。
    q_mid = quat.normalize(quat.multiply(q[:-1], half_deltas))
    acceleration = quat.rotate(q_mid, specific_force[:-1]) + g

    initial_velocity = _as_initial(v0, "v0")
    initial_position = _as_initial(p0, "p0")
    v = np.empty((n, 3), dtype=np.float64)
    p = np.empty((n, 3), dtype=np.float64)
    v[0] = initial_velocity
    p[0] = initial_position

    delta_v = acceleration * steps[:, None]
    v[1:] = v[0] + np.cumsum(delta_v, axis=0)
    # p 的每步增量用**区间起点**的速度：p += v[k]·dt + ½·a·dt²。
    # v[:-1] 正是各区间起点的速度，所以这里不能写成 v[1:]。
    delta_p = v[:-1] * steps[:, None] + 0.5 * acceleration * (steps[:, None] ** 2)
    p[1:] = p[0] + np.cumsum(delta_p, axis=0)

    return q, v, p


def _as_initial(value: np.ndarray | None, name: str) -> np.ndarray:
    """`v0` / `p0` 的默认值与形状校验。省略即原点、静止。"""
    if value is None:
        return np.zeros(3, dtype=np.float64)
    array = np.asarray(value, dtype=np.float64)
    if array.shape != (3,):
        raise InsError(f"{name} 应为 (3,)，收到 shape={array.shape}")
    return array


def _as_steps(dt: float | np.ndarray, n: int) -> np.ndarray:
    """把标量或数组形式的 `dt` 归一成 `(n-1,)` 的正数组。"""
    steps = np.asarray(dt, dtype=np.float64)
    if steps.ndim == 0:
        steps = np.full(n - 1, float(steps))
    elif steps.shape != (n - 1,):
        raise InsError(
            f"dt 为数组时应有 n-1 = {n - 1} 个元素（区间数比采样数少一个），"
            f"收到 shape={steps.shape}"
        )
    if steps.size and not np.all(steps > 0):
        raise InsError("dt 必须全为正 —— 非正的间隔会让积分沿时间倒着走且不报错")
    return steps
