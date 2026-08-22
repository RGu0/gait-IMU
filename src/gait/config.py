"""统一配置体系：算法参数与协议配置。

依据 PRD v1.2 §7（T-01 定时步行测试）与 FR-09（协议配置版本化，进报告页脚）。

## 这个模块被所有层读取，它谁都不依赖

与 `gait/contracts.py` 同样的角色。这个不对称是有意的 —— 配置若能反向依赖某一层，
它就成了穿透分层的后门。

## 不含设备配置

R2 决定设备侧采用外部库 `wt901`：输出速率、带宽、寄存器表都由它的 `Settings` 与
`RegisterAccess` 表达，`SessionMeta.config_snapshot` 直接取自其 `applied_writes`。
在这里再写一份寄存器映射，就是把设备知识散回业务仓库 —— 引入那个库的初衷正是不这么做。

## 数值默认值是结构占位，不是标定结果

`AlgoConfig` 的阈值与窗口长度**尚未标定**。它们的真实取值属于 RAY-203（自适应零速检测
与低速/病理预设）与 RAY-204（ESKF）。此处给出的是能让结构成立、让预设机制可测的占位值，
每一个都在注释里标了归属。

因此本模块的测试不断言具体数字，而断言**关系**：低速预设的窗口必须更长、阈值必须更松、
ZARU 必须强制开启（PRD §7 的原话）。关系在标定之后依然成立，数字不会。
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields, replace
from typing import Any, Final, Literal

#: 配置结构的版本。改变任一字段的**含义**都要升它，改默认值不必 —— 默认值随
#: 标定演进是常态，含义变化才会让历史快照无法被正确读回。
#:
#: 它写进每一份快照。`from_snapshot` 拒绝不认识的版本，否则"版本化"（FR-09）
#: 只是把一个数字存下来而已，没有任何东西依赖它。
CONFIG_VERSION: Final[str] = "1.0"

#: PRD §7：默认 180 s，可配 60/120/180。**时长是系统配置项，服务方预设，机构侧
#: 不可改**，因此校验拒绝预设之外的值 —— 一个"差不多"的 175 s 会产生一份既不能与
#: 180 s 组比较、也不属于任何已知协议的数据。
DURATION_PRESETS: Final[tuple[int, ...]] = (60, 120, 180)
DEFAULT_DURATION_S: Final[int] = 180

#: 疲劳衰减（后 1/3 与前 1/3 时段的步速差）只在这个时长下输出。PRD §7 明写此限；
#: 60 s 下前后各 20 s，样本量不足以支撑该差值。
FATIGUE_DECAY_DURATION_S: Final[int] = 180

AlgoPreset = Literal["default", "low_speed"]


class ConfigError(ValueError):
    """配置取值非法。

    与 `contracts.ContractError` 分开：那是结构错误（编程错误），这是取值错误
    （可能来自配置文件或界面），两者的处置不同。
    """


def _positive(value: float, name: str) -> float:
    if not value > 0:
        raise ConfigError(f"{name} 必须为正，收到 {value}")
    return value


@dataclass(frozen=True)
class ProtocolConfig:
    """T-01 定时步行测试的协议配置。PRD v1.2 §7。

    frozen：协议配置在会话开始时固定并写入元数据。中途可变的话，元数据记录的就
    不再是这次测试实际使用的协议 —— 而"不同时长视为不同协议、不直接比较"这条规则
    正是建立在元数据可信之上。
    """

    #: 测试时长。服务方预设，机构侧不可改。
    duration_s: int = DEFAULT_DURATION_S
    #: 累计有效时长低于配置时长的这个比例时提示重测（PRD §7：70%）。
    valid_fraction: float = 0.70
    #: 中途停顿超过这个秒数即标记该时段并跳过，不作废测试（PRD §7：5 s）。
    pause_threshold_s: float = 5.0
    #: 直行段首尾各剔除的步数。PRD §7 默认 1，且"参数可调且分离结果存档可复查"。
    trim_steps_per_segment: int = 1
    version: str = CONFIG_VERSION

    def __post_init__(self) -> None:
        if self.duration_s not in DURATION_PRESETS:
            raise ConfigError(
                f"duration_s 必须是预设之一 {DURATION_PRESETS}，收到 {self.duration_s}。"
                "PRD §7：时长是服务方预设的系统配置项，不同时长视为不同协议、不直接比较；"
                "预设之外的值会产生一份不属于任何已知协议的数据。"
            )
        if not 0 < self.valid_fraction <= 1:
            raise ConfigError(f"valid_fraction 应在 (0, 1] 内，收到 {self.valid_fraction}")
        _positive(self.pause_threshold_s, "pause_threshold_s")
        if self.trim_steps_per_segment < 0:
            raise ConfigError(
                f"trim_steps_per_segment 不得为负，收到 {self.trim_steps_per_segment}"
            )

    @property
    def minimum_valid_seconds(self) -> float:
        """低于这个有效时长就提示重测。由时长与比例算出，不单独配置。

        两个都可配的话，它们迟早会相互矛盾（例如 180 s 配 200 s 的下限），而矛盾
        发生时没有任何一方是权威。
        """
        return self.duration_s * self.valid_fraction

    @property
    def fatigue_decay_available(self) -> bool:
        """疲劳衰减是否输出。PRD §7 明写只在 180 s 配置下输出。

        做成属性而不是让分析层各自判断：那条规则只该有一处实现，否则报告与指标
        计算可能对"这次要不要出疲劳衰减"给出不同答案。
        """
        return self.duration_s == FATIGUE_DECAY_DURATION_S

    def snapshot(self) -> dict[str, Any]:
        return _snapshot(self)

    @classmethod
    def from_snapshot(cls, data: dict[str, Any]) -> ProtocolConfig:
        return _from_snapshot(cls, data)


@dataclass(frozen=True)
class AlgoConfig:
    """算法参数。

    **下列数值均为结构占位，尚未标定** —— 真实取值属于 RAY-203（零速检测与低速预设）
    与 RAY-204（ESKF）。它们存在的意义是让配置结构、预设切换与快照往返可以被测试；
    测试断言的是预设之间的**关系**，不是具体数字。
    """

    preset: AlgoPreset = "default"
    #: 零速检测的滑窗长度（样本数）。占位，待 RAY-203 标定。
    zupt_window_samples: int = 15
    #: 加速度幅值判据阈值，m/s²。占位，待 RAY-203 标定。
    zupt_acc_threshold: float = 0.35
    #: 角速度幅值判据阈值，rad/s（R2：全链路 SI）。占位，待 RAY-203 标定。
    zupt_gyr_threshold: float = 0.30
    #: 强制零角速度更新。低速/病理步态下支撑相角速度不一定过阈，PRD §7 要求该预设
    #: 强制 ZARU。
    force_zaru: bool = False
    version: str = CONFIG_VERSION

    def __post_init__(self) -> None:
        if self.preset not in ("default", "low_speed"):
            raise ConfigError(f"preset 应为 'default' 或 'low_speed'，收到 {self.preset!r}")
        if self.zupt_window_samples <= 0:
            raise ConfigError(
                f"zupt_window_samples 必须为正，收到 {self.zupt_window_samples}"
            )
        _positive(self.zupt_acc_threshold, "zupt_acc_threshold")
        _positive(self.zupt_gyr_threshold, "zupt_gyr_threshold")

    @classmethod
    def low_speed(cls) -> AlgoConfig:
        """低速/病理步态预设。PRD §7：更长窗口、更松阈值、强制 ZARU。

        档案勾选「拖步/小碎步」时自动切换。**采集这些波形本身就是 v1 的目标**，
        所以这个预设的存在不是为了让检测"看起来更好"，而是为了让本来就该被采到的
        步态不被默认阈值判成噪声。

        倍数同样是占位（待 RAY-203），但方向不是：更长、更松、强制 ZARU 是 PRD 的
        原话，测试断言的正是这三个方向。
        """
        base = cls()
        return replace(
            base,
            preset="low_speed",
            zupt_window_samples=base.zupt_window_samples * 2,
            zupt_acc_threshold=base.zupt_acc_threshold * 2,
            zupt_gyr_threshold=base.zupt_gyr_threshold * 2,
            force_zaru=True,
        )

    def snapshot(self) -> dict[str, Any]:
        return _snapshot(self)

    @classmethod
    def from_snapshot(cls, data: dict[str, Any]) -> AlgoConfig:
        return _from_snapshot(cls, data)


def _snapshot(config: Any) -> dict[str, Any]:
    """配置对象 → 可写入 `SessionMeta` 的普通字典。

    直接由对象产出而不是让调用方手写，是"任一历史会话可凭元数据精确复现算法输入"
    这条验收标准的前提：手写的快照可以漏字段，而漏掉的那个字段正是复现失败的原因。
    """
    return {f.name: getattr(config, f.name) for f in fields(config)}


def _from_snapshot(cls: type, data: dict[str, Any]) -> Any:
    """快照 → 配置对象。往返必须相等，否则元数据无法复现输入。"""
    if not isinstance(data, dict):
        raise ConfigError(f"快照必须是字典，收到 {type(data).__name__}")
    version = data.get("version")
    if version != CONFIG_VERSION:
        # 认不出的版本必须拒绝，不能按当前字段"尽力"解读：一个含义已经改变的
        # 字段会被静默地当成现在的含义，而那正是复现出错却无人知晓的方式。
        raise ConfigError(
            f"{cls.__name__} 快照的版本是 {version!r}，本代码只认识 {CONFIG_VERSION!r}。"
            "拒绝按当前字段解读历史快照 —— 含义变化会静默产生错误的复现。"
        )
    known = {f.name for f in fields(cls)}
    unknown = sorted(set(data) - known)
    if unknown:
        raise ConfigError(f"{cls.__name__} 快照含未知字段：{unknown}")
    missing = sorted(known - set(data))
    if missing:
        raise ConfigError(f"{cls.__name__} 快照缺少字段：{missing}")
    return cls(**data)


@dataclass(frozen=True)
class SessionConfig:
    """一次会话用到的全部本仓库配置。

    把两者装在一起，是为了让「写进元数据」成为一个动作而不是两个 —— 分两次写，
    就有一次被漏掉的可能，而漏掉的那半正是复现不出来的那半。

    设备配置不在其中：它由 wt901 的 `applied_writes` 提供，直接进
    `SessionMeta.config_snapshot`。
    """

    protocol: ProtocolConfig = field(default_factory=ProtocolConfig)
    algo: AlgoConfig = field(default_factory=AlgoConfig)

    def snapshot(self) -> dict[str, Any]:
        """按 `SessionMeta` 的字段名分装。

        键名对应 PRD §6.1 的强制元数据字段：`protocol_config` 与 `algo_params`。
        在这里对齐，调用方就不必记住哪个配置写进哪个字段。
        """
        return {
            "protocol_config": self.protocol.snapshot(),
            "algo_params": self.algo.snapshot(),
        }

    @classmethod
    def from_snapshot(cls, data: dict[str, Any]) -> SessionConfig:
        if not isinstance(data, dict):
            raise ConfigError(f"快照必须是字典，收到 {type(data).__name__}")
        for key in ("protocol_config", "algo_params"):
            if key not in data:
                raise ConfigError(f"会话配置快照缺少 {key}")
        return cls(
            protocol=ProtocolConfig.from_snapshot(data["protocol_config"]),
            algo=AlgoConfig.from_snapshot(data["algo_params"]),
        )
