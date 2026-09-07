"""`gait.cloud.ingest_http` —— `IngestionClient` 的 HTTP 实现。RAY-355。

**这一组测试真正在守的是失败翻译。** 队列的全部重试逻辑建在
`UploadUnavailable`（可重试）与 `UploadConflict`（不可重试）这条分界上，翻错一边
的后果是不对称的：

* 该重试的翻成冲突 —— 一次网络抖动把会话永久停在「需人工处理」；
* 该停的翻成重试 —— 在一个永远不会成功的请求上空转到天荒地老。

所以每一类 HTTP 现象都有一条测试，而不是抽样。

另外两组守的是**不靠人记得**的两件事：每一次请求都带显式超时（没有超时的调用会把
慢网变成挂住，而挂住的上传既不重试也不报错），以及凭据不落盘。

假服务端替掉的只有 socket 那一层 —— 请求构造、信封解析、翻译全都跑真的。
"""

from __future__ import annotations

import hashlib
import io
import json
import ssl
import urllib.error
import urllib.request
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Self

import pytest

from gait.app import protocol
from gait.cloud.ingest_http import (
    DEFAULT_TIMEOUT,
    PART_TIMEOUT,
    PAYLOAD_SCHEMA,
    HttpIngestionClient,
    IngestHttpError,
    build_opener,
)
from gait.cloud.package import build_package
from gait.cloud.tenancy import AccessError, DeviceBinding, DeviceGroup, TerminalIdentity
from gait.cloud.upload import (
    INGESTED,
    STATE_CONFIRMED,
    SessionUploader,
    UploadConflict,
    UploadQueue,
    UploadUnavailable,
    enqueue_session,
)
from gait.contracts import SessionMeta
from gait.io.session import (
    create_session,
    new_session_id,
    new_subject_uuid,
    raw_path,
    session_directory,
)

TOKEN = "terminal-secret-do-not-write-to-disk"
BASE_URL = "https://cloud.example.invalid"
PART_SIZE = 4096


# ── 素材 ────────────────────────────────────────────────────────────────────


def incompressible(size: int, seed: int) -> bytes:
    """确定性伪随机字节：压不动，所以包会有多件，断点续传才有断点可言。"""
    blocks = [
        hashlib.blake2b(f"{seed}:{chunk}".encode(), digest_size=64).digest()
        for chunk in range(size // 64 + 1)
    ]
    return b"".join(blocks)[:size]


def make_session(root: Path, *, repeat: int = 20000) -> str:
    session_id = new_session_id()
    create_session(
        root,
        SessionMeta(
            session_id=session_id,
            created_at=datetime.now(UTC).isoformat(),
            subject_uuid=new_subject_uuid(),
            scenario="walk",
            devices={"L": {"mac": "aa:01"}, "R": {"mac": "aa:02"}},
            config_snapshot={"fs": 200},
            calib_snapshot={"bias": [0.0, 0.0, 0.0]},
            algo_version="0.1.0",
            algo_params={"zupt_window": 40},
            sync_report={"fs": 200.3},
            integrity_report={"grade": "normal"},
            protocol_config={"duration_s": 1800},
        ),
    )
    for foot, seed in (("L", 1), ("R", 2)):
        raw_path(root, session_id, foot).write_bytes(incompressible(repeat * 3, seed))
    return session_id


def identity(ca_bundle: str = "") -> TerminalIdentity:
    return TerminalIdentity(
        tenant_id="tenant-1",
        terminal_id="terminal-1",
        api_base_url=BASE_URL,
        device_group=DeviceGroup(
            group_id="group-1",
            revision=1,
            bindings=(
                DeviceBinding(
                    foot="L",
                    address="aa:bb:cc:dd:ee:01",
                    address_kind="mac",
                    calibration_id="cal-L",
                ),
                DeviceBinding(
                    foot="R",
                    address="aa:bb:cc:dd:ee:02",
                    address_kind="mac",
                    calibration_id="cal-R",
                ),
            ),
            issued_at="2026-09-07T00:00:00+00:00",
        ),
        ca_bundle=ca_bundle,
    )


# ── 假服务端 ────────────────────────────────────────────────────────────────


class FakeResponse:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return self._payload

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


def header(call: dict[str, Any], name: str) -> str:
    """按 urllib 的规则取头。

    `Request.add_header` 对整个键名做 `capitalize()`，所以 `Idempotency-Key` 存进去
    是 `Idempotency-key`。测试照同一规则查，而不是各写各的大小写。
    """
    return call["headers"][name.capitalize()]


def envelope(data: Mapping[str, Any]) -> bytes:
    return json.dumps({"data": dict(data), "meta": {"request_id": "r"}}).encode("utf-8")


def http_error(status: int, *, code: str = "", message: str = "") -> urllib.error.HTTPError:
    body = json.dumps(
        {"error": {"code": code, "message": message, "retryable": False, "action": "X"}}
    ).encode("utf-8")
    return urllib.error.HTTPError(
        BASE_URL, status, "boom", {}, io.BytesIO(body)  # type: ignore[arg-type]
    )


class FakeServer:
    """实现约定的最小服务端：路由、幂等、按件存放、收尾校验。"""

    def __init__(self) -> None:
        self.parts: dict[str, dict[int, bytes]] = {}
        self.sessions: dict[str, dict[str, Any]] = {}
        self.completed: dict[str, str] = {}
        self.idempotency_keys: list[str] = []
        self.status_text = "INGESTED"  # 服务端用大写枚举（抄录卷 §2.6）

    def handle(self, method: str, path: str, headers: Mapping[str, str], body: bytes) -> bytes:
        parts = [p for p in path.split("/") if p]
        if method == "POST" and parts == ["v1", "sessions"]:
            self.idempotency_keys.append(headers["Idempotency-Key"])
            manifest = json.loads(body.decode("utf-8"))
            self.sessions.setdefault(manifest["session_id"], manifest)
            self.parts.setdefault(manifest["session_id"], {})
            return envelope({"session_id": manifest["session_id"]})
        if method == "GET" and len(parts) == 4 and parts[3] == "segments":
            stored = self.parts.get(parts[2], {})
            return envelope(
                {
                    "received": [
                        {"index": i, "sha256": hashlib.sha256(p).hexdigest()}
                        for i, p in sorted(stored.items())
                    ],
                    "missing": [],
                }
            )
        if method == "PUT" and len(parts) == 5 and parts[3] == "segments":
            index = int(parts[4])
            digest = hashlib.sha256(body).hexdigest()
            if digest != headers["X-Content-SHA256"]:
                raise http_error(409, code="E-SYN-409", message="摘要不符")
            self.parts.setdefault(parts[2], {})[index] = body
            return envelope({"index": index, "sha256": digest})
        if method == "POST" and len(parts) == 4 and parts[3] == "complete":
            self.idempotency_keys.append(headers["Idempotency-Key"])
            manifest = json.loads(body.decode("utf-8"))
            expected = {part["index"] for part in manifest["parts"]}
            if set(self.parts.get(parts[2], {})) != expected:
                raise http_error(409, code="E-SYN-409", message="件与清单不符")
            self.completed[parts[2]] = self.status_text
            return envelope({"session_id": parts[2], "ingest_status": self.status_text})
        raise http_error(404, code="E-API-404", message=f"没有 {method} {path}")

    def assembled(self, session_id: str) -> bytes:
        stored = self.parts.get(session_id, {})
        return b"".join(stored[i] for i in sorted(stored))


class RecordingOpener:
    """把请求交给假服务端，并记下每一次调用 —— 超时是被断言的对象之一。"""

    def __init__(self, server: FakeServer | None = None, *, error: Exception | None = None):
        self.server = server if server is not None else FakeServer()
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def open(self, request: urllib.request.Request, timeout: float | None = None):
        self.calls.append(
            {
                "method": request.get_method(),
                "url": request.full_url,
                "timeout": timeout,
                "headers": dict(request.header_items()),
                "body": request.data or b"",
            }
        )
        if self.error is not None:
            raise self.error
        path = request.full_url[len(BASE_URL) :]
        # urllib 把头名规范成 Title-Case，服务端按原名取，这里统一一次。
        headers = {k.replace("_", "-").title(): v for k, v in request.header_items()}
        headers["X-Content-SHA256"] = headers.get("X-Content-Sha256", "")
        return FakeResponse(self.server.handle(request.get_method(), path, headers, request.data or b""))


def client(opener: RecordingOpener, *, token_calls: list[int] | None = None) -> HttpIngestionClient:
    def provider() -> str:
        if token_calls is not None:
            token_calls.append(1)
        return TOKEN

    return HttpIngestionClient(identity(), provider, opener=opener)


# ── 四个动作 ────────────────────────────────────────────────────────────────


def test_begin_session_posts_the_manifest_under_its_idempotency_key() -> None:
    opener = RecordingOpener()
    api = client(opener)
    manifest = {"session_id": "s1", "parts": []}

    api.begin_session("s1", manifest, "session:s1:abc")

    call = opener.calls[0]
    assert call["method"] == "POST"
    assert call["url"] == f"{BASE_URL}/v1/sessions"
    assert header(call, "Idempotency-Key") == "session:s1:abc"
    assert header(call, "X-Schema-Version") == PAYLOAD_SCHEMA
    assert json.loads(call["body"].decode("utf-8")) == manifest


def test_accepted_parts_comes_from_the_server_not_from_local_progress() -> None:
    server = FakeServer()
    server.parts["s1"] = {0: b"aaa", 2: b"ccc"}
    api = client(RecordingOpener(server))

    assert api.accepted_parts("s1") == {
        0: hashlib.sha256(b"aaa").hexdigest(),
        2: hashlib.sha256(b"ccc").hexdigest(),
    }


def test_put_part_sends_the_bytes_and_returns_the_server_digest() -> None:
    opener = RecordingOpener()
    api = client(opener)
    payload = b"payload-bytes"
    digest = hashlib.sha256(payload).hexdigest()

    assert api.put_part("s1", 3, digest, payload) == digest

    call = opener.calls[0]
    assert call["method"] == "PUT"
    assert call["url"] == f"{BASE_URL}/v1/sessions/s1/segments/3"
    assert call["body"] == payload
    metadata = json.loads(header(call, "X-Segment-Metadata"))
    assert metadata == {
        "segment_index": 3,
        "size_bytes": len(payload),
        "sha256": digest,
        "payload_schema_version": PAYLOAD_SCHEMA,
    }
    # 帧区间字段**不发** —— 本仓库按归档字节切件（待确认卷 §3.2）。
    assert "start_frame_index" not in metadata
    assert "frame_count" not in metadata


def test_complete_session_normalises_the_status_case() -> None:
    """服务端枚举是大写 `INGESTED`，队列的常量是小写。

    差这一位就会让一次**已经落库**的上传被判成未确认，然后无限重试它。
    """
    server = FakeServer()
    server.status_text = "INGESTED"
    api = client(RecordingOpener(server))
    manifest = {"session_id": "s1", "parts": []}
    api.begin_session("s1", manifest, "k")

    assert api.complete_session("s1", manifest, "complete:k") == INGESTED


def test_a_status_other_than_ingested_is_returned_verbatim_for_the_queue_to_judge() -> None:
    server = FakeServer()
    server.status_text = "RECEIVING"
    api = client(RecordingOpener(server))
    manifest = {"session_id": "s1", "parts": []}
    api.begin_session("s1", manifest, "k")

    # 客户端不替队列判定 —— 它只归一化大小写，`INGESTED 才算数`那条留在 `_transfer`。
    assert api.complete_session("s1", manifest, "complete:k") == "receiving"


# ── 失败翻译：本模块的核心 ──────────────────────────────────────────────────


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_overload_and_server_faults_are_retryable(status: int) -> None:
    api = client(RecordingOpener(error=http_error(status)))
    with pytest.raises(UploadUnavailable):
        api.accepted_parts("s1")


@pytest.mark.parametrize("status", [401, 403])
def test_rejected_credentials_are_retryable_so_reprovisioning_recovers_on_its_own(
    status: int,
) -> None:
    """凭据被拒归**可重试**，是一次判断而不是照搬 —— 理由记在模块文档。

    归冲突的话，凭据换新之后条目也不会自己恢复，还得有人逐条捞回来；而那恰好发生在
    最坏的时刻（凭据集中到期）。
    """
    api = client(RecordingOpener(error=http_error(status)))
    with pytest.raises(UploadUnavailable):
        api.accepted_parts("s1")


def test_conflict_is_not_retryable() -> None:
    api = client(RecordingOpener(error=http_error(409, code="E-SYN-409")))
    with pytest.raises(UploadConflict):
        api.accepted_parts("s1")


@pytest.mark.parametrize("status", [400, 404, 422])
def test_a_request_the_server_refuses_is_a_conflict_not_a_retry(status: int) -> None:
    """重试不会让一个不合契约的请求变合法。它是客户端缺陷，要人看。"""
    api = client(RecordingOpener(error=http_error(status)))
    with pytest.raises(UploadConflict):
        api.accepted_parts("s1")


@pytest.mark.parametrize(
    "error",
    [
        TimeoutError("slow"),
        urllib.error.URLError(TimeoutError("slow")),
    ],
)
def test_timeouts_are_retryable_and_never_escape(error: Exception) -> None:
    """慢网必须可重试。

    漏成别的异常时队列会走 RAY-233 的未知异常兜底 —— 兜住了，但**兜住不等于翻译对了**。
    """
    api = client(RecordingOpener(error=error))
    with pytest.raises(UploadUnavailable):
        api.accepted_parts("s1")


def test_connection_reset_is_retryable() -> None:
    api = client(RecordingOpener(error=ConnectionResetError("peer reset")))
    with pytest.raises(UploadUnavailable):
        api.accepted_parts("s1")


def test_unreachable_host_is_retryable() -> None:
    api = client(RecordingOpener(error=urllib.error.URLError(OSError("no route"))))
    with pytest.raises(UploadUnavailable):
        api.accepted_parts("s1")


def test_a_tls_failure_is_a_conflict_because_retrying_meets_the_same_certificate() -> None:
    api = client(RecordingOpener(error=urllib.error.URLError(ssl.SSLError("bad cert"))))
    with pytest.raises(UploadConflict):
        api.accepted_parts("s1")


def test_an_unwrapped_tls_error_is_judged_the_same_as_a_wrapped_one() -> None:
    """`SSLError` 是 `OSError` 的子类。

    评审时发现的：包在 `URLError` 里的走冲突，直接抛出的会掉进 OSError 分支变成
    可重试 —— **同一个证书问题两种判定，取决于它被谁包过**。
    """
    api = client(RecordingOpener(error=ssl.SSLError("bad cert")))
    with pytest.raises(UploadConflict):
        api.accepted_parts("s1")


def test_a_non_integer_part_index_is_our_error_not_a_bare_crash() -> None:
    """裸 `ValueError` 逃出去会绕过整套翻译，让契约违反看起来像来路不明的崩溃。"""

    class Nonsense(FakeServer):
        def handle(self, method, path, headers, body):  # type: ignore[override]
            return envelope({"received": [{"index": "第一件", "sha256": "x"}], "missing": []})

    api = client(RecordingOpener(Nonsense()))
    with pytest.raises(IngestHttpError):
        api.accepted_parts("s1")


def test_a_receipt_digest_that_disagrees_is_a_conflict() -> None:
    class Liar(FakeServer):
        def handle(self, method, path, headers, body):  # type: ignore[override]
            if method == "PUT":
                return envelope({"index": 0, "sha256": "0" * 64})
            return super().handle(method, path, headers, body)

    api = client(RecordingOpener(Liar()))
    payload = b"x"
    with pytest.raises(UploadConflict):
        api.put_part("s1", 0, hashlib.sha256(payload).hexdigest(), payload)


def test_a_malformed_response_is_our_problem_not_a_data_conflict() -> None:
    """读不懂的响应不该伪装成一次数据冲突 —— 那会让一个客户端缺陷被记成需人工处理。"""

    class Garbled(FakeServer):
        def handle(self, method, path, headers, body):  # type: ignore[override]
            return b"not json at all"

    api = client(RecordingOpener(Garbled()))
    with pytest.raises(IngestHttpError):
        api.accepted_parts("s1")


def test_diagnostics_that_cannot_be_read_do_not_change_the_translation() -> None:
    """错误体读不出来时，「服务端 500」不能变成「客户端异常」。"""
    broken = urllib.error.HTTPError(BASE_URL, 503, "x", {}, io.BytesIO(b"<html>"))  # type: ignore[arg-type]
    api = client(RecordingOpener(error=broken))
    with pytest.raises(UploadUnavailable):
        api.accepted_parts("s1")


# ── 超时：每一次调用都必须显式带 ────────────────────────────────────────────


def test_every_http_call_carries_an_explicit_timeout() -> None:
    """没有超时的调用会把慢网变成挂住，而挂住的上传既不重试也不报错。"""
    opener = RecordingOpener()
    api = client(opener)
    manifest = {"session_id": "s1", "parts": []}
    payload = b"p"

    api.begin_session("s1", manifest, "k")
    api.accepted_parts("s1")
    api.put_part("s1", 0, hashlib.sha256(payload).hexdigest(), payload)
    api.complete_session("s1", {"session_id": "s1", "parts": [{"index": 0}]}, "complete:k")

    assert len(opener.calls) == 4
    for call in opener.calls:
        assert isinstance(call["timeout"], float) and call["timeout"] > 0, call


def test_uploading_a_part_gets_the_longer_timeout() -> None:
    """默认一件 4 MiB。小 JSON 请求的 30 秒对它不够。"""
    opener = RecordingOpener()
    api = client(opener)
    payload = b"p"
    api.put_part("s1", 0, hashlib.sha256(payload).hexdigest(), payload)

    assert opener.calls[0]["timeout"] == PART_TIMEOUT
    assert PART_TIMEOUT > DEFAULT_TIMEOUT


def test_a_non_positive_timeout_is_refused_at_construction() -> None:
    for bad in (0, -1, True):
        with pytest.raises(IngestHttpError):
            HttpIngestionClient(identity(), lambda: TOKEN, opener=RecordingOpener(), timeout=bad)


# ── 凭据与 TLS ──────────────────────────────────────────────────────────────


def test_the_token_is_fetched_per_request_and_not_cached_on_the_instance() -> None:
    calls: list[int] = []
    opener = RecordingOpener()
    api = client(opener, token_calls=calls)

    api.begin_session("s1", {"session_id": "s1", "parts": []}, "k")
    api.accepted_parts("s1")

    assert len(calls) == 2
    assert all(TOKEN not in str(value) for value in vars(api).values())
    assert header(opener.calls[0], "Authorization") == f"Bearer {TOKEN}"


def test_the_credential_never_reaches_any_file_on_disk(tmp_path: Path) -> None:
    """断言而不是靠人看 —— 沿用 RAY-323 同款要求。"""
    root = tmp_path / "sessions"
    root.mkdir()
    session_id = make_session(root)
    queue = UploadQueue(tmp_path / "queue.sqlite3")
    enqueue_session(queue, root, session_id)
    opener = RecordingOpener()
    uploader = SessionUploader(queue, client(opener), part_size=PART_SIZE)

    uploader.upload_once()

    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert TOKEN.encode() not in path.read_bytes(), path


def test_tls_verification_is_on_and_has_no_off_switch() -> None:
    opener = build_opener(identity())
    contexts = [
        handler.__dict__.get("_context")
        for handler in opener.handlers
        if isinstance(handler, urllib.request.HTTPSHandler)
    ]
    assert contexts, "没有找到 HTTPS handler"
    for context in contexts:
        assert context.check_hostname is True
        assert context.verify_mode == ssl.CERT_REQUIRED


def test_the_base_url_can_only_be_https() -> None:
    """本模块不自带降级开关：那道闸在 `TerminalIdentity` 上，这里只确认它还在。"""
    with pytest.raises(AccessError):
        identity().__class__(
            tenant_id="t",
            terminal_id="t",
            api_base_url="http://cloud.example.invalid",
            device_group=identity().device_group,
        )


# ── 端到端 ──────────────────────────────────────────────────────────────────


def test_a_queued_session_reaches_the_fake_server_and_is_confirmed(tmp_path: Path) -> None:
    """验收最后一条：真实入队 → 本客户端 → 假服务端 → 确认，且本地文件仍在。"""
    root = tmp_path / "sessions"
    root.mkdir()
    session_id = make_session(root)
    queue = UploadQueue(tmp_path / "queue.sqlite3")
    enqueue_session(queue, root, session_id)
    opener = RecordingOpener()
    uploader = SessionUploader(queue, client(opener), part_size=PART_SIZE)

    outcome = uploader.upload_once()

    assert outcome.confirmed
    entry = queue.get(session_id)
    assert entry is not None and entry.state == STATE_CONFIRMED
    # 服务端拿到的与本地打出来的逐字节一致。
    package = build_package(
        session_directory(root, session_id), session_id=session_id, part_size=PART_SIZE
    )
    assert opener.server.assembled(session_id) == package.archive
    # **确认前不删本地** —— 确认之后也不该由上传流程去删。
    assert raw_path(root, session_id, "L").exists()
    assert raw_path(root, session_id, "R").exists()


def test_a_resumed_upload_only_sends_what_the_server_is_missing(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    root.mkdir()
    session_id = make_session(root)
    package = build_package(
        session_directory(root, session_id), session_id=session_id, part_size=PART_SIZE
    )
    assert len(package.manifest.parts) > 1, "这条测试需要多件的包"

    server = FakeServer()
    # 服务端已经收下第一件 —— 模拟上一次传到一半断掉。
    server.parts[session_id] = {0: package.part(0)}

    queue = UploadQueue(tmp_path / "queue.sqlite3")
    enqueue_session(queue, root, session_id)
    opener = RecordingOpener(server)
    uploader = SessionUploader(queue, client(opener), part_size=PART_SIZE)

    outcome = uploader.upload_once()

    assert outcome.confirmed
    sent = [c for c in opener.calls if c["method"] == "PUT"]
    assert len(sent) == len(package.manifest.parts) - 1
    assert all(not c["url"].endswith("/segments/0") for c in sent)


def test_the_same_content_produces_the_same_idempotency_key_on_retry(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    root.mkdir()
    session_id = make_session(root)
    queue = UploadQueue(tmp_path / "queue.sqlite3")
    enqueue_session(queue, root, session_id)
    server = FakeServer()

    SessionUploader(queue, client(RecordingOpener(server)), part_size=PART_SIZE).upload_once()
    first = list(server.idempotency_keys)

    queue2 = UploadQueue(tmp_path / "queue2.sqlite3")
    enqueue_session(queue2, root, session_id)
    server2 = FakeServer()
    SessionUploader(queue2, client(RecordingOpener(server2)), part_size=PART_SIZE).upload_once()

    assert first == server2.idempotency_keys


# ── 契约翻面 ────────────────────────────────────────────────────────────────


def test_upload_transport_is_no_longer_an_unimplemented_capability() -> None:
    """契约与实现必须同时翻面（RAY-248）。翻面后还报缺口就是在骗界面。"""
    with pytest.raises(protocol.ProtocolError):
        protocol.unimplemented("req-1", "upload-transport")
