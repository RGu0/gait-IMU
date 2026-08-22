"""四元数与旋转工具。契约 §1 的 `core/quaternion.py`。

## 这个模块存在的理由是「约定只有一处」

RAY-202（初始对准）、RAY-203（零速检测的角速度判据）、RAY-204（ESKF 的姿态误差
注入）、RAY-205（双足约束的横向位置符号）都要做同一件事：把一个方向从足部系转到
导航系，或反过来。如果各写一份，四处的约定迟早对不上。

而**姿态约定对不上不会报错**。它的症状是轨迹缓慢弯曲、步长系统性偏小百分之几、
左右脚横向位置符号反了 —— 全都是"看起来像标定没做好"的样子。这类 bug 的定位成本
远高于写这个模块的成本，所以它先于任何算法存在。

## 四条写死的约定

1. **分量顺序标量在前**：`q = [w, x, y, z]`。
   注意 `scipy.spatial.transform.Rotation` 用的是标量在后 `[x, y, z, w]`。本仓库不依赖
   scipy（`pyproject.toml` 的运行时依赖只有 numpy 与 wt901），但迟早有人会拿 scipy 交叉
   验证 —— 顺序反了不会抛异常，只会得到一个错误的旋转。因此 `QUATERNION_LAYOUT`
   是可断言的声明，`FIELD_UNITS` 之于单位是什么，它之于分量顺序就是什么。

2. **Hamilton 约定**（而非 JPL）：`ij = k`。两者的乘法差一个符号，复合旋转的顺序因而
   相反。

3. **`q` 表示足部系 → 导航系**，与契约 §3.3 `NavResult.q` 的注释一致：

       v_n = q ⊗ [0, v_f] ⊗ q*

   `rotate()` 走这个方向，`rotate_inverse()` 走反向。**没有一个不带方向的
   `apply()`** —— 一个方向不明的旋转函数是上面那类 bug 的主要来源。

4. **导航系是 ENU**（东-北-天），来自整体设计 §1.3。这里不直接用到，但欧拉角的解读
   依赖它，所以写在同一处。

## 小角度分支不是可选的优化

200 Hz 下每个采样的旋转角约 `ω·dt`，走路时 ω 峰值约 6 rad/s，即 0.03 rad；支撑相里
则接近 0。`sin(θ/2)/θ` 在 θ→0 时是 0/0，直接算会得到 nan —— 而 nan 一旦进入姿态，
整条轨迹后续全是 nan。所以泰勒展开分支是正确性的一部分，不是性能优化。

## 全部函数支持 `(..., 4)` 批量

契约里 `NavResult.q` 是 `(n, 4)`，一次会话 180 s × 200 Hz = 36000 个四元数。逐个用
Python 循环转换会成为报告生成的瓶颈，而 numpy 的广播是免费的。
"""

from __future__ import annotations

from typing import Final

import numpy as np

#: 分量顺序的可断言声明。见模块文档第 1 条。
QUATERNION_LAYOUT: Final[str] = "wxyz"

#: 旋转方向的可断言声明：`rotate()` 把向量从足部系转到导航系。
ROTATION_SENSE: Final[str] = "foot_to_nav"

#: 欧拉角序列。`from_euler` / `to_euler` 用 R = Rz(yaw) · Ry(pitch) · Rx(roll)，
#: 即"先绕自身 x 转 roll，再绕自身 y 转 pitch，最后绕自身 z 转 yaw"的内旋 ZYX。
#: 航空航天惯例，也是整体设计 §5.3 谈 roll/pitch/yaw 时默认的那一个。
EULER_SEQUENCE: Final[str] = "ZYX"

#: 低于这个旋转角就走泰勒展开。取 1e-8 而非更小：float64 下 θ² ≈ 1e-16 已经是
#: eps 量级，再小的分支没有意义；再大则展开的截断误差开始可见。
_SMALL_ANGLE: Final[float] = 1e-8


class QuaternionError(ValueError):
    """四元数的形状或取值非法。

    与 `contracts.ContractError` 分开：那是跨层数据契约被违反，这是一次调用里的
    参数错误，两者的排查方向不同。
    """


def _as_quaternion(q: np.ndarray, name: str = "q") -> np.ndarray:
    """校验并转成 float64 的 `(..., 4)`。"""
    array = np.asarray(q, dtype=np.float64)
    if array.ndim == 0 or array.shape[-1] != 4:
        raise QuaternionError(
            f"{name} 的最后一维应为 4（{QUATERNION_LAYOUT}），收到 shape={array.shape}"
        )
    return array


def _as_vector(v: np.ndarray, name: str = "v") -> np.ndarray:
    """校验并转成 float64 的 `(..., 3)`。"""
    array = np.asarray(v, dtype=np.float64)
    if array.ndim == 0 or array.shape[-1] != 3:
        raise QuaternionError(f"{name} 的最后一维应为 3，收到 shape={array.shape}")
    return array


def identity(shape: tuple[int, ...] = ()) -> np.ndarray:
    """单位四元数，`shape + (4,)`。"""
    out = np.zeros(shape + (4,), dtype=np.float64)
    out[..., 0] = 1.0
    return out


def normalize(q: np.ndarray) -> np.ndarray:
    """单位化。零范数即报错，不静默返回单位四元数。

    一个范数为零的四元数只会来自未初始化的内存或已经发散的滤波器。把它当成"没有
    旋转"会让发散继续传播，直到某个远处的指标看起来只是有点不对 —— 而当场报错能
    把现场留在出问题的地方。
    """
    array = _as_quaternion(q)
    norm = np.linalg.norm(array, axis=-1, keepdims=True)
    if np.any(norm < _SMALL_ANGLE):
        raise QuaternionError("四元数范数为零，无法单位化 —— 上游可能已经发散")
    return array / norm


def conjugate(q: np.ndarray) -> np.ndarray:
    """共轭。对单位四元数它就是逆旋转。"""
    array = _as_quaternion(q)
    out = array.copy()
    out[..., 1:] *= -1.0
    return out


def multiply(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Hamilton 乘积 `a ⊗ b`，按 numpy 规则广播。

    含义是**先转 b 再转 a**：若 `a` 是 f→n、`b` 是 g→f，则 `a ⊗ b` 是 g→n。
    这个顺序与矩阵乘法一致，也是 `integrate_angular_rate` 里 `q ⊗ dq` 表示"在
    自身坐标系里再转一点"的原因。
    """
    left = _as_quaternion(a, "a")
    right = _as_quaternion(b, "b")
    aw, ax, ay, az = (left[..., i] for i in range(4))
    bw, bx, by, bz = (right[..., i] for i in range(4))
    return np.stack(
        (
            aw * bw - ax * bx - ay * by - az * bz,
            aw * bx + ax * bw + ay * bz - az * by,
            aw * by - ax * bz + ay * bw + az * bx,
            aw * bz + ax * by - ay * bx + az * bw,
        ),
        axis=-1,
    )


def rotate(q: np.ndarray, v: np.ndarray) -> np.ndarray:
    """把向量由**足部系转到导航系**：`v_n = q ⊗ [0, v_f] ⊗ q*`。

    实现用的是 Rodrigues 展开而不是两次四元数乘法：结果相同，但少一半运算，且
    不需要为标量分量分配中间数组 —— 36000 个采样各转一次时这不是无谓的讲究。

    先单位化再转。Rodrigues 展开只在单位四元数下等于旋转；喂进一个范数 1.01 的
    四元数不会报错，只会把向量额外放大约 2%，而"轨迹整体偏长 2%"看起来正是标定
    没做好的样子。滤波器每步都归一化，所以这一次归一化通常是恒等的 —— 它防的是
    那些不经滤波器直接构造四元数的调用点。
    """
    quaternion = normalize(q)
    vector = _as_vector(v)
    w = quaternion[..., :1]
    u = quaternion[..., 1:]
    cross_uv = np.cross(u, vector)
    return (
        vector
        + 2.0 * w * cross_uv
        + 2.0 * np.cross(u, cross_uv)
    )


def rotate_inverse(q: np.ndarray, v: np.ndarray) -> np.ndarray:
    """把向量由**导航系转回足部系**。`rotate` 的逆。

    单独一个函数而不是让调用方写 `rotate(conjugate(q), v)`：后者能写对，但读代码
    的人要在脑子里再走一遍方向，而方向正是这个模块要消灭的歧义。
    """
    return rotate(conjugate(q), v)


def to_matrix(q: np.ndarray) -> np.ndarray:
    """四元数 → 旋转矩阵 `C_f^n`，`(..., 3, 3)`。

    列是足部系三轴在导航系中的表示，因此 `C @ v_f == rotate(q, v_f)`。
    """
    quaternion = normalize(q)
    w, x, y, z = (quaternion[..., i] for i in range(4))
    return np.stack(
        (
            np.stack((1 - 2 * (y * y + z * z), 2 * (x * y - w * z), 2 * (x * z + w * y)), axis=-1),
            np.stack((2 * (x * y + w * z), 1 - 2 * (x * x + z * z), 2 * (y * z - w * x)), axis=-1),
            np.stack((2 * (x * z - w * y), 2 * (y * z + w * x), 1 - 2 * (x * x + y * y)), axis=-1),
        ),
        axis=-2,
    )


def from_matrix(matrix: np.ndarray) -> np.ndarray:
    """旋转矩阵 → 四元数，`(..., 4)`，标量分量取非负。

    用 Shepperd 的分支法：按迹与三个对角元中最大的那个决定用哪条公式。
    单一公式（例如总是 `w = sqrt(1+trace)/2`）在旋转角接近 180° 时 `1+trace → 0`，
    相对误差爆掉 —— 而 180° 附近正是"戴反了"这类情形会出现的地方。
    """
    m = np.asarray(matrix, dtype=np.float64)
    if m.ndim < 2 or m.shape[-2:] != (3, 3):
        raise QuaternionError(f"旋转矩阵的最后两维应为 (3, 3)，收到 shape={m.shape}")

    m00, m11, m22 = m[..., 0, 0], m[..., 1, 1], m[..., 2, 2]
    trace = m00 + m11 + m22

    # 四条候选各自在对应分量最大时数值最稳，逐条算完再按判据选。全部算一遍看似
    # 浪费，但让整个函数保持无分支的数组语义 —— 批量输入下 Python 层的逐元素
    # 分支才是真正的代价。
    candidates = np.stack(
        (
            _branch_trace(m, trace),
            _branch_diagonal(m, 0),
            _branch_diagonal(m, 1),
            _branch_diagonal(m, 2),
        ),
        axis=-2,
    )
    which = np.where(
        trace > 0,
        0,
        np.where(np.logical_and(m00 >= m11, m00 >= m22), 1, np.where(m11 >= m22, 2, 3)),
    )
    picked = np.take_along_axis(candidates, which[..., None, None], axis=-2)[..., 0, :]
    # w 与 -w 表示同一个旋转。固定取 w ≥ 0，好让"两个四元数是否相等"这件事有
    # 唯一答案 —— 否则测试与去重都得每次考虑双覆盖。
    sign = np.where(picked[..., :1] < 0, -1.0, 1.0)
    return normalize(picked * sign)


def _branch_trace(m: np.ndarray, trace: np.ndarray) -> np.ndarray:
    s = np.sqrt(np.maximum(trace + 1.0, 0.0)) * 2.0
    s = np.where(s < _SMALL_ANGLE, 1.0, s)
    return np.stack(
        (
            0.25 * s,
            (m[..., 2, 1] - m[..., 1, 2]) / s,
            (m[..., 0, 2] - m[..., 2, 0]) / s,
            (m[..., 1, 0] - m[..., 0, 1]) / s,
        ),
        axis=-1,
    )


def _branch_diagonal(m: np.ndarray, axis: int) -> np.ndarray:
    """对角分支：`axis` 是 x/y/z 中被认为最大的那个分量。"""
    i, j, k = axis, (axis + 1) % 3, (axis + 2) % 3
    s = np.sqrt(np.maximum(1.0 + m[..., i, i] - m[..., j, j] - m[..., k, k], 0.0)) * 2.0
    s = np.where(s < _SMALL_ANGLE, 1.0, s)
    parts = [
        (m[..., k, j] - m[..., j, k]) / s,  # w
        np.zeros_like(s),
        np.zeros_like(s),
        np.zeros_like(s),
    ]
    parts[1 + i] = 0.25 * s
    parts[1 + j] = (m[..., j, i] + m[..., i, j]) / s
    parts[1 + k] = (m[..., k, i] + m[..., i, k]) / s
    return np.stack(parts, axis=-1)


def from_rotation_vector(rotation_vector: np.ndarray) -> np.ndarray:
    """旋转矢量（轴 × 角，rad）→ 四元数。指数映射。

    这是姿态积分的入口：一个采样区间内的旋转就是 `ω · Δt`。小角度分支见模块文档。
    """
    rv = _as_vector(rotation_vector, "rotation_vector")
    theta = np.linalg.norm(rv, axis=-1, keepdims=True)
    half = 0.5 * theta
    # sin(θ/2)/θ 在 θ→0 时是 0/0。展开到二阶：1/2 - θ²/48。
    # np.where 的两个分支都会被求值，所以除数要先换掉，否则 0 除仍会产出 nan
    # 并触发警告 —— nan 一旦进姿态，后面整条轨迹都是 nan。
    safe = np.where(theta < _SMALL_ANGLE, 1.0, theta)
    scale = np.where(
        theta < _SMALL_ANGLE,
        0.5 - theta * theta / 48.0,
        np.sin(half) / safe,
    )
    return np.concatenate((np.cos(half), rv * scale), axis=-1)


def to_rotation_vector(q: np.ndarray) -> np.ndarray:
    """四元数 → 旋转矢量。对数映射，`from_rotation_vector` 的逆。

    结果取**最短旋转**（角度落在 [0, π]）：`q` 与 `-q` 是同一个旋转，不归一化的话
    同一个姿态会给出相差 2π 的两个矢量，而 ESKF 的误差注入按小量处理，一个 2π
    的"小量"会立刻毁掉滤波器。
    """
    quaternion = normalize(q)
    # 强制 w ≥ 0 后 2·atan2(|u|, w) 自然落在 [0, π]。
    sign = np.where(quaternion[..., :1] < 0, -1.0, 1.0)
    quaternion = quaternion * sign
    w = quaternion[..., :1]
    u = quaternion[..., 1:]
    norm_u = np.linalg.norm(u, axis=-1, keepdims=True)
    theta = 2.0 * np.arctan2(norm_u, w)
    safe = np.where(norm_u < _SMALL_ANGLE, 1.0, norm_u)
    # θ/sin(θ/2) 在 θ→0 时 → 2。展开：2 + θ²/12，用 |u| 表达即 2·(1 + |u|²/6)/w。
    scale = np.where(norm_u < _SMALL_ANGLE, 2.0 / np.where(w < _SMALL_ANGLE, 1.0, w), theta / safe)
    return u * scale


def integrate_angular_rate(q: np.ndarray, omega: np.ndarray, dt: float | np.ndarray) -> np.ndarray:
    """用足部系角速度把姿态推进 `dt`：`q ⊗ exp(½ ω Δt)`。

    `omega` 的单位是 **rad/s**（契约 R2：全链路 SI，见 `contracts.FIELD_UNITS`）。
    写成 deg/s 不会报错，只会差 57.3 倍。

    右乘而不是左乘：`ω` 是在**自身**（足部）坐标系里测得的，右乘表示"在自身系里
    再转一点"。左乘表示在导航系里转，那对应的是完全不同的物理量。

    在区间内 `ω` 恒定的前提下，这一步是**精确**的（不是一阶近似）—— 因为转轴不变
    时旋转矢量可以直接取指数。真正的近似在于"采样点之间 `ω` 恒定"这个假设本身，
    以及转轴变化带来的圆锥误差；后者在走路场景下可忽略（整体设计 §5.4：行走场景
    一阶即可），跑步场景需要等效旋转矢量法二子样以上，届时替换的是这一个函数。
    """
    delta = from_rotation_vector(np.asarray(omega, dtype=np.float64) * np.asarray(dt))
    return normalize(multiply(q, delta))


def from_euler(roll: np.ndarray, pitch: np.ndarray, yaw: np.ndarray) -> np.ndarray:
    """内旋 ZYX 欧拉角（rad）→ 四元数。见 `EULER_SEQUENCE`。"""
    half_roll = 0.5 * np.asarray(roll, dtype=np.float64)
    half_pitch = 0.5 * np.asarray(pitch, dtype=np.float64)
    half_yaw = 0.5 * np.asarray(yaw, dtype=np.float64)
    cr, sr = np.cos(half_roll), np.sin(half_roll)
    cp, sp = np.cos(half_pitch), np.sin(half_pitch)
    cy, sy = np.cos(half_yaw), np.sin(half_yaw)
    return np.stack(
        (
            cr * cp * cy + sr * sp * sy,
            sr * cp * cy - cr * sp * sy,
            cr * sp * cy + sr * cp * sy,
            cr * cp * sy - sr * sp * cy,
        ),
        axis=-1,
    )


def to_euler(q: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """四元数 → `(roll, pitch, yaw)`，rad。

    ZYX 序列在 pitch = ±90° 处有万向节死锁：那里 roll 与 yaw 不再可分，只有它们的
    和（或差）有意义。这里**不做特殊处理，也不报错** —— 用 `arcsin` 的裁剪保证不
    出 nan，剩下的由调用方判断。理由是死锁在本系统里对应"脚尖竖直朝上或朝下"，
    正常步态里不会出现；若真出现了，它是数据本身有问题的信号，不该被这个函数掩盖。

    需要连续、无奇点的姿态表示时用四元数本身，别绕道欧拉角 —— 欧拉角在本仓库里
    只服务于两件事：报告里给人看的角度，以及初始对准的 roll/pitch（RAY-202）。
    """
    quaternion = normalize(q)
    w, x, y, z = (quaternion[..., i] for i in range(4))
    roll = np.arctan2(2.0 * (w * x + y * z), 1.0 - 2.0 * (x * x + y * y))
    # 裁剪到 [-1, 1]：数值误差会让这个量偶尔落到 1+1e-16，而 arcsin 对它返回 nan。
    pitch = np.arcsin(np.clip(2.0 * (w * y - z * x), -1.0, 1.0))
    yaw = np.arctan2(2.0 * (w * z + x * y), 1.0 - 2.0 * (y * y + z * z))
    return roll, pitch, yaw


def angle_between(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """两个姿态之间的旋转角，rad，落在 [0, π]。

    测试与质量指标都要回答"这两个姿态差多少"，而直接比四元数分量会被 `q ≡ -q`
    的双覆盖坑到：同一个姿态的两种写法逐分量相差可以是 2。
    """
    relative = multiply(conjugate(_as_quaternion(a, "a")), _as_quaternion(b, "b"))
    return np.linalg.norm(to_rotation_vector(relative), axis=-1)
