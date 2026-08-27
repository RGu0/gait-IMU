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

`MAC_LAYOUT_EXTERNALLY_CONFIRMED` 就是这条状态。它为假时，这个值可以当键用、
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
`MAC_LAYOUT_EXTERNALLY_CONFIRMED`，不是 `MAC_PROVENANCE`。

## 全零应答的防护在上游，这里不重复

`read_mac()` 对全零应答抛 `UnexpectedRegisterResponse` 而不是返回
`00:00:00:00:00:00` —— 该器件的序列号寄存器就读到过逐字节全零，而一个「所有
设备都相同的稳定绑定键」会让两台设备安静地互相冒充，且绑定看起来完全正常。
"""

from __future__ import annotations

from typing import Final, Protocol

from gait.device.binding import DeviceIdentity

__all__ = [
    "MAC_LAYOUT_EXTERNALLY_CONFIRMED",
    "MAC_PROVENANCE",
    "IdentitySource",
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

#: 该排布是否已被**外部**证实（拿另一台主机显示的 MAC 比对过）。
#:
#: **目前为假。** 证实的方法上游写明了：在 Windows / Linux / Android 主机上看
#: 一眼同一台设备的 MAC。做完之后把这里改成 `True` 并附证据 —— **不要动**
#: `MAC_PROVENANCE`：证实不改变任何值，改了反而会让所有已有绑定被误报成需重建。
#:
#: 它为假时，这个值可以当绑定键、可以进会话元数据，但**不该显示给用户去与别处
#: 的 MAC 对照**。
MAC_LAYOUT_EXTERNALLY_CONFIRMED: Final[bool] = False


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


def provenance_note() -> dict[str, object]:
    """进会话元数据的一条记录：这次用的身份是怎么来的、验证到哪一步。

    存在的理由是事后可追溯：一份历史会话若用的是后来被推翻的排布，只有这条能
    让人认出来。
    """
    return {
        "provenance": MAC_PROVENANCE,
        "layout_externally_confirmed": MAC_LAYOUT_EXTERNALLY_CONFIRMED,
        "caveat": (
            "字节排布由两台真机的应答推断（四种可能排布里只有一种让两台都得到"
            "合法 BLE 随机静态地址），尚未与另一主机显示的 MAC 比对。"
            "可作绑定键；未经证实前不要显示给用户去与别处的 MAC 对照。"
        )
        if not MAC_LAYOUT_EXTERNALLY_CONFIRMED
        else None,
    }
