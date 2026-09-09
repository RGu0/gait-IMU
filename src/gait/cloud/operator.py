"""操作员机构账号登录（P-00）的终端侧客户端。RAY-323，需求修订 R1。

## 它与终端接入凭据是两件事，混起来就违反 FR-01

PRD §19 有一张表专门讲这个：**终端接入凭据**回答「这是哪台设备」，服务方安装时写入，
操作员**永不接触**；**操作员机构账号**回答「谁在操作这台设备」，操作员在 P-00 输入。
RAY-225 交付的是前者（`cloud/tenancy.py`），本模块是后者。

所以这里的 HTTP 请求**同时出示两者**：`Authorization: Bearer <终端凭据>` 先证明是哪台
设备，请求体里才是操作员的账号与口令。线格式见《待确认卷》§1.2，**尚待服务方确认**。

## 三条产品决定（R1，用户 2026-09-08 拍板）

| | 决定 | 本模块怎么落 |
| -- | -- | -- |
| 断网能否登录 | **限期票据**：首次必须联网真验，之后凭未过期票据可开工 | `OperatorTicket` + `TicketStore` |
| 票据活多久 | **7 天**；换班靠显式登出；不做空闲重认证 | `TICKET_TTL` |
| 身份进不进会话元数据 | **不进，服务端审计** | `snapshot()` 只出 `operatorId` 与显示名，且**不进任何落盘文件** |

**第三条的代价写在 Issue 里**：断网采集且长时间未上传的会话，操作员归属是零。
本模块不去补它 —— 补它就等于把身份写进本地，正是那条拍板拒绝的事。

## 为什么初版实现「非空就放行」是最坏的一种错

RAY-248 接线时这里写过一个「账号密码非空就通过」的检查。**那不是功能不全，是假装
存在一个认证后端** —— 一个看起来能过的步骤，实际什么都没验。所以本模块的第一条约束是：

**没有任何不发请求就能成功的路径。** `login()` 要么真的拿到服务端签发的票据，
要么抛 `OperatorAuthFailed`。空账号/空口令在**发请求之前**就 `ValueError`，
那是表单校验（渲染进程的事），不是一条“本地通过”的捷径。

## token 不出 IPC

`OperatorTicket.snapshot()` **不含 `token`**。渲染进程拿它没有任何用处 —— 数据面只认
终端凭据（《待确认卷》§1.2 约束 1），而每多一个副本就多一个能泄的地方。同理它也不进
会话元数据、不进日志。

## 过期票据会被删掉，而不是留着

`TicketStore.load()` 遇到过期票据返回 `None` **并清掉它**。一个 getter 带副作用值得
解释：过期票据在密钥库里没有任何用处，留着只多一个能泄的副本；而「返回 None 但东西
还在」会让下一次读又走一遍同样的判断。
"""

from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
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
from gait.cloud.tenancy import AccessStore, SecretStore, TerminalIdentity

#: 线格式见《待确认卷》§1.2 —— **提案，尚待服务方确认**。
LOGIN_PATH: Final[str] = "/v1/operator/login"

#: 登录的超时。与查找同量级：操作员正站在屏幕前等。
LOGIN_TIMEOUT: Final[float] = 15.0

#: 票据有效期（R1-3 拍板：7 天）。**服务端给的 `expires_at` 优先** —— 这个值只在
#: 服务端没给时兜底。让本地兜底值盖过服务端的判断，等于把有效期的权威搬到终端上。
TICKET_TTL: Final[timedelta] = timedelta(days=7)

#: 票据在密钥库里的键。**不落配置文件** —— 理由与 `tenancy.py` 给终端凭据立的那条
#: 完全相同：配置目录会被备份、同步、在排障时被整个拷走，而没有任何一步会报错。
TICKET_SECRET_KEY: Final[str] = "gait.operator.ticket"


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class OperatorTicket:
    """一次登录换来的凭证。**`token` 是秘密，不出 IPC、不进落盘文件。**"""

    operator_id: str
    display_name: str
    token: str
    expires_at: datetime
    allow_new_test: bool = True
    allow_report_view: bool = True

    def __post_init__(self) -> None:
        if not self.operator_id:
            raise ValueError("票据必须带 operator_id —— 没有它就答不了「谁在操作」")
        if not self.token:
            raise ValueError("票据必须带 token")
        if self.expires_at.tzinfo is None:
            # 朴素时间会让「过期了没有」的判断依赖运行机器的时区，而那个依赖
            # 不会报错，只会在某些机器上把有效票据判成过期。
            raise ValueError("expires_at 必须带时区")

    def expired(self, now: datetime | None = None) -> bool:
        return (now or _utcnow()) >= self.expires_at

    def snapshot(self) -> dict[str, Any]:
        """给界面看的部分。**故意不含 token** —— 见模块文档。"""
        return {
            "operatorId": self.operator_id,
            "displayName": self.display_name,
            "expiresAt": self.expires_at.isoformat(),
            "allowNewTest": self.allow_new_test,
            "allowReportView": self.allow_report_view,
        }

    def to_secret(self) -> str:
        return json.dumps(
            {
                "operator_id": self.operator_id,
                "display_name": self.display_name,
                "token": self.token,
                "expires_at": self.expires_at.isoformat(),
                "allow_new_test": self.allow_new_test,
                "allow_report_view": self.allow_report_view,
            }
        )

    @classmethod
    def from_secret(cls, raw: str) -> OperatorTicket:
        payload = json.loads(raw)
        return cls(
            operator_id=str(payload["operator_id"]),
            display_name=str(payload.get("display_name") or ""),
            token=str(payload["token"]),
            expires_at=datetime.fromisoformat(str(payload["expires_at"])),
            allow_new_test=bool(payload.get("allow_new_test", True)),
            allow_report_view=bool(payload.get("allow_report_view", True)),
        )


class OperatorAuthFailed(Exception):
    """登录失败，**携带**一个已经成文的 `TerminalError`。

    与 `SubjectLookupFailed` 同型，理由也相同：`TerminalError` 是**数据类不是异常**
    （跨 IPC 的返回值，`TerminalService.handle` 靠 `isinstance` 判返回值），而客户端
    这一侧「登不上」是控制流。库抛异常，sidecar 把里面的 `TerminalError` 原样返回，
    文案仍然只有一个出处（RAY-248 验收第二条）。
    """

    def __init__(self, failure: TerminalError) -> None:
        super().__init__(failure.message)
        self.failure = failure


class OperatorAuth(Protocol):
    """终端侧认证客户端的最小接口。"""

    def login(self, account_name: str, password: str) -> OperatorTicket:
        """真验一次。失败抛 `OperatorAuthFailed`（内含成文的错误）。"""
        ...


# ── 错误 ────────────────────────────────────────────────────────────────────
#
# 与 `subjects.py` 相反，这里**一律 blocking=True**：登录失败没有「换条路继续」可走,
# 操作员必须留在 P-00。
#
# 这与《待确认卷》§1.2 约束 2「登录失败不阻断已在进行的会话」不冲突 —— 那条说的是
# **已在进行的会话**，而登录只发生在 P-00，那时没有会话在跑。两句话管的是两个时刻。


def _rejected(detail: str) -> OperatorAuthFailed:
    return OperatorAuthFailed(TerminalError(
        code="E-NET-6040",
        message=f"机构账号或口令不对{detail}。",
        action="请重新输入；若确认无误，联系服务方核对该账号是否已开通。",
        blocking=True,
    ))


def _unreachable(detail: str) -> OperatorAuthFailed:
    return OperatorAuthFailed(TerminalError(
        code="E-NET-6041",
        message=f"连不上登录服务{detail}。",
        action="检查网络后重试。若此前已成功登录过且票据未过期，可直接开始检测。",
        blocking=True,
    ))


def _unreadable(detail: str) -> OperatorAuthFailed:
    return OperatorAuthFailed(TerminalError(
        code="E-NET-6042",
        message=f"登录服务返回了看不懂的内容{detail}。",
        action="请重试一次；仍然如此就把这条码报给服务方。",
        blocking=True,
    ))


def not_provisioned() -> TerminalError:
    """本终端没有云端访问方式。**不是异常** —— service 在没有认证客户端时直接返回它。

    与 `ticket_expired()` 同形：两者都是「还没到发请求那一步」就已经确定的结局，
    没有任何一次 HTTP 调用可以改变它们，所以做成返回值而不是异常。
    """
    return TerminalError(
        code="E-NET-6043",
        message="本终端还没有配置云端访问方式，无法验证机构账号。",
        action="联系服务方完成终端预配置（安装时写入的接入凭据）。",
        blocking=True,
    )



def ticket_expired() -> TerminalError:
    """票据过期。**不是异常** —— 它由 service 在开新会话前判出来并直接返回。"""
    return TerminalError(
        code="E-NET-6044",
        message="登录已过期。",
        action="请重新登录机构账号后再开始检测。",
        blocking=True,
    )


class TicketStore:
    """票据的存放处：操作系统密钥库，**不落配置文件**。"""

    def __init__(
        self,
        secrets: SecretStore,
        *,
        key: str = TICKET_SECRET_KEY,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._secrets = secrets
        self._key = key
        self._clock = clock

    def save(self, ticket: OperatorTicket) -> None:
        self._secrets.set_secret(self._key, ticket.to_secret())

    def load(self) -> OperatorTicket | None:
        """有效票据，或 None。**过期与读不懂的都会被清掉** —— 见模块文档。"""
        raw = self._secrets.get_secret(self._key)
        if not raw:
            return None
        try:
            ticket = OperatorTicket.from_secret(raw)
        except (KeyError, ValueError, TypeError):
            # 读不懂的票据没有任何用处，而留着它会让每次读都重走一遍解析失败。
            self.clear()
            return None
        if ticket.expired(self._clock()):
            self.clear()
            return None
        return ticket

    def clear(self) -> None:
        self._secrets.delete_secret(self._key)


class HttpOperatorAuth:
    """把登录发到真实服务端。

    与 `HttpSubjectDirectory` / `HttpIngestionClient` 一样**不持有任何可变状态**，
    也**不记住口令** —— 它只在这一次调用的栈上存在。
    """

    def __init__(
        self,
        identity: TerminalIdentity,
        token_provider: Callable[[], str],
        *,
        opener: urllib.request.OpenerDirector | None = None,
        timeout: float = LOGIN_TIMEOUT,
        correlation_id_factory: Callable[[], str] = new_correlation_id,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
            raise ValueError(f"timeout 必须是正数，收到 {timeout!r}")
        self._identity = identity
        self._token_provider = token_provider
        self._timeout = float(timeout)
        self._correlation_id = correlation_id_factory
        self._clock = clock
        self._opener = opener if opener is not None else build_opener(identity)

    @classmethod
    def from_access_store(cls, store: AccessStore, **kwargs: Any) -> HttpOperatorAuth:
        """常规构造：身份从预配置读，终端凭据每次现取密钥库（RAY-225）。"""
        return cls(store.load(), store.token, **kwargs)

    def login(self, account_name: str, password: str) -> OperatorTicket:
        account = str(account_name).strip()
        if not account or not password:
            # 表单校验，**不是**一条本地通过的捷径：它抛 ValueError 而不是返回票据，
            # 也不带错误码 —— 「文案与错误码同源」管的是错误，不是表单。
            raise ValueError("机构账号与口令都不能为空")

        body = json.dumps({"account_name": account, "password": password}).encode("utf-8")
        data = self._post(LOGIN_PATH, body)
        return self._parse(data)

    def _post(self, path: str, body: bytes) -> dict[str, Any]:
        # 这里**不检查 base_url 是否为空**：`TerminalIdentity` 在构造时就拒绝非
        # https 的地址（`tenancy.py` 的 `AccessError`），所以空值到不了这里。
        # 第一版写过那个守卫，测试直接证明它不可达 —— 一段假装在防守的死代码比
        # 没有守卫更糟，它会让人以为这条路已经被想过了。
        #
        # 「本终端未预配置」的真实入口在 service：`self.auth is None`，那时连
        # 客户端都造不出来。见 `not_provisioned()`。
        url = self._identity.api_base_url.rstrip("/") + path
        request = urllib.request.Request(url, data=body, method="POST")
        # 终端凭据在头里，操作员账号在体里 —— 两者同时出示，谁都不冒充谁。
        request.add_header("Authorization", f"Bearer {self._token_provider()}")
        request.add_header("Content-Type", JSON_CONTENT_TYPE)
        request.add_header("Accept", JSON_CONTENT_TYPE)
        request.add_header("X-Correlation-ID", self._correlation_id())

        try:
            with self._opener.open(request, timeout=self._timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            raise _translate_status(exc) from exc
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
            # 必须排在 OSError 前：`SSLError` 是它的子类。这条在 RAY-355 上交过学费。
            raise _unreachable(f"（证书校验失败：{exc}）") from exc
        except OSError as exc:
            raise _unreachable(f"（{exc}）") from exc

        try:
            return envelope_data(raw, method="POST", path=path)
        except WireError as exc:
            raise _unreadable(f"（{exc}）") from exc

    def _parse(self, data: dict[str, Any]) -> OperatorTicket:
        operator_id = data.get("operator_id") or data.get("operatorId")
        token = data.get("operator_token") or data.get("operatorToken")
        if not isinstance(operator_id, str) or not operator_id:
            raise _unreadable("（响应里没有 operator_id）")
        if not isinstance(token, str) or not token:
            # 没有 token 就不算登录成功。**不要在这里兜底造一个** —— 那就回到了
            # 「非空就放行」：一个看着通过、实际没有凭证的登录。
            raise _unreadable("（响应里没有 operator_token）")

        raw_expiry = data.get("expires_at") or data.get("expiresAt")
        expires_at = self._deadline(raw_expiry)

        capabilities = data.get("capabilities")
        caps = capabilities if isinstance(capabilities, dict) else {}
        return OperatorTicket(
            operator_id=operator_id,
            display_name=str(data.get("display_name") or data.get("displayName") or ""),
            token=token,
            expires_at=expires_at,
            allow_new_test=bool(caps.get("allow_new_test", caps.get("allowNewTest", True))),
            allow_report_view=bool(
                caps.get("allow_report_view", caps.get("allowReportView", True))
            ),
        )

    def _deadline(self, raw: Any) -> datetime:
        """服务端给的期限优先；给不出才用本地的 7 天兜底。

        **不取两者的较小值**：那会让一台时钟偏快的终端悄悄缩短所有人的有效期，
        而缩短不会报错，只会让现场莫名其妙地要求重新登录。期限的权威在服务端。
        """
        if isinstance(raw, str) and raw:
            try:
                # `fromisoformat` 从 3.11 起原生认 `Z`，不必先替换成 `+00:00`
                # （本仓库 requires-python >= 3.12）。多那一步只会让人以为它必须。
                parsed = datetime.fromisoformat(raw)
            except ValueError:
                raise _unreadable(f"（expires_at 读不懂：{raw!r}）") from None
            if parsed.tzinfo is None:
                raise _unreadable("（expires_at 没带时区）")
            return parsed
        return self._clock() + TICKET_TTL


def _translate_status(exc: urllib.error.HTTPError) -> OperatorAuthFailed:
    detail = error_detail(exc.read())
    if exc.code in (400, 401, 403):
        # 400 也归这里：一个被服务端判为格式不对的登录请求，对操作员而言与
        # 「账号或口令不对」要做的事完全相同 —— 重新输一遍。
        return _rejected(detail)
    # 其余（429 / 5xx / 404）一律归「连不上」：对操作员来说三者的动作相同，
    # 而真正的区别留在 message 的括号里给服务方看。同 subjects.py 的取法。
    return _unreachable(detail)


__all__ = [
    "LOGIN_PATH",
    "LOGIN_TIMEOUT",
    "TICKET_SECRET_KEY",
    "TICKET_TTL",
    "HttpOperatorAuth",
    "OperatorAuth",
    "OperatorAuthFailed",
    "OperatorTicket",
    "TicketStore",
    "not_provisioned",
    "ticket_expired",
]
