"""左右模块绑定的**设备级**存放处（RAY-479）。

`device/binding.py` 定义了绑定的语义与文件格式，但在生产代码里零调用 —— 设备页上的
「重新配对模块」因此是一个什么都不做的按钮，而确认框里还写着「操作将被记录」。本模块
把那套语义接到一个真实的目录上，并补上那句话要兑现的记录。

## 为什么不放在 `GAIT_SESSION_ROOT` 下

会话根装的是**数据**：会被上传、会被清理、换一台终端就换一套。绑定说的是**这台终端
手边这两只物理模块**哪只是左脚 —— 它属于设备配置，生命周期与数据无关。放进会话根，
清一次数据就把左右绑定一起清掉，而下一次采集会安静地回到「未绑定」。所以它有自己的
根：`GAIT_CONFIG_ROOT`。

## 记录是追加的，绑定文件是覆盖的

`device-binding.json` 只回答「现在是什么」，格式由 `device/binding.py` 锁死（未知字段
一律拒绝），不能往里塞时间。「什么时候、从什么改成什么」写进同目录的
`device-binding-log.jsonl`，一次绑定一行，只追加不改写。界面上的「绑定于」取自这里。

记录与会话元数据无关：`SessionMeta.devices` 仍是采集层读到的身份（RAY-441），本模块
不改它的语义。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from gait.contracts import FootLabel
from gait.device.binding import (
    BindingError,
    DeviceIdentity,
    FootBinding,
    binding_path,
    read_binding,
    write_binding,
)

__all__ = [
    "BINDING_LOG_FILENAME",
    "FOOT_COLORS",
    "BindingStore",
    "IdentifyFailure",
    "mask_mac",
]

BINDING_LOG_FILENAME: Final[str] = "device-binding-log.jsonl"

#: 物理外壳颜色（2026-09-16 用户拍板）：左脚蓝色模块，右脚橙色模块。
#: 这是**外壳**的颜色，不是界面上的左右识别色（设计令牌里左蓝右青）。
FOOT_COLORS: Final[dict[str, str]] = {"L": "蓝色（左脚）", "R": "橙色（右脚）"}


class IdentifyFailure(RuntimeError):
    """配对识别没能唯一认出一台模块。两段都是给操作员的成品文案（现象 + 动作）。

    住在这里而不是 `blesource`：service 要 catch 它，而 service 不该为此 import BLE 栈。
    """

    def __init__(self, message: str, action: str) -> None:
        super().__init__(message)
        self.message = message
        self.action = action


def mask_mac(value: str | None) -> str | None:
    """只留最后两段。与 `blesource.mask_address` 同口径，但不引入 BLE 依赖。"""
    if not value:
        return None
    if ":" in value:
        return "…:" + ":".join(value.split(":")[-2:])
    return "…" + value[-4:]


def _now() -> datetime:
    return datetime.now(UTC)


class BindingStore:
    """一个目录下的绑定文件 + 绑定记录。无内部状态：每次都从磁盘读。

    无状态是有意的：sidecar 里设备源与 service 各持一个实例（同一个根），任何一方
    写下的绑定，另一方下一次读就看得到 —— 不需要谁通知谁。
    """

    def __init__(self, root: Path, *, clock: Callable[[], datetime] = _now) -> None:
        self.root = Path(root)
        self._clock = clock

    @property
    def log_path(self) -> Path:
        return self.root / BINDING_LOG_FILENAME

    def read(self) -> FootBinding:
        """当前绑定。目录或文件不存在都是「还没绑过」；文件坏了抛 `BindingError`。"""
        if not binding_path(self.root).exists():
            return FootBinding()
        return read_binding(self.root)

    def bind(
        self, label: FootLabel, identity: DeviceIdentity, *, via: str = "bindFoot"
    ) -> FootBinding:
        """绑一只脚、落盘、追加一条记录。返回新的绑定。

        旧绑定文件读不回来时**不拒绝**：重新配对正是修复它的那条路。那种情况照实
        记进记录（`previousUnreadable`），而不是假装之前什么都没有。
        """
        self.root.mkdir(parents=True, exist_ok=True)
        unreadable: str | None = None
        try:
            previous = self.read()
        except BindingError as error:
            previous = FootBinding()
            unreadable = str(error)
        updated = previous.bind(label, identity)
        write_binding(self.root, updated)
        other = "R" if label == "L" else "L"
        self._append(
            {
                "at": self._clock().isoformat(timespec="seconds"),
                "event": "bind",
                "foot": label,
                "identity": identity.snapshot(),
                "replaced": _snapshot_or_none(previous.get(label)),
                "removedFromOtherFoot": previous.get(other) == identity,
                "previousUnreadable": unreadable,
                "via": via,
            }
        )
        return updated

    def history(self) -> list[dict[str, Any]]:
        """全部记录，按写入顺序。坏行跳过 —— 一行写坏不该让整本记录读不出来。"""
        try:
            lines = self.log_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        entries: list[dict[str, Any]] = []
        for line in lines:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(entry, dict):
                entries.append(entry)
        return entries

    def bound_at(self, binding: FootBinding) -> dict[str, str | None]:
        """每只脚**当前那份**绑定是什么时候建立的。

        只认身份与当前绑定一致的最后一条：那只脚后来被改绑过，旧时间就不属于它。
        """
        result: dict[str, str | None] = {"L": None, "R": None}
        for entry in self.history():
            label = entry.get("foot")
            if label not in result or entry.get("event") != "bind":
                continue
            current = binding.get(label)
            if current is not None and entry.get("identity") == current.snapshot():
                at = entry.get("at")
                result[label] = at if isinstance(at, str) else None
        return result

    def _append(self, entry: dict[str, Any]) -> None:
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")


def _snapshot_or_none(identity: DeviceIdentity | None) -> dict[str, str] | None:
    return None if identity is None else identity.snapshot()
