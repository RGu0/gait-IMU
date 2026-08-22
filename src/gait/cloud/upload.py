"""待传队列与上传协议。契约 §1 的 `cloud/upload.py`（F6.2 的后半）。

PRD §6.1：失败自动重试、断点续传、幂等去重、摘要冲突检测；**服务端确认前不删本地**；
网络恢复自动补传；待传超 100 次 / 1 GB 提示（v1 只提示不阻断）。

打包在 `cloud/package.py`（RAY-226 的前一个 scope）。本模块消费它产出的
`PackageManifest`，尤其是 `idempotency_key` —— 那个键由归档摘要导出，所以**重试必定
产生同一个键**。这是整套幂等的地基；地基的证明在 `test_package.py`。

## 两类失败必须分开，重试逻辑建在这条分界上

参考 FeetForcePlate `client/sync/persistent_upload.py`：

* `UploadUnavailable` —— 网络错误、429、5xx。**可重试**：问题在链路或服务端负载，
  同样的请求过一会儿会成功。
* `UploadConflict` —— 409、摘要不符、服务端有本地清单之外的件。**不可重试**：
  同样的请求再发一万次也还是冲突，重试只是把一个需要人看的问题变成一条无声的死循环。

把重试建在"重试几次"上是常见的错法：它对这两类一视同仁，于是要么放弃得太早（真的
只是网络抖动），要么在冲突上空转到天荒地老。

## 断点续传靠服务端列举，不靠本地记"传到哪了"

本地进度记录会陈旧 —— 崩溃、时钟跳变、手工改文件都能让它与服务端不一致，而不一致的
方向偏偏是最坏的那种：本地以为传过了，实际没有。

所以每次上传先问服务端**已经收了哪几件**，再只补缺的那些。已收且摘要相符的跳过；
**摘要不符则报冲突** —— 那意味着服务端手上那一件与本地这一件不是同一份数据，继续传
只会把两份不同的数据缝在一起。

这个做法顺带把幂等做掉了：重复上传同一件在服务端看来是幂等的，而客户端连发都不会发。

## 退避：断网 24 小时的代价

"断网 24 h 零丢失"是正确性要求，PRD 写了。**代价**没写，实测如下（单个待传会话在
24 小时里打出的注定失败的请求数）：

| 策略 | 请求数 | 恢复后最坏等待 |
| --- | --- | --- |
| 固定 30 s | 2880 | 30 s |
| 固定 300 s | 288 | 5 min |
| 指数 30 s → 上限 15 min | **100** | 15 min |
| 指数 30 s → 上限 30 min | 53 | 30 min |
| 指数 30 s → 上限 60 min | 30 | 60 min |

40 个待传会话（约一天的采集量）时，固定 30 s 会打出 **115200** 次请求。

上限取 **15 分钟**，再加一条：**任何一次成功都把其余条目的退避清零**。这两条合起来
拿到了两头 —— 断网期间请求数压到 100 量级，而网络一恢复，第一个到期的条目一旦成功，
其余全部立即可传，不必各自等满自己的退避。

## 服务端确认前不删本地

不是靠文档约定，是靠**接口形状**：`forget()` 对未确认的条目直接抛错。要让危险的事
做不到，而不是只在注释里劝阻。

确认的判据是服务端回 `INGESTED`，不是 HTTP 200 —— 200 只说明请求被受理了。

## 租约：崩溃的上传不能把条目永久卡住

取条目用**租约**而不是"标记为进行中"：租约带到期时刻，进程崩在传输中途时，条目会在
租约到期后重新可取。只标记不带期限的话，一次崩溃就让那个会话永远留在"正在上传"，
而它恰恰是最需要被重传的那个。
"""

from __future__ import annotations

import hashlib
import sqlite3
import time
from collections.abc import Iterator, Mapping
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Protocol

from gait.cloud.package import (
    PackageError,
    PackageManifest,
    SessionPackage,
    build_package,
)

#: 上传协议的结构版本。
UPLOAD_PROTOCOL_VERSION: Final[str] = "1.0"

#: 队列条目的状态。
STATE_PENDING: Final[str] = "pending"
STATE_CONFIRMED: Final[str] = "confirmed"
STATE_CONFLICT: Final[str] = "conflict"
STATES: Final[tuple[str, ...]] = (STATE_PENDING, STATE_CONFIRMED, STATE_CONFLICT)

#: 服务端表示"已完整落库"的状态。**不是 HTTP 200** —— 200 只说明请求被受理了。
INGESTED: Final[str] = "ingested"


class UploadError(RuntimeError):
    """上传失败的基类。"""


class UploadUnavailable(UploadError):
    """**可重试**：网络错误、429、5xx。同样的请求过一会儿会成功。"""


class UploadConflict(UploadError):
    """**不可重试**：409、摘要不符。同样的请求再发一万次也还是冲突。"""


@dataclass(frozen=True)
class UploadPolicy:
    """上传行为的配置。

    **刻意不放进 `config.SessionConfig`。** 那个类装的是要写进 `meta.json` 的东西
    （`protocol_config` 与 `algo_params`），而退避策略既不是会话的属性、也不该被
    冻结：把退避上限从 15 分钟调到 30 分钟，不应该让队列里已经打好的包失效。
    """

    #: 首次退避，s。
    backoff_base_s: float = 30.0
    #: 退避上限，s。实测理由见模块文档 —— 15 分钟是"请求数"与"恢复速度"的折中。
    backoff_cap_s: float = 900.0
    #: 租约时长，s。崩溃的上传在这之后重新可取。
    lease_seconds: float = 600.0
    #: 积压提示阈值。PRD §6.1：**只提示不阻断**。
    backlog_warn_sessions: int = 100
    backlog_warn_bytes: int = 1024**3

    def delay_for(self, attempts: int) -> float:
        """第 `attempts` 次失败之后该等多久。指数退避，封顶。"""
        if attempts < 1:
            return self.backoff_base_s
        return min(self.backoff_base_s * 2 ** (attempts - 1), self.backoff_cap_s)


@dataclass(frozen=True)
class QueueEntry:
    """一个待传会话。"""

    session_id: str
    directory: str
    state: str
    attempts: int
    #: 下次可取的时刻（epoch 秒）。
    next_attempt_at: float
    enqueued_at: float
    #: 打包后的字节数；入队时还没打包则为 0。积压提示按它算。
    size_bytes: int
    #: 上一次成功打包时的归档摘要。用于发现"包重打之后内容变了"。
    archive_sha256: str
    #: 租约到期时刻；未被租出时为 0。
    leased_until: float
    last_error: str
    confirmed_at: float

    @property
    def pending(self) -> bool:
        return self.state == STATE_PENDING


@dataclass(frozen=True)
class BacklogReport:
    """积压情况。PRD §6.1：超 100 次 / 1 GB 提示，**只提示不阻断**。"""

    sessions: int
    bytes: int
    oldest_age_s: float
    conflicts: int
    over_sessions: bool
    over_bytes: bool

    @property
    def warn(self) -> bool:
        """是否该提示。冲突条目也算 —— 它们不会自己消失，需要人看。"""
        return self.over_sessions or self.over_bytes or bool(self.conflicts)

    def snapshot(self) -> dict[str, Any]:
        return {
            "sessions": self.sessions,
            "bytes": self.bytes,
            "oldest_age_s": self.oldest_age_s,
            "conflicts": self.conflicts,
            "over_sessions": self.over_sessions,
            "over_bytes": self.over_bytes,
            "warn": self.warn,
        }


@dataclass
class _Progress:
    """一轮传输的进度。失败时也要读得到 —— 见 `UploadOutcome.parts_sent`。"""

    sent: int = 0
    skipped: int = 0


@dataclass(frozen=True)
class UploadOutcome:
    """一次 `upload_once` 的结果。"""

    session_id: str | None
    #: `confirmed` / `deferred` / `conflict` / `idle`。
    result: str
    #: 这一轮**真正送出**的件数。**失败的一轮也算数**：传到一半断网时，那一轮确实
    #: 把几件送到了服务端，下一轮会跳过它们。报 0 会让看日志的人以为完全没动，而
    #: "看起来没动"正是让人误以为卡死、去手工干预的那种表象。
    parts_sent: int
    #: 因服务端已有且摘要相符而跳过的件数。断点续传的效果直接体现在这个数上。
    parts_skipped: int
    detail: str = ""

    @property
    def confirmed(self) -> bool:
        return self.result == "confirmed"


class IngestionClient(Protocol):
    """服务端的最小接口。

    刻意只有四个动作。`accepted_parts` 是断点续传的全部依据 —— 客户端不记进度，
    每次都问服务端。
    """

    def begin_session(
        self, session_id: str, manifest: Mapping[str, Any], idempotency_key: str
    ) -> None:
        """登记一次会话。重复调用必须幂等（同一个 `idempotency_key`）。"""
        ...

    def accepted_parts(self, session_id: str) -> Mapping[int, str]:
        """服务端已经收下的件：`{件号: sha256}`。"""
        ...

    def put_part(self, session_id: str, index: int, sha256: str, payload: bytes) -> str:
        """上传一件，返回服务端算出的 sha256。"""
        ...

    def complete_session(
        self, session_id: str, manifest: Mapping[str, Any], idempotency_key: str
    ) -> str:
        """收尾，返回状态字符串。只有 `INGESTED` 算数。"""
        ...


_SCHEMA: Final[str] = """
CREATE TABLE IF NOT EXISTS queue (
    session_id      TEXT PRIMARY KEY,
    directory       TEXT NOT NULL,
    state           TEXT NOT NULL,
    attempts        INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL NOT NULL DEFAULT 0,
    enqueued_at     REAL NOT NULL,
    size_bytes      INTEGER NOT NULL DEFAULT 0,
    archive_sha256  TEXT NOT NULL DEFAULT '',
    leased_until    REAL NOT NULL DEFAULT 0,
    last_error      TEXT NOT NULL DEFAULT '',
    confirmed_at    REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS queue_due ON queue (state, next_attempt_at);
"""


class UploadQueue:
    """SQLite 上的持久化待传队列。

    用 SQLite 而不是一堆 JSON 文件，是为了**取条目这一步的原子性**：租约必须是
    "查到并标记"一步完成，否则两个上传者会同时取到同一个会话。

    数据库放在会话根目录旁边而不是里面 —— 它是跨会话的状态，放进某一个会话目录会让
    那个会话的打包把队列自己也打进去。
    """

    def __init__(self, path: Path | str, *, policy: UploadPolicy | None = None) -> None:
        self.path = Path(path)
        self.policy = policy or UploadPolicy()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(_SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, isolation_level=None)
        try:
            # WAL：写不挡读，且崩溃后能恢复到最后一次提交。PRD §15 要求"写入点断电
            # 可恢复已关闭数据"，队列自己也在这条要求之内。
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            yield connection
        finally:
            connection.close()

    @staticmethod
    def _row(row: sqlite3.Row | tuple) -> QueueEntry:
        return QueueEntry(
            session_id=row[0],
            directory=row[1],
            state=row[2],
            attempts=row[3],
            next_attempt_at=row[4],
            enqueued_at=row[5],
            size_bytes=row[6],
            archive_sha256=row[7],
            leased_until=row[8],
            last_error=row[9],
            confirmed_at=row[10],
        )

    def enqueue(
        self, session_id: str, directory: Path | str, *, now: float | None = None
    ) -> QueueEntry:
        """把一个会话排进待传队列。**重复入队是幂等的**，不会重置已有的进度。

        重复入队在真实使用里会发生（重启后扫一遍会话目录），而重置退避或状态会让
        一个已经确认的会话被重传，或让一个冲突条目回到待传、于是无声地循环下去。
        """
        moment = time.time() if now is None else now
        with self._connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO queue "
                "(session_id, directory, state, enqueued_at, next_attempt_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (session_id, str(Path(directory)), STATE_PENDING, moment, moment),
            )
        entry = self.get(session_id)
        if entry is None:  # pragma: no cover - 插入之后必然存在
            raise UploadError(f"入队之后找不到条目：{session_id}")
        return entry

    def get(self, session_id: str) -> QueueEntry | None:
        with self._connect() as connection, closing(
            connection.execute("SELECT * FROM queue WHERE session_id = ?", (session_id,))
        ) as cursor:
            row = cursor.fetchone()
        return self._row(row) if row else None

    def entries(self, state: str | None = None) -> list[QueueEntry]:
        query = "SELECT * FROM queue"
        parameters: tuple[Any, ...] = ()
        if state is not None:
            if state not in STATES:
                raise UploadError(f"未知状态 {state!r}；可选 {STATES}")
            query += " WHERE state = ?"
            parameters = (state,)
        query += " ORDER BY enqueued_at"
        with self._connect() as connection, closing(
            connection.execute(query, parameters)
        ) as cursor:
            return [self._row(row) for row in cursor.fetchall()]

    def lease(self, *, now: float | None = None) -> QueueEntry | None:
        """取一个到期的待传条目并加租约。没有就返回 `None`。

        租约让崩溃的上传不会把条目永久卡住 —— 见模块文档。取与标记在同一条 UPDATE
        里完成，所以两个上传者不会取到同一个会话。
        """
        moment = time.time() if now is None else now
        deadline = moment + self.policy.lease_seconds
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            with closing(
                connection.execute(
                    "SELECT * FROM queue WHERE state = ? AND next_attempt_at <= ? "
                    "AND leased_until <= ? ORDER BY next_attempt_at, enqueued_at LIMIT 1",
                    (STATE_PENDING, moment, moment),
                )
            ) as cursor:
                row = cursor.fetchone()
            if row is None:
                connection.execute("COMMIT")
                return None
            connection.execute(
                "UPDATE queue SET leased_until = ? WHERE session_id = ?", (deadline, row[0])
            )
            connection.execute("COMMIT")
        entry = self.get(row[0])
        return entry

    def defer(
        self, session_id: str, *, error: str, now: float | None = None
    ) -> QueueEntry:
        """一次可重试的失败：记账、退避、解除租约。"""
        moment = time.time() if now is None else now
        entry = self._require(session_id)
        attempts = entry.attempts + 1
        with self._connect() as connection:
            connection.execute(
                "UPDATE queue SET attempts = ?, next_attempt_at = ?, leased_until = 0, "
                "last_error = ? WHERE session_id = ?",
                (attempts, moment + self.policy.delay_for(attempts), error, session_id),
            )
        return self._require(session_id)

    def mark_conflict(self, session_id: str, *, error: str) -> QueueEntry:
        """一次不可重试的失败。**不再自动重试** —— 冲突需要人看。"""
        with self._connect() as connection:
            connection.execute(
                "UPDATE queue SET state = ?, leased_until = 0, last_error = ? "
                "WHERE session_id = ?",
                (STATE_CONFLICT, error, session_id),
            )
        return self._require(session_id)

    def mark_confirmed(
        self, session_id: str, *, archive_sha256: str, size_bytes: int, now: float | None = None
    ) -> QueueEntry:
        """服务端已确认落库。**这一步之后才允许删本地。**"""
        moment = time.time() if now is None else now
        with self._connect() as connection:
            connection.execute(
                "UPDATE queue SET state = ?, leased_until = 0, last_error = '', "
                "confirmed_at = ?, archive_sha256 = ?, size_bytes = ? WHERE session_id = ?",
                (STATE_CONFIRMED, moment, archive_sha256, size_bytes, session_id),
            )
        return self._require(session_id)

    def record_size(self, session_id: str, *, size_bytes: int, archive_sha256: str) -> None:
        """记下打包后的大小与摘要，好让积压提示算得准。"""
        with self._connect() as connection:
            connection.execute(
                "UPDATE queue SET size_bytes = ?, archive_sha256 = ? WHERE session_id = ?",
                (size_bytes, archive_sha256, session_id),
            )

    def reset_backoff(self, *, now: float | None = None) -> int:
        """把所有待传条目的退避清零，返回受影响的条目数。

        **网络一恢复就该用它。** 没有它，断网期间攒下的 40 个会话会各自等满自己的
        退避（最坏 15 分钟）才动一下；有了它，第一个成功的上传就把其余全部放行。
        退避上限因此可以定得比较大而不牺牲恢复速度 —— 两头都要，见模块文档。
        """
        moment = time.time() if now is None else now
        with self._connect() as connection, closing(
            connection.execute(
                "UPDATE queue SET next_attempt_at = ? WHERE state = ? AND next_attempt_at > ?",
                (moment, STATE_PENDING, moment),
            )
        ) as cursor:
            return cursor.rowcount

    def backlog(self, *, now: float | None = None) -> BacklogReport:
        """积压情况。PRD §6.1 的「100 次 / 1 GB 提示」。"""
        moment = time.time() if now is None else now
        pending = self.entries(STATE_PENDING)
        conflicts = len(self.entries(STATE_CONFLICT))
        total_bytes = sum(entry.size_bytes for entry in pending)
        oldest = min((entry.enqueued_at for entry in pending), default=moment)
        return BacklogReport(
            sessions=len(pending),
            bytes=total_bytes,
            oldest_age_s=max(moment - oldest, 0.0),
            conflicts=conflicts,
            over_sessions=len(pending) > self.policy.backlog_warn_sessions,
            over_bytes=total_bytes > self.policy.backlog_warn_bytes,
        )

    def forget(self, session_id: str) -> None:
        """把条目移出队列。**只允许对已确认的条目做。**

        这是"服务端确认前不删本地"那条规则的执行点。它靠接口形状而不是文档约定 ——
        危险的事应该做不到，而不是只在注释里被劝阻。
        """
        entry = self._require(session_id)
        if entry.state != STATE_CONFIRMED:
            raise UploadError(
                f"会话 {session_id} 还没有被服务端确认（当前 {entry.state}），不能移出队列。"
                "PRD §6.1：服务端确认前不删本地。"
            )
        with self._connect() as connection:
            connection.execute("DELETE FROM queue WHERE session_id = ?", (session_id,))

    def _require(self, session_id: str) -> QueueEntry:
        entry = self.get(session_id)
        if entry is None:
            raise UploadError(f"队列里没有会话 {session_id}")
        return entry


class SessionUploader:
    """把队列里的会话一个一个传上去。

    **一次只推进一个条目**（`upload_once`），而不是自己开线程循环。理由是 PRD §6.1
    要求"不阻塞 UI 与下一位检测"—— 那是调用方的调度问题，本类只保证每一步都短、
    可中断、且中断之后状态是一致的。把线程藏在这里会让调用方失去对时机的控制。
    """

    def __init__(
        self,
        queue: UploadQueue,
        client: IngestionClient,
        *,
        policy: UploadPolicy | None = None,
        part_size: int | None = None,
    ) -> None:
        self.queue = queue
        self.client = client
        self.policy = policy or queue.policy
        self.part_size = part_size

    def _package(self, entry: QueueEntry) -> SessionPackage:
        kwargs: dict[str, Any] = {"session_id": entry.session_id}
        if self.part_size is not None:
            kwargs["part_size"] = self.part_size
        return build_package(entry.directory, **kwargs)

    def upload_once(self, *, now: float | None = None) -> UploadOutcome:
        """推进一个条目。没有到期的条目时返回 `idle`。"""
        moment = time.time() if now is None else now
        entry = self.queue.lease(now=moment)
        if entry is None:
            return UploadOutcome(session_id=None, result="idle", parts_sent=0, parts_skipped=0)

        try:
            package = self._package(entry)
        except PackageError as exc:
            # 打不出包不是网络问题，重试也打不出来。
            self.queue.mark_conflict(entry.session_id, error=f"打包失败：{exc}")
            return UploadOutcome(
                session_id=entry.session_id,
                result="conflict",
                parts_sent=0,
                parts_skipped=0,
                detail=str(exc),
            )

        manifest = package.manifest
        self.queue.record_size(
            entry.session_id,
            size_bytes=manifest.archive_size,
            archive_sha256=manifest.archive_sha256,
        )

        progress = _Progress()
        try:
            self._transfer(package, progress)
        except UploadConflict as exc:
            self.queue.mark_conflict(entry.session_id, error=str(exc))
            return UploadOutcome(
                session_id=entry.session_id,
                result="conflict",
                parts_sent=progress.sent,
                parts_skipped=progress.skipped,
                detail=str(exc),
            )
        except UploadUnavailable as exc:
            self.queue.defer(entry.session_id, error=str(exc), now=moment)
            return UploadOutcome(
                session_id=entry.session_id,
                result="deferred",
                parts_sent=progress.sent,
                parts_skipped=progress.skipped,
                detail=str(exc),
            )

        self.queue.mark_confirmed(
            entry.session_id,
            archive_sha256=manifest.archive_sha256,
            size_bytes=manifest.archive_size,
            now=moment,
        )
        # 成功即证明链路通了：把其余条目的退避清零，不必让它们各自等满。
        self.queue.reset_backoff(now=moment)
        return UploadOutcome(
            session_id=entry.session_id,
            result="confirmed",
            parts_sent=progress.sent,
            parts_skipped=progress.skipped,
        )

    def _transfer(self, package: SessionPackage, progress: _Progress) -> None:
        """一次完整的上传尝试。进度写进 `progress`，**失败时也留在里面**。"""
        manifest = package.manifest
        snapshot = manifest.snapshot()
        self.client.begin_session(manifest.session_id, snapshot, manifest.idempotency_key)

        accepted = dict(self.client.accepted_parts(manifest.session_id))
        expected = {part.index: part for part in manifest.parts}

        stray = set(accepted) - set(expected)
        if stray:
            raise UploadConflict(
                f"服务端有本地清单之外的件 {sorted(stray)} —— "
                "它手上的不是这一份数据，继续传会把两份缝在一起"
            )

        for index, part in sorted(expected.items()):
            already = accepted.get(index)
            if already is not None:
                if already != part.sha256:
                    raise UploadConflict(
                        f"第 {index} 件的摘要与服务端不符："
                        f"本地 {part.sha256[:16]}，服务端 {already[:16]}"
                    )
                progress.skipped += 1
                continue
            payload = package.part(index)
            acknowledged = self.client.put_part(
                manifest.session_id, index, part.sha256, payload
            )
            if acknowledged != part.sha256:
                raise UploadConflict(
                    f"第 {index} 件的回执摘要与本地不符：服务端算出 {acknowledged[:16]}，"
                    f"本地 {part.sha256[:16]}"
                )
            progress.sent += 1

        status = self.client.complete_session(
            manifest.session_id, snapshot, f"complete:{manifest.idempotency_key}"
        )
        if status != INGESTED:
            # **不是 200 就算数** —— 服务端受理了请求不等于数据已经落库。
            raise UploadUnavailable(f"服务端未确认落库，状态为 {status!r}")

    def drain(self, *, limit: int = 100, now: float | None = None) -> list[UploadOutcome]:
        """连着推进，直到没有到期条目或达到 `limit`。

        `limit` 是硬上限而不是建议：没有它，一个持续失败又持续到期的条目能把这个
        调用变成不返回的循环，而调用方以为自己只是"处理一批"。
        """
        moment = time.time() if now is None else now
        outcomes: list[UploadOutcome] = []
        for _ in range(limit):
            outcome = self.upload_once(now=moment)
            if outcome.result == "idle":
                break
            outcomes.append(outcome)
        return outcomes


@dataclass
class InMemoryIngestionClient:
    """内存里的服务端，用于测试与本地演练。

    它刻意实现了真实服务端**必须**有的幂等语义：同一件重复 PUT 不产生第二份，
    `accepted_parts` 反映已收到的件。测试断点续传时靠的就是这个。
    """

    sessions: dict[str, dict[str, Any]] = field(default_factory=dict)
    parts: dict[str, dict[int, bytes]] = field(default_factory=dict)
    completed: dict[str, str] = field(default_factory=dict)
    #: 记下每一件被真正接收的次数 —— 重复上传的证据在这里。
    put_calls: dict[tuple[str, int], int] = field(default_factory=dict)
    idempotency_keys: list[str] = field(default_factory=list)

    def begin_session(
        self, session_id: str, manifest: Mapping[str, Any], idempotency_key: str
    ) -> None:
        self.idempotency_keys.append(idempotency_key)
        self.sessions.setdefault(session_id, dict(manifest))
        self.parts.setdefault(session_id, {})

    def accepted_parts(self, session_id: str) -> Mapping[int, str]:
        return {
            index: hashlib.sha256(payload).hexdigest()
            for index, payload in self.parts.get(session_id, {}).items()
        }

    def put_part(self, session_id: str, index: int, sha256: str, payload: bytes) -> str:
        digest = hashlib.sha256(payload).hexdigest()
        if digest != sha256:
            raise UploadConflict(f"第 {index} 件的声明摘要与内容不符")
        self.put_calls[(session_id, index)] = self.put_calls.get((session_id, index), 0) + 1
        self.parts.setdefault(session_id, {})[index] = payload
        return digest

    def complete_session(
        self, session_id: str, manifest: Mapping[str, Any], idempotency_key: str
    ) -> str:
        self.idempotency_keys.append(idempotency_key)
        expected = {part["index"] for part in manifest["parts"]}
        if set(self.parts.get(session_id, {})) != expected:
            raise UploadUnavailable("服务端手上的件与清单不符，尚不能收尾")
        self.completed[session_id] = INGESTED
        return INGESTED

    def assembled(self, session_id: str) -> bytes:
        """把收到的件拼起来 —— 用于验证服务端最终拿到的与本地一致。"""
        stored = self.parts.get(session_id, {})
        return b"".join(stored[index] for index in sorted(stored))


def enqueue_session(
    queue: UploadQueue, root: Path | str, session_id: str, *, now: float | None = None
) -> QueueEntry:
    """按 `io/session.py` 的布局把一个会话排进队列。"""
    from gait.io.session import session_directory

    return queue.enqueue(session_id, session_directory(Path(root), session_id), now=now)


def manifest_of(entry: QueueEntry, **kwargs: Any) -> PackageManifest:
    """条目对应的包清单。调试与巡检用。"""
    return build_package(entry.directory, session_id=entry.session_id, **kwargs).manifest
