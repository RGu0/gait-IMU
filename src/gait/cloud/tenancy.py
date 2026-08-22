"""预配置接入与租户边界。契约 §1 的 `cloud/tenancy.py`（F6.1）。

PRD §19：v1 的最小接入模型是**机构零管理** —— 服务方创建租户与终端身份，安装时把终端
凭据与设备组绑定写进机器；机构侧没有任何要管理的东西，也就没有任何可以配错的东西。

完整的 License / 激活 / 心跳体系按 RAY-225 的描述属 P1，不在这里。本模块只做四件事：
认得出自己是谁（终端身份）、认得出数据属于谁（租户边界）、认得出手上的模块是不是绑定
的那两个（设备组）、以及把设备组的每一次变更留下痕迹（审计）。

## 一、「左右 MAC」在 macOS 上不是 MAC

PRD 说设备组绑定「左右 MAC」。bleak 自己的源码写得很明白：

    #: The Bluetooth address of the device on this machine (UUID on macOS).

注意 **on this machine** —— 在 macOS 上它不只是平台相关，还是**机器相关**：同一个物理
模块在两台 Mac 上会得到不同的标识符。

产品目标平台是 Windows，那里它是稳定的 MAC，所以绑定方案成立。但这件事不能只活在
某个人的脑子里：在 macOS 上开发的人会发现绑定"坏了"，而最省事的反应是把那道闸放松
—— 而它正是验收标准里「未绑定模块不可用于正式会话」那一条。

所以绑定里**连同地址种类一起存**。种类不符时报一个说得出原因的错误，而不是静静地
匹配失败 —— 后者看起来就像"这个模块没绑定"，会把人引向完全错误的方向。

## 二、地址要归一化，否则同一个模块会被判成两个

MAC 有很多写法：`AA:BB:CC:DD:EE:FF`、`aa-bb-cc-dd-ee-ff`、`aabbccddeeff`。按原字符串
比，一个大小写不同的绑定就会被判成"未绑定"，而那道闸是硬拦截 —— 现场表现是"设备明明
是对的，就是开不了测试"。

归一化放在**进出口两端**：写绑定时归一化，比对时也归一化。只在一端做，另一端迟早会
被绕过。

## 三、秘密不落文件

参考 FeetForcePlate `client/cloud/access_store.py`：状态进文件，**凭据进操作系统的
密钥库**，文件本身 `chmod 0o600`。

理由不是"文件不安全"这种笼统说法，而是具体的：会话包会被打包上传（`cloud/package.py`
把会话目录整个打进去），配置目录会被备份、会被同步、会在排障时被整个拷走。一个躺在
文件里的长期凭据迟早会跟着某一份拷贝出门，而且没有任何一步会报错。

本模块因此把秘密交给 `SecretStore`，并**在写文件之前扫一遍**：文件里出现凭据就拒绝
落盘。这与 `io/session.py` 对身份明文那道检查是同一种设计 —— 让常见的错误当场失败。

## 四、操作员不能改绑定

PRD §19：模块更换走**服务方远程设备组变更**（审计）。所以本模块**没有**给操作员用的
`bind()`：唯一的改法是 `apply_device_group(revision)`，而它要求修订号严格递增。

这不是防恶意，是防"现场临时换个模块先测着"。那种操作单次看无害，但它让"这台终端用的
是哪两个模块"这件事失去唯一答案，而出厂标定参数正是按模块绑的 —— 换了模块不换标定，
数据会**静静地**错下去。

## 五、TLS 是加载时的硬条件

`api_base_url` 不是 https 就拒绝加载整份预配置。不给"降级到 http 试试"的口子：那种
口子在开发机上很方便，也正因为方便会被带进安装包。
"""

from __future__ import annotations

import json
import os
import re
import stat
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Protocol
from urllib.parse import urlparse

#: 预配置文件的结构版本。
PROVISION_SCHEMA_VERSION: Final[str] = "1.0"

#: 预配置文件名。固定名字 —— 安装程序与客户端不该靠约定各写各的。
PROVISION_FILENAME: Final[str] = "provisioning.json"
AUDIT_FILENAME: Final[str] = "device-group-audit.jsonl"

#: 地址的两种种类。见模块文档 §1。
ADDRESS_MAC: Final[str] = "mac"
ADDRESS_PLATFORM_UUID: Final[str] = "platform-uuid"

FEET: Final[tuple[str, ...]] = ("L", "R")

_MAC_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[0-9A-F]{2}(:[0-9A-F]{2}){5}$")
_MAC_CHARS: Final[re.Pattern[str]] = re.compile(r"^[0-9a-fA-F]{12}$")


class AccessError(ValueError):
    """接入配置相关的失败。"""


class NotProvisioned(AccessError):
    """这台终端还没有被预配置。"""


class TenantMismatch(AccessError):
    """数据不属于本终端的租户。AC-17 租户隔离。"""


class UnboundModule(AccessError):
    """手上的模块不是设备组绑定的那些。**正式会话的硬拦截。**"""


def normalize_address(value: str) -> tuple[str, str]:
    """归一化一个设备地址，返回 `(归一化后的地址, 种类)`。

    MAC 一律化成大写冒号分隔；平台 UUID（macOS）化成小写标准形。识别不了的原样返回
    并标为 `platform-uuid` —— 猜一个种类比承认不认识更糟。

    归一化必须在**写入与比对两端**都做。只在一端做，另一端迟早被绕过，而绕过的表现
    是"设备明明是对的，就是开不了测试"。
    """
    if not isinstance(value, str) or not value.strip():
        raise AccessError(f"设备地址不能为空，收到 {value!r}")
    text = value.strip()

    compact = text.replace(":", "").replace("-", "").replace(".", "")
    if _MAC_CHARS.match(compact):
        pairs = [compact[index : index + 2].upper() for index in range(0, 12, 2)]
        return ":".join(pairs), ADDRESS_MAC

    try:
        return str(uuid.UUID(text)).lower(), ADDRESS_PLATFORM_UUID
    except ValueError:
        return text, ADDRESS_PLATFORM_UUID


@dataclass(frozen=True)
class DeviceBinding:
    """设备组里的一只脚。"""

    foot: str
    #: 归一化后的地址。
    address: str
    #: 地址种类。**连同地址一起存**，理由见模块文档 §1。
    address_kind: str
    #: 出厂标定参数集的标识。标定是按模块绑的 —— 换模块不换标定，数据会静静地错下去。
    calibration_id: str

    def __post_init__(self) -> None:
        if self.foot not in FEET:
            raise AccessError(f"foot 应为 'L' 或 'R'，收到 {self.foot!r}")
        if self.address_kind not in (ADDRESS_MAC, ADDRESS_PLATFORM_UUID):
            raise AccessError(f"未知的地址种类 {self.address_kind!r}")
        if not self.calibration_id:
            raise AccessError(
                f"{self.foot} 足缺少出厂标定参数集标识。"
                "标定按模块绑定，缺了它就无从知道这个模块该用哪套参数。"
            )

    def snapshot(self) -> dict[str, Any]:
        return {
            "foot": self.foot,
            "address": self.address,
            "address_kind": self.address_kind,
            "calibration_id": self.calibration_id,
        }


@dataclass(frozen=True)
class DeviceGroup:
    """一台终端绑定的左右模块。由服务方下发，终端不自行修改。"""

    group_id: str
    #: 修订号。服务方每次变更递增；终端只接受**严格更大**的修订。
    revision: int
    bindings: tuple[DeviceBinding, ...]
    issued_at: str
    note: str = ""

    def __post_init__(self) -> None:
        if self.revision < 1:
            raise AccessError(f"设备组修订号至少为 1，收到 {self.revision}")
        feet = [binding.foot for binding in self.bindings]
        if sorted(feet) != sorted(FEET):
            raise AccessError(
                f"设备组必须恰好绑定左右两只脚，收到 {feet}。"
                "少一只脚的设备组会让双足指标在会话中途才失败。"
            )
        addresses = {binding.address for binding in self.bindings}
        if len(addresses) != len(self.bindings):
            raise AccessError(
                "设备组的左右绑定到了同一个地址 —— 那是配置错误，"
                "而它在采集时会表现为两只脚的数据完全相同。"
            )

    def binding(self, foot: str) -> DeviceBinding:
        for candidate in self.bindings:
            if candidate.foot == foot:
                return candidate
        raise AccessError(f"设备组里没有 {foot!r} 足")

    def snapshot(self) -> dict[str, Any]:
        return {
            "group_id": self.group_id,
            "revision": self.revision,
            "issued_at": self.issued_at,
            "note": self.note,
            "bindings": [binding.snapshot() for binding in self.bindings],
        }

    @classmethod
    def from_snapshot(cls, data: Mapping[str, Any]) -> DeviceGroup:
        try:
            return cls(
                group_id=data["group_id"],
                revision=int(data["revision"]),
                issued_at=data["issued_at"],
                note=data.get("note", ""),
                bindings=tuple(
                    DeviceBinding(
                        foot=item["foot"],
                        address=item["address"],
                        address_kind=item["address_kind"],
                        calibration_id=item["calibration_id"],
                    )
                    for item in data["bindings"]
                ),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise AccessError(f"设备组数据不完整或格式错误：{exc}") from exc


@dataclass(frozen=True)
class TerminalIdentity:
    """这台终端是谁、属于哪个租户、连哪个服务端。

    **不含凭据。** 凭据在 `SecretStore` 里，理由见模块文档 §3。
    """

    tenant_id: str
    terminal_id: str
    api_base_url: str
    device_group: DeviceGroup
    #: CA 证书包的路径，用于校验服务端。空表示用系统信任库。
    ca_bundle: str = ""
    schema_version: str = PROVISION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        for name in ("tenant_id", "terminal_id"):
            if not getattr(self, name):
                raise AccessError(f"{name} 不能为空")
        parsed = urlparse(self.api_base_url)
        if parsed.scheme != "https":
            raise AccessError(
                f"api_base_url 必须是 https，收到 {self.api_base_url!r}。"
                "不给降级到 http 的口子 —— 那种口子在开发机上很方便，"
                "也正因为方便会被带进安装包。"
            )
        if not parsed.netloc:
            raise AccessError(f"api_base_url 缺少主机名：{self.api_base_url!r}")

    def snapshot(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "tenant_id": self.tenant_id,
            "terminal_id": self.terminal_id,
            "api_base_url": self.api_base_url,
            "ca_bundle": self.ca_bundle,
            "device_group": self.device_group.snapshot(),
        }

    @classmethod
    def from_snapshot(cls, data: Mapping[str, Any]) -> TerminalIdentity:
        if data.get("schema_version") != PROVISION_SCHEMA_VERSION:
            raise AccessError(
                f"预配置的结构版本是 {data.get('schema_version')!r}，"
                f"本客户端只认 {PROVISION_SCHEMA_VERSION!r}"
            )
        try:
            return cls(
                tenant_id=data["tenant_id"],
                terminal_id=data["terminal_id"],
                api_base_url=data["api_base_url"],
                ca_bundle=data.get("ca_bundle", ""),
                device_group=DeviceGroup.from_snapshot(data["device_group"]),
            )
        except KeyError as exc:
            raise AccessError(f"预配置缺少字段 {exc}") from exc


@dataclass(frozen=True)
class AuditEntry:
    """一次设备组变更。PRD §19：模块更换走服务方远程设备组变更（审计）。"""

    at: str
    from_revision: int
    to_revision: int
    #: 变更前后的绑定，各自完整记下来 —— 只记"变了"说明不了换掉的是哪一个模块。
    before: list[dict[str, Any]]
    after: list[dict[str, Any]]
    reason: str

    def snapshot(self) -> dict[str, Any]:
        return {
            "at": self.at,
            "from_revision": self.from_revision,
            "to_revision": self.to_revision,
            "before": self.before,
            "after": self.after,
            "reason": self.reason,
        }


class SecretStore(Protocol):
    """凭据的存放处。实现应当是操作系统的密钥库。"""

    def set_secret(self, key: str, value: str) -> None: ...

    def get_secret(self, key: str) -> str | None: ...

    def delete_secret(self, key: str) -> None: ...


@dataclass
class InMemorySecretStore:
    """测试与演练用。**不要在产品里用它** —— 进程一退凭据就没了。"""

    values: dict[str, str] = field(default_factory=dict)

    def set_secret(self, key: str, value: str) -> None:
        self.values[key] = value

    def get_secret(self, key: str) -> str | None:
        return self.values.get(key)

    def delete_secret(self, key: str) -> None:
        self.values.pop(key, None)


class KeyringSecretStore:
    """操作系统密钥库。Windows 上是 Credential Manager。

    `keyring` 不是本仓库的依赖 —— 它在采集端应用那一层引入（RAY-219/250）。这里只给出
    适配器，导入放在方法里，好让没装它的环境仍然能 import 本模块。
    """

    def __init__(self, service: str = "gait-imu") -> None:
        self.service = service

    def _backend(self) -> Any:
        try:
            import keyring
        except ImportError as exc:  # pragma: no cover - 取决于运行环境
            raise AccessError(
                "keyring 不可用，无法安全存放终端凭据。"
                "不退回到写文件 —— 见 cloud/tenancy.py 模块文档 §3。"
            ) from exc
        return keyring

    def set_secret(self, key: str, value: str) -> None:  # pragma: no cover - 需要密钥库
        self._backend().set_password(self.service, key, value)

    def get_secret(self, key: str) -> str | None:  # pragma: no cover - 需要密钥库
        return self._backend().get_password(self.service, key)

    def delete_secret(self, key: str) -> None:  # pragma: no cover - 需要密钥库
        backend = self._backend()
        try:
            backend.delete_password(self.service, key)
        except Exception:  # noqa: BLE001 - 各后端的"不存在"异常类型不一
            return


class AccessStore:
    """终端的预配置：身份进文件，凭据进密钥库。

    文件与密钥库分开不是形式主义。会话包会被整个打包上传，配置目录会被备份、同步、
    在排障时被整个拷走 —— 一个躺在文件里的长期凭据迟早跟着某一份拷贝出门，而且没有
    任何一步会报错。
    """

    def __init__(self, directory: Path | str, secrets: SecretStore | None = None) -> None:
        self.directory = Path(directory)
        self.secrets = secrets if secrets is not None else KeyringSecretStore()

    @property
    def provisioning_path(self) -> Path:
        return self.directory / PROVISION_FILENAME

    @property
    def audit_path(self) -> Path:
        return self.directory / AUDIT_FILENAME

    def _secret_key(self, identity: TerminalIdentity) -> str:
        return f"{identity.tenant_id}:{identity.terminal_id}"

    def install(self, identity: TerminalIdentity, *, token: str) -> TerminalIdentity:
        """安装时写入。**由服务方的安装程序调用，不是操作员。**"""
        if not token:
            raise AccessError("终端凭据不能为空")
        self.directory.mkdir(parents=True, exist_ok=True)
        payload = identity.snapshot()
        self._write_provisioning(payload, token)
        self.secrets.set_secret(self._secret_key(identity), token)
        return identity

    def _write_provisioning(self, payload: Mapping[str, Any], token: str) -> None:
        """原子写入，并在落盘**之前**确认凭据没混进去。

        这道检查与 `io/session.py` 对身份明文那道是同一种设计：让常见的错误当场失败，
        而不是三个月后在一份被拷走的配置里被发现。
        """
        text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
        if token and token in text:
            raise AccessError(
                "终端凭据出现在了预配置文件的内容里。凭据只进密钥库 —— "
                "见 cloud/tenancy.py 模块文档 §3。"
            )
        temporary = self.provisioning_path.with_suffix(".tmp")
        temporary.write_text(text + "\n", encoding="utf-8")
        try:
            os.chmod(temporary, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:  # pragma: no cover - 部分文件系统不支持
            pass
        os.replace(temporary, self.provisioning_path)

    def load(self) -> TerminalIdentity:
        """读出终端身份。没有预配置就抛 `NotProvisioned`。"""
        path = self.provisioning_path
        if not path.is_file():
            raise NotProvisioned(
                f"这台终端还没有被预配置（找不到 {path}）。"
                "v1 的接入模型是机构零管理 —— 预配置由服务方在安装时写入。"
            )
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AccessError(f"预配置文件无法解析：{exc}") from exc
        if not isinstance(payload, dict):
            raise AccessError("预配置文件的顶层必须是对象")
        return TerminalIdentity.from_snapshot(payload)

    def token(self) -> str:
        """取终端凭据。没有就抛错，**不回退到文件**。"""
        identity = self.load()
        value = self.secrets.get_secret(self._secret_key(identity))
        if not value:
            raise NotProvisioned(
                f"密钥库里没有终端 {identity.terminal_id} 的凭据。"
                "需要重新走一次服务方的安装流程。"
            )
        return value

    def apply_device_group(
        self, group: DeviceGroup, *, reason: str, now: datetime | None = None
    ) -> TerminalIdentity:
        """应用服务方下发的新设备组，并留下审计记录。

        **这是设备组唯一的改法。** 本模块刻意不提供给操作员用的 `bind()` —— 见模块
        文档 §4：现场临时换模块单次看无害，但它让"这台终端用的是哪两个模块"失去唯一
        答案，而出厂标定正是按模块绑的。

        修订号必须严格递增：等于或小于当前修订的下发会被拒绝，那多半是重放或回滚，
        两者都不该静静地生效。
        """
        identity = self.load()
        current = identity.device_group
        if group.revision <= current.revision:
            raise AccessError(
                f"设备组修订号必须严格递增：当前 {current.revision}，收到 {group.revision}。"
                "相等或更小多半是重放或回滚，两者都不该静静地生效。"
            )
        if not reason:
            raise AccessError(
                "设备组变更必须写明理由。"
                "PRD §19 要求模块更换可审计，而没有理由的记录审计不出任何东西。"
            )
        moment = (now or datetime.now(UTC)).isoformat()
        updated = replace(identity, device_group=group)
        token = self.secrets.get_secret(self._secret_key(identity)) or ""
        self._write_provisioning(updated.snapshot(), token)
        self._append_audit(
            AuditEntry(
                at=moment,
                from_revision=current.revision,
                to_revision=group.revision,
                before=[binding.snapshot() for binding in current.bindings],
                after=[binding.snapshot() for binding in group.bindings],
                reason=reason,
            )
        )
        return updated

    def _append_audit(self, entry: AuditEntry) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        with self.audit_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry.snapshot(), ensure_ascii=False) + "\n")

    def audit(self) -> list[AuditEntry]:
        """读出全部设备组变更记录，最早的在前。"""
        if not self.audit_path.is_file():
            return []
        entries: list[AuditEntry] = []
        for line in self.audit_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                data = json.loads(line)
                entries.append(
                    AuditEntry(
                        at=data["at"],
                        from_revision=data["from_revision"],
                        to_revision=data["to_revision"],
                        before=data["before"],
                        after=data["after"],
                        reason=data["reason"],
                    )
                )
            except (json.JSONDecodeError, KeyError, TypeError) as exc:
                raise AccessError(f"审计记录损坏：{exc}") from exc
        return entries


def assert_bound(
    identity: TerminalIdentity, observed: Mapping[str, str]
) -> dict[str, DeviceBinding]:
    """确认手上的模块正是设备组绑定的那两个。返回 `{足: 绑定}`。

    **这是「未绑定模块不可用于正式会话」那条验收标准的执行点。** 它抛错而不是返回
    布尔值：布尔值可以被忽略，而忽略它的后果是一次用错模块（因而用错标定）的采集，
    数据看起来完全正常。

    地址种类不符时给出**专门的**错误信息，因为那多半不是"模块没绑定"，而是这台机器的
    平台与绑定时不同（macOS 给的是机器相关的 UUID，不是 MAC）—— 见模块文档 §1。
    """
    missing = [foot for foot in FEET if foot not in observed]
    if missing:
        raise UnboundModule(
            f"正式会话需要左右两个模块，缺少 {missing}。"
            "单足会话不是本产品的形态：PRD 的核心指标是双足的。"
        )

    resolved: dict[str, DeviceBinding] = {}
    for foot in FEET:
        binding = identity.device_group.binding(foot)
        address, kind = normalize_address(observed[foot])
        if kind != binding.address_kind:
            raise UnboundModule(
                f"{foot} 足观测到的地址种类是 {kind}，而设备组绑定的是 "
                f"{binding.address_kind}。这多半不是模块没绑定，而是这台机器的平台与"
                "绑定时不同 —— macOS 给的是机器相关的 UUID，不是 MAC。"
            )
        if address != binding.address:
            raise UnboundModule(
                f"{foot} 足的模块 {address} 不在本终端的设备组里"
                f"（绑定的是 {binding.address}）。"
                "模块更换须走服务方的设备组变更，不能在现场临时替换 —— "
                "出厂标定按模块绑定，换模块不换标定的数据会静静地错下去。"
            )
        resolved[foot] = binding
    return resolved


def assert_tenant(identity: TerminalIdentity, meta: Mapping[str, Any]) -> None:
    """确认一份会话元数据属于本终端的租户。**AC-17 租户隔离。**

    检查放在客户端不是为了防攻击 —— 攻击者可以改客户端。它防的是**串号**：一台终端
    读到了另一个租户的会话目录（共享盘、恢复的备份、拷错的目录），然后把它当自己的
    数据上传。那种事没有恶意，但后果是一个租户的数据出现在另一个租户的账下，而
    FR-02 那套隐私设计到此为止全部失效。
    """
    tenant = meta.get("tenant_id")
    if tenant is None:
        raise TenantMismatch(
            "会话元数据里没有 tenant_id，无法确认它属于哪个租户。"
            "不默认它属于本租户 —— 那正是串号发生的方式。"
        )
    if tenant != identity.tenant_id:
        raise TenantMismatch(
            f"这份会话属于租户 {tenant}，而本终端属于 {identity.tenant_id}。"
            "拒绝处理 —— 一个租户的数据出现在另一个租户账下，隐私设计到此全部失效。"
        )
    terminal = meta.get("terminal_id")
    if terminal is not None and terminal != identity.terminal_id:
        raise TenantMismatch(
            f"这份会话来自终端 {terminal}，而本终端是 {identity.terminal_id}。"
            "同租户的其他终端的数据也不该由本终端代传 —— 那会让上传来源失去意义。"
        )


def session_stamp(identity: TerminalIdentity) -> dict[str, Any]:
    """要写进 `SessionMeta.extra` 的归属信息。

    **不含任何身份明文**，所以过得了 `io/session.py` 的 FR-02 检查。它记的是"这次采集
    属于哪个租户、哪台终端、哪个设备组修订"—— 三者都是机构侧的标识，不是人的标识。

    设备组修订也记下来：一份数据用的是哪套出厂标定，事后必须查得出来。
    """
    return {
        "tenant_id": identity.tenant_id,
        "terminal_id": identity.terminal_id,
        "device_group_id": identity.device_group.group_id,
        "device_group_revision": identity.device_group.revision,
        "calibration_ids": {
            binding.foot: binding.calibration_id for binding in identity.device_group.bindings
        },
    }
