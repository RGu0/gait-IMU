"""`gait.cloud.operator` —— P-00 机构操作员登录。RAY-323，需求修订 R1。

守的是验收原文加 R1 的增量，其中第一条是本 Issue 存在的理由：

* **错误口令不能登录成功** —— 初版实现「非空就放行」失守的正是这里，所以这里不只测
  「401 会被翻译」，还测**没有任何不发请求就能成功的路径**；
* **口令与 token 不落盘、不出 IPC** —— 用断言，不靠人看；
* **票据过期后开不了新会话**（可注入时钟，不真等 7 天）；
* **断网下凭未过期票据能开新会话** —— R1-1 的正面用例，不光测失败路径；
* **登出后票据失效**（R1-3 的换班路径）；
* **会话元数据里找不到任何操作员字段**（R1-2 的反向断言 —— 防的是将来有人顺手补上）。
"""

from __future__ import annotations

import io
import json
import ssl
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from typing import Any, Self

import pytest

from gait.app import protocol
from gait.app.errors import check_code
from gait.app.service import TerminalService
from gait.cloud.operator import (
    LOGIN_PATH,
    TICKET_SECRET_KEY,
    TICKET_TTL,
    HttpOperatorAuth,
    OperatorAuthFailed,
    OperatorTicket,
    TicketStore,
    not_provisioned,
    ticket_expired,
)
from gait.cloud.tenancy import (
    DeviceBinding,
    DeviceGroup,
    InMemorySecretStore,
    TerminalIdentity,
)

TERMINAL_TOKEN = "terminal-secret"
OPERATOR_TOKEN = "operator-secret"
PASSWORD = "hunter2-口令"
BASE_URL = "https://cloud.example.invalid"
NOW = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)


def identity(base_url: str = BASE_URL) -> TerminalIdentity:
    return TerminalIdentity(
        tenant_id="tenant-1",
        terminal_id="terminal-1",
        api_base_url=base_url,
        device_group=DeviceGroup(
            group_id="g1",
            revision=1,
            bindings=(
                DeviceBinding(foot="L", address="aa:bb:cc:dd:ee:01", address_kind="mac", calibration_id="cal-L"),
                DeviceBinding(foot="R", address="aa:bb:cc:dd:ee:02", address_kind="mac", calibration_id="cal-R"),
            ),
            issued_at="2026-09-08T00:00:00+00:00",
        ),
    )


class FakeResponse:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return self._payload

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


def envelope(data: dict[str, Any]) -> bytes:
    return json.dumps({"data": data, "meta": {"request_id": "r"}}).encode("utf-8")


def http_error(status: int, *, code: str = "", message: str = "") -> urllib.error.HTTPError:
    body = json.dumps({"error": {"code": code, "message": message}}).encode("utf-8")
    return urllib.error.HTTPError(BASE_URL, status, "boom", {}, io.BytesIO(body))  # type: ignore[arg-type]


class Opener:
    def __init__(self, payload: bytes | None = None, *, error: Exception | None = None):
        self.payload = payload
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def open(self, request: urllib.request.Request, timeout: float | None = None):
        self.calls.append(
            {
                "url": request.full_url,
                "timeout": timeout,
                "headers": dict(request.header_items()),
                "body": json.loads((request.data or b"{}").decode("utf-8")),
            }
        )
        if self.error is not None:
            raise self.error
        return FakeResponse(self.payload or b"")


OK = envelope(
    {
        "operator_id": "op-77",
        "display_name": "张医生",
        "operator_token": OPERATOR_TOKEN,
        "expires_at": "2026-09-15T12:00:00Z",
        "capabilities": {"allow_new_test": True, "allow_report_view": True},
    }
)


def auth(opener: Opener, *, base_url: str = BASE_URL) -> HttpOperatorAuth:
    return HttpOperatorAuth(
        identity(base_url),
        lambda: TERMINAL_TOKEN,
        opener=opener,  # type: ignore[arg-type]
        clock=lambda: NOW,
    )


def ticket(expires_at: datetime | None = None) -> OperatorTicket:
    return OperatorTicket(
        operator_id="op-77",
        display_name="张医生",
        token=OPERATOR_TOKEN,
        expires_at=expires_at or (NOW + TICKET_TTL),
    )


# ── 验收第一条：错误口令不能登录成功 ────────────────────────────────────────


@pytest.mark.parametrize("status", [400, 401, 403])
def test_口令不对不能登录成功(status: int) -> None:
    opener = Opener(error=http_error(status, code="AUTH-1", message="bad credentials"))
    with pytest.raises(OperatorAuthFailed) as caught:
        auth(opener).login("clinic-a", "wrong")
    failure = caught.value.failure
    assert failure.code == "E-NET-6040"
    assert failure.blocking is True  # 登录失败没有「换条路继续」可走
    check_code(failure.code)


def test_没有不发请求就能成功的路径() -> None:
    """初版「非空就放行」的直接回归：非空字段**必须**产生一次真实请求。"""
    opener = Opener(OK)
    auth(opener).login("clinic-a", PASSWORD)
    assert len(opener.calls) == 1
    assert opener.calls[0]["url"] == BASE_URL + LOGIN_PATH


@pytest.mark.parametrize(("account", "password"), [("", PASSWORD), ("clinic-a", ""), (" ", PASSWORD)])
def test_空字段是表单校验不是本地通过(account: str, password: str) -> None:
    """空字段抛 ValueError 且**不发请求** —— 它不是一条「本地通过」的捷径，
    也不带错误码（「文案与错误码同源」管的是错误，不是表单）。"""
    opener = Opener(OK)
    with pytest.raises(ValueError):
        auth(opener).login(account, password)
    assert opener.calls == []


def test_响应里没有token就不算成功() -> None:
    """**不在这里兜底造一个 token** —— 那就回到了「看着通过、实际没有凭证」。"""
    opener = Opener(envelope({"operator_id": "op-77", "display_name": "张医生"}))
    with pytest.raises(OperatorAuthFailed) as caught:
        auth(opener).login("clinic-a", PASSWORD)
    assert caught.value.failure.code == "E-NET-6042"


# ── 两套凭据不混（FR-01） ───────────────────────────────────────────────────


def test_终端凭据在头里操作员账号在体里() -> None:
    opener = Opener(OK)
    auth(opener).login("clinic-a", PASSWORD)
    call = opener.calls[0]
    assert call["headers"]["Authorization"] == f"Bearer {TERMINAL_TOKEN}"
    assert call["body"] == {"account_name": "clinic-a", "password": PASSWORD}
    # 操作员口令**不进**头，终端凭据**不进**体 —— 谁都不冒充谁。
    assert PASSWORD not in json.dumps(call["headers"], ensure_ascii=False)
    assert TERMINAL_TOKEN not in json.dumps(call["body"], ensure_ascii=False)


def test_未预配置的真实入口在service而不是客户端里() -> None:
    """`TerminalIdentity` 构造时就拒绝非 https 的 base_url，所以「地址为空」这条路
    到不了 HTTP 客户端 —— 第一版在 `_post` 里写的那个守卫是死代码，已删。

    真实入口是 `auth is None`：未预配置时连客户端都造不出来（`build_operator_auth`
    返回两个 None）。"""
    from gait.cloud.tenancy import AccessError

    with pytest.raises(AccessError):
        identity("")  # 构造阶段就挡住了

    svc = service()  # auth is None
    reply = svc.handle({"id": 1, "method": "login", "params": {"organization": "a", "password": "b"}})
    assert reply["error"]["code"] == "E-NET-6043"


# ── 失败翻译 ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "error",
    [
        TimeoutError("slow"),
        urllib.error.URLError(TimeoutError("slow")),
        urllib.error.URLError(ssl.SSLError("cert")),
        ssl.SSLError("cert"),
        OSError("down"),
        http_error(500),
        http_error(429),
        http_error(404),
    ],
)
def test_网络类失败一律归连不上(error: Exception) -> None:
    """429 / 5xx / 404 对操作员而言动作相同（检查网络后重试），分成三句只会更难读。

    `ssl.SSLError` 单独列一条不是冗余：它是 `OSError` 的子类，except 顺序写反过一次
    （RAY-355 的学费），裸的 SSL 错误会掉进 OSError 分支。
    """
    opener = Opener(error=error)
    with pytest.raises(OperatorAuthFailed) as caught:
        auth(opener).login("clinic-a", PASSWORD)
    assert caught.value.failure.code == "E-NET-6041"


def test_读不懂的信封归无法解析() -> None:
    opener = Opener(b"not json at all")
    with pytest.raises(OperatorAuthFailed) as caught:
        auth(opener).login("clinic-a", PASSWORD)
    assert caught.value.failure.code == "E-NET-6042"


def test_每条错误都有现象动作码三段() -> None:
    """RAY-248 验收第二条：渲染端不自造文案，所以三段必须在 sidecar 侧就齐。"""
    for maker in (
        lambda: auth(Opener(error=http_error(401))).login("a", "b"),
        lambda: auth(Opener(error=OSError("x"))).login("a", "b"),
        lambda: auth(Opener(b"junk")).login("a", "b"),
    ):
        with pytest.raises(OperatorAuthFailed) as caught:
            maker()
        failure = caught.value.failure
        assert failure.message and failure.action and failure.code
        check_code(failure.code)


# ── 期限 ────────────────────────────────────────────────────────────────────


def test_服务端给的期限优先() -> None:
    got = auth(Opener(OK)).login("clinic-a", PASSWORD)
    assert got.expires_at == datetime(2026, 9, 15, 12, 0, tzinfo=UTC)


def test_服务端不给期限才用本地七天兜底() -> None:
    payload = envelope({"operator_id": "op-77", "operator_token": OPERATOR_TOKEN})
    got = auth(Opener(payload)).login("clinic-a", PASSWORD)
    assert got.expires_at == NOW + TICKET_TTL
    assert TICKET_TTL == timedelta(days=7)  # R1-3 拍板的那个数


def test_不取服务端与本地的较小值() -> None:
    """一台时钟偏快的终端不该悄悄缩短所有人的有效期 —— 缩短不报错，只会让现场
    莫名其妙地被要求重新登录。期限的权威在服务端。"""
    far = envelope(
        {"operator_id": "op-77", "operator_token": OPERATOR_TOKEN, "expires_at": "2026-12-31T00:00:00Z"}
    )
    got = auth(Opener(far)).login("clinic-a", PASSWORD)
    assert got.expires_at > NOW + TICKET_TTL


@pytest.mark.parametrize("raw", ["昨天", "2026-09-15T12:00:00"])
def test_期限读不懂或没带时区都算无法解析(raw: str) -> None:
    """朴素时间会让「过期了没有」依赖运行机器的时区，而那个依赖不会报错。"""
    payload = envelope(
        {"operator_id": "op-77", "operator_token": OPERATOR_TOKEN, "expires_at": raw}
    )
    with pytest.raises(OperatorAuthFailed) as caught:
        auth(Opener(payload)).login("clinic-a", PASSWORD)
    assert caught.value.failure.code == "E-NET-6042"


# ── token 不出 IPC、不落盘 ──────────────────────────────────────────────────


def test_snapshot不含token() -> None:
    rendered = json.dumps(ticket().snapshot(), ensure_ascii=False)
    assert OPERATOR_TOKEN not in rendered
    assert "token" not in rendered.lower()


def test_票据进密钥库而不是文件() -> None:
    secrets = InMemorySecretStore()
    TicketStore(secrets, clock=lambda: NOW).save(ticket())
    assert TICKET_SECRET_KEY in secrets.values
    assert OPERATOR_TOKEN in secrets.values[TICKET_SECRET_KEY]


def test_票据往返() -> None:
    secrets = InMemorySecretStore()
    store = TicketStore(secrets, clock=lambda: NOW)
    store.save(ticket())
    assert store.load() == ticket()


def test_过期票据被读成None并且被清掉() -> None:
    """留着过期票据只多一个能泄的副本，而它没有任何用处。"""
    secrets = InMemorySecretStore()
    store = TicketStore(secrets, clock=lambda: NOW)
    store.save(ticket(expires_at=NOW - timedelta(seconds=1)))
    assert store.load() is None
    assert TICKET_SECRET_KEY not in secrets.values


def test_读不懂的票据也被清掉() -> None:
    secrets = InMemorySecretStore()
    secrets.set_secret(TICKET_SECRET_KEY, "{坏的")
    store = TicketStore(secrets, clock=lambda: NOW)
    assert store.load() is None
    assert TICKET_SECRET_KEY not in secrets.values


def test_票据必须带时区() -> None:
    """朴素时间会让「过期了没有」依赖运行机器的时区，而那个依赖不会报错 —— 它只会
    在某些机器上把有效票据判成过期。所以这里**故意**造一个朴素时间。"""
    with pytest.raises(ValueError, match="时区"):
        OperatorTicket(
            operator_id="a",
            display_name="",
            token="t",
            expires_at=datetime(2026, 1, 1),  # noqa: DTZ001 —— 正是本条要拒绝的东西
        )


@pytest.mark.parametrize(("operator_id", "token"), [("", "t"), ("a", "")])
def test_票据缺关键字段就构造不出(operator_id: str, token: str) -> None:
    with pytest.raises(ValueError):
        OperatorTicket(
            operator_id=operator_id, display_name="", token=token, expires_at=NOW + TICKET_TTL
        )


# ── sidecar 侧：service 的三条路 ────────────────────────────────────────────


def service(*, auth_client: Any = None, secrets: InMemorySecretStore | None = None) -> TerminalService:
    store = TicketStore(secrets, clock=lambda: NOW) if secrets is not None else None
    return TerminalService(auth=auth_client, tickets=store)


class FakeAuth:
    def __init__(self, result: Any) -> None:
        self.result = result
        self.calls: list[tuple[str, str]] = []

    def login(self, account_name: str, password: str) -> OperatorTicket:
        self.calls.append((account_name, password))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def test_契约翻面后不能再报缺口() -> None:
    """RAY-248 的 `unimplemented` 会拒绝一个已实现的能力 —— 契约与实现同时翻面。"""
    with pytest.raises(protocol.ProtocolError):
        protocol.unimplemented("r", "operator-auth")


def test_登录成功后快照带操作员且不带token() -> None:
    secrets = InMemorySecretStore()
    svc = service(auth_client=FakeAuth(ticket()), secrets=secrets)
    reply = svc.handle({"id": 1, "method": "login", "params": {"organization": "clinic-a", "password": PASSWORD}})
    assert "error" not in reply
    assert svc.operator is not None and svc.operator["operatorId"] == "op-77"
    assert OPERATOR_TOKEN not in json.dumps(reply, ensure_ascii=False)
    assert TICKET_SECRET_KEY in secrets.values  # 票据存了


def test_登录失败原样带出客户端的错误() -> None:
    """不再包一层 —— 多一个出处就是两份会分头漂移的开始。"""
    failure = OperatorAuthFailed(
        __import__("gait.app.errors", fromlist=["TerminalError"]).TerminalError(
            code="E-NET-6040", message="m", action="a", blocking=True
        )
    )
    svc = service(auth_client=FakeAuth(failure), secrets=InMemorySecretStore())
    reply = svc.handle({"id": 1, "method": "login", "params": {"organization": "a", "password": "b"}})
    assert reply["error"]["code"] == "E-NET-6040"


def test_没有认证客户端时说得出原因而不是报缺口() -> None:
    svc = service()
    reply = svc.handle({"id": 1, "method": "login", "params": {"organization": "a", "password": "b"}})
    assert reply["error"]["code"] == "E-NET-6043"
    assert not_provisioned().code == "E-NET-6043"


def test_空字段抛协议错误而不是造错误码() -> None:
    svc = service(auth_client=FakeAuth(ticket()))
    with pytest.raises(protocol.ProtocolError):
        svc.handle({"id": 1, "method": "login", "params": {"organization": "", "password": ""}})


# ── 登录闸（R1-1 / R1-3） ──────────────────────────────────────────────────


def test_断网下凭未过期票据能开新会话() -> None:
    """R1-1 的**正面**用例：认证客户端整个不可用（模拟断网），票据仍有效即可开工。"""
    secrets = InMemorySecretStore()
    TicketStore(secrets, clock=lambda: NOW).save(ticket())
    svc = service(auth_client=None, secrets=secrets)
    reply = svc.handle({"id": 1, "method": "startSession", "params": {"now": 0.0}})
    assert "error" not in reply
    assert reply["result"]["totalSeconds"] > 0


def test_票据过期后开不了新会话() -> None:
    secrets = InMemorySecretStore()
    TicketStore(secrets, clock=lambda: NOW).save(ticket(expires_at=NOW - timedelta(seconds=1)))
    svc = service(auth_client=FakeAuth(ticket()), secrets=secrets)
    reply = svc.handle({"id": 1, "method": "startSession", "params": {"now": 0.0}})
    assert reply["error"]["code"] == "E-NET-6044"
    assert ticket_expired().blocking is True
    assert svc.operator is None  # 界面必须回到 P-00


def test_没有票据也开不了新会话() -> None:
    svc = service(auth_client=FakeAuth(ticket()), secrets=InMemorySecretStore())
    reply = svc.handle({"id": 1, "method": "startSession", "params": {"now": 0.0}})
    assert reply["error"]["code"] == "E-NET-6044"


def test_未预配置终端不设登录闸() -> None:
    """`tickets is None` 时不加闸 —— 未预配置终端没有可验的东西，加闸只会让它
    彻底开不了工。这不是「可选的安全」，是「没有云端时没有可验的东西」。"""
    svc = service()
    reply = svc.handle({"id": 1, "method": "startSession", "params": {"now": 0.0}})
    assert "error" not in reply


def test_登出后票据失效且回到P00() -> None:
    secrets = InMemorySecretStore()
    svc = service(auth_client=FakeAuth(ticket()), secrets=secrets)
    svc.handle({"id": 1, "method": "login", "params": {"organization": "a", "password": PASSWORD}})
    assert TICKET_SECRET_KEY in secrets.values

    reply = svc.handle({"id": 2, "method": "logout", "params": {}})
    assert "error" not in reply
    assert svc.operator is None
    assert TICKET_SECRET_KEY not in secrets.values
    # 换班后第二个人未登录，开不了会话 —— 否则他做的会话会归到第一个人名下。
    blocked = svc.handle({"id": 3, "method": "startSession", "params": {"now": 0.0}})
    assert blocked["error"]["code"] == "E-NET-6044"
