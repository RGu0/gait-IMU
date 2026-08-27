"""双设备会话编排：准入、断连语义与收尾遥测。契约 §1 的 `device/`（F1.3 → RAY-197）。

PRD §6.1 / FR-07：「电量在开启高速流**之前**读取记录（<30% 阻断）；采集结束后再读
电量+温度记录温升」「两路独立 BLE 连接，任一路异常不影响另一路安全收尾」。

## 这个模块只做判断，不建连接

连接、重连、合流都由 wt901 提供（`WT901Device` / `merge`），本仓库不重复实现。
留给这里的是**会话语义**：这次能不能开、开完算不算完整、要把什么记进元数据。

把判断与 I/O 分开是为了让它们可测：一次「电量不足该不该阻断」的判断不需要蓝牙，
而它恰恰是最该被钉住的部分 —— 判错的代价是一轮 30 分钟的采集跑到一半没电。

## 电量有三种状态，不是两种

`Battery.percent` 可能是 `None`：wt901 的 `battery_percent` 对不可能是真实测量的
原始值（`raw <= 0`）返回 `None`，理由是「一台刚刚回答完寄存器读的设备不可能是
0 V」，若把它套进阶梯表最低一档就会变成一个看着正常的「没电了」。`read_battery`
本身也可能整个失败（返回 `None`）。

所以准入必须分三路：**够电**放行、**低电**阻断、**读不到**也阻断 —— 但后两者
给的理由不同。把「电量未知」说成「电量不足」会让操作者去换电池，而真正的问题在
别处，这正是 wt901 那条 docstring 想避免的事。

阻断「未知」而不是放行，是因为 PRD 要求电量在开流前被**记录**；记不到就等于这条
前置条件没有成立，而不是成立了但没写下来。

## 判据用 percent 而不是 raw

`battery_percent` 的阶梯表里 30% 是一个**精确档位**（`raw >= 373`），所以
`percent < 30` 与 `raw < 373` 是同一个判据，不存在边界抖动。用 percent 的好处是
它就是操作者在界面上看到的那个数，阻断理由与他看到的一致。`raw` 仍然记进结果
（阶梯很粗，`350~367` 全报 10%，细判只能看它）。

## 单路断连：另一路继续，会话标为不完整

wt901 的 `merge()` 已经保证「一台掉线不让整条合流停住」（有界延迟归并，
WT901 RAY-190 修过按流付等待预算导致存活流塌到 19.5 Hz 的缺陷）。这里要补的是
**会话侧的记账**：哪只脚掉过线、掉了几次、这份会话还能不能当完整数据用。

「另一路数据完整」与「会话完整」是两回事：前者成立不代表后者成立。一份缺了一只
脚后半段的会话，双足指标（步长对称性、双支撑期）就不再可算 —— 标记不完整是为了
让下游知道该拒绝算哪些量，而不是让它把半份数据当整份用。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Final

from wt901 import Battery, ReconnectPolicy

from gait.contracts import FootLabel

__all__ = [
    "MIN_BATTERY_PERCENT",
    "LinkOutcome",
    "OrchestrationError",
    "PreflightVerdict",
    "SessionOutcome",
    "preflight_battery",
    "reconnect_snapshot",
    "summarize_session",
]

#: PRD §6.1 的开流前电量下限。低于它阻断会话。
#:
#: 用 percent 而非 raw：30 在 `battery_percent` 的阶梯表里是精确档位
#: （`raw >= 373`），两者是同一个判据，而 percent 是操作者看到的那个数。
MIN_BATTERY_PERCENT: Final[int] = 30

_LABELS: Final[tuple[FootLabel, ...]] = ("L", "R")
_SIDES: Final[dict[str, str]] = {"L": "左脚", "R": "右脚"}


class OrchestrationError(ValueError):
    """会话编排的入参不自洽。"""


def _check_label(label: str) -> FootLabel:
    if label not in _LABELS:
        raise OrchestrationError(f"脚标必须是 'L' 或 'R'，收到 {label!r}")
    return label  # type: ignore[return-value]


@dataclass(frozen=True, slots=True)
class PreflightVerdict:
    """开流前的准入结论。

    `problems` 是给操作者的可执行理由：「电量不足」要去换电池，「电量读不到」
    要去查连接 —— 两者的动作不同，所以不能合并成一句。
    """

    admitted: bool
    problems: tuple[str, ...] = ()
    readings: dict[str, dict[str, Any]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.admitted and self.problems:
            raise OrchestrationError("准入通过时不应带 problems —— 那会让调用方两头猜")


def _battery_entry(battery: Battery | None) -> dict[str, Any]:
    if battery is None:
        return {"percent": None, "raw": None, "read": False}
    return {"percent": battery.percent, "raw": battery.raw, "read": True}


def preflight_battery(readings: dict[str, Battery | None]) -> PreflightVerdict:
    """开高速流**之前**的电量准入。

    `readings` 是每只脚读到的电量；值为 `None` 表示这次没读到。**两只脚都必须
    有条目** —— 少一只不是「那只脚没问题」，是这次准入没覆盖它。

    三路判定见模块文档：够电放行、低电阻断、读不到也阻断但理由不同。
    """
    if not isinstance(readings, dict):
        raise OrchestrationError(
            f"readings 必须是 dict，收到 {type(readings).__name__}"
        )
    missing = sorted(set(_LABELS) - set(readings))
    if missing:
        raise OrchestrationError(
            f"缺少这些脚的电量条目：{missing}。少一只不是「那只脚没问题」，"
            "是这次准入没覆盖它 —— 传 None 表示读不到。"
        )
    unknown = sorted(set(readings) - set(_LABELS))
    if unknown:
        raise OrchestrationError(f"readings 含未知脚标：{unknown}")

    problems: list[str] = []
    for label in _LABELS:
        battery = readings[label]
        side = _SIDES[label]
        if battery is None:
            problems.append(
                f"{side}电量读不到：请确认模块已连接且未处于高速流。"
                "（这不是电量不足 —— 换电池解决不了。）"
            )
            continue
        if battery.percent is None:
            problems.append(
                f"{side}电量读数无效（原始值 {battery.raw}）：这不是一次真实测量，"
                "请重读。（这不是电量不足 —— 换电池解决不了。）"
            )
            continue
        if battery.percent < MIN_BATTERY_PERCENT:
            problems.append(
                f"{side}电量 {battery.percent}% 低于 {MIN_BATTERY_PERCENT}%："
                "请更换或充电后再开始。"
            )

    return PreflightVerdict(
        admitted=not problems,
        problems=tuple(problems),
        readings={label: _battery_entry(readings[label]) for label in _LABELS},
    )


@dataclass(frozen=True, slots=True)
class LinkOutcome:
    """一只脚这一轮的链路结局。

    `disconnected_at` 非空即表示这一路中途掉过线 —— 哪怕它后来重连成功，样本
    序列在那一刻断过（wt901 的 `ConnectionEvent` 文档：`seq` 每次重连后归零，
    把跨连接的样本当一条连续序列会得到错误的时间/序号推断）。
    """

    foot: FootLabel
    disconnected_at: float | None = None
    reconnects: int = 0
    battery_before: Battery | None = None
    battery_after: Battery | None = None
    temperature_after_c: float | None = None
    recording_error: str | None = None

    def __post_init__(self) -> None:
        _check_label(self.foot)
        if self.reconnects < 0:
            raise OrchestrationError("reconnects 不能为负")

    @property
    def clean(self) -> bool:
        """这一路是否全程无断连、无写盘错误。"""
        return (
            self.disconnected_at is None
            and self.reconnects == 0
            and self.recording_error is None
        )

    @property
    def temperature_rise_note(self) -> str | None:
        """温升记录用的一句话；没读到温度时为 `None`。

        只记录不判定 —— PRD 要的是「记录温升」，没有给阈值，本模块不发明一个。
        """
        if self.temperature_after_c is None:
            return None
        return f"{_SIDES[self.foot]}结束温度 {self.temperature_after_c:.1f} °C"

    def snapshot(self) -> dict[str, Any]:
        return {
            "foot": self.foot,
            "disconnected_at": self.disconnected_at,
            "reconnects": self.reconnects,
            "battery_before": _battery_entry(self.battery_before),
            "battery_after": _battery_entry(self.battery_after),
            "temperature_after_c": self.temperature_after_c,
            "recording_error": self.recording_error,
            "clean": self.clean,
        }


@dataclass(frozen=True, slots=True)
class SessionOutcome:
    """一次双足会话的完整性结论，进会话元数据。

    `complete` 为假**不表示数据不可用** —— 它表示这份会话不能当作完整双足数据
    使用。区别是实的：单足指标可能仍然可算，而双足对称性不行。下游据此决定
    拒绝算哪些量，所以这个标记必须落进元数据，不能只在日志里出现。
    """

    complete: bool
    links: tuple[LinkOutcome, ...]
    problems: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.complete and self.problems:
            raise OrchestrationError("complete 为真时不应带 problems")
        feet = [link.foot for link in self.links]
        if sorted(feet) != sorted(_LABELS):
            raise OrchestrationError(
                f"双足会话必须恰好两条链路（L 与 R），收到 {feet}"
            )

    def snapshot(self) -> dict[str, Any]:
        return {
            "complete": self.complete,
            "problems": list(self.problems),
            "links": [link.snapshot() for link in self.links],
        }


def summarize_session(links: tuple[LinkOutcome, ...]) -> SessionOutcome:
    """把两条链路的结局汇成会话完整性结论。

    一路出问题**不影响另一路的数据**（wt901 的有界延迟归并保证存活流继续全速
    推进），但**会话**因此不完整 —— 见模块文档。
    """
    problems: list[str] = []
    for link in sorted(links, key=lambda item: item.foot):
        side = _SIDES[link.foot]
        if link.disconnected_at is not None:
            problems.append(
                f"{side}在 {link.disconnected_at:.1f}s 处断连："
                "该时刻之后这一路的样本序列不连续，双足指标不可算。"
            )
        elif link.reconnects:
            problems.append(
                f"{side}重连过 {link.reconnects} 次：每次重连后样本序号归零，"
                "跨重连的时序推断不成立。"
            )
        if link.recording_error is not None:
            problems.append(f"{side}原始数据落盘失败：{link.recording_error}")
    return SessionOutcome(
        complete=not problems, links=tuple(links), problems=tuple(problems)
    )


def reconnect_snapshot(policy: ReconnectPolicy, *, enabled: bool) -> dict[str, Any]:
    """重连策略进会话元数据。

    PRD 原文写的是「autoConnect 必须关闭（厂商 FAQ 已知坑）」，但那是 Android
    `connectGatt()` 的参数；wt901 走 bleak，Windows / macOS 后端没有这个开关。
    真正对应的是 `WT901Device(auto_reconnect=...)`，**默认就是** `False`。

    所以这条要求在交付平台上不是「去关掉某个开关」，而是「**把用的是哪套重连
    策略写下来**」—— 一份会话的样本连续性取决于它，而事后无从反推。
    """
    return {
        "auto_reconnect": enabled,
        "initial_delay_s": policy.initial_delay,
        "max_delay_s": policy.max_delay,
        "factor": policy.factor,
        "max_attempts": policy.max_attempts,
    }
