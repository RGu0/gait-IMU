"""RAY-233 `session-upload-wiring`：把上传接进 sidecar，并堵住慢网暴露的缺口。

## 断网与重复请求不在这里

RAY-226 已经把它们做完并测透（`tests/test_upload.py` 33 条：离线不丢、整天离线一轮
排完、断点续传不重发、幂等键稳定、摘要冲突停重试、租约过期回收、重启存活）。
**本文件不重做那些**，只补两件它没覆盖的：

1. 会话结束后**有没有人**把它排进队列 —— 在此之前没有；
2. 客户端抛出一个队列不认识的异常时会怎样 —— 在此之前它会逃逸，并停掉整个 drain。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gait.app.service import TerminalService
from gait.app.sources import StubDeviceSource, synthetic_frame
from gait.cloud.upload import (
    STATE_PENDING,
    SessionUploader,
    UploadQueue,
    enqueue_session,
)
from gait.contracts import SessionMeta
from gait.io.session import create_session, new_session_id, new_subject_uuid

# ── 一、会话结束后真的入队了 ──────────────────────────────────────────────


def _finished_session(tmp_path: Path, **kwargs) -> tuple[TerminalService, str]:
    source = StubDeviceSource(**kwargs)
    service = TerminalService(source=source, session_root=tmp_path)
    session_id = service.handle(
        {"id": "s", "method": "startSession", "params": {"now": 0.0}}
    )["result"]["sessionId"]
    for index in range(4):
        source.feed("L", synthetic_frame(index))
    service.handle({"id": "e", "method": "stopSession", "params": {"now": 200.0}})
    return service, session_id


def test_a_finished_session_is_queued_for_upload(tmp_path: Path) -> None:
    """PRD §9 的「结束 → 后台上传」那个箭头，在此之前不存在。"""
    service, session_id = _finished_session(tmp_path)
    queued = [entry.session_id for entry in service.uploads.entries(STATE_PENDING)]
    assert queued == [session_id]


def test_the_queue_points_at_the_real_session_directory(tmp_path: Path) -> None:
    service, session_id = _finished_session(tmp_path)
    entry = service.uploads.entries(STATE_PENDING)[0]
    # `QueueEntry.directory` 是字符串（SQLite 里存的就是它）。
    assert Path(entry.directory) == tmp_path / session_id
    assert (Path(entry.directory) / "meta.json").is_file()


def test_an_incomplete_session_is_queued_too(tmp_path: Path) -> None:
    """不排它，等于恰恰把记录了一次故障的那份数据丢掉。

    G-04 要的是数据不静默丢失，不是只保住顺利的那些。残缺由元数据自己说明
    （`integrity_report.complete = False`），它跟着数据一起上去。
    """
    source = StubDeviceSource()
    service = TerminalService(source=source, session_root=tmp_path)
    session_id = service.handle(
        {"id": "s", "method": "startSession", "params": {"now": 0.0}}
    )["result"]["sessionId"]
    service.capture._writers["L"].error = OSError("[Errno 28] No space left on device")
    service.tick(1.0)  # 安全停止

    queued = [entry.session_id for entry in service.uploads.entries(STATE_PENDING)]
    assert queued == [session_id]


def test_a_run_without_a_session_root_tracks_nothing_and_says_so(
    tmp_path: Path,
) -> None:
    """没在记账要说出来，而不是报一个永远为 0 的待传数。"""
    service = TerminalService(source=StubDeviceSource(), session_root=None)
    assert service.uploads is None
    summary = service.handle({"id": "a", "method": "snapshot"})["result"][
        "uploadSummary"
    ]
    assert summary == {"tracked": False}


def test_the_workbench_sees_the_real_backlog(tmp_path: Path) -> None:
    """P-01 的待传条数来自真实队列。一个永远显示 0 的数字会让积压永远不被发现。"""
    service = TerminalService(source=StubDeviceSource(), session_root=tmp_path)
    before = service.handle({"id": "a", "method": "snapshot"})["result"][
        "uploadSummary"
    ]
    assert before["sessions"] == 0

    _finished_session(tmp_path)  # 另起一场，写进同一个 root
    after = service.handle({"id": "b", "method": "snapshot"})["result"]["uploadSummary"]
    assert after["sessions"] == 1
    assert after["tracked"] is True


# ── 二、慢网：未知异常不再逃逸 ────────────────────────────────────────────


class SlowServer:
    """一个只会超时的服务端。

    `IngestionClient` 是 Protocol，**没有规定实现者该抛什么** —— 真实的 HTTP 客户端
    会很自然地让 `TimeoutError` 逃出来。这个替身就长成那样。
    """

    def __init__(self, error: BaseException | None = None) -> None:
        self.error = error or TimeoutError("read timed out")
        self.calls = 0

    def begin_session(self, session_id, manifest, idempotency_key) -> None:
        self.calls += 1
        raise self.error

    def accepted_parts(self, session_id):  # pragma: no cover - 到不了
        return {}

    def put_part(self, session_id, index, sha256, payload):  # pragma: no cover
        return sha256

    def complete_session(
        self, session_id, manifest, idempotency_key
    ):  # pragma: no cover
        return "ingested"


def _queued(tmp_path: Path, count: int) -> UploadQueue:
    queue = UploadQueue(tmp_path / "queue.sqlite3")
    for index in range(count):
        session_id = new_session_id()
        create_session(tmp_path, _meta(session_id, index))
        # 用同一个 now 入队：默认走真实时钟，而下面按 now=0.0 租约会拿不到条目
        # （它还没到期）—— 那会让这些测试变成一直 idle 的空转。
        enqueue_session(queue, tmp_path, session_id, now=0.0)
    return queue


def _meta(session_id: str, seed: int) -> SessionMeta:
    return SessionMeta(
        session_id=session_id,
        created_at="2026-09-02T00:00:00Z",
        subject_uuid=new_subject_uuid(),
        scenario="walk",
        devices={"L": {"device_id": f"dev-{seed}"}},
        config_snapshot={"rate": 11},
        calib_snapshot={"state": "unimplemented"},
        algo_version="test",
        algo_params={"duration_s": 60},
        sync_report={"state": "pending"},
        integrity_report={"state": "pending"},
        protocol_config={"duration_s": 60},
    )


def test_a_timeout_does_not_escape_upload_once(tmp_path: Path) -> None:
    """在此之前它会直接抛出去。"""
    queue = _queued(tmp_path, 1)
    uploader = SessionUploader(queue, SlowServer())
    outcome = uploader.upload_once(now=0.0)
    assert outcome.result == "deferred"


def test_a_timeout_is_retryable_not_a_conflict(tmp_path: Path) -> None:
    """方向的代价不对称：判成冲突会让这份数据**永远传不上去**。"""
    queue = _queued(tmp_path, 1)
    uploader = SessionUploader(queue, SlowServer())
    uploader.upload_once(now=0.0)
    assert len(queue.entries(STATE_PENDING)) == 1
    assert queue.backlog().conflicts == 0


def test_the_error_text_names_the_exception_type(tmp_path: Path) -> None:
    """代价是这可能盖住我们自己的 bug。

    连着几十条 `TypeError: ...` 与连着几十条 `Timeout` 长得完全不同 —— 类型名是
    把前者认出来的唯一线索。
    """
    queue = _queued(tmp_path, 1)
    uploader = SessionUploader(queue, SlowServer(error=TypeError("我们自己的 bug")))
    outcome = uploader.upload_once(now=0.0)
    assert "TypeError" in outcome.detail
    assert "TypeError" in queue.entries(STATE_PENDING)[0].last_error


def test_one_slow_session_does_not_stop_the_whole_drain(tmp_path: Path) -> None:
    """这是这组测试里唯一真正重要的一条。

    `drain` 循环调 `upload_once` 且没有异常保护。一条会话碰上一次超时，整个排空
    循环就停了 —— 其余待传会话一并停住，而「断网 24 h 零丢失」正指望它活着。
    """
    queue = _queued(tmp_path, 3)
    uploader = SessionUploader(queue, SlowServer())
    outcomes = uploader.drain(now=0.0)
    # 三条都被推进过（各自退避），而不是在第一条上抛出去。
    assert len(outcomes) == 3
    assert {outcome.result for outcome in outcomes} == {"deferred"}


@pytest.mark.parametrize(
    "error",
    [
        TimeoutError("read timed out"),
        ConnectionResetError("peer reset"),
        OSError("network down"),
    ],
)
def test_the_usual_slow_network_exceptions_are_all_survivable(
    tmp_path: Path, error: BaseException
) -> None:
    queue = _queued(tmp_path, 1)
    uploader = SessionUploader(queue, SlowServer(error=error))
    assert uploader.upload_once(now=0.0).result == "deferred"
