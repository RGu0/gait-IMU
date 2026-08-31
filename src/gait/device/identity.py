"""从已连接的设备读出可持久化的身份。契约 §1 的 `device/`（F1.1 → RAY-302）。

RAY-196 交付绑定语义时，wt901 还给不出任何可跨主机持久化的设备身份，所以身份
被做成可注入的。上游 RAY-279 补上了 `Telemetry.read_mac()`，本模块就是那个注入
点的真实实现。**绑定逻辑一行没改** —— 那正是当初把它做成注入的目的。

## 这个 MAC 的字节排布是推出来的，不是比出来的

必须先说清楚这条，因为它决定了本模块的形状。

设备标签上没印 MAC，macOS 也不给。上游的排布结论来自两台 WT901BLE67 的实测
应答：四种可能排布里**只有一种让两台设备都得到合法的蓝牙地址** —— 倒过来后
首字节高 2 位都是 `11`（BLE 随机静态地址），另三种至少有一台落在「组播位为 1
的公有地址」（不存在）或「私有地址」（会自己轮换，不可能固定写在寄存器里）。

上游也给了推翻自己的最短路径：**在 Windows / Linux / Android 主机上看一眼同一
台设备的 MAC**。

### 拿它做绑定键没问题

跨主机稳定这一点**与排布对不对无关**：同一台设备在任何主机上读 `0x66` 都得到
同一串字节，因而得到同一个键。绑定要的就是这个。

### 但不能拿它当已证实的 MAC 显示给用户

`is_layout_confirmed()` 回答这条。它为假时，这个值可以当键用、
可以存进会话元数据，**不该**摆到界面上让操作者与系统蓝牙面板对照 —— 对不上时
他会以为设备坏了，而实际是排布还没验。

### 排布若被推翻，旧绑定必须能被认出来

那时同一台设备读出的 `value` 会变，而 `kind` 还是 `mac`。没有别的信息的话，
这次变更与「设备换了一台」在数据上无法区分 —— 两者都表现为「扫得到但认不出是
左脚」，而处置相反。

所以本模块把**推导**写进 `DeviceIdentity.provenance` 并随绑定持久化：
`MAC_PROVENANCE` 的值里带着排布的标识，排布一改它就改，旧绑定随即被
`stale_provenances` 报成「绑定需重建」。

**它记的是推导，不是「验证过没有」**：外部证实只是去掉一条保留、不改变任何值，
所以不该让已有绑定失效。证实之后要动的是
`CONFIRMED_DERIVATIONS`（把被证实的那个推导标识加进去），不是 `MAC_PROVENANCE`。

**证实状态按推导索引，不是一个全局布尔。** 一个布尔只描述得了当前那一个推导，
于是「旧推导被推翻 → 新推导被证实 → 布尔翻真」这条路径会让旧记录读成已证实。
见 `CONFIRMED_DERIVATIONS`。

## 全零应答的防护在上游，这里不重复

`read_mac()` 对全零应答抛 `UnexpectedRegisterResponse` 而不是返回
`00:00:00:00:00:00` —— 该器件的序列号寄存器就读到过逐字节全零，而一个「所有
设备都相同的稳定绑定键」会让两台设备安静地互相冒充，且绑定看起来完全正常。
"""

from __future__ import annotations

from typing import Final, Protocol

from gait.device.binding import DeviceIdentity

__all__ = [
    "CONFIRMED_DERIVATIONS",
    "MAC_PROVENANCE",
    "IdentitySource",
    "is_layout_confirmed",
    "mac_identity",
    "provenance_note",
    "read_device_identity",
]

#: 当前 MAC 值的**推导**标识。排布一改，这里就要改 —— 那正是让旧绑定被认出来
#: 的机制（见模块文档）。
#:
#: `le-reversed` 指的是：`0x66`–`0x68` 按小端取出 6 字节（空口顺序），整体倒过来
#: 得到显示顺序。日期是上游做出该推断的日子，用来把「哪一次推断」钉死。
MAC_PROVENANCE: Final[str] = "wt901-read-mac/le-reversed/2026-08-27"

#: 已被**外部**证实过的推导，按推导标识索引。
#:
#: **目前为空** —— 还没人拿另一台主机显示的 MAC 比对过。
#:
#: ## 为什么是一个集合而不是一个布尔
#:
#: 「证实过没有」是**推导**的属性，而同时可能存在不止一个推导：一条记录用的
#: 可能是当前这个，也可能是某个已被取代的旧的。一个裸布尔只描述得了当前那一个，
#: 于是这条路径会出错：
#:
#: 1. 排布 X（未证实）→ 若干绑定用 X 建立；
#: 2. 真机比对发现对不上 → 上游改排布 → 推导变成 Y；
#: 3. Y 经比对证实 → 有人把那个布尔翻成 `True`。
#:
#: 此刻拿一条 `provenance = X` 的旧记录去问，得到「已证实」—— 而 X 恰恰是被推翻
#: 的那个。按推导索引就不会：X 从不曾进过这个集合。
#:
#: 而这条路径正是真机验证计划会走的那条，不是假想。
#:
#: ## 怎么加进来
#:
#: 比对通过后把**那次比对所验证的推导标识**加进来，并附证据。**不要动**
#: `MAC_PROVENANCE` 的取值 —— 证实不改变任何值，改了会让所有已有绑定被误报成
#: 需重建。
CONFIRMED_DERIVATIONS: Final[frozenset[str]] = frozenset()


class IdentitySource(Protocol):
    """能报出自己 MAC 的东西。

    只要求 `telemetry.read_mac()`，不要求整个 `WT901Device` —— 这样测试可以喂
    一个两行的替身，而不必搭出一台假设备。
    """

    @property
    def telemetry(self) -> _MacReader: ...


class _MacReader(Protocol):
    async def read_mac(self) -> str: ...


def mac_identity(mac: str) -> DeviceIdentity:
    """把一个 MAC 字符串包成带推导标识的身份。

    单独拆出来是因为它是纯函数：测试与回放路径不必真去读寄存器。
    """
    return DeviceIdentity(kind="mac", value=mac, provenance=MAC_PROVENANCE)


async def read_device_identity(device: IdentitySource) -> DeviceIdentity:
    """从已连接的设备读出绑定用的身份。

    读不到时**不吞异常**：`read_mac()` 的失败（超时、全零应答）都意味着这次拿不到
    可信的身份，而一个「拿不到身份就跳过绑定」的会话正是 RAY-196 要排除的东西。
    调用方该让它冒出来，而不是拿一个占位符继续。
    """
    return mac_identity(await device.telemetry.read_mac())


def is_layout_confirmed(provenance: str) -> bool:
    """**这个推导**被外部证实过没有。

    入参是记录自己的 `provenance`，不是「当前推导」—— 一条旧记录在任何时刻都
    该拿自己的推导得到正确答案。见 `CONFIRMED_DERIVATIONS` 的说明。

    `PROVENANCE_UNKNOWN`（1.0 格式的历史绑定）自然得 `False`：来源都不知道，
    谈不上证实过。
    """
    if not isinstance(provenance, str):
        raise TypeError(f"provenance 必须是 str，收到 {type(provenance).__name__}")
    return provenance in CONFIRMED_DERIVATIONS


def provenance_note() -> dict[str, object]:
    """进会话元数据的一条记录：这次用的身份是怎么来的、验证到哪一步。

    存在的理由是事后可追溯：一份历史会话若用的是后来被推翻的排布，只有这条能
    让人认出来。
    """
    confirmed = is_layout_confirmed(MAC_PROVENANCE)
    return {
        "provenance": MAC_PROVENANCE,
        "layout_externally_confirmed": confirmed,
        "caveat": None
        if confirmed
        else (
            "字节排布由两台真机的应答推断（四种可能排布里只有一种让两台都得到"
            "合法 BLE 随机静态地址），尚未与另一主机显示的 MAC 比对。"
            "可作绑定键；未经证实前不要显示给用户去与别处的 MAC 对照。"
        ),
    }
