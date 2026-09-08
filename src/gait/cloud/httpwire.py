"""两个云端客户端共用的线上约定。RAY-322 从 `ingest_http.py`（RAY-355）抽出。

## 为什么抽出来而不是各写一份

`ingest_http`（上传）与 `subjects`（档案查找）打的是同一个服务端，共用《云端服务
接口约定·抄录卷》§1 的那一节：Bearer 认证、统一信封 `{data, meta}`、`X-Correlation-ID`、
TLS 不可关、遵循系统代理。

各写一份的代价不是重复本身，而是**两份会分头漂移**。而这里漂移的方向特别糟：一边
关掉了证书校验、另一边没关，那是一个不会报错也没人看得出的差别 —— 直到有人拿着抓包
问「为什么这条连接是明文」。

## 这里**不放**什么

**失败翻译不在这里。** 两个客户端对失败的处置根本不同：

* 上传要的是「可重试 / 不可重试」，因为队列的退避建在这条分界上；
* 查找要的是「现象 + 动作 + 码」三段成品文案，因为它直接进操作员的屏幕。

把它们统一成一套，等于逼其中一个把自己的语义翻译两次。所以本模块只提供**判定所需
的原料**（状态码、异常类型），翻译各自做。
"""

from __future__ import annotations

import json
import ssl
import urllib.request
import uuid
from typing import Any, Final

from gait.cloud.tenancy import TerminalIdentity

JSON_CONTENT_TYPE: Final[str] = "application/json"


class WireError(RuntimeError):
    """线上层面的问题，尚未被翻译成任何一个客户端的语义。"""


def new_correlation_id() -> str:
    """抄录卷 §1.5：必须是 UUID，否则服务端回 400。"""
    return str(uuid.uuid4())


def build_opener(identity: TerminalIdentity) -> urllib.request.OpenerDirector:
    """按终端身份造一个 opener：强制校验证书，遵循系统代理。

    **没有关掉校验的参数。** 一个能关的开关迟早会在排障时被打开、然后跟着安装包出门。

    降级到 http 的闸在 `TerminalIdentity` 上（它自己拒绝非 https，并写明「不给降级到
    http 的口子」），这里不再加一个自己的 —— 加了就等于把那个口子又开回来。

    代理走 `build_opener` 的默认 `ProxyHandler`，即遵循系统代理设置：PRD §18 把
    「打印机/代理/Windows 兼容性」列为已知约束，机构网络里常有强制代理。
    """
    context = ssl.create_default_context(
        cafile=identity.ca_bundle if identity.ca_bundle else None
    )
    # create_default_context 已经是这两个值，显式写出来是为了让「校验没被关掉」成为
    # 一件可以被测试断言的事，而不是一个需要读文档才知道的默认。
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    return urllib.request.build_opener(urllib.request.HTTPSHandler(context=context))


def envelope_data(raw: bytes, *, method: str, path: str) -> dict[str, Any]:
    """拆统一信封，回 `data` 段（抄录卷 §1.6）。

    读不懂时抛 `WireError` 而不是返回空 —— 返回空会让「服务端没给数据」和
    「我们看不懂服务端给的数据」变成同一件事，而前者可能是正常的、后者一定是缺陷。
    """
    if not raw:
        return {}
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WireError(f"{method} {path} 的响应不是 JSON：{exc}") from exc
    if not isinstance(document, dict):
        raise WireError(f"{method} {path} 的响应顶层不是对象")
    data = document.get("data")
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise WireError(f"{method} {path} 的 data 段不是对象")
    return data


def error_detail(raw: bytes) -> str:
    """从错误信封里取出 `code` 与 `message`，取不到就算了。

    诊断信息不该让翻译本身失败 —— 一个读不出的错误体不能把「服务端 500」变成
    「客户端异常」。
    """
    try:
        error = json.loads(raw.decode("utf-8"))["error"]
        parts = [str(error.get(key, "")) for key in ("code", "message")]
    except Exception:  # noqa: BLE001 - 诊断路径，任何失败都退回空字符串
        return ""
    kept = [part for part in parts if part]
    return "：" + " ".join(kept) if kept else ""


__all__ = [
    "JSON_CONTENT_TYPE",
    "WireError",
    "build_opener",
    "envelope_data",
    "error_detail",
    "new_correlation_id",
]
