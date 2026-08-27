"""左右模块的身份绑定与会话准入。契约 §1 的 `device/`（F1.1–1.2 → RAY-196）。

PRD §6.1：「双模块按 MAC 绑定左右」；验收：「换机重启后绑定关系保持」「绑定
错误可通过重新配对流程修正」。

## 为什么这里出现一个「身份种类」

本模块**不读取**设备身份，只消费一个已经拿到的 `DeviceIdentity`。原因是 wt901
目前给不出可跨主机持久化的身份：`DiscoveredDevice.address` 的 docstring 自己写着
「跨平台不可移植：macOS 上是 CoreBluetooth UUID……不要跨主机持久化」，`handle`
明令不可持久化，`name` 同批次重名。手册给的官方解法是读寄存器 `0x66` 拿设备自报
MAC，而 wt901 没接这条（WT901 RAY-279，需真机证据才能开工）。

绑定的**语义**不必跟着那个缺口一起卡住，所以身份由调用方注入，本模块只管绑定。

但注入就带来一个新的失败模式：**换了身份来源之后，旧绑定与新键对不上**。如果
只存值，这种失配与「设备真的换了一台」在数据上完全无法区分 —— 两者的表现都是
「扫得到但认不出是左脚」。所以 `DeviceIdentity` 带 `kind`，`stale_identity_kinds`
把这种情况报成**绑定需重建**，而不是安静地当成陌生设备。

## 一台设备只能是一只脚

`bind()` 在绑定前先把该身份从另一只脚上摘掉。这不是防御性编程，而是「重新配对」
这条验收路径的实现：左右装反是最常见的绑定错误，而修正它必然要经过「同一台设备
从右脚移到左脚」这个中间状态。若允许一台设备同时占着两只脚，`label_for()` 就得
回答一个没有正确答案的问题。

左右装反有专门的 `swap()`，因为用两次 `bind()` 修正它需要经过一个只剩单脚的中间
态，中途放弃就把数据改坏了。

## 未绑定的模块可见但不可用

`admit_for_session()` 只回答「能不能开正式会话」，**不过滤扫描结果** —— PRD 要求
未绑定模块可见（否则操作者无法把它绑上去）。拒绝时给出的是可操作的理由，不是
一个布尔值：「缺右脚」与「右脚那台不在场」需要的动作完全不同。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal

from gait.contracts import FootLabel

__all__ = [
    "BINDING_FORMAT_VERSION",
    "PROVENANCE_UNKNOWN",
    "AdmissionVerdict",
    "BindingError",
    "DeviceIdentity",
    "FootBinding",
    "IdentityKind",
    "admit_for_session",
    "read_binding",
    "stale_identity_kinds",
    "stale_provenances",
    "write_binding",
]

#: 持久化格式版本。认不出的版本一律拒绝而不是按当前字段解读 —— 与
#: `config._from_snapshot` / `io.session.read_meta` 同一条理由：含义变化会静默
#: 产生错误的结果。
BINDING_FORMAT_VERSION: Final[str] = "1.1"

#: 读得回来的历史版本。1.0 只差 `DeviceIdentity.provenance` 一个字段，而它**缺席
#: 时有一个真实的读法**：那份绑定确实是在不记录来源的年代写的，来源就是未知。
#: 所以 1.0 被读成 `provenance = PROVENANCE_UNKNOWN`，随即被判为「绑定需重建」——
#: 这比整份 refuse 掉信息更多：refuse 只说「读不了」，这样说的是「读到了，但它
#: 是用不明来源的键建的」。
#:
#: 这不与「认不出的版本一律拒绝」矛盾：那条针对的是**含义变了**的字段，按新含义
#: 解读旧值会静默出错。新增一个有默认真值的字段不属于那种情况。
_READABLE_VERSIONS: Final[frozenset[str]] = frozenset({"1.0", "1.1"})

BINDING_FILENAME: Final[str] = "device-binding.json"

#: 来源未知 —— 要么是 1.0 格式的历史绑定，要么调用方没说。
#: 它与任何真实来源都不相等，所以一定会被报成「需重建」，这正是想要的。
PROVENANCE_UNKNOWN: Final[str] = "unknown"

IdentityKind = Literal["mac", "serial", "platform-address"]

_IDENTITY_KINDS: Final[frozenset[str]] = frozenset(
    {"mac", "serial", "platform-address"}
)

#: 可跨主机持久化的种类。`platform-address` 不在其中：它在 macOS 上是
#: CoreBluetooth 会话内标识，换机即失效，而失效方式是安静的。
_PORTABLE_KINDS: Final[frozenset[str]] = frozenset({"mac", "serial"})

_LABELS: Final[tuple[FootLabel, ...]] = ("L", "R")


class BindingError(ValueError):
    """绑定数据不合法，或一次绑定操作会产生自相矛盾的状态。"""


@dataclass(frozen=True, slots=True)
class DeviceIdentity:
    """一台设备的持久化身份。

    `kind` 与 `value` 一起才构成身份 —— 见模块文档「为什么这里出现一个身份种类」。
    """

    kind: IdentityKind
    value: str
    provenance: str = PROVENANCE_UNKNOWN
    """这个值是**怎么算出来的**，不是它是什么。

    `kind` 回答「哪一种身份」，这个字段回答「同一种身份里，用的哪套推导」。
    两者都需要，因为**同一个 kind 内部的推导也会变**。

    真实例子：`wt901.Telemetry.read_mac()` 的字节排布是**推出来的**（设备标签
    没印 MAC，macOS 也不给），上游从「四种排布里只有一种让两台设备都得到合法
    蓝牙地址」倒推得出，并写明「在 Windows/Linux/Android 上看一眼即可推翻」。
    若哪天被推翻，同一台设备读出来的 `value` 就变了 —— 而 `kind` 还是 `mac`。
    没有这个字段，那次变更与「设备换了一台」在数据上无法区分。

    它记的是**推导**而不是「验证过没有」：外部证实只是去掉一条保留，不改变
    任何值，所以不该让已有绑定失效。见 `device/identity.py`。
    """

    def __post_init__(self) -> None:
        if self.kind not in _IDENTITY_KINDS:
            raise BindingError(
                f"未知的身份种类 {self.kind!r}，已登记：{sorted(_IDENTITY_KINDS)}"
            )
        if not isinstance(self.value, str) or not self.value.strip():
            raise BindingError("身份的 value 不能为空")
        if not isinstance(self.provenance, str) or not self.provenance.strip():
            raise BindingError("身份的 provenance 不能为空；来源不明就写 'unknown'")
        # 规范化后再比较：同一个 MAC 写成 aa:bb… 与 AA-BB… 是同一台设备，而
        # 「大小写不同就认不出」这种失败在日志里看起来和设备换了一台一样。
        normalized = self.value.strip()
        if self.kind == "mac":
            normalized = normalized.upper().replace("-", ":")
        object.__setattr__(self, "value", normalized)

    @property
    def portable(self) -> bool:
        """这个种类的身份换一台主机之后还认得出吗。"""
        return self.kind in _PORTABLE_KINDS

    def snapshot(self) -> dict[str, str]:
        return {
            "kind": self.kind,
            "value": self.value,
            "provenance": self.provenance,
        }

    @classmethod
    def from_snapshot(cls, data: Any) -> DeviceIdentity:
        """读回一份身份快照。

        `provenance` 缺席（1.0 格式）读成 `PROVENANCE_UNKNOWN` —— 那是它的真实
        含义，而不是一个凑数的默认值。
        """
        if not isinstance(data, dict):
            raise BindingError(f"身份快照必须是字典，收到 {type(data).__name__}")
        required = {"kind", "value"}
        known = required | {"provenance"}
        unknown = sorted(set(data) - known)
        if unknown:
            raise BindingError(f"身份快照含未知字段：{unknown}")
        missing = sorted(required - set(data))
        if missing:
            raise BindingError(f"身份快照缺少字段：{missing}")
        return cls(
            kind=data["kind"],
            value=data["value"],
            provenance=data.get("provenance", PROVENANCE_UNKNOWN),
        )


@dataclass(frozen=True, slots=True)
class FootBinding:
    """左右脚各绑定到哪台设备。两侧都可以是 `None`（尚未绑定）。

    不可变：每个操作返回新的实例。绑定关系会被写进会话元数据用于事后追溯，
    就地修改会让「这次会话用的是哪套绑定」变得取决于读取时机。
    """

    left: DeviceIdentity | None = None
    right: DeviceIdentity | None = None

    def __post_init__(self) -> None:
        if (
            self.left is not None
            and self.right is not None
            and self.left == self.right
        ):
            raise BindingError(
                f"同一台设备（{self.left.kind}:{self.left.value}）不能同时绑成左右脚。"
                "一台设备只能是一只脚 —— 见模块文档。"
            )

    @property
    def complete(self) -> bool:
        return self.left is not None and self.right is not None

    def get(self, label: FootLabel) -> DeviceIdentity | None:
        _check_label(label)
        return self.left if label == "L" else self.right

    def label_for(self, identity: DeviceIdentity) -> FootLabel | None:
        """这台设备绑的是哪只脚；没绑过返回 `None`。

        `kind` 与 `value` 都相等才算同一台 —— 只比 value 会让换了身份来源之后的
        失配伪装成匹配。
        """
        if self.left is not None and self.left == identity:
            return "L"
        if self.right is not None and self.right == identity:
            return "R"
        return None

    def bind(self, label: FootLabel, identity: DeviceIdentity) -> FootBinding:
        """把一台设备绑到某只脚，并**把它从另一只脚上摘掉**（如果在那儿）。

        摘掉是必要的而不是贴心：见模块文档「一台设备只能是一只脚」。
        """
        _check_label(label)
        if not isinstance(identity, DeviceIdentity):
            raise BindingError(
                f"identity 必须是 DeviceIdentity，收到 {type(identity).__name__}"
            )
        left, right = self.left, self.right
        if label == "L":
            left = identity
            if right == identity:
                right = None
        else:
            right = identity
            if left == identity:
                left = None
        return FootBinding(left=left, right=right)

    def unbind(self, label: FootLabel) -> FootBinding:
        """解绑一只脚。重新配对流程的一半；另一半是 `bind`。"""
        _check_label(label)
        if label == "L":
            return FootBinding(left=None, right=self.right)
        return FootBinding(left=self.left, right=None)

    def swap(self) -> FootBinding:
        """左右对调。

        装反是最常见的绑定错误，而用两次 `bind()` 修正它要经过一个只剩单脚的
        中间态 —— 中途失败就把绑定改坏了。这里一步到位。
        """
        return FootBinding(left=self.right, right=self.left)

    def snapshot(self) -> dict[str, Any]:
        return {
            "version": BINDING_FORMAT_VERSION,
            "left": None if self.left is None else self.left.snapshot(),
            "right": None if self.right is None else self.right.snapshot(),
        }

    @classmethod
    def from_snapshot(cls, data: Any) -> FootBinding:
        if not isinstance(data, dict):
            raise BindingError(f"绑定快照必须是字典，收到 {type(data).__name__}")
        version = data.get("version")
        if version not in _READABLE_VERSIONS:
            raise BindingError(
                f"绑定文件的版本是 {version!r}，本代码读得了 "
                f"{sorted(_READABLE_VERSIONS)}。拒绝按当前字段解读 —— "
                "含义变化会静默产生错误的左右。"
            )
        known = {"version", "left", "right"}
        unknown = sorted(set(data) - known)
        if unknown:
            raise BindingError(f"绑定快照含未知字段：{unknown}")
        missing = sorted(known - set(data))
        if missing:
            raise BindingError(f"绑定快照缺少字段：{missing}")
        return cls(
            left=None if data["left"] is None else DeviceIdentity.from_snapshot(data["left"]),
            right=(
                None if data["right"] is None else DeviceIdentity.from_snapshot(data["right"])
            ),
        )


@dataclass(frozen=True, slots=True)
class AdmissionVerdict:
    """能不能用这套绑定开一次正式会话，以及不能的话缺什么。

    `problems` 是给操作者看的可执行理由，不是诊断日志：「缺右脚绑定」要去配对，
    「右脚那台不在场」要去开机或走近，两者的动作完全不同。
    """

    admitted: bool
    problems: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.admitted and self.problems:
            raise BindingError("准入通过时不应带 problems —— 那会让调用方两头猜")


def _check_label(label: str) -> None:
    if label not in _LABELS:
        raise BindingError(f"脚标必须是 'L' 或 'R'，收到 {label!r}")


def stale_identity_kinds(
    binding: FootBinding, current_kind: IdentityKind
) -> tuple[IdentityKind, ...]:
    """绑定里有哪些身份的种类与当前身份来源不一致。

    非空即表示**这套绑定需要重建**，而不是「设备不在场」。调用方必须把两者分开
    报给操作者：前者要重新配对，后者要去把设备打开。只存值的持久化格式区分不了
    这两种情况 —— 那正是本模块给身份带上 `kind` 的原因。
    """
    if current_kind not in _IDENTITY_KINDS:
        raise BindingError(
            f"未知的身份种类 {current_kind!r}，已登记：{sorted(_IDENTITY_KINDS)}"
        )
    stale = {
        identity.kind
        for identity in (binding.left, binding.right)
        if identity is not None and identity.kind != current_kind
    }
    return tuple(sorted(stale))


def stale_provenances(binding: FootBinding, current_provenance: str) -> tuple[str, ...]:
    """绑定里有哪些身份的**推导**与当前身份提供者不一致。

    与 `stale_identity_kinds` 同形，守的是另一维：那条管「换了身份来源」，
    这条管「同一种来源换了推导」。后者更隐蔽 —— `kind` 还是 `mac`，值却变了。

    非空即「绑定需重建」，不是「设备不在场」，也不是「设备换了一台」。
    """
    if not isinstance(current_provenance, str) or not current_provenance.strip():
        raise BindingError("current_provenance 不能为空")
    stale = {
        identity.provenance
        for identity in (binding.left, binding.right)
        if identity is not None and identity.provenance != current_provenance
    }
    return tuple(sorted(stale))


def admit_for_session(
    binding: FootBinding,
    present: object,
    *,
    current_kind: IdentityKind | None = None,
    current_provenance: str | None = None,
) -> AdmissionVerdict:
    """判定这套绑定加上在场设备能否开一次**正式**会话。

    `present` 是本次扫描到的身份集合。未绑定的设备出现在里面是正常的 —— 本函数
    不过滤扫描结果，只拒绝会话（PRD：未绑定模块可见但不可用于正式会话）。

    传 `current_kind` 时会一并检查绑定是否来自另一种身份来源；那种情况报的是
    「绑定需重建」，与「设备不在场」分开。
    """
    if not isinstance(binding, FootBinding):
        raise BindingError(
            f"binding 必须是 FootBinding，收到 {type(binding).__name__}"
        )
    present_set = set(present)
    for identity in present_set:
        if not isinstance(identity, DeviceIdentity):
            raise BindingError(
                f"present 的元素必须是 DeviceIdentity，收到 {type(identity).__name__}"
            )

    problems: list[str] = []

    if current_kind is not None:
        stale = stale_identity_kinds(binding, current_kind)
        if stale:
            problems.append(
                f"绑定需重建：已保存的绑定用的是 {', '.join(stale)} 身份，"
                f"当前身份来源是 {current_kind}。这不是设备不在场，"
                "重新配对才能修正。"
            )

    if current_provenance is not None:
        outdated = stale_provenances(binding, current_provenance)
        if outdated:
            problems.append(
                f"绑定需重建：已保存的绑定是用 {', '.join(outdated)} 推导出的键建的，"
                f"当前推导是 {current_provenance}。同一台设备在两套推导下得到不同的"
                "键，所以这不是设备换了一台，也不是设备不在场 —— 重新配对才能修正。"
            )

    for label in _LABELS:
        identity = binding.get(label)
        side = "左脚" if label == "L" else "右脚"
        if identity is None:
            problems.append(f"{side}尚未绑定：请先完成配对。")
            continue
        if not identity.portable:
            problems.append(
                f"{side}绑定用的是 {identity.kind} 身份，换一台主机就认不出。"
                "请用设备自报的 MAC 或序列号重新配对。"
            )
        if identity not in present_set:
            problems.append(
                f"{side}绑定的设备（{identity.kind}:{identity.value}）不在场："
                "请确认它已开机且在范围内。"
            )

    return AdmissionVerdict(admitted=not problems, problems=tuple(problems))


def binding_path(root: Path) -> Path:
    return Path(root) / BINDING_FILENAME


def write_binding(root: Path, binding: FootBinding) -> Path:
    """原子地写入绑定文件。

    与 `io.session.write_meta` 同一条理由：半份绑定文件比没有更糟 —— 它看起来
    存在、解析却失败，而「左右绑定坏了」与「还没绑过」需要的处置不同。
    先写临时文件再 `os.replace`，同目录内在 POSIX 与 Windows 上都是原子的。
    """
    if not isinstance(binding, FootBinding):
        raise BindingError(
            f"binding 必须是 FootBinding，收到 {type(binding).__name__}"
        )
    root = Path(root)
    if not root.is_dir():
        raise BindingError(f"目录不存在：{root}")
    target = binding_path(root)
    temporary = root / f".{BINDING_FILENAME}.partial"
    temporary.write_text(
        json.dumps(binding.snapshot(), ensure_ascii=False, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, target)
    return target


def read_binding(root: Path) -> FootBinding:
    """读回绑定文件。**文件不存在时返回空绑定**，不抛异常。

    「还没绑过」是首次使用的正常状态，不是错误；而文件存在却解析不了是错误，
    那时抛 `BindingError`。把两者混成同一个异常会逼调用方去猜。
    """
    target = binding_path(root)
    if not target.exists():
        return FootBinding()
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise BindingError(f"绑定文件不是合法 JSON：{error}") from error
    return FootBinding.from_snapshot(data)
