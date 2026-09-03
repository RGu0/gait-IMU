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


#: 12 s 而不是 6 s。本文件测的是重算队列的语义（幂等、版本、状态机），不是步态质量 ——
#: 但它要跑完整条链，而链末的分段筛选会剔掉每段首尾各一步。6 s 在旧的边缘细化路径下
#: 恰好剩 4 个周期，**余量为零**；RAY-351 把产品链路切到支撑相区间之后每个数据段少一个
#: 周期（网格只铺在首末摆动峰之间），4 就变成 3，`trim=1` 一剔就报"把所有步都剔掉了"。
#:
#: 加长而不是放宽 `trim`：被测的东西与周期数无关，而一个刚好卡在边界上的夹具会在任何
#: 与周期数有关的改动上失败，且失败信息指向的是链而不是本文件真正在测的东西。
def dual_series(duration=12.0):
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
        queue.enqueue("s-1", tmp_path, archive_sha256=DIGEST, now=0.0)
        key = idempotency_key("s-1", DIGEST, FULL_CHAIN_ALGO_VERSION)
        queue.lease(now=0.0)  # `defer` 只对已领取（running）的任务生效
        queue.defer(key, error="boom", now=0.0)
        assert queue.get(key).attempts == 1

        queue.enqueue("s-1", tmp_path, archive_sha256=DIGEST, now=0.0)
        assert queue.get(key).attempts == 1

    def test_a_finished_result_file_short_circuits_a_re_leased_task(self, tmp_path):
        """崩溃恢复：结果写完了但 `mark_done` 没跑到，租约到期后重新领出来。

        产出是原子写的，存在即完整 —— 重算只会得到同一个结果并多烧一次 CPU。
        模拟方式是**领了不完成再让租约过期**，与真实的崩溃同形。
        """
        source = CountingSource()
        queue = RecomputeQueue(tmp_path / "queue.db", lease_seconds=60.0)
        runner = RecomputeRunner(queue, source, output_root=tmp_path / "out")
        task = queue.enqueue("s-1", tmp_path, archive_sha256=DIGEST, now=0.0)

        # 手工把结果写到位，但**不** mark_done —— 进程在这两步之间没了。
        destination = runner.result_path(task)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text("{}", encoding="utf-8")
        queue.lease(now=0.0)

        outcome = runner.run_once(sync_quality=SYNC, now=10_000.0)
        assert outcome.result == "already_done"
        assert source.calls == 0, "结果已在，不该再算一遍"
        assert queue.get(task.key).state == STATE_DONE

    def test_a_repackaged_session_is_actually_recomputed(self, tmp_path):
        """同一会话重新打包（内容变了）必须真的重算。

        产出路径若只按 `会话 / 算法版本` 分层，新任务会被上一份内容的结果文件短路掉，
        于是新数据一次也没被算过 —— 而产出里的 `archive_sha256` 还是旧的那个。
        路径的构成必须与幂等键一致。
        """
        import json

        source = CountingSource()
        queue, runner = make_runner(tmp_path, source)
        queue.enqueue("s-1", tmp_path, archive_sha256=DIGEST)
        first = runner.run_once(sync_quality=SYNC)
        assert first.result == STATE_DONE

        repackaged = "b" * 64
        queue.enqueue("s-1", tmp_path, archive_sha256=repackaged)
        second = runner.run_once(sync_quality=SYNC)

        assert second.result == STATE_DONE, "重打包的会话被静默跳过了"
        assert source.calls == 2, "新内容一次也没被读过"
        assert first.detail != second.detail, "两份内容的结果写到了同一个路径"
        assert Path(first.detail).exists(), "旧内容的结果被覆盖了"
        payload = json.loads(Path(second.detail).read_text(encoding="utf-8"))
        assert payload["archive_sha256"] == repackaged


class TestAlgorithmVersioning:
    def test_two_algo_versions_keep_separate_results(self, tmp_path):
        """PRD G-08：算法版本可回溯重算。两个版本各有各的结果，**互不覆盖**。

        两个 runner 代表两次部署 —— 一个执行器只产出它自己实现的那个版本。
        """
        source = CountingSource()
        queue = RecomputeQueue(tmp_path / "queue.db")
        old_runner = RecomputeRunner(queue, source, output_root=tmp_path / "out")
        new_runner = RecomputeRunner(
            queue, source, output_root=tmp_path / "out", algo_version="full-2.0.0"
        )

        queue.enqueue("s-1", tmp_path, archive_sha256=DIGEST)
        old_result = old_runner.run_once(sync_quality=SYNC)
        assert old_result.result == STATE_DONE

        queue.enqueue("s-1", tmp_path, archive_sha256=DIGEST, algo_version="full-2.0.0")
        new_result = new_runner.run_once(sync_quality=SYNC)
        assert new_result.result == STATE_DONE

        assert source.calls == 2
        assert len(queue.tasks()) == 2
        assert Path(old_result.detail).exists(), "旧算法版本的结果被新版本覆盖了"
        assert old_result.detail != new_result.detail

    def test_a_runner_refuses_a_version_it_does_not_implement(self, tmp_path):
        """给一份用当前代码算出的数字贴上别的版本号，是在伪造 G-08 的可追溯链。

        事后没有任何办法分辨「标着 2.0.0 的结果」到底是不是 2.0.0 算的，所以宁可拒绝。
        重算历史的正确做法是部署对应版本的代码。
        """
        source = CountingSource()
        queue, runner = make_runner(tmp_path, source)
        queue.enqueue("s-1", tmp_path, archive_sha256=DIGEST, algo_version="full-9.9.9")

        outcome = runner.run_once(sync_quality=SYNC)
        assert outcome.result == STATE_FAILED
        assert "full-9.9.9" in outcome.detail
        assert source.calls == 0, "版本对不上就不该动数据"
        # 不可重试：换个时间再跑版本还是对不上。
        assert runner.run_once(sync_quality=SYNC, now=10_000.0).result == "idle"

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
        outcomes = [
            runner.run_once(sync_quality=SYNC, now=attempt * 10_000.0) for attempt in range(3)
        ]
        task = queue.tasks()[0]
        assert task.state == STATE_FAILED
        assert "attempts_exhausted" in task.last_error
        # 最后一次要据实报 `failed`：报 `deferred` 会让调用方以为它还会重试，
        # 而"它还在重试"读起来很像"它还在处理中"。
        assert [item.result for item in outcomes] == ["deferred", "deferred", STATE_FAILED]

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

    def test_a_late_defer_cannot_revert_a_finished_task(self, tmp_path):
        """租约超时后 worker B 完成了任务，随后 worker A 才失败并 defer。

        没有状态谓词的话，一份算好的结果会被退回 `pending` 重新排队、重算、重写。
        只在租约超时且原 worker 仍活着时才可达 —— 很少发生，发生时很难查。
        """
        queue = RecomputeQueue(tmp_path / "queue.db", lease_seconds=60.0)
        task = queue.enqueue("s-1", tmp_path, archive_sha256=DIGEST, now=0.0)
        queue.lease(now=0.0)                        # worker A 领走
        queue.lease(now=100.0)                      # 租约过期，worker B 接手
        queue.mark_done(task.key, result_path="/tmp/r.json", now=100.0)

        queue.defer(task.key, error="A 现在才失败", now=101.0)   # worker A 姗姗来迟
        assert queue.get(task.key).state == STATE_DONE
        assert queue.lease(now=200.0) is None, "已完成的任务被重新排队了"

    def test_a_late_mark_done_cannot_resurrect_a_failed_task(self, tmp_path):
        """同一条谓词的另一半：完成标记也只对 running 生效。"""
        queue = RecomputeQueue(tmp_path / "queue.db", lease_seconds=60.0)
        task = queue.enqueue("s-1", tmp_path, archive_sha256=DIGEST, now=0.0)
        queue.mark_failed(task.key, error="坏数据")
        queue.mark_done(task.key, result_path="/tmp/r.json", now=1.0)
        assert queue.get(task.key).state == STATE_FAILED


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
