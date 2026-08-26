"""上传确认触发的云端重算任务。PRD §6.1、§15（云端任务幂等）、AC-14。

## 触发点是"确认"，不是"收到"

`cloud/upload.py` 已经把这条线划清楚了：HTTP 200 不算数，只有 `complete_session`
返回 `ingested` 才把队列条目置为 `confirmed`。重算从 `confirmed` 开始，而不是从
"收到了最后一个分片"开始 —— 后者意味着可能对一份尚未完整落盘的会话动手。

## 幂等键包含算法版本

    recompute:{session_id}:{archive_sha256[:16]}:{algo_version}

前两段与 `PackageManifest.idempotency_key` 同源（同一份内容 → 同一个键），第三段是
算法版本。三段合起来的含义是"**这份数据在这个算法下的结果**"，而那正是重算这件事
要生产的东西：

* 同一份数据重试任意多次 → 同一个键 → 第二次直接返回已有结果，不重算；
* 算法升级后要重跑历史 → 键变了 → 新任务，**旧结果不被覆盖**（PRD G-08 要求可回溯
  重算，覆盖等于把历史抹掉）。

## 失败分类沿用上传那一套

`RecomputeUnavailable`（可重试：资源不足、依赖暂时不可用）与 `RecomputeRejected`
（不可重试：数据本身有问题）。分错的代价是具体的：把不可重试的当成可重试，队列会用
指数退避把同一份坏数据重算到天荒地老。

## 云端失败不影响本地基础报告

PRD FR-10 / AC-14。本模块不持有任何本地报告的引用，也不写本地会话目录 —— 结构上
就做不到影响它。重算失败时任务进 `failed`，日志上传，基础报告照常。
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from collections.abc import Mapping
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Protocol

from gait.cloud.chain import FULL_CHAIN_ALGO_VERSION, ChainResult, run_full_chain
from gait.config import AlgoConfig
from gait.contracts import FootLabel, FootSeries

#: 任务状态。
STATE_PENDING: Final[str] = "pending"
STATE_RUNNING: Final[str] = "running"
STATE_DONE: Final[str] = "done"
STATE_FAILED: Final[str] = "failed"
STATES: Final[tuple[str, ...]] = (STATE_PENDING, STATE_RUNNING, STATE_DONE, STATE_FAILED)

#: 单个任务的租约时长，秒。进程崩溃后任务在此之后可被重新领取。
DEFAULT_LEASE_SECONDS: Final[float] = 900.0

#: 退避：与 `upload.UploadPolicy` 同形，但基数更小 —— 重算失败多半是资源问题，
#: 而资源问题的恢复时间尺度比网络问题短。
DEFAULT_BACKOFF_BASE_S: Final[float] = 10.0
DEFAULT_BACKOFF_CAP_S: Final[float] = 600.0

#: 单个任务的最大尝试次数。超过之后停在 `failed` 等人看 —— 无限重试会让一个必然
#: 失败的任务永远占着队列，而"它一直在重试"读起来很像"它还在处理中"。
DEFAULT_MAX_ATTEMPTS: Final[int] = 5


class RecomputeError(RuntimeError):
    """重算失败的基类。"""


class RecomputeUnavailable(RecomputeError):
    """**可重试**：资源不足、依赖暂时不可用。"""


class RecomputeRejected(RecomputeError):
    """**不可重试**：数据本身有问题，重算多少次都是同一个结果。"""


def idempotency_key(session_id: str, archive_sha256: str, algo_version: str) -> str:
    """任务幂等键。三段的含义见模块文档。"""
    if not session_id or not archive_sha256 or not algo_version:
        raise RecomputeRejected(
            "幂等键的三段都不能为空："
            f"session_id={session_id!r} archive_sha256={archive_sha256!r} "
            f"algo_version={algo_version!r}"
        )
    return f"recompute:{session_id}:{archive_sha256[:16]}:{algo_version}"


class SeriesSource(Protocol):
    """把一份已确认上传的会话解成逐足的 `FootSeries`。

    这是本模块唯一的外部依赖，做成协议而不是直接调用解码函数，理由有两条：

    1. 原始帧的线格式属 RAY-198，本 scope 不定义它 —— 在它定下来之前写死一个解析
       等于替另一个 scope 做决定；
    2. 合成数据的对照实验与幂等测试都要在**不经过任何真实帧**的情况下驱动整条管线。

    实现方负责校验与切分（`sync/integrity.assess` 产出的 `segments` 可直接用）。
    """

    def load(self, session_id: str, directory: Path) -> dict[FootLabel, FootSeries]:
        """解出逐足序列。数据本身不可解时抛 `RecomputeRejected`。"""
        ...


@dataclass(frozen=True)
class RecomputeTask:
    """一条重算任务。"""

    key: str
    session_id: str
    directory: Path
    archive_sha256: str
    algo_version: str
    state: str
    attempts: int
    enqueued_at: float
    next_attempt_at: float
    leased_until: float
    last_error: str
    completed_at: float
    result_path: str

    @property
    def pending(self) -> bool:
        return self.state == STATE_PENDING

    @property
    def done(self) -> bool:
        return self.state == STATE_DONE


@dataclass(frozen=True)
class RecomputeOutcome:
    """一次 `run_once` 的结果。"""

    #: `done` / `deferred` / `failed` / `idle` / `already_done`
    result: str
    key: str = ""
    session_id: str = ""
    duration_s: float = 0.0
    detail: str = ""
    chain_result: ChainResult | None = None

    @property
    def succeeded(self) -> bool:
        return self.result in (STATE_DONE, "already_done")


_SCHEMA: Final[str] = """
CREATE TABLE IF NOT EXISTS tasks (
    key TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    directory TEXT NOT NULL,
    archive_sha256 TEXT NOT NULL,
    algo_version TEXT NOT NULL,
    state TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    enqueued_at REAL NOT NULL,
    next_attempt_at REAL NOT NULL DEFAULT 0,
    leased_until REAL NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT '',
    completed_at REAL NOT NULL DEFAULT 0,
    result_path TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS tasks_ready ON tasks (state, next_attempt_at);
"""


class RecomputeQueue:
    """重算任务队列。SQLite + WAL，与 `upload.UploadQueue` 同形。

    同形不是偷懒：两个队列的失败模式一样（进程可能在任何一步崩），一样的租约与退避
    语义意味着运维只需要理解一套。差别只有幂等键的构成与"完成"的含义。
    """

    def __init__(self, path: Path | str, *, lease_seconds: float = DEFAULT_LEASE_SECONDS,
                 max_attempts: int = DEFAULT_MAX_ATTEMPTS) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lease_seconds = lease_seconds
        self.max_attempts = max_attempts
        with closing(self._connect()) as connection:
            connection.executescript(_SCHEMA)
            connection.commit()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, isolation_level=None)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.row_factory = sqlite3.Row
        return connection

    def enqueue(
        self,
        session_id: str,
        directory: Path | str,
        *,
        archive_sha256: str,
        algo_version: str = FULL_CHAIN_ALGO_VERSION,
        now: float | None = None,
    ) -> RecomputeTask:
        """登记一条任务。**同一个键重复登记不会重置任何进度**。

        `INSERT OR IGNORE` 而不是 upsert：重复 enqueue 是幂等重试的正常路径（上传
        确认可能被重放），把它变成"重新开始"会让一个已经跑到一半、甚至已经完成的
        任务凭空退回起点。
        """
        moment = time.time() if now is None else now
        key = idempotency_key(session_id, archive_sha256, algo_version)
        with closing(self._connect()) as connection:
            connection.execute(
                "INSERT OR IGNORE INTO tasks "
                "(key, session_id, directory, archive_sha256, algo_version, state, "
                " enqueued_at, next_attempt_at) VALUES (?,?,?,?,?,?,?,?)",
                (key, session_id, str(directory), archive_sha256, algo_version,
                 STATE_PENDING, moment, moment),
            )
            connection.commit()
        task = self.get(key)
        assert task is not None
        return task

    def get(self, key: str) -> RecomputeTask | None:
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT * FROM tasks WHERE key = ?", (key,)).fetchone()
        return _task_from_row(row) if row else None

    def tasks(self, state: str | None = None) -> list[RecomputeTask]:
        query = "SELECT * FROM tasks"
        parameters: tuple[Any, ...] = ()
        if state is not None:
            query += " WHERE state = ?"
            parameters = (state,)
        query += " ORDER BY enqueued_at"
        with closing(self._connect()) as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [_task_from_row(row) for row in rows]

    def lease(self, *, now: float | None = None) -> RecomputeTask | None:
        """领一条待办任务，标记为 `running` 并加租约。

        select 与 mark 在同一个 `BEGIN IMMEDIATE` 里完成：两个 worker 同时领到同一
        条任务会让同一份数据被算两遍，而两遍的结果写到同一个路径上时，谁最后写完是
        不确定的。租约到期（进程崩了）后任务重新可领。
        """
        moment = time.time() if now is None else now
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT * FROM tasks WHERE "
                    "(state = ? AND next_attempt_at <= ?) OR (state = ? AND leased_until <= ?) "
                    "ORDER BY next_attempt_at LIMIT 1",
                    (STATE_PENDING, moment, STATE_RUNNING, moment),
                ).fetchone()
                if row is None:
                    connection.execute("ROLLBACK")
                    return None
                connection.execute(
                    "UPDATE tasks SET state = ?, leased_until = ? WHERE key = ?",
                    (STATE_RUNNING, moment + self.lease_seconds, row["key"]),
                )
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise
            row = connection.execute(
                "SELECT * FROM tasks WHERE key = ?", (row["key"],)
            ).fetchone()
        return _task_from_row(row)

    def mark_done(self, key: str, *, result_path: str, now: float | None = None) -> None:
        moment = time.time() if now is None else now
        with closing(self._connect()) as connection:
            connection.execute(
                "UPDATE tasks SET state = ?, completed_at = ?, leased_until = 0, "
                "last_error = '', result_path = ? WHERE key = ?",
                (STATE_DONE, moment, result_path, key),
            )
            connection.commit()

    def defer(self, key: str, *, error: str, now: float | None = None) -> RecomputeTask:
        """可重试的失败：加尝试次数、退避、放掉租约。超过上限则转 `failed`。"""
        moment = time.time() if now is None else now
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT attempts FROM tasks WHERE key = ?", (key,)).fetchone()
            attempts = (row["attempts"] if row else 0) + 1
            if attempts >= self.max_attempts:
                connection.execute(
                    "UPDATE tasks SET state = ?, attempts = ?, leased_until = 0, "
                    "last_error = ? WHERE key = ?",
                    (STATE_FAILED, attempts, f"attempts_exhausted: {error}", key),
                )
            else:
                delay = min(DEFAULT_BACKOFF_BASE_S * (2 ** (attempts - 1)), DEFAULT_BACKOFF_CAP_S)
                connection.execute(
                    "UPDATE tasks SET state = ?, attempts = ?, next_attempt_at = ?, "
                    "leased_until = 0, last_error = ? WHERE key = ?",
                    (STATE_PENDING, attempts, moment + delay, error, key),
                )
            connection.commit()
        task = self.get(key)
        assert task is not None
        return task

    def mark_failed(self, key: str, *, error: str) -> None:
        """不可重试的失败。"""
        with closing(self._connect()) as connection:
            connection.execute(
                "UPDATE tasks SET state = ?, leased_until = 0, last_error = ? WHERE key = ?",
                (STATE_FAILED, error, key),
            )
            connection.commit()


def _task_from_row(row: sqlite3.Row) -> RecomputeTask:
    return RecomputeTask(
        key=row["key"],
        session_id=row["session_id"],
        directory=Path(row["directory"]),
        archive_sha256=row["archive_sha256"],
        algo_version=row["algo_version"],
        state=row["state"],
        attempts=row["attempts"],
        enqueued_at=row["enqueued_at"],
        next_attempt_at=row["next_attempt_at"],
        leased_until=row["leased_until"],
        last_error=row["last_error"],
        completed_at=row["completed_at"],
        result_path=row["result_path"],
    )


def enqueue_confirmed(
    queue: RecomputeQueue,
    confirmed: Mapping[str, tuple[Path, str]],
    *,
    algo_version: str = FULL_CHAIN_ALGO_VERSION,
    now: float | None = None,
) -> list[RecomputeTask]:
    """把已确认上传的会话登记为重算任务。

    `confirmed` 是 `{session_id: (directory, archive_sha256)}` —— 正是
    `upload.UploadQueue.entries(STATE_CONFIRMED)` 里每条的三个字段。传映射而不是
    直接吃 `UploadQueue`，是为了不把两个队列的生命周期绑在一起：云端的重算 worker
    与采集端的上传队列本来就跑在不同的机器上。
    """
    return [
        queue.enqueue(session_id, directory, archive_sha256=digest,
                      algo_version=algo_version, now=now)
        for session_id, (directory, digest) in sorted(confirmed.items())
    ]


class RecomputeRunner:
    """一次跑一条任务的执行器。

    与 `upload.SessionUploader` 一样不自带线程与调度：`run_once` 推进一条，
    调度归调用方。云端的 worker 编排（几个进程、怎么分片）是部署决定，不是算法决定。
    """

    def __init__(
        self,
        queue: RecomputeQueue,
        source: SeriesSource,
        *,
        output_root: Path | str,
        cfg: AlgoConfig | None = None,
        algo_version: str = FULL_CHAIN_ALGO_VERSION,
    ) -> None:
        self.queue = queue
        self.source = source
        self.output_root = Path(output_root)
        self.cfg = cfg or AlgoConfig()
        self.algo_version = algo_version

    def result_path(self, task: RecomputeTask) -> Path:
        """产出路径按 `会话 / 算法版本` 分层 —— 同一会话的两个算法版本各有各的结果，
        互不覆盖（PRD G-08 可回溯重算）。"""
        return self.output_root / task.session_id / f"{task.algo_version}.json"

    def run_once(
        self,
        *,
        now: float | None = None,
        sync_quality: dict[str, Any] | None = None,
        protocol_seconds: int | None = None,
    ) -> RecomputeOutcome:
        task = self.queue.lease(now=now)
        if task is None:
            return RecomputeOutcome(result="idle")

        destination = self.result_path(task)
        if destination.exists():
            # 崩溃恢复路径：上一次尝试写完了结果文件，但在 `mark_done` 之前进程没了，
            # 租约到期后任务被重新领出来。产出是原子写的，存在即完整，重算一遍只会
            # 得到同一个结果并多烧一次 CPU。
            #
            # 已经 `done` 的任务走不到这里 —— `lease` 只领 `pending` 与租约过期的
            # `running`。重复 enqueue 由 `INSERT OR IGNORE` 挡在更前面。
            self.queue.mark_done(task.key, result_path=str(destination), now=now)
            return RecomputeOutcome(result="already_done", key=task.key,
                                    session_id=task.session_id, detail=str(destination))

        started = time.perf_counter()
        try:
            series = self.source.load(task.session_id, task.directory)
            if not series:
                raise RecomputeRejected(f"会话 {task.session_id} 没有任何一只脚的数据")
            result = run_full_chain(
                series,
                self.cfg,
                sync_quality=sync_quality,
                protocol_seconds=protocol_seconds,
                algo_version=task.algo_version,
            )
        except RecomputeRejected as error:
            self.queue.mark_failed(task.key, error=str(error))
            return RecomputeOutcome(result=STATE_FAILED, key=task.key,
                                    session_id=task.session_id, detail=str(error))
        except (RecomputeUnavailable, MemoryError, OSError) as error:
            self.queue.defer(task.key, error=str(error), now=now)
            return RecomputeOutcome(result="deferred", key=task.key,
                                    session_id=task.session_id, detail=str(error))
        except Exception as error:  # noqa: BLE001 — 见下方注释
            # 未预期的异常按**不可重试**处理。反过来（当成可重试）会让一个确定性的
            # 程序 bug 被退避重试掩盖成"暂时的故障"，而它每次都会以同样的方式失败。
            self.queue.mark_failed(task.key, error=f"{type(error).__name__}: {error}")
            return RecomputeOutcome(result=STATE_FAILED, key=task.key,
                                    session_id=task.session_id,
                                    detail=f"{type(error).__name__}: {error}")

        duration = time.perf_counter() - started
        _write_atomic(destination, _payload(task, result, duration))
        self.queue.mark_done(task.key, result_path=str(destination), now=now)
        return RecomputeOutcome(result=STATE_DONE, key=task.key, session_id=task.session_id,
                                duration_s=duration, detail=str(destination),
                                chain_result=result)

    def drain(self, *, limit: int = 100, **kwargs: Any) -> list[RecomputeOutcome]:
        """推进至多 `limit` 条，遇到 `idle` 停下。上限是防跑飞，不是调优参数。"""
        outcomes: list[RecomputeOutcome] = []
        for _ in range(limit):
            outcome = self.run_once(**kwargs)
            if outcome.result == "idle":
                break
            outcomes.append(outcome)
        return outcomes


def _payload(task: RecomputeTask, result: ChainResult, duration: float) -> dict[str, Any]:
    """落盘的产出。`algo_version` 与 `chain` 在顶层 —— 它们要进报告页脚，
    埋在嵌套结构里会让读的人去猜哪个才是权威的那个。"""
    return {
        "session_id": task.session_id,
        "algo_version": result.algo_version,
        "chain": result.chain,
        "idempotency_key": task.key,
        "archive_sha256": task.archive_sha256,
        "duration_s": duration,
        "result": result.snapshot(),
    }


def _write_atomic(path: Path, payload: dict[str, Any]) -> None:
    """先写临时文件再 `os.replace`，与 `io/session.write_meta` 同一个理由：
    半份结果文件比没有更糟，它看起来存在、解析却失败。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)
