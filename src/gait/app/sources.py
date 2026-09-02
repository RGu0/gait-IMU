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

import struct
import threading
from dataclasses import dataclass, field
from typing import Any, Protocol

from wt901 import Battery, Transport
from wt901.transport.memory import MemoryTransport

from gait.contracts import FootLabel

#: 链路三档（UI 设计 §4.2）。采集中唯一允许的链路表达（FR-07）。
LINK_GRADES: tuple[str, ...] = ("good", "fair", "bad")


def synthetic_frame(seed: int) -> bytes:
    """一帧 0x55 0x61 运动数据，9 个 int16 计数。

    **这不是模拟真实步态**，只是一串结构合法的字节 —— 它存在的唯一目的是让写盘
    那条路上真的有东西流过。任何从它算出来的指标都没有意义，而
    `provenance()` 会把这件事写进会话元数据，确保没人事后误读。
    """
    return b"\x55\x61" + struct.pack("<9h", *((seed + i) % 30000 for i in range(9)))


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

    def transports(self) -> dict[str, Transport]:
        """双足各一条字节通道，交给 `SessionCapture` 包住落盘。

        **返回的是未包装的内层传输** —— 包装由 service 做，因为「这次会话往哪里
        落盘」是会话的事，不是设备源的事。
        """

    def begin_stream(self) -> None:
        """开高速流。PRD §6.1：自检通过后开启并持续记录 —— 不是采集开始才开。

        真设备上这里是下发配置并订阅 Notify；stub 上是开始产生合成字节。
        """

    def end_stream(self) -> None:
        """停高速流。"""

    def provenance(self) -> dict[str, Any]:
        """这些读数与字节从哪来。**会随会话元数据落盘。**

        存在的唯一理由：一份 stub 产生的会话文件，与一份真机会话，在磁盘上长得
        一模一样。没有这个字段，事后没有任何办法把它们分开 —— 而把一份合成字节
        的会话当成实测数据去看，是这一整条链上最坏的失败。
        """


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
    #: 每秒往每只脚推多少帧合成字节。0 表示不推 —— 默认不推，因为大多数测试
    #: 只关心状态机，不需要磁盘上真的长出东西来。
    autofeed_hz: float = 0.0
    _transports: dict[str, MemoryTransport] = field(default_factory=dict)
    _feeder: threading.Thread | None = None
    _stop: threading.Event = field(default_factory=threading.Event)

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

    def transports(self) -> dict[str, Transport]:
        """内存传输，每只脚一条；同一实例重复取到的是同一条。

        `MemoryTransport` 是 wt901 自带的内存通道 —— 没有硬件时它是**字节从哪来**
        的替身，而落盘那条路（`SessionCapture` → 写线程 → 文件）完全真实。
        本 scope 要验的正是后者。
        """
        for label in ("L", "R"):
            self._transports.setdefault(label, MemoryTransport(device_id=f"stub-{label}"))
        return dict(self._transports)

    def feed(self, foot: str, payload: bytes) -> None:
        """把一段字节推进某只脚的通道。测试用它制造「正在写盘」这个状态。"""
        self.transports()[label_of(foot)].feed(payload)

    def begin_stream(self) -> None:
        if self.autofeed_hz <= 0 or self._feeder is not None:
            return
        self._stop.clear()
        period = 1.0 / self.autofeed_hz

        def pump() -> None:
            seed = 0
            while not self._stop.wait(period):
                seed += 1
                for foot in ("L", "R"):
                    self.feed(foot, synthetic_frame(seed))

        self._feeder = threading.Thread(target=pump, name="gait-stub-feed", daemon=True)
        self._feeder.start()

    def end_stream(self) -> None:
        self._stop.set()
        feeder, self._feeder = self._feeder, None
        if feeder is not None:
            feeder.join(timeout=2.0)

    def provenance(self) -> dict[str, Any]:
        return {
            "source": "stub",
            "hardware": False,
            "note": (
                "字节由 wt901 MemoryTransport 合成，不是实测数据。"
                "落盘路径本身是真实的；这条记录确保这份会话永远不会被当成实测。"
            ),
        }

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
