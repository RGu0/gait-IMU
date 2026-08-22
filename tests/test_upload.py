"""`gait.cloud.upload` 的待传队列与上传协议。

验收标准是 AC-08（断网会话完整）与 AC-09（恢复自动补传无重复），外加「断网 24 h 数据
零丢失」。这三条各有一组测试。

其余几组守的是让那三条**成立的机制**，而不是它们的表象：

* 两类失败必须分开 —— 可重试的退避，不可重试的停下等人看。混在一起要么放弃得太早，
  要么在冲突上空转到天荒地老。
* 断点续传靠服务端列举，不靠本地记进度。本地记录会陈旧，而陈旧的方向偏偏是最坏的
  那种：本地以为传过了，实际没有。
* 「服务端确认前不删本地」靠**接口形状**执行，不靠文档约定。
* 租约让崩溃的上传不会把条目永久卡住。
"""

import hashlib
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from gait.cloud.package import build_package
from gait.cloud.upload import (
    INGESTED,
    STATE_CONFIRMED,
    STATE_CONFLICT,
    STATE_PENDING,
    InMemoryIngestionClient,
    SessionUploader,
    UploadConflict,
    UploadError,
    UploadPolicy,
    UploadQueue,
    UploadUnavailable,
    enqueue_session,
)
from gait.contracts import SessionMeta
from gait.io.session import create_session, new_session_id, new_subject_uuid, raw_path

PART_SIZE = 4096


def incompressible(size: int, seed: int) -> bytes:
    """确定性的伪随机字节。

    测试要的是**多件**的包，而重复的填充字节会被压成一件，于是"传两件就断"这类
    断点续传测试根本没有断点可言 —— 第一轮就传完了。伪随机保证压不动，`seed`
    保证同一个会话每次打出同样的字节（否则幂等键会变，那正是被测的性质）。
    """
    blocks = [
        hashlib.blake2b(f"{seed}:{chunk}".encode(), digest_size=64).digest()
        for chunk in range(size // 64 + 1)
    ]
    return b"".join(blocks)[:size]


def make_session(root: Path, *, repeat: int = 20000) -> str:
    """一个带双足原始数据的会话目录，返回 id。"""
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


class FlakyClient(InMemoryIngestionClient):
    """可以被"断网"、也可以在传到一半时断的服务端。"""

    def __init__(self) -> None:
        super().__init__()
        self.online = True
        self.allow_puts: int | None = None
        self.puts = 0
        self.complete_status = INGESTED

    def _guard(self) -> None:
        if not self.online:
            raise UploadUnavailable("网络不可达")

    def begin_session(self, session_id, manifest, idempotency_key) -> None:
        self._guard()
        super().begin_session(session_id, manifest, idempotency_key)

    def accepted_parts(self, session_id) -> Mapping[int, str]:
        self._guard()
        return super().accepted_parts(session_id)

    def put_part(self, session_id, index, sha256, payload) -> str:
        self._guard()
        if self.allow_puts is not None and self.puts >= self.allow_puts:
            raise UploadUnavailable("传到一半断了")
        self.puts += 1
        return super().put_part(session_id, index, sha256, payload)

    def complete_session(self, session_id, manifest, idempotency_key) -> str:
        self._guard()
        status = super().complete_session(session_id, manifest, idempotency_key)
        return self.complete_status if self.complete_status != INGESTED else status


@pytest.fixture
def root(tmp_path: Path) -> Path:
    path = tmp_path / "sessions"
    path.mkdir()
    return path


@pytest.fixture
def queue(tmp_path: Path) -> UploadQueue:
    return UploadQueue(tmp_path / "queue.sqlite3")


@pytest.fixture
def client() -> FlakyClient:
    return FlakyClient()


@pytest.fixture
def uploader(queue: UploadQueue, client: FlakyClient) -> SessionUploader:
    return SessionUploader(queue, client, part_size=PART_SIZE)


def archive_of(root: Path, session_id: str) -> bytes:
    return build_package(root / session_id, session_id=session_id, part_size=PART_SIZE).archive


# ── AC-08：断网会话完整，24 h 零丢失 ──────────────────────────────────────────


def test_a_session_uploaded_while_offline_is_never_lost(root, queue, client, uploader):
    """断网期间条目留在队列里，恢复之后原样传上去。"""
    session_id = make_session(root)
    enqueue_session(queue, root, session_id, now=0.0)
    client.online = False

    assert uploader.upload_once(now=0.0).result == "deferred"
    assert queue.get(session_id).state == STATE_PENDING

    client.online = True
    queue.reset_backoff(now=100.0)
    assert uploader.upload_once(now=100.0).confirmed
    assert client.assembled(session_id) == archive_of(root, session_id)


def test_a_full_day_offline_loses_nothing_and_drains_in_one_pass(root, queue, client, uploader):
    """AC-08 的完整形态：多个会话、断网一整天、恢复后一轮 drain 全部确认。

    实测（`probe_upload.txt`）：40 个会话、24 小时断网期间打出 4000 次注定失败的尝试
    （每个会话 100 次，与指数退避的解析值一致），恢复后一轮 drain 确认 40/40，服务端
    拿到的字节与本地逐一相同。这里用 6 个会话跑同一条路径。
    """
    ids = [make_session(root, repeat=4000) for _ in range(6)]
    for index, session_id in enumerate(ids):
        enqueue_session(queue, root, session_id, now=index * 1800.0)

    client.online = False
    clock = 6 * 1800.0
    day = clock + 24 * 3600.0
    while clock < day:
        uploader.drain(limit=50, now=clock)
        clock += 900.0

    assert queue.backlog(now=clock).sessions == len(ids)

    client.online = True
    queue.reset_backoff(now=clock)
    outcomes = uploader.drain(limit=50, now=clock)

    assert sum(1 for outcome in outcomes if outcome.confirmed) == len(ids)
    assert queue.backlog(now=clock).sessions == 0
    for session_id in ids:
        assert client.assembled(session_id) == archive_of(root, session_id)


def test_backoff_keeps_a_day_of_retries_to_a_hundred_per_session(queue):
    """退避的**代价**：PRD 写了"零丢失"，没写要为它打出多少次请求。

    固定 30 s 重试在 24 小时里是 2880 次；指数退避封顶 15 分钟是 100 次。40 个会话
    时那是 115200 对 4000 的差别。
    """
    policy = UploadPolicy(backoff_base_s=30.0, backoff_cap_s=900.0)
    elapsed, attempts = 0.0, 0
    while elapsed < 24 * 3600:
        attempts += 1
        elapsed += policy.delay_for(attempts)

    assert attempts < 120
    assert policy.delay_for(1) == 30.0
    assert policy.delay_for(2) == 60.0
    assert policy.delay_for(50) == 900.0  # 封顶


def test_one_success_releases_everyone_else_from_their_backoff(root, queue, client, uploader):
    """退避上限可以定得大，正是因为有这一条。

    没有它，断网期间攒下的会话会各自等满自己的退避（最坏 15 分钟）才动一下；有了它，
    第一个成功的上传就证明链路通了，其余全部立即可传。
    """
    ids = [make_session(root, repeat=2000) for _ in range(4)]
    for session_id in ids:
        enqueue_session(queue, root, session_id, now=0.0)

    client.online = False
    for _ in range(5):
        uploader.drain(limit=10, now=0.0)
    deferred = queue.entries(STATE_PENDING)
    assert all(entry.next_attempt_at > 0.0 for entry in deferred)

    client.online = True
    # 只把第一个条目放行，让它成功。
    queue.reset_backoff(now=0.0)
    assert uploader.upload_once(now=0.0).confirmed
    # 那一次成功应当已经把其余条目的退避清零。
    assert all(entry.next_attempt_at <= 0.0 for entry in queue.entries(STATE_PENDING))


# ── AC-09：恢复自动补传无重复 ─────────────────────────────────────────────────


def test_resuming_never_sends_a_part_twice(root, queue, client, uploader):
    """AC-09 的核心断言。

    实测（`probe_upload.txt`）：40 件的会话每轮只放过 8 件，5 轮传完，跳过数依次是
    0 / 8 / 16 / 24 / 32，**没有任何一件被接收超过一次**。
    """
    session_id = make_session(root, repeat=20000)
    enqueue_session(queue, root, session_id, now=0.0)
    total = len(build_package(root / session_id, session_id=session_id, part_size=PART_SIZE).manifest.parts)
    assert total >= 4

    clock = 0.0
    rounds = 0
    while rounds < 30:
        rounds += 1
        client.allow_puts = client.puts + 2
        outcome = uploader.upload_once(now=clock)
        if outcome.confirmed:
            break
        clock += 3600.0
    else:  # pragma: no cover - 不收敛就是缺陷
        pytest.fail("补传没有收敛")

    assert max(client.put_calls.values()) == 1
    assert client.assembled(session_id) == archive_of(root, session_id)


def test_each_resume_round_skips_exactly_what_the_server_already_has(root, queue, client, uploader):
    """跳过数必须**单调增长** —— 那是断点续传真的在起作用的直接证据。

    如果每轮都从头传，跳过数会一直是 0，而总件数不变，测试就看不出区别。看跳过数
    才看得出。
    """
    session_id = make_session(root, repeat=20000)
    enqueue_session(queue, root, session_id, now=0.0)

    skipped: list[int] = []
    clock = 0.0
    for _ in range(30):
        client.allow_puts = client.puts + 2
        outcome = uploader.upload_once(now=clock)
        skipped.append(outcome.parts_skipped)
        if outcome.confirmed:
            break
        clock += 3600.0

    assert skipped[0] == 0
    assert skipped == sorted(skipped)
    assert skipped[-1] > skipped[0]


def test_a_failed_round_still_reports_how_far_it_got(root, queue, client, uploader):
    """失败的一轮也确实推进了 —— 报 0 会让人以为卡死了，进而去手工干预。"""
    session_id = make_session(root, repeat=20000)
    enqueue_session(queue, root, session_id, now=0.0)
    client.allow_puts = 2

    outcome = uploader.upload_once(now=0.0)

    assert outcome.result == "deferred"
    assert outcome.parts_sent == 2


def test_the_idempotency_key_is_the_same_on_every_retry(root, queue, client, uploader):
    """幂等的地基。键由归档摘要导出，所以重试必定产生同一个键。

    键要是每次都变，服务端就把每次重试当成一次新的上传 —— 而且**不会报错**，只是
    悄悄多存一份。
    """
    session_id = make_session(root, repeat=20000)
    enqueue_session(queue, root, session_id, now=0.0)

    clock = 0.0
    for _ in range(10):
        client.allow_puts = client.puts + 2
        if uploader.upload_once(now=clock).confirmed:
            break
        clock += 3600.0

    begins = [key for key in client.idempotency_keys if key.startswith("session:")]
    assert len(begins) > 1  # 确实重试了多次
    assert len(set(begins)) == 1  # 但键只有一个


# ── 两类失败必须分开 ──────────────────────────────────────────────────────────


def test_a_retryable_failure_defers_with_growing_backoff(root, queue, client, uploader):
    session_id = make_session(root)
    enqueue_session(queue, root, session_id, now=0.0)
    client.online = False

    uploader.upload_once(now=0.0)
    first = queue.get(session_id)
    queue.reset_backoff(now=0.0)
    uploader.upload_once(now=0.0)
    second = queue.get(session_id)

    assert first.state == STATE_PENDING
    assert second.attempts == first.attempts + 1
    assert second.next_attempt_at > first.next_attempt_at


def test_a_digest_conflict_stops_retrying_and_waits_for_a_human(root, queue, client, uploader):
    """冲突重试一万次也还是冲突。继续重试只是把一个需要人看的问题变成无声的死循环。"""
    session_id = make_session(root)
    enqueue_session(queue, root, session_id, now=0.0)
    # 服务端手上有一件，内容却不是本地那一件。
    client.parts[session_id] = {0: b"someone else's bytes"}

    outcome = uploader.upload_once(now=0.0)

    assert outcome.result == "conflict"
    assert queue.get(session_id).state == STATE_CONFLICT
    # 之后不再被取出重试。
    assert uploader.upload_once(now=1e9).result == "idle"


def test_a_server_part_outside_the_local_manifest_is_a_conflict(root, queue, client, uploader):
    """服务端手上有本地清单之外的件 —— 它记的不是这一份数据。

    继续传会把两份不同的数据缝在一起，而缝出来的东西每一件的摘要都对。
    """
    session_id = make_session(root)
    enqueue_session(queue, root, session_id, now=0.0)
    client.parts[session_id] = {999: b"stray"}

    outcome = uploader.upload_once(now=0.0)

    assert outcome.result == "conflict"
    assert "清单之外" in outcome.detail


def test_a_session_that_cannot_be_packaged_is_a_conflict_not_a_retry(root, queue, uploader):
    """打不出包不是网络问题，重试也打不出来。"""
    session_id = make_session(root)
    enqueue_session(queue, root, session_id, now=0.0)
    (root / session_id / "meta.json").unlink()

    outcome = uploader.upload_once(now=0.0)

    assert outcome.result == "conflict"
    assert queue.get(session_id).state == STATE_CONFLICT


def test_a_server_that_accepts_but_does_not_confirm_is_retryable(root, queue, client, uploader):
    """确认的判据是 `INGESTED`，不是 HTTP 200 —— 200 只说明请求被受理了。"""
    session_id = make_session(root)
    enqueue_session(queue, root, session_id, now=0.0)
    client.complete_status = "queued"

    outcome = uploader.upload_once(now=0.0)

    assert outcome.result == "deferred"
    assert queue.get(session_id).state == STATE_PENDING


# ── 服务端确认前不删本地 ──────────────────────────────────────────────────────


def test_forgetting_an_unconfirmed_session_is_refused(root, queue):
    """靠接口形状执行，不靠文档约定。危险的事应该做不到。"""
    session_id = make_session(root)
    enqueue_session(queue, root, session_id, now=0.0)

    with pytest.raises(UploadError, match="确认前不删本地"):
        queue.forget(session_id)


def test_forgetting_a_conflicted_session_is_also_refused(root, queue, client, uploader):
    """冲突条目更不能删 —— 它恰恰是数据可能没上去的那一个。"""
    session_id = make_session(root)
    enqueue_session(queue, root, session_id, now=0.0)
    client.parts[session_id] = {0: b"mismatch"}
    uploader.upload_once(now=0.0)

    with pytest.raises(UploadError, match="确认前不删本地"):
        queue.forget(session_id)


def test_a_confirmed_session_can_be_forgotten(root, queue, uploader):
    session_id = make_session(root)
    enqueue_session(queue, root, session_id, now=0.0)
    assert uploader.upload_once(now=0.0).confirmed

    queue.forget(session_id)
    assert queue.get(session_id) is None


# ── 队列本身 ──────────────────────────────────────────────────────────────────


def test_the_queue_survives_a_restart(root, tmp_path):
    """持久化的意义：进程重启之后待传的还在待传。"""
    path = tmp_path / "queue.sqlite3"
    session_id = make_session(root)
    enqueue_session(UploadQueue(path), root, session_id, now=0.0)

    reopened = UploadQueue(path)
    assert reopened.get(session_id).state == STATE_PENDING


def test_enqueuing_twice_does_not_reset_progress(root, queue, client, uploader):
    """重启后扫一遍会话目录会重复入队 —— 那不该把已有进度抹掉。

    抹掉的后果分两种，都不好：已确认的被重传，或冲突条目回到待传、于是无声地循环。
    """
    session_id = make_session(root)
    enqueue_session(queue, root, session_id, now=0.0)
    client.online = False
    uploader.upload_once(now=0.0)
    deferred = queue.get(session_id)

    enqueue_session(queue, root, session_id, now=500.0)
    after = queue.get(session_id)

    assert after.attempts == deferred.attempts
    assert after.next_attempt_at == deferred.next_attempt_at


def test_a_confirmed_session_is_not_re_uploaded_by_a_later_enqueue(root, queue, uploader):
    session_id = make_session(root)
    enqueue_session(queue, root, session_id, now=0.0)
    assert uploader.upload_once(now=0.0).confirmed

    enqueue_session(queue, root, session_id, now=10.0)

    assert queue.get(session_id).state == STATE_CONFIRMED
    assert uploader.upload_once(now=1e9).result == "idle"


def test_an_entry_is_not_leased_twice_at_the_same_time(root, queue):
    """租约必须是"查到并标记"一步完成，否则两个上传者会取到同一个会话。"""
    session_id = make_session(root)
    enqueue_session(queue, root, session_id, now=0.0)

    assert queue.lease(now=0.0) is not None
    assert queue.lease(now=0.0) is None


def test_a_crashed_upload_becomes_available_again_after_the_lease_expires(root, tmp_path):
    """只标记不带期限的话，一次崩溃就让那个会话永远留在"正在上传"。

    而它恰恰是最需要被重传的那个 —— 崩溃发生在传输中途，数据多半没上完。
    """
    queue = UploadQueue(tmp_path / "q.sqlite3", policy=UploadPolicy(lease_seconds=600.0))
    session_id = make_session(root)
    enqueue_session(queue, root, session_id, now=0.0)

    assert queue.lease(now=0.0) is not None  # 取走后"崩溃"，没有 defer 也没有确认
    assert queue.lease(now=300.0) is None  # 租约未到期
    assert queue.lease(now=601.0) is not None  # 到期后重新可取


def test_a_deferred_entry_is_not_leased_before_it_is_due(root, queue, client, uploader):
    session_id = make_session(root)
    enqueue_session(queue, root, session_id, now=0.0)
    client.online = False
    uploader.upload_once(now=0.0)

    assert queue.lease(now=1.0) is None
    assert queue.lease(now=1e9) is not None


def test_drain_stops_at_the_limit(root, queue, client, uploader):
    """`limit` 是硬上限：没有它，一个持续失败又持续到期的条目能让 drain 不返回。"""
    for _ in range(5):
        enqueue_session(queue, root, make_session(root, repeat=500), now=0.0)
    client.online = False

    assert len(uploader.drain(limit=3, now=0.0)) == 3


def test_drain_returns_idle_free_outcomes(root, queue, uploader):
    """空队列时 drain 返回空表，而不是一串 idle。"""
    assert uploader.drain(limit=10, now=0.0) == []


def test_an_unknown_state_filter_is_rejected(queue):
    with pytest.raises(UploadError, match="未知状态"):
        queue.entries("halfway")


def test_operating_on_a_session_that_is_not_queued_is_an_error(queue):
    with pytest.raises(UploadError, match="队列里没有"):
        queue.mark_conflict("nobody", error="x")


# ── 积压提示 ──────────────────────────────────────────────────────────────────


def test_the_backlog_warns_past_the_session_threshold(root, tmp_path, client):
    """PRD §6.1：待传超 100 次提示。v1 **只提示不阻断** —— 阻断会让采集停下来。"""
    policy = UploadPolicy(backlog_warn_sessions=3)
    queue = UploadQueue(tmp_path / "q.sqlite3", policy=policy)
    uploader = SessionUploader(queue, client, part_size=PART_SIZE)
    client.online = False

    for _ in range(4):
        enqueue_session(queue, root, make_session(root, repeat=200), now=0.0)
    uploader.drain(limit=10, now=0.0)

    report = queue.backlog(now=0.0)
    assert report.sessions == 4
    assert report.over_sessions
    assert report.warn
    # 只提示不阻断：队列照常继续接收与推进。
    queue.reset_backoff(now=0.0)
    client.online = True
    assert uploader.upload_once(now=0.0).confirmed


def test_the_backlog_warns_past_the_byte_threshold(root, tmp_path, client):
    policy = UploadPolicy(backlog_warn_bytes=100)
    queue = UploadQueue(tmp_path / "q.sqlite3", policy=policy)
    uploader = SessionUploader(queue, client, part_size=PART_SIZE)
    client.online = False

    enqueue_session(queue, root, make_session(root), now=0.0)
    uploader.drain(limit=5, now=0.0)  # 走一轮好让大小被记下来

    report = queue.backlog(now=0.0)
    assert report.bytes > 100
    assert report.over_bytes and report.warn


def test_a_conflicted_entry_makes_the_backlog_warn_on_its_own(root, queue, client, uploader):
    """冲突条目不会自己消失，需要人看 —— 哪怕只有一个也该提示。"""
    session_id = make_session(root)
    enqueue_session(queue, root, session_id, now=0.0)
    client.parts[session_id] = {0: b"mismatch"}
    uploader.upload_once(now=0.0)

    report = queue.backlog(now=0.0)
    assert report.conflicts == 1
    assert report.sessions == 0  # 已不在待传里
    assert report.warn


def test_an_empty_backlog_does_not_warn(queue):
    report = queue.backlog(now=0.0)

    assert report.sessions == 0
    assert not report.warn
    assert report.oldest_age_s == 0.0


def test_the_backlog_snapshot_is_plain_json_types(root, queue):
    import json

    enqueue_session(queue, root, make_session(root), now=0.0)
    snapshot = queue.backlog(now=10.0).snapshot()

    assert json.loads(json.dumps(snapshot))["sessions"] == 1
    assert isinstance(snapshot["warn"], bool)


# ── 内存服务端本身 ────────────────────────────────────────────────────────────


def test_the_in_memory_server_rejects_a_part_whose_declared_digest_is_wrong():
    """测试替身也必须守真实服务端的契约，否则测出来的是假的。"""
    server = InMemoryIngestionClient()
    server.begin_session("s", {"parts": []}, "k")

    with pytest.raises(UploadConflict):
        server.put_part("s", 0, "0" * 64, b"payload")


def test_the_in_memory_server_refuses_to_complete_with_missing_parts():
    server = InMemoryIngestionClient()
    manifest: dict[str, Any] = {"parts": [{"index": 0}, {"index": 1}]}
    server.begin_session("s", manifest, "k")
    server.put_part("s", 0, hashlib.sha256(b"a").hexdigest(), b"a")

    with pytest.raises(UploadUnavailable):
        server.complete_session("s", manifest, "k")
