"""`IngestionClient` 的 HTTP 实现。RAY-355。

队列那一半早就写完了（`upload.py`，RAY-226 + RAY-233，45 条测试钉住）：租约、退避、
断点续传、幂等、确认前不删本地。**它一直缺的只是一个真的会发包的对面。** 本模块补上
这一件事，别的一律不碰。

## 线格式从哪来

《云端服务接口约定·抄录卷》§1（共用部分）—— 那一节转录的是足压平台已经跑通的东西：

* `Authorization: Bearer` + 租户身份只来自凭据、**不由载荷自证**；
* `Idempotency-Key`，键由内容摘要导出，所以重试必定同键；
* `X-Correlation-ID` 必须是 UUID，服务端回显；
* 统一信封 `{"data": …, "meta": …}` 与 `{"error": {code,message,retryable,action,details}, "meta": …}`；
* **断点续传靠服务端列举**，不靠本地记进度；
* **`INGESTED` 才算确认**，HTTP 200 只说明请求被受理。

## 但请求体不是抄录卷 §2 的那些 DTO

足压平台的 `SessionCreateRequest` 要 `subject_uuid` / `consent_record_id` / `device_id` /
`test_protocol` / `versions` / `started_at`。**本仓库的 `IngestionClient` 签名根本给不到
那些字段** —— 它只拿到 `session_id` + `PackageManifest.snapshot()` + 幂等键，而 RAY-355
明确不改队列语义。

根因在《待确认卷》§3：**两边切分口径不同**。足压平台边采边切、按帧区间，元数据带
`start_frame_index` / `frame_count` / `monotonic_ns`；本仓库采完后确定性打包、**按归档
字节偏移**切件，元数据只有 `index` / `offset` / `size_bytes` / `sha256`。§3.2 提案保留
本仓库的切法，由服务端按 `payload_schema` 区分两种切分。

所以本模块：**路由与信封照抄录卷，请求体用本仓库的 `PackageManifest`**，并用
`PAYLOAD_SCHEMA` 声明这是哪一种切分。这一条**尚待服务方确认**（《待确认卷》§3.4）——
在那之前，测试跑在假服务端上，而不是假装它已经谈妥了。

## 失败翻译：这是本模块唯一真正难的地方

队列的全部重试逻辑建在两类异常的分界上，**翻错一边数据就永远传不上去**：

| HTTP 现象 | 翻成 | 为什么 |
| --- | --- | --- |
| 连接错误、超时、连接重置 | `UploadUnavailable` | 链路问题，同样的请求过一会儿会成功 |
| 429、5xx | `UploadUnavailable` | 服务端负载或故障，同上 |
| 409 | `UploadConflict` | 服务端手上那一件与本地不是同一份数据。再发一万次还是冲突 |
| 摘要不符（422 / 回执 sha 对不上） | `UploadConflict` | 同上，需要人看 |
| 400 / 422 等其余 4xx | `UploadConflict` | 请求本身不合契约。重试不会让它变合法，这是客户端缺陷 |
| **401 / 403** | `UploadUnavailable` | **见下，这一条是判断，不是照搬** |

### 401 / 403 为什么归可重试

RAY-355 列的翻译表里没有它们，两条路都说得通，所以这里记下取舍：

* 归 `UploadConflict`：立刻停手，条目转 `conflict`。但**凭据重新下发之后它也不会自己
  恢复** —— 还得有人手工把条目捞回来。
* 归 `UploadUnavailable`：退避重试。凭据一换就自动恢复；代价是断网/失效期间打出注定
  失败的请求，而退避上限 15 分钟已经把它压到 100 量级/天（`UploadPolicy` 的模块文档
  算过这笔账）。

选后者。理由是 PRD §6.1 那条「网络恢复自动补传」的设计意图 —— **凭据重新下发也是一种
「条件恢复」**，而让它需要人工干预会在最坏的时候（凭据到期）制造一批需要逐条处理的
积压。真实原因不会丢：`last_error` 记着，`BacklogReport` 会到阈值报警。

## 超时不是可选项

没有超时的 HTTP 调用会把「慢网」变成「挂住」，而**挂住的上传既不重试也不报错** ——
队列会一直以为那个条目在传。所以每一次 `urlopen` 都显式带 `timeout`，且分两档：
小 JSON 请求短、传件长（一件默认 4 MiB，慢网下 30 秒不够）。

## 凭据

每次请求现取（`token_provider()`），**不在实例上缓存**。`AccessStore.token()` 每次都问
密钥库，那正是 RAY-225 立的规矩：秘密不落文件。本模块也不写任何文件。

## TLS 与代理

`base_url` 来自 `TerminalIdentity`，它自己已经拒绝非 https 并且明说「不给降级到 http
的口子」，所以这里不再加一个自己的开关 —— 加了就等于把那个口子又开回来。

证书校验用 `ssl.create_default_context()`，`ca_bundle` 非空时载入它。
**没有关掉校验的参数**：一个能关的开关迟早会在排障时被打开、然后跟着安装包出门。

代理走 `build_opener` 的默认 `ProxyHandler`，即遵循系统代理设置 —— PRD §18 把
「打印机/代理/Windows 兼容性」列为已知约束，机构网络里常有强制代理。
"""

from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable, Mapping
from typing import Any, Final

from gait.cloud.tenancy import AccessStore, TerminalIdentity
from gait.cloud.upload import INGESTED, UploadConflict, UploadUnavailable

#: 本仓库的切分口径。服务端按它区分「按归档字节切件」与足压平台的「按帧区间切段」。
#: 见《待确认卷》§3.2 —— **这一条尚待服务方确认**。
PAYLOAD_SCHEMA: Final[str] = "gait-session-archive/1"

#: 小请求（建会话、列举、收尾）的超时。
DEFAULT_TIMEOUT: Final[float] = 30.0

#: 传一件的超时。默认件大小 4 MiB（`package.DEFAULT_PART_SIZE`），慢网下 30 秒不够。
PART_TIMEOUT: Final[float] = 300.0

_JSON: Final[str] = "application/json"
_OCTET: Final[str] = "application/octet-stream"


class IngestHttpError(RuntimeError):
    """本模块自己的问题 —— 不是链路，也不是数据，是这段代码或它的配置不对。

    它**不继承** `UploadError`：那两个子类各自对队列有明确含义（重试 / 冲突），
    而「客户端配置错了」两者都不是。让它逃到 `SessionUploader` 的未知异常兜底
    （RAY-233）去，那里会退避重试并记下类型名，不会伪装成一次数据冲突。
    """


def _require_uuid_correlation() -> str:
    return str(uuid.uuid4())


class HttpIngestionClient:
    """把 `IngestionClient` 的四个动作发到真实服务端。

    刻意**不持有任何可变状态**：没有会话缓存、没有进度记录。断点续传的唯一依据是
    `accepted_parts()` 问回来的东西 —— 客户端一旦开始记「我传到哪了」，那份记录就会
    在崩溃或时钟跳变后与服务端不一致，而不一致的方向偏偏是最坏那种（本地以为传过了）。
    """

    def __init__(
        self,
        identity: TerminalIdentity,
        token_provider: Callable[[], str],
        *,
        opener: urllib.request.OpenerDirector | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        part_timeout: float = PART_TIMEOUT,
        correlation_id_factory: Callable[[], str] = _require_uuid_correlation,
    ) -> None:
        self._identity = identity
        self._token_provider = token_provider
        self._timeout = _positive(timeout, name="timeout")
        self._part_timeout = _positive(part_timeout, name="part_timeout")
        self._correlation_id = correlation_id_factory
        self._opener = opener if opener is not None else build_opener(identity)

    @classmethod
    def from_access_store(
        cls, store: AccessStore, **kwargs: Any
    ) -> HttpIngestionClient:
        """常规构造：身份从预配置读，凭据每次现取密钥库。"""
        identity = store.load()
        return cls(identity, store.token, **kwargs)

    # ── IngestionClient 的四个动作 ──────────────────────────────────────────

    def begin_session(
        self, session_id: str, manifest: Mapping[str, Any], idempotency_key: str
    ) -> None:
        self._request(
            "POST",
            "/v1/sessions",
            body=json.dumps(dict(manifest)).encode("utf-8"),
            content_type=_JSON,
            headers={
                "Idempotency-Key": idempotency_key,
                "X-Schema-Version": PAYLOAD_SCHEMA,
            },
            timeout=self._timeout,
        )

    def accepted_parts(self, session_id: str) -> Mapping[int, str]:
        data = self._request(
            "GET",
            f"/v1/sessions/{_segment(session_id)}/segments",
            timeout=self._timeout,
        )
        received = data.get("received")
        if not isinstance(received, list):
            raise IngestHttpError(
                f"列举响应缺少 received 数组，拿到 {type(received).__name__}"
            )
        accepted: dict[int, str] = {}
        for item in received:
            if not isinstance(item, dict) or "index" not in item or "sha256" not in item:
                raise IngestHttpError(f"列举响应里有不完整的条目：{item!r}")
            try:
                accepted[int(item["index"])] = str(item["sha256"])
            except (TypeError, ValueError) as exc:
                # 裸 `ValueError` 逃出去会绕过整套翻译，让一次契约违反看起来像
                # 一个来路不明的崩溃。
                raise IngestHttpError(f"列举响应的件号不是整数：{item!r}") from exc
        return accepted

    def put_part(self, session_id: str, index: int, sha256: str, payload: bytes) -> str:
        data = self._request(
            "PUT",
            f"/v1/sessions/{_segment(session_id)}/segments/{int(index)}",
            body=payload,
            content_type=_OCTET,
            headers={
                "X-Content-SHA256": sha256,
                "X-Schema-Version": PAYLOAD_SCHEMA,
                "X-Segment-Metadata": _segment_metadata(index, sha256, len(payload)),
            },
            timeout=self._part_timeout,
        )
        acknowledged = data.get("sha256")
        if not isinstance(acknowledged, str) or not acknowledged:
            raise IngestHttpError(f"传件响应缺少 sha256：{data!r}")
        if acknowledged != sha256:
            # 这里也判一次，不只依赖 `_transfer` 的复核 —— 摘要不符是**冲突**，
            # 而不是一次可以重试的失败，这个判定必须在翻译层就定下来。
            raise UploadConflict(
                f"第 {index} 件的回执摘要与本地不符：服务端 {acknowledged[:16]}，"
                f"本地 {sha256[:16]}"
            )
        return acknowledged

    def complete_session(
        self, session_id: str, manifest: Mapping[str, Any], idempotency_key: str
    ) -> str:
        data = self._request(
            "POST",
            f"/v1/sessions/{_segment(session_id)}/complete",
            body=json.dumps(dict(manifest)).encode("utf-8"),
            content_type=_JSON,
            headers={
                "Idempotency-Key": idempotency_key,
                "X-Schema-Version": PAYLOAD_SCHEMA,
            },
            timeout=self._timeout,
        )
        status = data.get("ingest_status")
        if not isinstance(status, str) or not status:
            raise IngestHttpError(f"收尾响应缺少 ingest_status：{data!r}")
        # 服务端用 `INGESTED`（抄录卷 §2.6 的枚举是大写），队列的常量是小写。
        # 归一化放在这里而不是让调用方去比 —— 大小写差一位就会让一次成功的上传
        # 被判成「未确认」，然后无限重试一个其实已经落库的会话。
        return status.strip().lower()

    # ── 传输 ────────────────────────────────────────────────────────────────

    def _request(
        self,
        method: str,
        path: str,
        *,
        timeout: float,
        body: bytes | None = None,
        content_type: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        """发一次请求，回 `data` 段。所有失败在这里翻译成队列认得的两类。"""
        url = self._identity.api_base_url.rstrip("/") + path
        request = urllib.request.Request(url, data=body, method=method)
        request.add_header("Authorization", f"Bearer {self._token_provider()}")
        request.add_header("Accept", _JSON)
        request.add_header("X-Correlation-ID", self._correlation_id())
        if content_type is not None:
            request.add_header("Content-Type", content_type)
        for key, value in (headers or {}).items():
            request.add_header(key, value)

        try:
            with self._opener.open(request, timeout=timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            raise _translate_status(exc) from exc
        except TimeoutError as exc:
            # 慢网。**必须可重试** —— 把它漏成别的，队列要么放弃得太早，
            # 要么根本不知道发生过。
            raise UploadUnavailable(f"{method} {path} 超时（{timeout}s）") from exc
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, TimeoutError):
                raise UploadUnavailable(f"{method} {path} 超时（{timeout}s）") from exc
            if isinstance(reason, ssl.SSLError):
                # 证书问题不是链路抖动，重试一万次也还是同一张证书。
                raise UploadConflict(f"TLS 校验失败：{reason}") from exc
            raise UploadUnavailable(f"{method} {path} 连不上：{reason}") from exc
        except ssl.SSLError as exc:
            # **必须排在 OSError 前面**：`SSLError` 是 `OSError` 的子类，漏在后面
            # 就会掉进「链路错误 → 可重试」，而同一个证书问题包在 `URLError` 里时
            # 走的是冲突 —— 同一件事两种判定，取决于它被谁包过。
            raise UploadConflict(f"TLS 校验失败：{exc}") from exc
        except OSError as exc:
            # 连接重置一类。socket 层的错误在不同平台上并不都包成 URLError。
            raise UploadUnavailable(f"{method} {path} 链路错误：{exc}") from exc

        return _envelope_data(raw, method=method, path=path)


def build_opener(identity: TerminalIdentity) -> urllib.request.OpenerDirector:
    """按终端身份造一个 opener：强制校验证书，遵循系统代理。

    **没有关掉校验的参数。** 见模块文档最后一节。
    """
    context = ssl.create_default_context(
        cafile=identity.ca_bundle if identity.ca_bundle else None
    )
    # create_default_context 已经是这两个值，这里显式写出来是为了让「校验没被关掉」
    # 成为一件可以被测试断言的事，而不是一个需要读文档才知道的默认。
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    return urllib.request.build_opener(urllib.request.HTTPSHandler(context=context))


def _positive(value: float, *, name: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
        raise IngestHttpError(f"{name} 必须是正数，收到 {value!r}")
    return float(value)


def _segment(value: str) -> str:
    """路径段。不做 URL 编码 —— 会话号是本仓库生成的 UUID，出现别的东西是缺陷。"""
    text = str(value)
    if not text or "/" in text or "?" in text or "#" in text:
        raise IngestHttpError(f"会话号不能作为路径段使用：{value!r}")
    return text


def _segment_metadata(index: int, sha256: str, size_bytes: int) -> str:
    """本仓库口径的分段元数据。

    帧区间字段（`start_frame_index` / `frame_count` / `monotonic_ns`）**不发** ——
    本仓库按归档字节切件，那些字段在这里没有意义。《待确认卷》§3.2 提案让服务端按
    `payload_schema` 区分两种切分，本字段的形状就是那条提案。
    """
    return json.dumps(
        {
            "segment_index": int(index),
            "size_bytes": int(size_bytes),
            "sha256": sha256,
            "payload_schema_version": PAYLOAD_SCHEMA,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _envelope_data(raw: bytes, *, method: str, path: str) -> dict[str, Any]:
    """拆统一信封，回 `data` 段。"""
    if not raw:
        return {}
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IngestHttpError(f"{method} {path} 的响应不是 JSON：{exc}") from exc
    if not isinstance(document, dict):
        raise IngestHttpError(f"{method} {path} 的响应顶层不是对象")
    data = document.get("data")
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise IngestHttpError(f"{method} {path} 的 data 段不是对象")
    return data


def _translate_status(exc: urllib.error.HTTPError) -> Exception:
    """把一个 HTTP 错误状态翻成队列认得的两类之一。

    翻译表见模块文档。服务端错误信封里的 `code` / `action` 会被读出来放进消息，
    但**不参与判定** —— 判定只看状态码。

    理由在 2026-09-08 变过一次，这里记下现在的版本：**不是「没人承诺过 `action`
    稳定」**（techflex-cloud-foundation 的 RAY-410 已经承诺了，它的 `ErrorEnvelope`
    带 `retryable` + `action`，取值由产品经 `ErrorActionCatalog` 注册，还写明了未知
    action 退回按 `retryable` 判定的兜底契约），**而是「不知道对面是谁」** ——
    gait 的服务端建不建在 Foundation 上尚未拍板（《待确认卷》§1.4、§3.4）。

    在那之前依赖 `action`，等于假设了一个还没定的服务端。**服务端一旦确定基于
    Foundation，这里可以收窄成先看 `action`、未知则退回 `retryable`** —— 那是
    Foundation 已经给好的契约，不需要本仓库再发明。
    """
    status = exc.code
    detail = _error_detail(exc)

    if status in (401, 403):
        # 见模块文档「401 / 403 为什么归可重试」。
        return UploadUnavailable(f"凭据被拒（HTTP {status}）{detail}")
    if status == 409:
        return UploadConflict(f"服务端已有同编号但内容不同的数据（HTTP 409）{detail}")
    if status == 429:
        return UploadUnavailable(f"服务端限流（HTTP 429）{detail}")
    if 500 <= status:
        return UploadUnavailable(f"服务端错误（HTTP {status}）{detail}")
    if 400 <= status:
        # 400 / 404 / 422 等：请求本身不合契约，重试不会让它变合法。
        return UploadConflict(f"请求不被接受（HTTP {status}）{detail}")
    return IngestHttpError(f"无法翻译的 HTTP 状态 {status}{detail}")


def _error_detail(exc: urllib.error.HTTPError) -> str:
    """从错误信封里取出 `code` 与 `message`，取不到就算了。

    诊断信息不该让翻译本身失败 —— 一个读不出的错误体不能把「服务端 500」变成
    「客户端异常」。
    """
    try:
        document = json.loads(exc.read().decode("utf-8"))
        error = document["error"]
        code = error.get("code", "")
        message = error.get("message", "")
    except Exception:  # noqa: BLE001 - 诊断路径，任何失败都退回空字符串
        return ""
    parts = [str(item) for item in (code, message) if item]
    return "：" + " ".join(parts) if parts else ""


__all__ = [
    "DEFAULT_TIMEOUT",
    "INGESTED",
    "PART_TIMEOUT",
    "PAYLOAD_SCHEMA",
    "HttpIngestionClient",
    "IngestHttpError",
    "build_opener",
]
