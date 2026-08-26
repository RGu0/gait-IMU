"""云端重算任务的测试：幂等、重试、算法版本化。

PRD §15「上传与云端任务幂等」与 AC-14「云端算法失败 → 基础报告可用，自动重试」。
这里测的全部是**任务生命周期**，算法本身由 `test_rts.py` / `test_anchor.py` /
`test_cloud_chain.py` 负责。
"""

from pathlib import Path

import pytest

from gait.cloud.chain import FULL_CHAIN_ALGO_VERSION
from gait.cloud.recompute import (
    STATE_DONE,
    STATE_FAILED,
    STATE_PENDING,
    RecomputeQueue,
    RecomputeRejected,
    RecomputeRunner,
    RecomputeUnavailable,
    enqueue_confirmed,
    idempotency_key,
)
from gait.validate.synthetic import NoiseModel, WalkSpec, generate_dual_walk

SYNC = {"determinate": True, "flagged": False}
DIGEST = "a" * 64


def dual_series(duration=6.0):
    pair = generate_dual_walk(
        WalkSpec(duration_s=duration),
        noise=NoiseModel(accel_density=1.5e-3, gyro_density=3.0e-4, seed=3),
    )
    return {label: pair[label][0] for label in pair}


class CountingSource:
    """记录被调用了几次 —— 幂等性的证据就是这个计数不涨。"""

    def __init__(self, series=None, error=None):
        self.series = series if series is not None else dual_series()
        self.error = error
        self.calls = 0

    def load(self, session_id: str, directory: Path):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.series


def make_runner(tmp_path, source=None, **kwargs):
    queue = RecomputeQueue(tmp_path / "queue.db", **kwargs)
    runner = RecomputeRunner(queue, source or CountingSource(), output_root=tmp_path / "out")
    return queue, runner


class TestIdempotency:
    def test_the_key_is_content_and_version_derived(self):
        first = idempotency_key("s-1", DIGEST, FULL_CHAIN_ALGO_VERSION)
        assert first == idempotency_key("s-1", DIGEST, FULL_CHAIN_ALGO_VERSION)
        assert first != idempotency_key("s-1", "b" * 64, FULL_CHAIN_ALGO_VERSION)
        assert first != idempotency_key("s-1", DIGEST, "full-9.9.9")
        assert first != idempotency_key("s-2", DIGEST, FULL_CHAIN_ALGO_VERSION)

    def test_an_empty_component_is_refused(self):
        with pytest.raises(RecomputeRejected):
            idempotency_key("", DIGEST, FULL_CHAIN_ALGO_VERSION)
        with pytest.raises(RecomputeRejected):
            idempotency_key("s-1", DIGEST, "")

    def test_re_enqueue_does_not_recompute(self, tmp_path):
        """上传确认可能被重放。重放不该让一个已完成的任务重新跑一遍。"""
        source = CountingSource()
        queue, runner = make_runner(tmp_path, source)
        queue.enqueue("s-1", tmp_path, archive_sha256=DIGEST)
        assert runner.run_once(sync_quality=SYNC).result == STATE_DONE
        assert source.calls == 1

        queue.enqueue("s-1", tmp_path, archive_sha256=DIGEST)
        assert runner.run_once(sync_quality=SYNC).result == "idle"
        assert source.calls == 1
        assert [task.state for task in queue.tasks()] == [STATE_DONE]

    def test_re_enqueue_does_not_reset_progress(self, tmp_path):
        """`INSERT OR IGNORE` 而不是 upsert：重复登记不得把 attempts 清零。"""
        queue, _ = make_runner(tmp_path)
        queue.enqueue("s-1", tmp_path, archive_sha256=DIGEST)
        key = idempotency_key("s-1", DIGEST, FULL_CHAIN_ALGO_VERSION)
        queue.defer(key, error="boom")
        assert queue.get(key).attempts == 1

        queue.enqueue("s-1", tmp_path, archive_sha256=DIGEST)
        assert queue.get(key).attempts == 1

    def test_a_finished_result_file_short_circuits_a_re_leased_task(self, tmp_path):
        """崩溃恢复：结果写完了但 `mark_done` 没跑到，租约到期后重新领出来。

        产出是原子写的，存在即完整 —— 重算只会得到同一个结果并多烧一次 CPU。
        """
        source = CountingSource()
        queue = RecomputeQueue(tmp_path / "queue.db", lease_seconds=0.0)
        runner = RecomputeRunner(queue, source, output_root=tmp_path / "out")
        task = queue.enqueue("s-1", tmp_path, archive_sha256=DIGEST)
        assert runner.run_once(sync_quality=SYNC).result == STATE_DONE
        assert source.calls == 1

        # 手工把它退回 running，模拟"写完了结果但没标记完成"。
        queue.defer(task.key, error="crashed", now=0.0)
        outcome = runner.run_once(sync_quality=SYNC, now=10_000.0)
        assert outcome.result == "already_done"
        assert source.calls == 1


class TestAlgorithmVersioning:
    def test_a_new_algo_version_creates_a_separate_task(self, tmp_path):
        """PRD G-08：算法版本可回溯重算。新版本是新任务，**不覆盖旧结果**。"""
        source = CountingSource()
        queue, runner = make_runner(tmp_path, source)
        queue.enqueue("s-1", tmp_path, archive_sha256=DIGEST)
        assert runner.run_once(sync_quality=SYNC).result == STATE_DONE
        old = runner.result_path(queue.tasks()[0])
        assert old.exists()

        queue.enqueue("s-1", tmp_path, archive_sha256=DIGEST, algo_version="full-2.0.0")
        outcome = runner.run_once(sync_quality=SYNC)
        assert outcome.result == STATE_DONE
        assert source.calls == 2
        assert len(queue.tasks()) == 2
        assert old.exists(), "旧算法版本的结果被新版本覆盖了"

    def test_the_payload_carries_the_version_at_top_level(self, tmp_path):
        import json

        queue, runner = make_runner(tmp_path)
        queue.enqueue("s-1", tmp_path, archive_sha256=DIGEST)
        outcome = runner.run_once(sync_quality=SYNC)
        payload = json.loads(Path(outcome.detail).read_text(encoding="utf-8"))
        assert payload["algo_version"] == FULL_CHAIN_ALGO_VERSION
        assert payload["chain"] == "full"
        assert payload["idempotency_key"] == outcome.key
        assert payload["archive_sha256"] == DIGEST


class TestFailureHandling:
    def test_a_rejected_task_is_not_retried(self, tmp_path):
        """不可重试的失败直接进 `failed` —— 退避重算一份坏数据只是在烧钱。"""
        source = CountingSource(error=RecomputeRejected("坏数据"))
        queue, runner = make_runner(tmp_path, source)
        queue.enqueue("s-1", tmp_path, archive_sha256=DIGEST)
        assert runner.run_once(sync_quality=SYNC).result == STATE_FAILED
        assert queue.tasks()[0].state == STATE_FAILED
        assert runner.run_once(sync_quality=SYNC).result == "idle"
        assert source.calls == 1

    def test_an_unavailable_task_is_deferred_and_retried(self, tmp_path):
        source = CountingSource(error=RecomputeUnavailable("内存不够"))
        queue, runner = make_runner(tmp_path, source)
        queue.enqueue("s-1", tmp_path, archive_sha256=DIGEST, now=0.0)
        assert runner.run_once(sync_quality=SYNC, now=0.0).result == "deferred"

        task = queue.tasks()[0]
        assert task.state == STATE_PENDING
        assert task.attempts == 1
        assert task.next_attempt_at > 0.0
        # 退避未到期时领不到。
        assert runner.run_once(sync_quality=SYNC, now=1.0).result == "idle"
        assert runner.run_once(sync_quality=SYNC, now=10_000.0).result == "deferred"
        assert source.calls == 2

    def test_attempts_are_bounded(self, tmp_path):
        """无限重试会让一个必然失败的任务永远占着队列。"""
        source = CountingSource(error=RecomputeUnavailable("总是失败"))
        queue = RecomputeQueue(tmp_path / "queue.db", max_attempts=3)
        runner = RecomputeRunner(queue, source, output_root=tmp_path / "out")
        queue.enqueue("s-1", tmp_path, archive_sha256=DIGEST, now=0.0)
        for attempt in range(3):
            runner.run_once(sync_quality=SYNC, now=attempt * 10_000.0)
        task = queue.tasks()[0]
        assert task.state == STATE_FAILED
        assert "attempts_exhausted" in task.last_error

    def test_an_unexpected_exception_is_treated_as_non_retryable(self, tmp_path):
        """确定性的程序 bug 每次都会以同样的方式失败，退避只会把它掩盖成"暂时故障"。"""
        source = CountingSource(error=ZeroDivisionError("算法里有个 bug"))
        queue, runner = make_runner(tmp_path, source)
        queue.enqueue("s-1", tmp_path, archive_sha256=DIGEST)
        outcome = runner.run_once(sync_quality=SYNC)
        assert outcome.result == STATE_FAILED
        assert "ZeroDivisionError" in outcome.detail
        assert queue.tasks()[0].state == STATE_FAILED

    def test_an_empty_session_is_rejected(self, tmp_path):
        class Empty:
            def load(self, session_id, directory):
                return {}

        queue, runner = make_runner(tmp_path, Empty())
        queue.enqueue("s-1", tmp_path, archive_sha256=DIGEST)
        assert runner.run_once(sync_quality=SYNC).result == STATE_FAILED


class TestLeasing:
    def test_a_leased_task_is_not_handed_out_twice(self, tmp_path):
        """两个 worker 同时领到同一条任务会让同一份数据被算两遍。"""
        queue, _ = make_runner(tmp_path)
        queue.enqueue("s-1", tmp_path, archive_sha256=DIGEST, now=0.0)
        assert queue.lease(now=0.0) is not None
        assert queue.lease(now=1.0) is None

    def test_an_expired_lease_is_reclaimed(self, tmp_path):
        """进程崩了之后任务必须能被别人捡起来，否则它永远卡在 running。"""
        queue = RecomputeQueue(tmp_path / "queue.db", lease_seconds=60.0)
        queue.enqueue("s-1", tmp_path, archive_sha256=DIGEST, now=0.0)
        assert queue.lease(now=0.0) is not None
        assert queue.lease(now=30.0) is None
        assert queue.lease(now=61.0) is not None


class TestTheUploadHandoff:
    def test_confirmed_uploads_become_tasks(self, tmp_path):
        """触发点是上传**确认**，不是"收到最后一个分片"。"""
        queue, _ = make_runner(tmp_path)
        tasks = enqueue_confirmed(
            queue,
            {"s-2": (tmp_path / "s-2", "b" * 64), "s-1": (tmp_path / "s-1", DIGEST)},
        )
        assert [task.session_id for task in tasks] == ["s-1", "s-2"]
        assert all(task.algo_version == FULL_CHAIN_ALGO_VERSION for task in tasks)

    def test_replaying_the_same_confirmations_is_a_no_op(self, tmp_path):
        queue, _ = make_runner(tmp_path)
        confirmed = {"s-1": (tmp_path / "s-1", DIGEST)}
        enqueue_confirmed(queue, confirmed)
        enqueue_confirmed(queue, confirmed)
        assert len(queue.tasks()) == 1
