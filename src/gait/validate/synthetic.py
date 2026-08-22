"""合成步态数据生成器。契约 §1 的 `validate/synthetic.py`（F6.4）。

## 它为什么不在 `tests/` 里

契约 §6 的第二条非显然决策就是这件事："合成数据生成器不只是测试夹具，它是一个可以
被 CLI 调用的正式工具 —— 用来做参数敏感性分析、评估不同噪声水平下的精度退化。"

## 生成方向：由真值反推测量，不由测量凑出真值

不去手工拼一条"看起来像步态"的加速度波形，而是先写下**足部的真实运动**
（位置 `p(t)`、姿态 `q(t)` 的解析函数），再按导航方程反推 IMU 应该测到什么：

    ω_b = 姿态的角速度（本模型下恒为单轴，可解析给出）
    f_b = C_n^f · (p̈ - g_n)      ← 比力，与 `core/ins.py` 的约定严格互逆

这样一来，"真值"不是标注出来的，而是**构造过程本身**。一个正确的前向机械编排喂进
这份数据，必然回到同一条轨迹；差多少就是纯算法误差。反过来若手工拼波形，"真值"只能
靠再积分一次得到，那就成了用被测对象定义正确答案。

一条测试直接验证这个互逆关系：把生成的测量喂回 `ins.mechanize`，残差随 dt 趋于零。
断言的是**收敛**而不是某个绝对阈值 —— 生成器若有任何不自洽（符号反了、姿态与位置
各说各的、ω 与 q 对不上），残差会收敛到一个非零常数，那才是这条测试真正在找的东西。

顺带量出了一件生成器之外的事：摆动相**中段**的残差只按一阶收敛，因为
`ins.propagate` 把区间起点的 ω 当作整个区间的常量。RAY-201 的测试全部用恒定 ω，
结构上看不到这一项。触地时刻的残差不受影响（ω 在每个 stride 首尾都回到零，摆动相
内的误差自消，200 Hz 下实测姿态残差 ~1e-5 rad、位置残差 ~2.4e-4 m），所以步长精度
无碍；受影响的是足廓清高度与瞬时速度曲线这类摆动相中段的指标。已登记为后继 Issue。

## 模型：刚性平足支撑

支撑相内足部**完全静止** —— 速度、角速度、俯仰角全为零。整个俯仰摆动发生在摆动相，
且在两端连同一、二阶导数一起归零。

这是刻意的简化，因为它给出一个**精确为零的支撑相**，而那正是 ZUPT 假设的东西，
也正是 RAY-203/204 开发时需要的干净输入。

代价必须写清楚，否则这份数据会被用去回答它答不了的问题：

* **不能用来验证 IC/TO 的亚窗口细化。** 整体设计 §6.1 说"直接用 ZUPT 区间边界当作
  IC/TO 会有 10–30 ms 系统性偏差"。在本模型里这个偏差**恰好为零**（事件就定义在
  支撑相边界上）。RAY-216 若拿它验证事件细化，会得到一个必然通过、也必然无意义的
  结果。那件事只能靠真机 + 测力台（RAY-230）。
* **着地角恒为 0。** 平足支撑要求 IC 时刻俯仰角为零，所以 `strike_angle` 在本数据上
  没有信息。
* **没有触地冲击。** 落地瞬间的高频冲量不存在，因此不能用来调零速检测器对冲击的
  抗性，也不能评估饱和。

## 转身：单轴约束是精度的前提

`ω_b` 只有在"任一时刻旋转都绕单一体轴"时才有解析表达。因此本模型规定：

* 直行 stride：只有俯仰（绕体轴 y），航向不变；
* 转身 stride：只有航向（绕体轴 z），俯仰恒为 0。

两者都在 stride 边界上把自己的角度归零，所以不会同时出现。这不是随手的约束 ——
放弃它就得数值微分姿态，而那会让"真值"本身带上离散误差，生成器也就不再比被测对象
更可信。

真实转身当然是俯仰与航向同时发生的。本模型的转身相当于"原地转体"，它保留了
RAY-215（直行段/转身段分离）真正关心的性质：**转身 stride 的步长接近零，必须被
剔除**。

## 单位

全 SI，与契约 R2 一致：`acc` 是 m/s²（比力，静止时 z 读 +g），`gyr` 是 rad/s。
面向人的角度参数（俯仰幅度、转身角）以 **deg** 传入并在入口换算 —— 参数是给人填的，
中间量是给算法用的，两者的可读性诉求相反。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Final

import numpy as np

from gait.contracts import FootLabel, FootSeries, Quality
from gait.core import ins
from gait.core import quaternion as quat

#: 一个 stride 是同一只脚两次连续触地之间的过程，含两步。cadence 以**步/分**计
#: （PRD 与临床文献的通行口径），因此 stride 时长是 120/cadence 而不是 60/cadence。
#: 这个 2 倍关系是步态参数里最常见的口误来源，所以给它一个具名常量。
STEPS_PER_STRIDE: Final[int] = 2


class SyntheticError(ValueError):
    """生成参数非法。"""


def _quintic(s: np.ndarray) -> np.ndarray:
    """五次平滑阶跃：`g(0)=0`、`g(1)=1`，两端一阶与二阶导数均为零。

    用五次而不是三次（`3s²-2s³`）：三次的二阶导数在两端不为零，意味着加速度在
    支撑相与摆动相的交界处**跳变**。跳变本身在真实步态里存在，但在这里它会让
    "区间内加速度恒定"的假设恰好在每个周期的两个特定采样上失效 —— 于是数值误差
    不再随 dt 二阶收敛，而生成器的自洽性验证正是靠收敛阶来证明的。
    """
    return s * s * s * (10.0 + s * (-15.0 + 6.0 * s))


def _quintic_d1(s: np.ndarray) -> np.ndarray:
    return 30.0 * s * s * (s - 1.0) * (s - 1.0)


def _quintic_d2(s: np.ndarray) -> np.ndarray:
    return 60.0 * s * (2.0 * s - 1.0) * (s - 1.0)


def _lift(s: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """抬脚的高度剖面及其一、二阶导数（对归一化摆动相位 `s`）。

    前半程用 `g(2s)` 升到 1，后半程用 `g(2-2s)` 落回 0。两端与顶点处一、二阶导数
    全为零，理由同 `_quintic`。
    """
    up = s < 0.5
    u = np.where(up, 2.0 * s, 2.0 - 2.0 * s)
    sign = np.where(up, 1.0, -1.0)
    return _quintic(u), 2.0 * sign * _quintic_d1(u), 4.0 * _quintic_d2(u)


def _pitch_profile(
    s: np.ndarray, toe_off: float, swing_peak: float
) -> tuple[np.ndarray, np.ndarray]:
    """摆动相俯仰角及其对 `s` 的导数，rad。

    三段：离地跖屈到 `-toe_off` → 摆动背屈到 `+swing_peak` → 落地前回到 0。
    每一段都是五次平滑阶跃，因此三个接点与两个端点处一、二阶导数全部连续为零。

    末端回到 0 是**平足支撑模型的必然结果**，不是偷懒：支撑相要求足部完全静止且
    足底贴地，那么触地瞬间的俯仰角只能是 0。代价是着地角在本数据上恒为零，见模块文档。
    """
    angle = np.empty_like(s)
    rate = np.empty_like(s)

    first = s < 1.0 / 3.0
    second = (s >= 1.0 / 3.0) & (s < 2.0 / 3.0)
    third = s >= 2.0 / 3.0

    u = np.clip(3.0 * s, 0.0, 1.0)
    angle[first] = -toe_off * _quintic(u[first])
    rate[first] = -3.0 * toe_off * _quintic_d1(u[first])

    u = np.clip(3.0 * s - 1.0, 0.0, 1.0)
    span = swing_peak + toe_off
    angle[second] = -toe_off + span * _quintic(u[second])
    rate[second] = 3.0 * span * _quintic_d1(u[second])

    u = np.clip(3.0 * s - 2.0, 0.0, 1.0)
    angle[third] = swing_peak * (1.0 - _quintic(u[third]))
    rate[third] = -3.0 * swing_peak * _quintic_d1(u[third])

    return angle, rate


@dataclass(frozen=True)
class NoiseModel:
    """传感器噪声。默认**无噪声** —— 让"算法本身对不对"能被单独回答。

    噪声密度而不是标准差：白噪声的采样标准差随采样率变化（σ = density·√fs），
    写成标准差的话同一份参数在 100 Hz 与 200 Hz 下描述的是两种不同的传感器。
    Allan 方差给出的也是密度（ARW / VRW），两边对得上。
    """

    #: 速度随机游走 VRW，m/s²/√Hz。
    accel_density: float = 0.0
    #: 角度随机游走 ARW，rad/s/√Hz。
    gyro_density: float = 0.0
    #: 常值零偏，m/s² 与 rad/s。**加计零偏是本硬件的首要误差源**
    #: （《BS-BT91 硬件适配》发现 1：±20~40 mg，可致 1.75%~3.5% 步长偏差）。
    accel_bias: tuple[float, float, float] = (0.0, 0.0, 0.0)
    gyro_bias: tuple[float, float, float] = (0.0, 0.0, 0.0)
    seed: int = 0

    @classmethod
    def bs_bt91(cls, *, seed: int = 0) -> NoiseModel:
        """按 BS-BT91 规格量级的一组参数。**未经 Allan 方差实测标定。**

        它的用途是回答"噪声大致这个量级时精度退化多少"，不是回答"这台设备的精度
        是多少"。真实取值属 RAY-207（六面法标定与标定参数库）与真机 V1（RAY-230）。

        加计零偏取 30 mg（规格 ±20~40 mg 的中值）。这是**唯一一个有硬件依据的数**，
        其余两项按同类 MEMS 的常见量级给。
        """
        return cls(
            accel_density=1.5e-3,
            gyro_density=3.0e-4,
            accel_bias=(0.030 * ins.GRAVITY_STANDARD, 0.0, 0.0),
            gyro_bias=(0.0, 0.0, 0.0),
            seed=seed,
        )


@dataclass(frozen=True)
class WalkSpec:
    """一次合成行走的全部参数。面向人的角度用 deg，其余全 SI。"""

    #: 同一只脚两次连续触地之间的位移，m。
    stride_length: float = 1.30
    #: 步/分。stride 时长 = 120/cadence，见 `STEPS_PER_STRIDE`。
    cadence: float = 108.0
    duration_s: float = 20.0
    fs: float = 200.0
    #: 支撑相占步周期的比例。走路约 0.6（整体设计 §6.2）。
    stance_ratio: float = 0.60
    #: 摆动相最大足廓清高度，m。
    clearance: float = 0.05
    #: 左右足横向间距，m。
    step_width: float = 0.12
    #: 离地跖屈幅度与摆动背屈峰值，deg。
    toe_off_pitch_deg: float = 28.0
    swing_pitch_deg: float = 12.0
    #: 起步前的静止时长，s。RAY-202 的初始对准与 RAY-204 的零偏收敛都需要它。
    still_lead_s: float = 1.0
    #: 直行段长度，m。`None` 表示一直向前走、不转身。
    #: PRD §7 的 T-01 是往返走，4.0 对应「4 米往返」协议。
    path_length_m: float | None = None
    #: 一次 180° 转身分摊到几个 stride。
    turn_strides: int = 2

    def __post_init__(self) -> None:
        for name in ("stride_length", "cadence", "duration_s", "fs", "clearance"):
            if not getattr(self, name) > 0:
                raise SyntheticError(f"{name} 必须为正，收到 {getattr(self, name)}")
        if not 0.0 < self.stance_ratio < 1.0:
            raise SyntheticError(
                f"stance_ratio 应在 (0, 1) 内，收到 {self.stance_ratio}。"
                "取 0 表示没有支撑相，零速修正无从谈起；取 1 表示脚不动。"
            )
        if self.still_lead_s < 0:
            raise SyntheticError(f"still_lead_s 不得为负，收到 {self.still_lead_s}")
        if self.path_length_m is not None:
            if not self.path_length_m > 0:
                raise SyntheticError(f"path_length_m 必须为正，收到 {self.path_length_m}")
            if self.path_length_m < self.stride_length:
                raise SyntheticError(
                    f"path_length_m={self.path_length_m} 短于一个 stride "
                    f"({self.stride_length})，直行段里放不下一步。"
                )
            if self.turn_strides < 1:
                raise SyntheticError("turn_strides 至少为 1")

    @property
    def stride_time(self) -> float:
        return STEPS_PER_STRIDE * 60.0 / self.cadence

    @property
    def gait_speed(self) -> float:
        """名义步速，m/s。真值里逐 stride 的步速由位置差算出，这里只是名义值。"""
        return self.stride_length / self.stride_time


@dataclass(frozen=True)
class Stride:
    """一个 stride 的真值台账。分析层的每一条 `GaitCycle` 都该对得上其中一条。"""

    index: int
    #: 触地时刻、足尖离地时刻、下一次触地时刻，s。与契约 §3.4 同名字段对应。
    t_ic: float
    t_to: float
    t_ic_next: float
    #: 起止落脚点（导航系，m）。
    start: np.ndarray
    end: np.ndarray
    heading_start: float
    heading_end: float
    is_turn: bool

    @property
    def stride_length(self) -> float:
        """水平位移。转身 stride 上它接近零 —— 这正是 RAY-215 要剔除的那些。"""
        return float(np.linalg.norm((self.end - self.start)[:2]))

    @property
    def stride_time(self) -> float:
        return self.t_ic_next - self.t_ic


@dataclass(frozen=True)
class GroundTruth:
    """生成时就已知的真值。不是标注出来的，是构造过程本身。"""

    label: FootLabel
    t: np.ndarray  # (n,) s
    p: np.ndarray  # (n,3) m，导航系 ENU
    v: np.ndarray  # (n,3) m/s
    q: np.ndarray  # (n,4) 足部系 → 导航系
    #: 真实静止区间（含起步前的静止段）。零速检测器的召回率就对着它算。
    stance: list[tuple[int, int]] = field(default_factory=list)
    strides: list[Stride] = field(default_factory=list)
    spec: WalkSpec = field(default_factory=WalkSpec)

    @property
    def straight_strides(self) -> list[Stride]:
        """直行 stride。步长精度只在这些上有意义。"""
        return [stride for stride in self.strides if not stride.is_turn]

    @property
    def mean_stride_length(self) -> float:
        straight = self.straight_strides
        if not straight:
            raise SyntheticError("没有直行 stride，步长无从谈起")
        return float(np.mean([stride.stride_length for stride in straight]))


def _schedule(spec: WalkSpec, foot: FootLabel) -> list[Stride]:
    """落脚点与航向的台账。

    `heading` 是行进方向在 ENU 水平面内的角度（x 轴为 0，逆时针为正）。横向偏置
    挂在**航向**上而不是固定在导航系里 —— 否则转身 180° 之后左脚会跑到右边去。
    """
    stride_time = spec.stride_time
    lead = spec.still_lead_s
    lateral = (1.0 if foot == "L" else -1.0) * 0.5 * spec.step_width

    total = max(1, int(np.ceil((spec.duration_s - lead) / stride_time)) + 1)
    straight_per_leg = (
        None if spec.path_length_m is None else max(1, round(spec.path_length_m / spec.stride_length))
    )

    def foot_position(centre: np.ndarray, heading: float) -> np.ndarray:
        normal = np.array([-np.sin(heading), np.cos(heading), 0.0])
        return centre + lateral * normal

    strides: list[Stride] = []
    centre = np.zeros(3)
    heading = 0.0
    since_turn = 0
    turning_left = 0

    for index in range(total):
        # 转身占满 turn_strides 个 stride；其间不前进，只改航向。
        is_turn = straight_per_leg is not None and since_turn >= straight_per_leg
        start = foot_position(centre, heading)
        if is_turn:
            heading_next = heading + np.pi / spec.turn_strides
            centre_next = centre
            turning_left += 1
            if turning_left >= spec.turn_strides:
                turning_left = 0
                since_turn = 0
        else:
            heading_next = heading
            centre_next = centre + spec.stride_length * np.array(
                [np.cos(heading), np.sin(heading), 0.0]
            )
            since_turn += 1
        end = foot_position(centre_next, heading_next)

        t_ic = lead + index * stride_time
        strides.append(
            Stride(
                index=index,
                t_ic=t_ic,
                t_to=t_ic + spec.stance_ratio * stride_time,
                t_ic_next=t_ic + stride_time,
                start=start,
                end=end,
                heading_start=heading,
                heading_end=heading_next,
                is_turn=is_turn,
            )
        )
        centre = centre_next
        heading = heading_next

    return strides


def _trajectory(
    spec: WalkSpec, strides: list[Stride], t: np.ndarray
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray, list[tuple[int, int]]]:
    """按台账把 `p, v, a, q, ω_b` 与静止区间铺到采样网格上。"""
    n = t.size
    p = np.zeros((n, 3))
    v = np.zeros((n, 3))
    a = np.zeros((n, 3))
    omega = np.zeros((n, 3))
    yaw = np.zeros(n)
    pitch = np.zeros(n)

    swing_time = (1.0 - spec.stance_ratio) * spec.stride_time
    toe_off = np.radians(spec.toe_off_pitch_deg)
    swing_peak = np.radians(spec.swing_pitch_deg)

    # 起步前的静止段：位置停在第一个落脚点，航向为初始航向。
    first = strides[0]
    p[:] = first.start
    yaw[:] = first.heading_start
    stance: list[tuple[int, int]] = []
    lead_end = int(np.searchsorted(t, first.t_ic, side="left"))

    for stride in strides:
        in_stance = (t >= stride.t_ic) & (t < stride.t_to)
        in_swing = (t >= stride.t_to) & (t < stride.t_ic_next)
        if in_stance.any():
            p[in_stance] = stride.start
            yaw[in_stance] = stride.heading_start
        if not in_swing.any():
            continue

        s = (t[in_swing] - stride.t_to) / swing_time
        step = stride.end - stride.start
        shape, shape_d1, shape_d2 = _quintic(s), _quintic_d1(s), _quintic_d2(s)
        lift, lift_d1, lift_d2 = _lift(s)

        p[in_swing] = stride.start + step * shape[:, None]
        v[in_swing] = step * (shape_d1 / swing_time)[:, None]
        a[in_swing] = step * (shape_d2 / swing_time**2)[:, None]
        p[in_swing, 2] += spec.clearance * lift
        v[in_swing, 2] += spec.clearance * lift_d1 / swing_time
        a[in_swing, 2] += spec.clearance * lift_d2 / swing_time**2

        if stride.is_turn:
            # 只转航向。俯仰恒为 0，因此 ω_b 就是 ψ̇·ẑ（见模块文档的单轴约束）。
            turn = stride.heading_end - stride.heading_start
            yaw[in_swing] = stride.heading_start + turn * shape
            omega[in_swing, 2] = turn * shape_d1 / swing_time
        else:
            # 只俯仰。航向不变，ω_b 就是 θ̇·ŷ。
            yaw[in_swing] = stride.heading_start
            angle, rate = _pitch_profile(s, toe_off, swing_peak)
            pitch[in_swing] = angle
            omega[in_swing, 1] = rate / swing_time

    q = quat.multiply(
        quat.from_euler(0.0, 0.0, yaw), quat.from_euler(0.0, pitch, 0.0)
    )

    # 静止区间：起步前的静止段与每个支撑相。零速检测器的召回率对着它算。
    if lead_end > 0:
        stance.append((0, lead_end))
    for stride in strides:
        start = int(np.searchsorted(t, stride.t_ic, side="left"))
        end = int(np.searchsorted(t, stride.t_to, side="left"))
        if end > start:
            if stance and stance[-1][1] == start:
                stance[-1] = (stance[-1][0], end)
            else:
                stance.append((start, end))
    return p, v, a, q, omega, stance


def generate_walk(
    spec: WalkSpec | None = None,
    *,
    foot: FootLabel = "L",
    noise: NoiseModel | None = None,
    gravity: float = ins.GRAVITY_STANDARD,
) -> tuple[FootSeries, GroundTruth]:
    """生成一只脚的行走数据与真值。

    ## 与契约 §4 的签名差异

    契约写的是 `generate_walk(stride_length, cadence, duration, fs, noise)`。这里改成
    一个 `WalkSpec` 对象：模型需要十来个参数（支撑相占比、足廓清、步宽、俯仰幅度、
    静止前导、往返路长、转身 stride 数），而五个位置参数装不下它们。**更要紧的是**
    位置参数里全是同量纲的浮点数，调换任意两个都不会报错，只会安静地生成一份
    不同的步态。契约 §5 的文档维护规则要求"设计变更先改文档再改代码"，本次同步
    修订《模块划分与接口契约》§4。

    `noise` 默认无噪声：算法本身对不对，应该能被单独回答。
    """
    spec = spec or WalkSpec()
    noise = noise or NoiseModel()
    if foot not in ("L", "R"):
        raise SyntheticError(f"foot 应为 'L' 或 'R'，收到 {foot!r}")

    n = round(spec.duration_s * spec.fs) + 1
    if n < 2:
        raise SyntheticError(
            f"duration_s={spec.duration_s} 与 fs={spec.fs} 只够 {n} 个采样，无法构成序列"
        )
    t = np.arange(n) / spec.fs

    strides = _schedule(spec, foot)
    p, v, a, q, omega, stance = _trajectory(spec, strides, t)

    # 由真值反推测量。这一行是整个模块的支点，且与 core/ins.py 的导航方程严格互逆：
    #     ins:       a_n = C_f^n · f_f + g_n
    #     这里:      f_f = C_n^f · (a_n - g_n)
    gravity_vector = ins.gravity_vector(gravity)
    acc = quat.rotate_inverse(q, a - gravity_vector)
    gyr = omega.copy()

    if noise.accel_density or noise.gyro_density:
        rng = np.random.default_rng(noise.seed)
        scale = np.sqrt(spec.fs)
        acc = acc + rng.normal(scale=noise.accel_density * scale, size=acc.shape)
        gyr = gyr + rng.normal(scale=noise.gyro_density * scale, size=gyr.shape)
    acc = acc + np.asarray(noise.accel_bias, dtype=np.float64)
    gyr = gyr + np.asarray(noise.gyro_bias, dtype=np.float64)

    series = FootSeries(
        label=foot,
        t=t,
        acc=acc,
        gyr=gyr,
        # 合成数据没有饱和、没有插值、没有空洞 —— 全部标为正常，而不是随手填 0：
        # `Quality.NONE` 恰好是 0，但写常量才说明这是一个判断而不是默认值。
        quality=np.full(n, Quality.NONE, dtype=np.uint8),
        segments=[(0, n)],
        fs=spec.fs,
    )
    truth = GroundTruth(
        label=foot,
        t=t,
        p=p,
        v=v,
        q=q,
        stance=stance,
        # 只保留落在序列时间范围内的 stride：台账为了覆盖末尾多排了一个。
        strides=[stride for stride in strides if stride.t_ic_next <= t[-1] + 0.5 / spec.fs],
        spec=spec,
    )
    return series, truth


def generate_dual_walk(
    spec: WalkSpec | None = None,
    *,
    noise: NoiseModel | None = None,
    gravity: float = ins.GRAVITY_STANDARD,
) -> dict[FootLabel, tuple[FootSeries, GroundTruth]]:
    """双足数据，右足相位滞后半个 stride。

    双足不是"跑两遍单足"：RAY-205 的双足距离约束、RAY-211 的交替支撑一致性自检、
    以及双支撑期这个指标，全都建立在**两只脚的相位关系**上。让相位偏移在这里成立
    一次，好过让每个调用方各自算一遍半个周期是多少。

    半个 stride 的偏移由 `still_lead_s` 承担：右足的静止前导长半个周期。这样两只脚
    的时间轴仍然是同一条（`t` 完全相同），下游不必做任何对齐 —— 合成数据里不存在
    同步误差，那是 RAY-209/213 的题目，不该被这里悄悄引入。

    两只脚用**不同的噪声种子**，否则左右噪声完全相同，对称性指标会得到一个假的完美值。
    """
    spec = spec or WalkSpec()
    noise = noise or NoiseModel()
    right_spec = replace(spec, still_lead_s=spec.still_lead_s + 0.5 * spec.stride_time)
    return {
        "L": generate_walk(spec, foot="L", noise=noise, gravity=gravity),
        "R": generate_walk(
            right_spec,
            foot="R",
            noise=replace(noise, seed=noise.seed + 1),
            gravity=gravity,
        ),
    }
