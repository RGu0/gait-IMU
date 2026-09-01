"""硬件读数的入口（port）。

## 这不是「又一个 mock」

本 scope 要替换的是 `mockTerminalAdapter` —— 它假的是**业务判定**：电量准入的三态、
计时与有效时长、会话完整性，全都由前端 fixture 直接写死成想要的结果。那种假会让
「流程验证过了」变成一句空话。

这里分开的是另一件事：**硬件读数**。没有两只模块在手上，`read_batteries()` 就没有
真实数字可返回 —— 这是物理限制，不是设计选择。但读数之后的每一步判定
（`preflight_battery` 的三态、`TimedWalk` 的计时、`summarize_session` 的完整性）都是
真的，走的是仓库里已经交付并被 1294 条测试覆盖的那些代码。

所以 `StubDeviceSource` 的名字里是 **stub 不是 mock**，且它只被允许提供读数。
它不能决定准入是否通过 —— 那个结论只能由 `preflight_battery` 从读数推出来。
RAY-319 的那句话在这里同样成立：不需要硬件就能验的东西，不该靠上机来发现。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from wt901 import Battery

from gait.contracts import FootLabel

#: 链路三档（UI 设计 §4.2）。采集中唯一允许的链路表达（FR-07）。
LINK_GRADES: tuple[str, ...] = ("good", "fair", "bad")


class DeviceSource(Protocol):
    """sidecar 从硬件拿到的全部东西。实现它的要么是 BLE，要么是 stub。"""

    def read_batteries(self) -> dict[str, Battery | None]:
        """开高速流**之前**的电量。读不到用 `None` —— 那与「电量低」是两件事。"""

    def arrival_rates(self) -> dict[str, float]:
        """逐秒到达率，0–1。"""

    def step_counts(self) -> dict[str, int]:
        """左右累计步数。"""

    def link_grades(self) -> dict[str, str]:
        """左右链路档位，取自 `LINK_GRADES`。"""

    def factory_calibrated(self) -> dict[str, bool]:
        """出厂标定参数是否按 MAC 匹配到（FR-04，缺失即阻断）。"""

    def disk_free_bytes(self) -> int: ...

    def module_info(self) -> list[dict[str, Any]]:
        """设备页要显示的摘要。不含身份明文。"""


@dataclass
class StubDeviceSource:
    """无硬件时的读数替身。**只给读数，不给结论。**

    默认值取得「一切正常」，因为异常路径要由调用方显式摆出来 —— 一个默认就坏掉的
    stub 会让每条测试都先去修它。
    """

    batteries: dict[str, Battery | None] = field(
        default_factory=lambda: {
            "L": Battery(raw=82, percent=82),
            "R": Battery(raw=76, percent=76),
        }
    )
    arrival: dict[str, float] = field(default_factory=lambda: {"L": 0.99, "R": 0.98})
    steps: dict[str, int] = field(default_factory=lambda: {"L": 0, "R": 0})
    links: dict[str, str] = field(default_factory=lambda: {"L": "good", "R": "good"})
    calibrated: dict[str, bool] = field(default_factory=lambda: {"L": True, "R": True})
    disk_free: int = 64 * 1024**3

    def read_batteries(self) -> dict[str, Battery | None]:
        return dict(self.batteries)

    def arrival_rates(self) -> dict[str, float]:
        return dict(self.arrival)

    def step_counts(self) -> dict[str, int]:
        return dict(self.steps)

    def link_grades(self) -> dict[str, str]:
        return dict(self.links)

    def factory_calibrated(self) -> dict[str, bool]:
        return dict(self.calibrated)

    def disk_free_bytes(self) -> int:
        return self.disk_free

    def module_info(self) -> list[dict[str, Any]]:
        return [
            {
                "side": "left" if label == "L" else "right",
                "maskedAddress": "…:9A:4C" if label == "L" else "…:9A:51",
                "factoryCalibrated": self.calibrated[label],
                "batteryPercent": (
                    self.batteries[label].percent if self.batteries[label] else None
                ),
            }
            for label in ("L", "R")
        ]

    def advance(
        self, *, left: int = 0, right: int = 0, links: dict[str, str] | None = None
    ) -> None:
        """测试用：把读数往前推一拍。"""
        self.steps["L"] += left
        self.steps["R"] += right
        if links:
            for label, grade in links.items():
                if grade not in LINK_GRADES:
                    raise ValueError(f"未知链路档位 {grade!r}；三档为 {LINK_GRADES}")
                self.links[label] = grade


def label_of(side: str) -> FootLabel:
    mapping = {"left": "L", "right": "R", "L": "L", "R": "R"}
    if side not in mapping:
        raise ValueError(f"未知脚标 {side!r}")
    return mapping[side]  # type: ignore[return-value]
