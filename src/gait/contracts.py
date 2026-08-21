"""模块之间唯一认可的数据结构。

依据《模块划分与接口契约》§3 与 PRD v1.2 §6.1 / FR-02 / FR-09。

## 为什么它住在这里，而不是产出它的那一层

契约 §3 列出五个结构，却没说它们属于哪个模块。按产出者归属会立刻撞上红线：
§4 定义 `core/eskf.py: run_ins(series: FootSeries, ...)`，也就是 `core` 需要
`FootSeries` 这个类型；而 `FootSeries` 是"标定与同步之后"的产物，若它住在
`sync/` 或 `io/`，`core` 一 import 就违反 `core` 不得 import `io/`、`device/`、
`sync/` 的红线。

所以契约模块与 `config.py` 同级、扮演同一个角色：**谁都能读，它谁都不依赖**
（除 numpy 外）。这不改依赖 DAG，也不给红线开口子。

## 校验为什么是构造时做、且只做 O(1) 的检查

结构一旦跨层传递，错误的形状/dtype 会在很远的地方以难懂的方式炸开 —— 典型是
`(n,3)` 与 `(3,n)` 弄反，直到 ESKF 出现无意义的数值才被发现。因此在构造时就
拒绝。

但校验绝不扫描数组内容：`RawFrame` 在 200 Hz 双设备下每秒构造 400 次，逐样本
检查会把校验本身变成性能问题。所有检查都是 shape / dtype / 长度一致性这类
O(1) 判断。内容层面的判断（例如"加速度是否合理"）属于质量标注，只在
`gait/quality/` 实现一次（FR-08）。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import IntFlag
from typing import Any, Final, Literal

import numpy as np

#: 契约版本。§3 开头写明："任何一个变更都要同步改版本号"。跟随契约文档 v1.0。
#: 它会进入 SessionMeta，因而进入每一份历史会话 —— 三个月后判断某份报告用的是
#: 哪版结构，靠的就是这个数。
CONTRACT_VERSION: Final[str] = "1.0"

FootLabel = Literal["L", "R"]
Confidence = Literal["normal", "degraded", "invalid"]

_CONFIDENCE_VALUES: Final[frozenset[str]] = frozenset({"normal", "degraded", "invalid"})

#: SessionMeta 中 PRD v1.2 §6.1 明确要求"强制包含"的字段。列在这里而不是散在
#: 校验代码里，是为了让"强制"这件事有一处可读的声明。
MANDATORY_METADATA: Final[tuple[str, ...]] = (
    "algo_version",
    "algo_params",
    "calib_snapshot",
    "config_snapshot",
    "sync_report",
    "integrity_report",
    "protocol_config",
)


class ContractError(ValueError):
    """数据契约被违反。

    单独一个类型，好让上层能把"结构错了"与普通的取值错误区分开 —— 前者是编程
    错误，后者可能是数据问题。
    """


class Quality(IntFlag):
    """`FootSeries.quality` 的位掩码。

    契约 §3.2 写的是"饱和 / 插值 / 空洞边界"三类。给它们名字而不是让调用方记
    住 1/2/4，是因为掩码会写进会话文件并被离线工具读取，裸数字在三个月后没人
    认得。
    """

    NONE = 0
    SATURATED = 1  #: 任一加速度轴触及量程
    INTERPOLATED = 2  #: 该样本由插值得到，非实测
    GAP_EDGE = 4  #: 位于空洞边界，其邻域不连续


def _check_array(
    value: Any, name: str, *, shape: tuple[int | None, ...], dtype: Any = None
) -> np.ndarray:
    """形状与 dtype 的 O(1) 校验。`shape` 中的 None 表示该维任意。"""
    if not isinstance(value, np.ndarray):
        raise ContractError(f"{name} 必须是 np.ndarray，收到 {type(value).__name__}")
    if len(value.shape) != len(shape):
        raise ContractError(f"{name} 应为 {len(shape)} 维，收到 shape={value.shape}")
    for axis, (actual, expected) in enumerate(zip(value.shape, shape, strict=True)):
        if expected is not None and actual != expected:
            raise ContractError(
                f"{name} 的第 {axis} 维应为 {expected}，收到 shape={value.shape}"
            )
    if dtype is not None and value.dtype != dtype:
        raise ContractError(f"{name} 的 dtype 应为 {dtype}，收到 {value.dtype}")
    return value


def _check_same_length(name_to_array: dict[str, np.ndarray]) -> int:
    """同一序列内各数组的样本数必须一致，返回该长度。"""
    lengths = {name: len(array) for name, array in name_to_array.items()}
    if len(set(lengths.values())) != 1:
        detail = ", ".join(f"{n}={v}" for n, v in sorted(lengths.items()))
        raise ContractError(f"同一序列内各数组长度必须一致：{detail}")
    return next(iter(lengths.values()))


def _check_segments(segments: list[tuple[int, int]], n: int, name: str) -> None:
    """区间必须落在 [0, n]、非空、且按起点升序不重叠。

    检查顺序而不仅是范围，是因为下游按顺序遍历段来切分数据；乱序的段会静默地
    产出错乱的结果，而不是报错。
    """
    previous_end = 0
    for index, item in enumerate(segments):
        if not isinstance(item, tuple) or len(item) != 2:
            raise ContractError(f"{name}[{index}] 应为 (start, end) 二元组")
        start, end = item
        if not (0 <= start < end <= n):
            raise ContractError(
                f"{name}[{index}] = ({start}, {end}) 越界或为空，样本数为 {n}"
            )
        if start < previous_end:
            raise ContractError(
                f"{name} 必须按起点升序且互不重叠：{name}[{index}] = ({start}, {end}) "
                f"与前一段的终点 {previous_end} 冲突"
            )
        previous_end = end


@dataclass(frozen=True)
class RawFrame:
    """设备层输出。契约 §3.1。

    `t_host` 是**主机接收时刻**，不是采样时刻 —— 两者之间隔着 BLE 抖动。任何
    算法都不得直接把它当采样时刻用；真正的时间轴由 `sync/timebase.py` 构建。
    这句话在契约里是加粗的，此处再写一遍，因为字段名本身不会提醒任何人。
    """

    t_host: float
    acc_raw: np.ndarray  # (3,) int16 原始码值
    gyr_raw: np.ndarray  # (3,) int16
    ang_raw: np.ndarray  # (3,) int16 模块自算姿态，仅监控
    saturated: bool

    def __post_init__(self) -> None:
        _check_array(self.acc_raw, "acc_raw", shape=(3,), dtype=np.int16)
        _check_array(self.gyr_raw, "gyr_raw", shape=(3,), dtype=np.int16)
        _check_array(self.ang_raw, "ang_raw", shape=(3,), dtype=np.int16)


@dataclass
class FootSeries:
    """标定与同步之后、算法之前。契约 §3.2。

    契约称它为"整个系统最关键的契约"：它上游封装了协议、标定、同步的全部复杂
    度，下游算法层只需面对一个时间轴正确的干净序列。

    `fs` 是**实测**采样率（由锚点解出），不是标称的 200 Hz。用标称值会让所有
    时间参数系统性偏移。
    """

    label: FootLabel
    t: np.ndarray  # (n,) 统一时基下的时刻
    acc: np.ndarray  # (n,3) m/s²，已标定补偿 + 已重排到足部系
    gyr: np.ndarray  # (n,3) deg/s
    quality: np.ndarray  # (n,) uint8 位掩码，见 Quality
    segments: list[tuple[int, int]]  # 连续有效段，空洞在此切分
    fs: float

    def __post_init__(self) -> None:
        if self.label not in ("L", "R"):
            raise ContractError(f"label 应为 'L' 或 'R'，收到 {self.label!r}")
        _check_array(self.t, "t", shape=(None,))
        _check_array(self.acc, "acc", shape=(None, 3))
        _check_array(self.gyr, "gyr", shape=(None, 3))
        _check_array(self.quality, "quality", shape=(None,), dtype=np.uint8)
        n = _check_same_length(
            {"t": self.t, "acc": self.acc, "gyr": self.gyr, "quality": self.quality}
        )
        _check_segments(self.segments, n, "segments")
        if not self.fs > 0:
            raise ContractError(f"fs 必须为正的实测采样率，收到 {self.fs}")


@dataclass
class NavResult:
    """算法核心输出。契约 §3.3。

    `degraded` 标记使用了软零速（高速降级）的样本。它不是调试信息 —— 质量标注
    要靠它，所以和状态量一样是契约的一部分。
    """

    t: np.ndarray  # (n,)
    q: np.ndarray  # (n,4) 姿态四元数 足部系→导航系
    v: np.ndarray  # (n,3) m/s 导航系
    p: np.ndarray  # (n,3) m 导航系
    bg: np.ndarray  # (n,3) 陀螺零偏估计
    ba: np.ndarray  # (n,3) 加计零偏估计
    zupt: np.ndarray  # (n,) bool
    stances: list[tuple[int, int]]  # 支撑相区间
    degraded: np.ndarray  # (n,) bool 使用了软零速
    score: np.ndarray  # (n,) 零速检测统计量

    def __post_init__(self) -> None:
        _check_array(self.t, "t", shape=(None,))
        _check_array(self.q, "q", shape=(None, 4))
        for name in ("v", "p", "bg", "ba"):
            _check_array(getattr(self, name), name, shape=(None, 3))
        _check_array(self.zupt, "zupt", shape=(None,), dtype=np.bool_)
        _check_array(self.degraded, "degraded", shape=(None,), dtype=np.bool_)
        _check_array(self.score, "score", shape=(None,))
        n = _check_same_length(
            {
                name: getattr(self, name)
                for name in ("t", "q", "v", "p", "bg", "ba", "zupt", "degraded", "score")
            }
        )
        _check_segments(self.stances, n, "stances")


@dataclass
class GaitCycle:
    """分析层输出，逐步态一条。契约 §3.4。

    `valid` 与 `confidence` 并存不是冗余：PRD §13 要求**指标全量计算 + 质量标注**，
    无指标级门控。也就是说一条不可信的步态仍然要带着数值输出，由 `confidence`
    说明可信程度，而不是被丢掉。
    """

    foot: FootLabel
    idx: int
    t_ic: float  # 初始触地
    t_to: float  # 足尖离地
    t_ic_next: float
    stride_length: float  # m
    stride_time: float  # s
    gait_speed: float  # m/s
    stance_time: float
    swing_time: float
    stance_ratio: float  # %
    toe_clearance: float  # m
    strike_angle: float  # deg，正 = 后跟着地
    valid: bool
    confidence: Confidence

    def __post_init__(self) -> None:
        if self.foot not in ("L", "R"):
            raise ContractError(f"foot 应为 'L' 或 'R'，收到 {self.foot!r}")
        if self.confidence not in _CONFIDENCE_VALUES:
            raise ContractError(
                f"confidence 应为 {sorted(_CONFIDENCE_VALUES)} 之一，"
                f"收到 {self.confidence!r}"
            )
        if not (self.t_ic < self.t_to < self.t_ic_next):
            raise ContractError(
                "步态事件时刻必须严格递增 t_ic < t_to < t_ic_next，收到 "
                f"({self.t_ic}, {self.t_to}, {self.t_ic_next})"
            )


@dataclass
class SessionMeta:
    """可追溯性的载体。契约 §3.5，字段以 PRD v1.2 §6.1 为准。

    与契约 §3.5 的两处差异，均因 PRD v1.2 更新：

    * `subject_id` 改为 **`subject_uuid`**，且校验拒绝非 UUID 的值。FR-02 要求
      本地会话文件只含 `subject_uuid`、不落身份明文；一个叫 `subject_id` 的字段
      会让人顺手把机构档案号填进去，那正是 FR-02 要防的事。校验在这里就是防线
      本身，不是装饰。
    * 补上 **`protocol_config`**。契约 §3.5 没有它，但 PRD §6.1 把它列为强制字段
      （FR-09：测试时长等协议配置版本化，进报告页脚）。不同时长视为不同协议、
      不直接比较，所以它必须随会话存档。

    `algo_version` + `algo_params` 是科研可复现性的最低要求 —— 没有它们，三个月
    后没人能说清某份历史报告是用哪版算法算的。校验因此拒绝空值，而不只是拒绝
    缺席：一个空字典和没有这个字段，对复现来说是一回事。
    """

    session_id: str
    created_at: str
    subject_uuid: str
    scenario: str  # 'walk' / 'jog' / 'treadmill' / ...
    devices: dict[str, Any]  # {'L': {mac, name, fw_version}, 'R': {...}}
    config_snapshot: dict[str, Any]  # 下发的完整寄存器配置
    calib_snapshot: dict[str, Any]  # 本次使用的标定参数全量
    algo_version: str
    algo_params: dict[str, Any]
    sync_report: dict[str, Any]  # 锚点数量、残差、实测采样率
    integrity_report: dict[str, Any]  # 逐秒缺失率、空洞列表
    protocol_config: dict[str, Any]  # 测试时长等，PRD §6.1 / FR-09
    contract_version: str = CONTRACT_VERSION
    notes: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("session_id", "created_at", "scenario"):
            if not str(getattr(self, name)).strip():
                raise ContractError(f"{name} 不得为空")
        try:
            uuid.UUID(str(self.subject_uuid))
        except (ValueError, AttributeError, TypeError) as error:
            raise ContractError(
                f"subject_uuid 必须是 UUID，收到 {self.subject_uuid!r}。"
                "FR-02：本地会话文件只含 subject_uuid，不落身份明文 —— "
                "机构档案号不得写入此字段。"
            ) from error
        missing = [name for name in MANDATORY_METADATA if not getattr(self, name)]
        if missing:
            raise ContractError(
                f"PRD §6.1 要求会话元数据强制包含这些字段且非空：{missing}。"
                "空值与缺席对复现而言是一回事。"
            )
