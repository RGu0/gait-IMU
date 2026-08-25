"""双足联合约束。契约 §1 的 `core/dualfoot.py`（F4.4）。整体设计 §5.7。

## 它约束的是**差分**航向，而那恰好就是需要被约束的那一半

RAY-204 量到：6 轴下航向是**弱可观测**的 —— 导航系 1σ 从先验 30° 掉到 5 s 时的 4.5°，
然后卡在 2.3° 不动。两只脚各自卡在自己的 2° 上，于是它们的**差**也是几度，而几度的
差分航向在 30 s 行走里就让估计的足间距从真实的 1.31 m 长到 1.96 m。

这里的关键观察是：足间距只能看见两只脚航向的**差**。把两只脚一起转一个角度，所有距离
一字不变。所以本模块**只能**修正差分航向 —— 而这不是缺陷，因为：

* 共模航向本来就没有意义。RAY-202 定的产品边界是"输出定义在会话坐标系，yaw = 0"，
  整段轨迹一起转多少度是会话坐标系的定义问题，不是误差。
* 差分航向才是有害的那一半：它让两只脚在空间上分开，直接污染步宽、双支撑期、以及
  任何跨足的对称性指标。

一句话：**这个约束能修的正好是需要修的，修不了的正好是不需要修的。**

## 与整体设计 §5.7 的差别：这里是后处理，不是滤波器内注入

§5.7 写的是"将其作为不等式约束注入两个滤波器（对称分配修正量）"。契约 §4 给的签名
`apply_distance_constraint(left, right, d_max) -> (NavResult, NavResult)` 却是后处理的
形状 —— 两者对不上，本 scope 按契约走，理由有两条：

1. 滤波器内注入要求两足的 ESKF **同步推进**，而 `run_ins` 目前是逐足独立的。改成
   联合滤波是对 RAY-204 的结构性改动，属云端重算链（RAY-227「RTS 平滑 + 双足联合
   约束」）的范围。
2. PRD §6.1 的本地基础报告要在 15 秒内出结果，而后处理只需要在两条轨迹算完之后跑
   一次两参数拟合。

代价要说清楚：后处理拿不到协方差，因此**修正量不是按不确定度分配的，而是对半分**。
两只脚的航向不确定度若差很多（例如一只脚的支撑相检测明显更差），对半分不是最优。
这一条属于 RAY-227 的改进空间。

## 初始足间偏置从哪来

两条轨迹各自从自己的原点起算（`run_ins` 把 `p[0]` 置零），所以直接相减得到的不是真实
足间距。真实的初始偏置由**佩戴与会话标定**给出：PRD §7 的流程要求静立 5 s，那时两只脚
并排站着，横向相距一个步宽、纵向对齐。

`DEFAULT_STEP_WIDTH` 是这个假设的默认值。它是一个**假设**而不是测量 —— 纵向对齐尤其
如此（受试者可能一只脚略靠前）。所以它被做成参数，并且报告里带着它，好让下游知道
这次的距离是相对哪个偏置算出来的。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Final

import numpy as np

from gait.config import AlgoConfig
from gait.contracts import NavResult
from gait.core import quaternion as quat

#: 静立时两脚的横向间距，m。见模块文档"初始足间偏置从哪来"。
DEFAULT_STEP_WIDTH: Final[float] = 0.12

#: 差分航向漂移率的搜索范围，rad/s。
#:
#: 取值依据是 RAY-204 的实测：单足航向卡在 2~3°，两足之差在同一量级。30 s 里 2°
#: 对应 1.2e-3 rad/s，这里给两个数量级余量。
#: 范围给宽不给窄：搜到边界上说明模型或数据有问题，而那要被看见，不能被静静截断。
_YAW_RATE_RANGE: Final[float] = 0.02
_REFINEMENTS: Final[int] = 5
_GRID: Final[int] = 41


class DualFootError(ValueError):
    """双足约束的输入非法。"""


@dataclass(frozen=True)
class DualFootReport:
    """约束做了什么，以及它对这次会话的判断。

    进 `SessionMeta.sync_report` 与质量标注（RAY-218）。一个只改数据不留记录的修正，
    三个月后没人能说清某份报告里的步宽是测出来的还是修出来的。
    """

    #: 采用的最大足间距与初始横向偏置。
    max_distance: float
    step_width: float
    #: 修正前后的足间距峰值与越界样本占比。
    peak_distance_before: float
    peak_distance_after: float
    violation_fraction_before: float
    violation_fraction_after: float
    #: 拟合出的差分航向漂移率，rad/s。整段的差分航向是 `θ(t) = rate · t` —— 没有常数项，
    #: 因为两足各自以 yaw = 0 对准，起点上航向之差恒为零。
    #: 左足转 `+θ/2`、右足转 `−θ/2`，即 §5.7 的"对称分配修正量"。
    differential_yaw_rate: float
    #: 拟合是否顶到了搜索边界。顶到边界说明模型或数据有问题，不该被静静截断。
    hit_search_bound: bool

    @property
    def improved(self) -> bool:
        return self.peak_distance_after < self.peak_distance_before


@dataclass(frozen=True)
class AlternatingStanceReport:
    """交替支撑一致性。整体设计 §5.7 第 2 条。

    正常步态里双足支撑相**交替**出现，走路时还有 10~25% 的双支撑期（整体设计 §6.2）——
    所以"双足同时零速"本身完全正常，**异常的是它持续太久**（站着不动），以及"双足同时
    运动"持续太久（跑步腾空期之外）。

    这个检查不拦截任何东西，只打标注：PRD §13 的原则是全量计算 + 质量标注。
    """

    double_support_fraction: float
    flight_fraction: float
    longest_double_support_s: float
    longest_flight_s: float
    #: 行走窗口里的采样数。为 0 表示整段两只脚都没动过 —— 此时上面四个数全为零，
    #: 而那不是"一切正常"，是"没有可判断的东西"。分开一个字段，好让调用方能区分
    #: 这两种情况；返回 nan 会让下游在毫无察觉的情况下把它当成一个数。
    walking_samples: int
    #: 上面两个"最长"是否超出阈值。超出说明检测器或同步有问题。
    suspicious: bool


@dataclass(frozen=True)
class LateralSeparation:
    """两足横向分离量的实测，以及它**能不能**用来判断左右。

    结论先写在这里：**不能**。见 `lateral_separation` 的文档。这个结构存在是为了让
    那个结论有据可查，而不是让调用方拿 `swapped` 之类的字段去用。
    """

    #: 标称左脚相对双足中线的横向位置均值，m（行进方向的左法向为正）。
    nominal_left_lateral: float
    #: 计算时假定的初始步宽，m。
    assumed_step_width: float
    #: 两足轨迹估计出的相对横向发散量，m。它**全部**来自航向漂移。
    estimated_divergence: float
    strides_used: int

    @property
    def identifiable(self) -> bool:
        """漂移是否小到让符号还有意义。

        判据：估计出的发散量必须小于半个步宽。超过了，`nominal_left_lateral` 的符号
        就由漂移决定而不是由左右决定 —— 而漂移的方向是随机的。
        """
        return abs(self.estimated_divergence) < 0.5 * self.assumed_step_width


@dataclass(frozen=True)
class InversionSignature:
    """两足在摆动相的横滚（内外翻）差。整体设计 §5.7 第 3 条的第二个线索。

    **符号与左右的对应关系尚未标定。** 这里只给出原料：差值及其相对噪声的显著性。
    把 `difference > 0` 解读成"标称左脚确实是左脚"需要真机数据（RAY-230）或一次
    专门的佩戴实验来确立，而在那之前任何解读都是猜。

    它比位置法有前途，理由是**它不需要公共坐标系**：横滚由各自的 IMU 直接给出，
    不经过任何跨足的位置对齐，因此免疫于航向漂移。
    """

    #: 标称左脚与右脚在摆动相的平均横滚之差，rad。
    difference: float
    #: 差值除以逐 stride 的标准差，无量纲。小于 1 表示两脚分不开。
    significance: float
    strides_used: int

    @property
    def decisive(self) -> bool:
        return self.significance > 2.0


def lateral_separation(
    left: NavResult,
    right: NavResult,
    cfg: AlgoConfig | None = None,
    *,
    step_width: float = DEFAULT_STEP_WIDTH,
) -> LateralSeparation:
    """按整体设计 §5.7 第 3 条的位置法计算横向分离量。

    ## 这个方法**判断不了左右**，实测如此

    §5.7 写的是"用前 10 步的横向位置均值符号即可自动判定左右"。在本仓库的数据流下
    这条不成立，理由是一个恒等式：

    `run_ins` 把每只脚的 `p[0]` 置零（各自的轨迹只相对自己的起点有意义）。两条轨迹
    因此**不在同一个坐标系里**，真实的初始足间偏置是未知量。把它按假设的步宽补上之后，

        标称左脚相对中线的横向量 = 假设步宽/2 + 估计发散量/2

    第一项来自**假设**，第二项来自**航向漂移**。真实的左右身份**一个字也没进这个式子**。

    合成数据上的验证（3 个随机种子 × 3 个步宽，共 9 组）：把左右两路数据对调之后重算，
    两次结果之和**精确等于步宽**，一位小数都不差 —— 这正是上式的直接推论。9 组里有
    3 组把正常佩戴判成戴反，而它们判错的唯一原因是那几组的漂移恰好超过了半个步宽。

    换句话说：**这个判据在测它自己的假设**。它给出的符号是一枚由漂移加权的硬币。

    ## 那它为什么还留着

    因为分离量本身有用：它是步宽指标的原料（整体设计 §6.2），也是"两足是否已经发散到
    不可信"的直接读数。`identifiable` 报告的正是后者。

    真正能判左右的线索是 §5.7 同一条里提到的另一半 —— **踝关节的内外翻方向相反**，
    见 `inversion_signature`。那个量由各自的 IMU 直接给出，不需要公共坐标系。
    """
    cfg = cfg or AlgoConfig()
    if len(left.t) != len(right.t):
        raise DualFootError(f"两足的采样数不一致：{len(left.t)} vs {len(right.t)}")

    strides = min(len(left.stances), len(right.stances), cfg.dualfoot_identification_strides)
    if strides < 2:
        raise DualFootError(
            f"只有 {strides} 个支撑相可用，分离量算不出来。"
            "会话标定要求走 10 步；步数不足时应当提示重走。"
        )
    end = max(left.stances[strides - 1][1], right.stances[strides - 1][1])

    offset = np.array([0.0, -step_width, 0.0])
    left_path = left.p[:end]
    right_path = right.p[:end] + offset
    midline = 0.5 * (left_path + right_path)

    travel = midline[-1, :2] - midline[0, :2]
    distance = float(np.linalg.norm(travel))
    if distance < step_width:
        raise DualFootError(
            f"前 {strides} 步只前进了 {distance:.3f} m，不足一个步宽。"
            "行进方向定不下来 —— 原地踏步或转身段不能用来算横向分离量。"
        )
    direction = travel / distance
    # 左法向：把行进方向逆时针转 90°。ENU 下这就是"左边"。
    normal = np.array([-direction[1], direction[0]])

    lateral = float(np.mean((left_path[:, :2] - midline[:, :2]) @ normal))
    divergence = float(np.mean((left.p[:end, :2] - right.p[:end, :2]) @ normal))
    return LateralSeparation(
        nominal_left_lateral=lateral,
        assumed_step_width=step_width,
        estimated_divergence=divergence,
        strides_used=strides,
    )


def inversion_signature(
    left: NavResult, right: NavResult, cfg: AlgoConfig | None = None
) -> InversionSignature:
    """两足摆动相横滚均值之差。整体设计 §5.7 第 3 条的第二个线索。

    ## 为什么它比位置法有前途

    横滚由各自的姿态直接给出，**不经过任何跨足的位置对齐** —— 因此它免疫于航向漂移，
    而航向漂移正是位置法失效的原因（见 `lateral_separation`）。

    ## 为什么本 scope 不给结论

    符号与左右的对应关系**尚未标定**。把 `difference > 0` 解读成"标称左脚确实是左脚"
    需要真机数据（RAY-230）或一次专门的佩戴实验；在那之前任何解读都是猜，而一个猜出来
    的戴反判断比没有判断更糟 —— 它会让操作员把正确的佩戴改错。

    本函数因此只输出**差值与显著性**。显著性 = 差值 / 逐 stride 的标准差：小于 1 说明
    两只脚在这个量上根本分不开，那时连"有没有信号"都还没确立。

    合成数据（RAY-206）上这个量恒为零 —— 那个模型只有俯仰、没有横滚，是它已声明的
    限制之一。所以这个函数在本 scope 里只能验证"算得对"，验证不了"判得准"。
    """
    cfg = cfg or AlgoConfig()
    if len(left.t) != len(right.t):
        raise DualFootError(f"两足的采样数不一致：{len(left.t)} vs {len(right.t)}")

    strides = min(len(left.stances), len(right.stances), cfg.dualfoot_identification_strides)
    if strides < 2:
        raise DualFootError(f"只有 {strides} 个支撑相可用，算不出摆动相横滚")

    per_stride: list[float] = []
    for index in range(strides - 1):
        # 摆动相 = 相邻两个支撑相之间。
        swing_left = slice(left.stances[index][1], left.stances[index + 1][0])
        swing_right = slice(right.stances[index][1], right.stances[index + 1][0])
        if swing_left.stop <= swing_left.start or swing_right.stop <= swing_right.start:
            continue
        roll_left, _, _ = quat.to_euler(left.q[swing_left])
        roll_right, _, _ = quat.to_euler(right.q[swing_right])
        per_stride.append(float(np.mean(roll_left) - np.mean(roll_right)))

    if not per_stride:
        raise DualFootError("找不到成对的摆动相，横滚差算不出来")
    values = np.asarray(per_stride)
    spread = float(np.std(values))
    difference = float(np.mean(values))
    significance = abs(difference) / spread if spread > 0 else float("inf")
    return InversionSignature(
        difference=difference,
        significance=significance,
        strides_used=len(per_stride),
    )


def _rotate_xy(positions: np.ndarray, angles: np.ndarray) -> np.ndarray:
    """把每个位置在水平面内绕原点转 `angles[i]`。高度不动。

    逐样本用不同的角度而不是整体转一个固定角：航向误差是**累积**的，t 时刻的位置
    被截至 t 为止累积的那个角度歪着。用固定角只能修正末端，中段会被过修正。
    """
    cos = np.cos(angles)
    sin = np.sin(angles)
    out = positions.copy()
    out[:, 0] = cos * positions[:, 0] - sin * positions[:, 1]
    out[:, 1] = sin * positions[:, 0] + cos * positions[:, 1]
    return out


def _distances(
    left: np.ndarray, right: np.ndarray, offset: np.ndarray, t: np.ndarray, yaw_rate: float
) -> np.ndarray:
    """给定差分航向漂移率之后的水平足间距。

    模型是 `θ(t) = rate · t`，**没有常数项**。这不是简化，是物理约束：两只脚各自以
    `yaw = 0` 对准（RAY-202 的会话坐标系），所以在 t = 0 上它们的航向之差恒为零，
    差只能是之后累积出来的。

    留一个自由的常数项会让拟合用一个大的初始偏置去抵消一个正的漂移率 —— 越界消掉了，
    前几步的发散量反而更大。那正是第一版的实测行为。
    """
    angle = 0.5 * yaw_rate * t
    corrected_left = _rotate_xy(left, angle)
    corrected_right = _rotate_xy(right, -angle) + offset
    return np.linalg.norm((corrected_left - corrected_right)[:, :2], axis=1)


def _violation_cost(distances: np.ndarray, max_distance: float) -> float:
    """越界才计入。这正是"不等式约束"的意思 —— 没越界时不该有任何修正。"""
    excess = np.maximum(distances - max_distance, 0.0)
    return float(np.sum(excess * excess))


def _fit_differential_yaw(
    left: np.ndarray, right: np.ndarray, offset: np.ndarray, t: np.ndarray, max_distance: float
) -> tuple[float, bool]:
    """网格搜索 + 逐轮细化，拟合 `θ(t) = rate · t` 的漂移率。

    一个参数、目标函数分段可微 —— 网格 + 细化足够，而且没有引入优化器依赖（本仓库
    运行时只有 numpy 与 wt901）。每轮把范围缩到 1/5，五轮之后分辨率是初始范围的 1/3125。

    **零修正是起点也是并列时的胜者。** 目标函数在不越界时处处为零；若从任意点开始
    取 argmin，一个本来合规的会话会被转一个随意的角度。所以基准取 `rate = 0` 的代价，
    并且只在**严格**更优时才替换 —— 不等式约束在不越界时必须恒等。

    不用解析梯度：目标是 `max(0, d - d_max)²` 的和，在越界边界上不可微，而那正是最优解
    常在的地方。
    """
    best_rate = 0.0
    best_cost = _violation_cost(_distances(left, right, offset, t, 0.0), max_distance)

    centre = 0.0
    span = _YAW_RATE_RANGE
    for _ in range(_REFINEMENTS):
        for candidate in np.linspace(centre - span, centre + span, _GRID):
            cost = _violation_cost(
                _distances(left, right, offset, t, float(candidate)), max_distance
            )
            if cost < best_cost:
                best_cost = cost
                best_rate = float(candidate)
        centre = best_rate
        span /= 5.0

    return best_rate, abs(best_rate) > 0.95 * _YAW_RATE_RANGE


@dataclass(frozen=True)
class DualFootResult:
    """约束之后的两条轨迹与它的记录。

    契约 §4 的签名是 `-> tuple[NavResult, NavResult]`。多带一个 `report` 是因为
    "修正了多少"必须能进 `sync_report` 与质量标注 —— 一个只改数据不留记录的修正，
    三个月后没人能说清报告里的步宽是测出来的还是修出来的。契约文档同步修订。
    """

    left: NavResult
    right: NavResult
    report: DualFootReport


def apply_distance_constraint(
    left: NavResult,
    right: NavResult,
    cfg: AlgoConfig | None = None,
    *,
    step_width: float = DEFAULT_STEP_WIDTH,
) -> DualFootResult:
    """EDR 不等式约束：足间距越界时，对称地修正两足的差分航向。

    `left` / `right` 必须来自**同一段时间轴**（同一次会话的两只脚）。合成数据里这是
    天然成立的；真机上由 RAY-209 的主机侧时基保证，而它的误差（±10~30 ms）会以足间距
    的抖动形式进到这里 —— 那正是 RAY-213 要量化的东西。
    """
    cfg = cfg or AlgoConfig()
    if len(left.t) != len(right.t):
        raise DualFootError(
            f"两足的采样数不一致：{len(left.t)} vs {len(right.t)}。"
            "双足约束要求两条轨迹在同一段时间轴上 —— 对齐是同步层（RAY-209）的职责，"
            "在这里凑合会把同步误差伪装成航向误差。"
        )
    if not np.allclose(left.t, right.t):
        raise DualFootError("两足的时间轴不同。对齐属同步层，本模块不做重采样。")

    offset = np.array([0.0, -step_width, 0.0])
    max_distance = cfg.dualfoot_max_distance_m
    before = _distances(left.p, right.p, offset, left.t, 0.0)

    yaw_rate, hit_bound = _fit_differential_yaw(
        left.p, right.p, offset, left.t, max_distance
    )
    after = _distances(left.p, right.p, offset, left.t, yaw_rate)

    angle = 0.5 * yaw_rate * left.t
    report = DualFootReport(
        max_distance=max_distance,
        step_width=step_width,
        peak_distance_before=float(before.max()),
        peak_distance_after=float(after.max()),
        violation_fraction_before=float(np.mean(before > max_distance)),
        violation_fraction_after=float(np.mean(after > max_distance)),
        differential_yaw_rate=yaw_rate,
        hit_search_bound=hit_bound,
    )
    return DualFootResult(
        left=replace(left, p=_rotate_xy(left.p, angle)),
        right=replace(right, p=_rotate_xy(right.p, -angle)),
        report=report,
    )


def check_alternating_stance(
    left: NavResult, right: NavResult, cfg: AlgoConfig | None = None
) -> AlternatingStanceReport:
    """交替支撑一致性。整体设计 §5.7 第 2 条。

    双支撑期（两足同时着地）在走路里占 10~25%，**本身完全正常** —— 异常的是它持续
    太久（站着不动）或双足同时腾空太久（跑步腾空期之外）。所以判据是**最长持续时间**
    而不是占比。

    §5.7 的原文把"站立"排除在外，所以统计**只在行走窗口内**做：从第一次有脚离地到
    最后一次有脚离地。不排除的话，PRD §7 流程开头的静立 5 s 会被算成一段超长的双支撑
    期，每一次会话都报可疑 —— 一个永远报警的检查等于没有检查。

    只打标注，不拦截：PRD §13 的原则是全量计算 + 质量标注。
    """
    cfg = cfg or AlgoConfig()
    if len(left.t) != len(right.t):
        raise DualFootError(f"两足的采样数不一致：{len(left.t)} vs {len(right.t)}")
    if len(left.t) < 2:
        raise DualFootError("至少需要两个采样才能谈持续时间")

    dt = float(np.median(np.diff(left.t)))
    walking = _walking_window(left, right)
    both_stance = (left.zupt & right.zupt)[walking]
    neither = (~left.zupt & ~right.zupt)[walking]
    if both_stance.size == 0:
        # 整段两只脚都没动过：没有行走窗口，也就没有可判断的东西。返回零而不是 nan，
        # 并用 walking_samples 把"没得判"与"判过没问题"分开。会话级有效性（PRD §13
        # 的有效时长 ≥ 70%）会在别处拦下这种会话。
        return AlternatingStanceReport(0.0, 0.0, 0.0, 0.0, 0, False)

    longest_double = _longest_run(both_stance) * dt
    longest_flight = _longest_run(neither) * dt
    return AlternatingStanceReport(
        double_support_fraction=float(np.mean(both_stance)),
        flight_fraction=float(np.mean(neither)),
        longest_double_support_s=longest_double,
        longest_flight_s=longest_flight,
        walking_samples=int(both_stance.size),
        suspicious=(
            longest_double > cfg.dualfoot_double_support_max_s
            or longest_flight > cfg.dualfoot_flight_max_s
        ),
    )


def _walking_window(left: NavResult, right: NavResult) -> slice:
    """行走窗口：从第一次有脚离地到最后一次有脚离地。

    首尾的静立段（PRD §7 的流程要求静立 5 s 后开始）不属于步态，把它算进双支撑期
    统计会让每一次会话都报可疑。
    """
    moving = np.flatnonzero(~(left.zupt & right.zupt))
    if moving.size == 0:
        # 整段两只脚都没动过：没有行走窗口，交给调用方按空窗口处理。
        return slice(0, 0)
    return slice(int(moving[0]), int(moving[-1]) + 1)


def _longest_run(mask: np.ndarray) -> int:
    """最长的连续 True 长度。"""
    longest = current = 0
    for value in mask:
        current = current + 1 if value else 0
        longest = max(longest, current)
    return longest


def swapped(left: NavResult, right: NavResult) -> tuple[NavResult, NavResult]:
    """把两路数据对调。

    `NavResult` 没有 `label` 字段（契约 §3.3 —— 足别在 `FootSeries` 上），所以"交换"
    就是把两个对象换个位置。保留一个具名函数是为了让调用点读起来是"交换左右"，而不是
    "这两个变量为什么反着传"。

    它现在唯一的用处是**复核不可判定性**：把左右对调后重算 `lateral_separation`，两次的
    `nominal_left_lateral` 之和精确等于假设的步宽（见测试里的对调恒等式）。那条恒等式把
    "位置法给不出左右"从一个判断变成一个可以复核的事实。

    本模块**没有任何函数会给出左右结论**（需求修订 R2），所以也不存在"检测到戴反就把数据
    换过来"这条通路。戴反是一件要让操作员知道的事（PRD §6.1 的佩戴引导要给动作语言提
    示），静静地把数据换过来等于把一次真实的操作错误藏起来 —— 而下一次采集它还会发生。
    """
    return right, left
