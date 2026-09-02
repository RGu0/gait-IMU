"""真机行走协议的精度验证：已知几何真值下的距离误差、闭环误差、协议一致性。

契约 §1 的 `validate/`（L8）—— 与 `synthetic.py`、`v3prime.py` 同一个定位：它不是
产品流程的一部分，而是**验证点的可执行部分**，可以被 CLI 调用。

## 它回答的问题

RAY-230（PRD v1.2 §17.1 V1）的三条判据：

1. 50 m 直线误差 < 3%
2. 矩形闭环误差 < 1.5%
3. 4 米往返协议与长直线协议的一致性偏差**可量化**并写入协议说明

前两条是"算得准不准"，第三条是"换个场地口径会不会读出不同的数"。第三条没有
及格线 —— 判据原文是"可量化并写入协议说明"，所以本模块**报出偏差、不判定它**
（`ProtocolConsistency.passed` 恒为 None，语义见该类文档）。把一个用户从未定过的
门槛写进代码，就是发明判据。

## 判据只有一处家

三条判据写在 `STRAIGHT_LINE_MAX_ERROR`、`CLOSED_LOOP_MAX_ERROR` 与
`CONSISTENCY_REPORTING_ONLY`，各自的 `verdict` 是它们唯一的执行点。判据**开跑前
定死、跑完不得修改**（《06 测试与验证方案》§5 的冻结声明，照 `v3prime.py` 的
`NEGLIGIBLE_*` 先例）—— 写成具名常量而不是散在判断里，是为了让"有没有人在跑完
之后动过判据"这件事在 git 历史里一眼可查。

## 空样本返回 None，不返回"合格"

每个 `verdict` 在没有样本时返回 `None`。**"没数据"不是"合格"** —— 一条没跑过的
判据和一条跑过且通过的判据，在报告里必须长得不一样，否则"全绿"会把"没采到"读成
"验过了"。这与 `v3prime.Verdict.negligible` 是同一条约定。

## 三种协议各用各的尺，共用一把必然有一种是错的

* **直线趟**的真值是卷尺量出的**路径长度**。受试者沿直线走，首末位移的模长即路径
  长度；用 `‖p[-1] − p[0]‖` 而不是逐样本弧长积分，因为弧长会把 ESKF 在支撑相里
  的抖动也积进去，那是噪声不是路程。
* **闭环趟**的真值是**零**（走回起点）。误差取残差模长与周长之比 —— 闭环的意义
  就在于真值恒为零且与走的路径无关，它量的是航向漂移的累积，而直线趟量不到这个。
* **往返趟**的形状与闭环一样"回到原点"，但要问的问题相反：闭环问"回得准不准"，
  往返问"这条道有多长"。拿首末位移去量往返会读出约 −100% 的误差，那是在回答闭环
  的问题。往返取轨迹在**行走主轴上的跨度**，对应单程长度。

## 一致性偏差为什么按"每米"归一

4 米往返一趟只有 4 m，长直线一趟 45~50 m。两者的绝对误差没有可比性，能比的是
**单位距离的误差**。转身多、加减速多的往返协议若系统性地偏高，差值就是换协议要
付的代价，也正是要写进协议说明的那个数。
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final

import numpy as np

from gait.contracts import NavResult

#: 50 m 直线的相对距离误差上限（RAY-230 判据一，PRD v1.2 §17.1 V1）。
STRAIGHT_LINE_MAX_ERROR: Final[float] = 0.03
#: 矩形闭环的相对误差上限（RAY-230 判据二）。比直线严，因为闭环的真值恒为零，
#: 读数里没有"路径长度量得准不准"这一项，它纯粹是航向漂移。
CLOSED_LOOP_MAX_ERROR: Final[float] = 0.015
#: 判据三**没有**门槛。原文是"一致性偏差可量化并写入协议说明"——量化即达成，
#: 没有及格线。写成一个具名常量而不是留白，是为了让"这里为什么不判定"在代码里
#: 有据可查，而不是看起来像漏写了一条。
CONSISTENCY_REPORTING_ONLY: Final[bool] = True

#: 本报告的结构版本。结论会被 PRD §17 与协议说明引用，读的人要知道按哪版判据算的。
PROTOCOL_REPORT_VERSION: Final[str] = "1.0"

#: 协议标识。`SHUTTLE` 是 4 米往返，`STRAIGHT` 是长直线，`LOOP` 是矩形闭环。
PROTOCOL_STRAIGHT: Final[str] = "straight"
PROTOCOL_SHUTTLE: Final[str] = "shuttle"
PROTOCOL_LOOP: Final[str] = "loop"

__all__ = [
    "CLOSED_LOOP_MAX_ERROR",
    "CONSISTENCY_REPORTING_ONLY",
    "PROTOCOL_LOOP",
    "PROTOCOL_REPORT_VERSION",
    "PROTOCOL_SHUTTLE",
    "PROTOCOL_STRAIGHT",
    "STRAIGHT_LINE_MAX_ERROR",
    "ClosedLoopVerdict",
    "ProtocolConsistency",
    "ProtocolError",
    "StraightLineVerdict",
    "TrialGeometry",
    "TrialMeasurement",
    "evaluate_trial",
    "summarize",
]


class ProtocolError(ValueError):
    """协议验证的输入不满足前提。"""


def _finite(value: float | None) -> float | None:
    """非有限值以 None 落盘：`json.dumps` 会把 nan 写成裸 `NaN`，那不是合法 JSON。"""
    if value is None:
        return None
    return value if math.isfinite(value) else None


@dataclass(frozen=True)
class TrialGeometry:
    """一趟的已知几何真值。由卷尺／场地标记给出，不由算法给出。"""

    #: 趟次标识，用于把结果对回原始记录。
    label: str
    #: 协议：`PROTOCOL_STRAIGHT` / `PROTOCOL_SHUTTLE` / `PROTOCOL_LOOP`。
    protocol: str
    #: 真值距离，m。**三种协议的口径不同**，因为三者能被测到的量本来就不同：
    #:
    #: * `straight`：整趟直线长度（如 50 m）。读数是首末位移模长。
    #: * `shuttle`：**单程**长度（如 4 m），不是往返累计。往返走完回到原点，首末
    #:   位移按构造接近零，测不到累计路程；能测到的是**这条道有多长**，即轨迹在
    #:   行走主轴上的跨度。所以真值填单程。
    #: * `loop`：**周长**（用于把残差归一），不是 0。
    distance_m: float

    def __post_init__(self) -> None:
        if self.protocol not in (PROTOCOL_STRAIGHT, PROTOCOL_SHUTTLE, PROTOCOL_LOOP):
            raise ProtocolError(f"未知协议 {self.protocol!r}")
        if not math.isfinite(self.distance_m) or self.distance_m <= 0.0:
            raise ProtocolError(
                f"{self.label}：真值路径长度须为正的有限值，得到 {self.distance_m!r}。"
                "闭环趟填周长而不是 0 —— 残差要除以它才谈得上相对误差"
            )


@dataclass(frozen=True)
class TrialMeasurement:
    """一趟的算法读数与真值的对照。"""

    geometry: TrialGeometry
    #: 各足测得的量，m。口径随协议变（见 `_path_length`）：直线是首末位移模长，
    #: 往返是主轴跨度，闭环是残差模长。
    per_foot: dict[str, float]

    @property
    def measured_m(self) -> float:
        """两足取均值。

        取均值而不是任选一足：左右各有独立的零偏与航向漂移，单足读数的方差明显
        大于均值。两足**不是**独立重复测量（同一次行走、同一条路径），所以均值
        只用来降方差，不用来算标准误。
        """
        values = [value for value in self.per_foot.values() if math.isfinite(value)]
        return float(np.mean(values)) if values else float("nan")

    @property
    def error(self) -> float:
        """相对误差。直线／往返是有符号的（正 = 测长了），闭环恒非负。

        闭环的真值是零，残差没有方向可言 —— 它的"符号"只反映漂到了哪一侧，
        对判据无意义，所以那一支返回残差与周长之比本身。
        """
        measured = self.measured_m
        if not math.isfinite(measured):
            return float("nan")
        truth = self.geometry.distance_m
        if self.geometry.protocol == PROTOCOL_LOOP:
            return measured / truth
        return (measured - truth) / truth

    @property
    def error_per_m(self) -> float:
        """单位距离的相对误差 —— 这是**跨协议唯一可比**的量。

        4 米往返一趟只有 4 m，长直线一趟 45~50 m，绝对误差没有可比性。相对误差
        本身已经除过一次距离，这里再除一次是为了回答"每走一米积多少误差"，
        而往返协议的转身开销正体现在这个量上。
        """
        error = self.error
        return (
            error / self.geometry.distance_m if math.isfinite(error) else float("nan")
        )

    def snapshot(self) -> dict[str, Any]:
        return {
            "label": self.geometry.label,
            "protocol": self.geometry.protocol,
            "truth_m": self.geometry.distance_m,
            "measured_m": _finite(self.measured_m),
            "per_foot_m": {
                foot: _finite(value) for foot, value in self.per_foot.items()
            },
            "error": _finite(self.error),
            "error_per_m": _finite(self.error_per_m),
        }


def _principal_extent(positions: np.ndarray) -> float:
    """轨迹在**行走主轴**上的跨度（峰到峰），m。

    往返走的是一条道的来回，位置在水平面上落成一条线段；主轴就是这条线段的方向。
    取水平面内方差最大的方向（两点协方差的主特征向量）投影后取 ptp。

    只用水平两轴：竖直方向是步态起伏，与道有多长无关，混进来会把主轴拧斜。
    """
    horizontal = positions[:, :2]
    centred = horizontal - horizontal.mean(axis=0)
    # 2×2 协方差的主特征向量。样本数远大于 2，`eigh` 对称矩阵路径是稳的。
    _, vectors = np.linalg.eigh(centred.T @ centred)
    axis = vectors[:, -1]
    return float(np.ptp(centred @ axis))


def _path_length(nav: NavResult, protocol: str) -> float:
    """一足在一趟里测得的、与该协议真值同口径的量。

    三种协议测的是三样东西，共用一个式子必然有一种是错的：

    * **直线**取**首末位移的模长**。不取逐样本弧长：弧长会把 ESKF 在支撑相里的
      位置抖动一并积进去，那是噪声不是路程，且恒为正 —— 只会让读数系统性偏大。
    * **往返**取轨迹在行走主轴上的**跨度**。往返走完回到原点，首末位移按构造接近
      零 —— 拿它去比累计路程会读出约 −100% 的误差，那量的是"回没回到原点"，不是
      "走得准不准"。跨度对应的是单程长度，所以真值填单程（见 `TrialGeometry`）。
    * **闭环**取**残差模长**，真值恒为零。
    """
    if nav.p.shape[0] < 2:
        raise ProtocolError("轨迹不足两个样本，算不出位移")
    if protocol == PROTOCOL_SHUTTLE:
        return _principal_extent(nav.p)
    return float(np.linalg.norm(nav.p[-1] - nav.p[0]))


def evaluate_trial(
    geometry: TrialGeometry, feet: dict[str, NavResult]
) -> TrialMeasurement:
    """把一趟的两足轨迹折成一个与真值可比的读数。"""
    if not feet:
        raise ProtocolError(f"{geometry.label}：没有任何一足的轨迹")
    per_foot = {
        foot: _path_length(nav, geometry.protocol) for foot, nav in sorted(feet.items())
    }
    return TrialMeasurement(geometry=geometry, per_foot=per_foot)


@dataclass(frozen=True)
class StraightLineVerdict:
    """判据一：50 m 直线误差 < 3%。"""

    trials: tuple[TrialMeasurement, ...]

    @property
    def errors(self) -> np.ndarray:
        return np.array([trial.error for trial in self.trials], dtype=float)

    @property
    def max_abs_error(self) -> float:
        errors = np.abs(self.errors)
        errors = errors[np.isfinite(errors)]
        return float(errors.max()) if errors.size else float("nan")

    @property
    def passed(self) -> bool | None:
        """判据一的唯一执行点。样本为空时返回 None —— "没数据"不是"合格"。

        取**最大**而不是中位：三趟里有一趟超标就是超标。判据写的是"50 m 直线
        误差 < 3%"，不是"典型误差 < 3%"。
        """
        errors = np.abs(self.errors)
        errors = errors[np.isfinite(errors)]
        if not errors.size:
            return None
        return bool(errors.max() < STRAIGHT_LINE_MAX_ERROR)

    def snapshot(self) -> dict[str, Any]:
        return {
            "trials": [trial.snapshot() for trial in self.trials],
            "max_abs_error": _finite(self.max_abs_error),
            "criterion": {
                "max_abs_error": STRAIGHT_LINE_MAX_ERROR,
                "source": "RAY-230 判据一 / PRD v1.2 §17.1 V1",
            },
            "passed": self.passed,
        }


@dataclass(frozen=True)
class ClosedLoopVerdict:
    """判据二：矩形闭环误差 < 1.5%。"""

    trials: tuple[TrialMeasurement, ...]

    @property
    def errors(self) -> np.ndarray:
        return np.array([trial.error for trial in self.trials], dtype=float)

    @property
    def max_error(self) -> float:
        errors = self.errors[np.isfinite(self.errors)]
        return float(errors.max()) if errors.size else float("nan")

    @property
    def passed(self) -> bool | None:
        """判据二的唯一执行点。样本为空时返回 None —— "没数据"不是"合格"。"""
        errors = self.errors[np.isfinite(self.errors)]
        if not errors.size:
            return None
        return bool(errors.max() < CLOSED_LOOP_MAX_ERROR)

    def snapshot(self) -> dict[str, Any]:
        return {
            "trials": [trial.snapshot() for trial in self.trials],
            "max_error": _finite(self.max_error),
            "criterion": {
                "max_error": CLOSED_LOOP_MAX_ERROR,
                "source": "RAY-230 判据二 / PRD v1.2 §17.1 V1",
            },
            "passed": self.passed,
        }


@dataclass(frozen=True)
class ProtocolConsistency:
    """判据三：4 米往返与长直线的一致性偏差。**量化，不判定。**

    `passed` 恒为 `None` 而不是布尔值 —— 这不是"未实现"，是判据原文就没有门槛：
    "一致性偏差可量化并写入协议说明"。量化即达成。给它编一个及格线，就是发明
    一条用户没定过的判据（契约：不得发明验收标准）。

    `bias` 的符号有意义：正 = 往返协议每米积的误差**多于**长直线，也就是转身与
    加减速要付的代价。协议说明里要写的就是这个数。
    """

    straight: tuple[TrialMeasurement, ...]
    shuttle: tuple[TrialMeasurement, ...]

    @staticmethod
    def _per_m(trials: Sequence[TrialMeasurement]) -> np.ndarray:
        values = np.array([trial.error_per_m for trial in trials], dtype=float)
        return values[np.isfinite(values)]

    @property
    def bias(self) -> float | None:
        """两组"每米误差"中位数之差。任一组为空时返回 None。

        取中位而不是均值：每组只有三趟，一趟异常就能把均值拽走。
        """
        straight = self._per_m(self.straight)
        shuttle = self._per_m(self.shuttle)
        if not straight.size or not shuttle.size:
            return None
        return float(np.median(shuttle) - np.median(straight))

    @property
    def quantified(self) -> bool | None:
        """判据三的唯一执行点：偏差**算出来了没有**。

        空样本返回 None（"没数据"不是"合格"）；算出来了返回 True。它不检查偏差
        的大小，因为判据没有给大小定门槛。
        """
        bias = self.bias
        if bias is None:
            return None
        return math.isfinite(bias)

    @property
    def passed(self) -> None:
        """恒为 None：判据三没有及格线。见类文档。"""
        return None

    def snapshot(self) -> dict[str, Any]:
        return {
            "straight": [trial.snapshot() for trial in self.straight],
            "shuttle": [trial.snapshot() for trial in self.shuttle],
            "bias_per_m": _finite(self.bias),
            "criterion": {
                "reporting_only": CONSISTENCY_REPORTING_ONLY,
                "source": "RAY-230 判据三 / PRD v1.2 §17.1 V1",
                "note": "原文为「可量化并写入协议说明」，没有门槛；量化即达成",
            },
            "quantified": self.quantified,
            "passed": self.passed,
        }


def summarize(measurements: Sequence[TrialMeasurement]) -> dict[str, Any]:
    """把逐趟读数折成 RAY-230 的三条判据。

    分组按 `TrialGeometry.protocol`，不按趟次名 —— 名字是现场记的，协议是数据
    模型里的，后者才该决定一趟进哪条判据。
    """
    straight = tuple(
        m for m in measurements if m.geometry.protocol == PROTOCOL_STRAIGHT
    )
    shuttle = tuple(m for m in measurements if m.geometry.protocol == PROTOCOL_SHUTTLE)
    loop = tuple(m for m in measurements if m.geometry.protocol == PROTOCOL_LOOP)

    straight_verdict = StraightLineVerdict(trials=straight)
    loop_verdict = ClosedLoopVerdict(trials=loop)
    consistency = ProtocolConsistency(straight=straight, shuttle=shuttle)

    return {
        "version": PROTOCOL_REPORT_VERSION,
        "trials": len(measurements),
        "straight_line": straight_verdict.snapshot(),
        "closed_loop": loop_verdict.snapshot(),
        "protocol_consistency": consistency.snapshot(),
        "decision": _decision(straight_verdict, loop_verdict, consistency),
    }


def _decision(
    straight: StraightLineVerdict,
    loop: ClosedLoopVerdict,
    consistency: ProtocolConsistency,
) -> str:
    """本管线**能**定的那一半。

    三条判据里任何一条没数据，结论就是"未验"而不是"未通过"—— 两者要在报告里
    分得开，否则"没采到"会被读成"验过了但没过"，而这两件事的下一步动作完全不同。
    """
    missing = [
        name
        for name, verdict in (
            ("50 m 直线", straight.passed),
            ("矩形闭环", loop.passed),
            ("协议一致性", consistency.quantified),
        )
        if verdict is None
    ]
    if missing:
        return f"未验：{'、'.join(missing)}尚无数据（「没数据」不是「合格」）"
    if straight.passed and loop.passed:
        return "通过：直线与闭环均在判据内，协议一致性偏差已量化"
    failed = [
        name
        for name, ok in (("50 m 直线", straight.passed), ("矩形闭环", loop.passed))
        if not ok
    ]
    return f"未通过：{'、'.join(failed)}超出判据"
