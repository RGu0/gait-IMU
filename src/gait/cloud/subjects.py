"""按机构档案号查找受试者。RAY-322，P-02 的后端。

FR-02 规定**身份字段仅存云端加密库**，本地会话文件只含 `subject_uuid`。所以这条路
必然要出网 —— 而本仓库此前一行实现都没有，`_do_lookupSubject` 一直返回缺口。

## 三种结果，各自是不同的东西

| 结果 | 返回 | 为什么这样分 |
| --- | --- | --- |
| 命中 | `Found` | 带脱敏核对信息，**含上次时长配置**（PRD §11 P-02 / C-13 明写必须显示） |
| 冲突 | `Conflict` | 多条候选，**由操作员选**。PRD §6.1：冲突不自动合并 |
| 查无此人 | 抛 `SubjectLookupFailed`（携 `E-NET-6020`） | 见下 |

**查无此人为什么是错误而不是第三种返回值。** 它对操作员是一个需要处置的状况：核对
编号，或改走快速建档。做成返回值，界面就得自己决定说什么 —— 而 RAY-248 要求文案与
错误码同源于 sidecar。做成带码的错误，那句话就只有一个出处。

## 断网时说什么：这是产品决定，不是实现细节

RAY-322 待确认 1 已拍板（2026-09-08）：**降级——只能走「无编号快速建档」，检测照常
进行**，而不是「断网就不能开始检测」。

所以断网时那个错误的**动作**一段写的是「改用『无编号，快速建档』继续本次检测」，
不是「恢复网络后重试」。**这一条不定，这个模块就写不出来** —— RAY-248 要求错误必须
带「现象 + 动作 + 码」，而动作那一段恰恰就是这条决定的内容。

由此还得出一件事：**本模块的失败全部 `blocking=False`**。降级的意思就是不接管界面、
让操作员换条路走完这次检测。

## 不做本地缓存

RAY-322「明确不做」第一条。为了「断网也能查」在本地留一份，等于把 FR-02 悄悄作废
—— 而且**缓存不会报错**，没有任何一步会提示这件事发生了。

## 不写审计

待确认 3 已拍板（2026-09-08）：**服务端审计，终端不记**，照足压平台的做法（它把身份
明文访问放在 Platform 面并要求 `SensitiveAccessGrant`）。本模块因此也不必处理「审计
文件自己会不会落身份明文」那个难题。

## 线格式：§1 照抄，响应体是本仓库的扩展

《抄录卷》§1（共用部分）直接适用，`POST /v1/subjects/resolve` 的路由与请求三元组
也照抄（§3.1）。**但响应体不是足压平台的 `SubjectSummary`**：那个 DTO 只有
`subject_uuid` / `external_id_masked` / `conflict`（**布尔**）/ `analysis_profile`，
而 P-02 需要 `last_protocol_seconds`（C-13 强制显示）与**候选列表**（冲突要由人选，
一个布尔选不了）。

与 RAY-355 同型：**路由与信封按抄录卷，响应体是本仓库的扩展并标为待服务方确认**，
所以测试跑在假服务端上，而不是假装它已经谈妥了。
"""

from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final, Protocol

from gait.app.errors import TerminalError
from gait.cloud.httpwire import (
    JSON_CONTENT_TYPE,
    WireError,
    build_opener,
    envelope_data,
    error_detail,
    new_correlation_id,
)
from gait.cloud.tenancy import AccessStore, TerminalIdentity

#: 档案号的种类。服务端按 `issuer + id_type + external_id` 三元组定位（抄录卷 §3.1），
#: 而 P-02 只让操作员输一个号 —— 另外两段由本模块补。**待服务方确认**。
ID_TYPE: Final[str] = "institution-record-number"

#: 查找的超时。比上传的小：操作员正站在屏幕前等，而队列不是。
LOOKUP_TIMEOUT: Final[float] = 15.0


@dataclass(frozen=True, slots=True)
class SubjectCard:
    """一条脱敏的核对信息。**不含任何身份明文** —— 只有掩码后的编号。"""

    masked_id: str
    age_band: str
    sex: str
    last_assessed_at: str
    last_protocol_seconds: int | None = None
    consent_valid: bool = False

    def snapshot(self) -> dict[str, Any]:
        return {
            "maskedId": self.masked_id,
            "ageBand": self.age_band,
            "sex": self.sex,
            "lastAssessedAt": self.last_assessed_at,
            "lastProtocolSeconds": self.last_protocol_seconds,
            "consentValid": self.consent_valid,
        }


@dataclass(frozen=True, slots=True)
class Found:
    subject: SubjectCard

    def snapshot(self) -> dict[str, Any]:
        return {"kind": "found", "subject": self.subject.snapshot()}


@dataclass(frozen=True, slots=True)
class Conflict:
    """同一个编号对应多条档案。**不自动合并** —— PRD §6.1。

    合并需要判断「这两条是不是同一个人」，而那个判断只有现场的人做得了：候选之间
    的差别可能只是上次检测日期不同。自动合并一旦错了，两个人的数据就永久混在一起，
    而且**不会有任何一步报错**。
    """

    candidates: tuple[SubjectCard, ...]

    def __post_init__(self) -> None:
        if len(self.candidates) < 2:
            raise ValueError(
                f"冲突至少要有两条候选，收到 {len(self.candidates)} 条。"
                "只有一条却报冲突，界面会显示一个选不了的选择题。"
            )

    def snapshot(self) -> dict[str, Any]:
        return {
            "kind": "conflict",
            "candidates": [card.snapshot() for card in self.candidates],
        }


LookupResult = Found | Conflict


class SubjectLookupFailed(Exception):
    """查找失败，**携带**一个已经成文的 `TerminalError`。

    为什么要包一层：`TerminalError` 是**数据类不是异常** —— 它被设计成跨 IPC 的
    返回值，`TerminalService.handle` 靠 `isinstance` 判返回值来决定发不发错误响应。
    而客户端这一侧「查不到」是控制流，用返回值表达会逼每个调用点都先判类型再用。

    所以：库这一侧抛异常，sidecar 那一侧把里面的 `TerminalError` 原样取出来返回。
    文案仍然只有一个出处。
    """

    def __init__(self, failure: TerminalError) -> None:
        super().__init__(failure.message)
        self.failure = failure


class SubjectDirectory(Protocol):
    """云端加密身份库的最小接口。"""

    def lookup(self, external_id: str) -> LookupResult:
        """按机构档案号查。查不成时抛 `SubjectLookupFailed`（内含成文的错误）。"""
        ...


# ── 错误 ────────────────────────────────────────────────────────────────────
#
# 全部 `blocking=False`：待确认 1 拍板为「降级」，界面不该被接管 —— 操作员换条路
# 就能走完这次检测。


def _not_found(entered: str) -> SubjectLookupFailed:
    return SubjectLookupFailed(TerminalError(
        code="E-NET-6020",
        message=f"本机构的档案库里没有编号 {entered}。",
        action="请核对编号；确认无误就选「无编号，快速建档」继续。",
        blocking=False,
    ))


def _unreachable(detail: str) -> SubjectLookupFailed:
    return SubjectLookupFailed(TerminalError(
        code="E-NET-6022",
        message=f"连不上云端档案库{detail}。",
        action="改用「无编号，快速建档」继续本次检测；网络恢复后再补录档案号。",
        blocking=False,
    ))


def _unreadable(detail: str) -> SubjectLookupFailed:
    return SubjectLookupFailed(TerminalError(
        code="E-NET-6023",
        message=f"云端档案库返回了看不懂的内容{detail}。",
        action="改用「无编号，快速建档」继续本次检测，并把这条码报给服务方。",
        blocking=False,
    ))


def _rejected(detail: str) -> SubjectLookupFailed:
    return SubjectLookupFailed(TerminalError(
        code="E-NET-6024",
        message=f"云端档案库拒绝了本终端的凭据{detail}。",
        action="改用「无编号，快速建档」继续本次检测，并联系服务方核对终端凭据。",
        blocking=False,
    ))


class HttpSubjectDirectory:
    """把查找发到真实服务端。

    与 `ingest_http.HttpIngestionClient` 一样**不持有任何可变状态**，也不缓存结果 ——
    缓存就是本地身份副本，而那正是 FR-02 禁止的东西。
    """

    def __init__(
        self,
        identity: TerminalIdentity,
        token_provider: Callable[[], str],
        *,
        opener: urllib.request.OpenerDirector | None = None,
        timeout: float = LOOKUP_TIMEOUT,
        correlation_id_factory: Callable[[], str] = new_correlation_id,
    ) -> None:
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
            raise ValueError(f"timeout 必须是正数，收到 {timeout!r}")
        self._identity = identity
        self._token_provider = token_provider
        self._timeout = float(timeout)
        self._correlation_id = correlation_id_factory
        self._opener = opener if opener is not None else build_opener(identity)

    @classmethod
    def from_access_store(cls, store: AccessStore, **kwargs: Any) -> HttpSubjectDirectory:
        """常规构造：身份从预配置读，凭据每次现取密钥库（秘密不落文件，RAY-225）。"""
        return cls(store.load(), store.token, **kwargs)

    def lookup(self, external_id: str) -> LookupResult:
        entered = str(external_id).strip()
        if not entered:
            # 空输入是表单校验，不该走到网络上；它没有错误码，因为「文案与错误码
            # 同源」管的是错误，不是表单（同 `service._do_login` 的那段理由）。
            raise ValueError("档案号不能为空")

        body = json.dumps(
            {
                "issuer": self._identity.tenant_id,
                "id_type": ID_TYPE,
                "external_id": entered,
            }
        ).encode("utf-8")
        data = self._post("/v1/subjects/resolve", body, entered=entered)
        return _parse(data, entered=entered)

    def _post(self, path: str, body: bytes, *, entered: str) -> dict[str, Any]:
        url = self._identity.api_base_url.rstrip("/") + path
        request = urllib.request.Request(url, data=body, method="POST")
        request.add_header("Authorization", f"Bearer {self._token_provider()}")
        request.add_header("Content-Type", JSON_CONTENT_TYPE)
        request.add_header("Accept", JSON_CONTENT_TYPE)
        request.add_header("X-Correlation-ID", self._correlation_id())

        try:
            with self._opener.open(request, timeout=self._timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            raise _translate_status(exc, entered=entered) from exc
        except TimeoutError as exc:
            raise _unreachable(f"（超时 {self._timeout}s）") from exc
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, TimeoutError):
                raise _unreachable(f"（超时 {self._timeout}s）") from exc
            if isinstance(reason, ssl.SSLError):
                raise _unreachable(f"（证书校验失败：{reason}）") from exc
            raise _unreachable(f"（{reason}）") from exc
        except ssl.SSLError as exc:
            # 必须排在 OSError 前：`SSLError` 是它的子类。
            raise _unreachable(f"（证书校验失败：{exc}）") from exc
        except OSError as exc:
            raise _unreachable(f"（{exc}）") from exc

        try:
            return envelope_data(raw, method="POST", path=path)
        except WireError as exc:
            raise _unreadable(f"（{exc}）") from exc


def _translate_status(exc: urllib.error.HTTPError, *, entered: str) -> SubjectLookupFailed:
    detail = error_detail(exc.read())
    if exc.code == 404:
        return _not_found(entered)
    if exc.code in (401, 403):
        return _rejected(detail)
    # 其余一律归「连不上」：对操作员来说 429 / 500 / 400 的区别没有意义 —— 三者
    # 要做的事完全相同（改走快速建档）。把它们分成三句话只会让屏幕更难读，而真正
    # 的区别留在 `message` 的括号里给服务方看。
    return _unreachable(detail)


def _parse(data: dict[str, Any], *, entered: str) -> LookupResult:
    kind = data.get("kind")
    if kind == "found":
        return Found(subject=_card(data.get("subject"), entered=entered))
    if kind == "conflict":
        raw = data.get("candidates")
        if not isinstance(raw, list) or len(raw) < 2:
            raise _unreadable("（冲突结果里的候选少于两条）")
        return Conflict(tuple(_card(item, entered=entered) for item in raw))
    if not data:
        # 空 data 段是服务端说「没有」的另一种写法；按查无此人处理，而不是当成
        # 一次解析失败 —— 后者会把一个正常结局报成缺陷。
        raise _not_found(entered)
    raise _unreadable(f"（未知的 kind：{kind!r}）")


def _card(payload: Any, *, entered: str) -> SubjectCard:
    if not isinstance(payload, dict):
        raise _unreadable("（缺少受试者信息）")
    masked = payload.get("external_id_masked") or payload.get("maskedId")
    if not isinstance(masked, str) or not masked:
        raise _unreadable("（受试者信息里没有脱敏编号）")
    if entered and masked == entered:
        # 掩码把原号**原样**带了回来，等于没掩。
        #
        # 判据是**相等**而不是「包含」：正确的掩码恰恰会包含原号的一段
        # （`**2781` 掩前缀、留尾号供核对），用包含判会把每一次正常掩码都误伤。
        # 这一条第一版就写成了包含，被「命中」那条测试当场抓住。
        raise _unreadable("（返回的编号没有被掩码）")
    seconds = payload.get("last_protocol_seconds", payload.get("lastProtocolSeconds"))
    return SubjectCard(
        masked_id=masked,
        age_band=str(payload.get("age_band") or payload.get("ageBand") or "未提供"),
        sex=str(payload.get("sex") or "未提供"),
        last_assessed_at=str(
            payload.get("last_assessed_at") or payload.get("lastAssessedAt") or "—"
        ),
        last_protocol_seconds=int(seconds) if isinstance(seconds, int) else None,
        consent_valid=bool(payload.get("consent_valid", payload.get("consentValid", False))),
    )


__all__ = [
    "ID_TYPE",
    "LOOKUP_TIMEOUT",
    "Conflict",
    "Found",
    "HttpSubjectDirectory",
    "LookupResult",
    "SubjectCard",
    "SubjectDirectory",
    "SubjectLookupFailed",
]
