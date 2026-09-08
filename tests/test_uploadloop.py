"""`gait.app.uploadloop` —— 待传队列的排空线程。RAY-416。

这一组守的是三条**拍过板的产品决定**，外加一条本 Issue 存在的理由：

* **不阻塞 IPC** —— sidecar 主循环是单线程阻塞读，一件 4 MiB 在慢网上能耗到 300 s。
  验收原文写明这条「必须是测试而不是承诺」，所以这里让上传**真的卡住**，再去调
  `handle()` 看它多久回来；
* **采集中停传**（待确认 2）—— 断言采集期间**一个请求都不发**，而不是只断言某个标志位；
* **退出不等**（待确认 3）—— `stop()` 在上传卡着时也要立刻返回；
* **死掉的线程要看得见** —— 它不报错、不让任何测试变红，只是队列从此不动，
  而那正是本 Issue 要修的毛病换了个更隐蔽的形态。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any

from gait.app.service import TerminalService
from gait.app.uploadloop import UploadLoop


@dataclass
class Outcome:
    result: str


class FakeUploader:
    """记下每一次被调用；可以被要求卡住或抛错。"""

    def __init__(self, *, result: str = "confirmed") -> None:
        self.calls = 0
        self.result = result
        self.error: Exception | None = None
        self.block = threading.Event()
        self.entered = threading.Event()

    def upload_once(self, *, now: float | None = None) -> Outcome:
        self.calls += 1
        self.entered.set()
        if self.error is not None:
            raise self.error
        # 卡住模拟慢网：`block` 不被 set 就一直等。
        self.block.wait(timeout=5.0)
        return Outcome(self.result)


def loop_for(uploader: FakeUploader, **kwargs: Any) -> UploadLoop:
    kwargs.setdefault("idle_interval", 0.01)
    kwargs.setdefault("paused_interval", 0.01)
    kwargs.setdefault("error_backoff", 0.01)
    return UploadLoop(uploader, **kwargs)


def wait_until(predicate, *, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return False


# ── 队列真的会动 ────────────────────────────────────────────────────────────


def test_the_queue_finally_drains_without_anyone_asking() -> None:
    """本 Issue 的全部意义：此前 `SessionUploader` 在 `src/` 里从未被构造过。"""
    uploader = FakeUploader()
    uploader.block.set()
    loop = loop_for(uploader)
    try:
        loop.start()
        assert wait_until(lambda: uploader.calls >= 3)
    finally:
        loop.stop()


def test_a_confirmed_upload_is_followed_immediately_by_the_next() -> None:
    """积压是要清完的，不是每 5 秒挪一格。"""
    uploader = FakeUploader(result="confirmed")
    uploader.block.set()
    loop = loop_for(uploader, idle_interval=10.0)  # 空闲间隔很长，但不该被用到
    try:
        loop.start()
        assert wait_until(lambda: uploader.calls >= 5, timeout=2.0)
    finally:
        loop.stop()


# ── 不阻塞 IPC：本 Issue 存在的理由 ────────────────────────────────────────


def test_a_stuck_upload_does_not_block_the_ipc_loop() -> None:
    """让上传**真的卡住**，再去调 `handle()`。

    验收原文：「这条正是本 Issue 存在的理由，必须是测试而不是承诺」。
    同步 drain 会把 IPC 一起卡住 —— 一件 4 MiB 在慢网上能耗到 `PART_TIMEOUT` 的
    300 秒，那 300 秒里渲染进程的每一次调用都没有回应。
    """
    uploader = FakeUploader()  # block 不 set，upload_once 会一直卡着
    service = TerminalService(uploader=uploader)
    try:
        service.start_background_work()
        assert uploader.entered.wait(timeout=2.0), "上传没有开始，这条测试就没测到东西"

        started = time.monotonic()
        response = service.handle({"id": "1", "method": "describe", "params": {}})
        elapsed = time.monotonic() - started

        assert response["status"] == "ok"
        # 上传正卡着，而 IPC 立刻就回来了。
        assert elapsed < 0.5, f"handle() 花了 {elapsed:.2f}s —— IPC 被上传挡住了"
    finally:
        uploader.block.set()
        service.close()


# ── 采集中停传：待确认 2 ────────────────────────────────────────────────────


def test_nothing_is_sent_while_a_capture_is_running() -> None:
    """断言的是**一个请求都不发**，而不是某个标志位被设上了。

    理由在代价的不对称：§18 已把链路余量列为已知薄弱项，而占带宽干扰一旦发生，
    表现是到达率下降、这次采集直接废掉 —— 那远大于晚传几分钟。
    """
    uploader = FakeUploader()
    uploader.block.set()
    capturing = True
    loop = loop_for(uploader, paused=lambda: capturing)
    try:
        loop.start()
        time.sleep(0.15)  # 给它足够多轮去犯错
        assert uploader.calls == 0

        capturing = False
        assert wait_until(lambda: uploader.calls >= 1), "采集结束后没有接着传"
    finally:
        loop.stop()


def test_the_pause_is_rechecked_after_every_single_item() -> None:
    """用 `upload_once` 而不是 `drain(limit=100)` 的原因。

    一批传到一半采集开始了，`drain` 还会继续传下去 —— 而那正是待确认 2 要避免的。
    """
    uploader = FakeUploader()
    uploader.block.set()
    capturing = False
    loop = loop_for(uploader, paused=lambda: capturing)
    try:
        loop.start()
        assert wait_until(lambda: uploader.calls >= 2)
        capturing = True
        time.sleep(0.1)
        settled = uploader.calls
        time.sleep(0.15)
        # 采集开始后最多再传一件；不该继续往下清。
        assert uploader.calls <= settled + 1
    finally:
        loop.stop()


def test_the_service_pauses_the_loop_while_it_is_capturing() -> None:
    """判据接的是真实的采集状态，不是测试里另造的一个开关。"""
    uploader = FakeUploader()
    uploader.block.set()
    service = TerminalService(uploader=uploader)
    assert service.drain is not None

    assert service.drain._paused() is False
    service.capture = object()  # type: ignore[assignment]
    assert service.drain._paused() is True


# ── 退出不等：待确认 3 ──────────────────────────────────────────────────────


def test_stop_returns_at_once_even_with_an_upload_in_flight() -> None:
    """一件最长 300 s，而诊室下班时人已经要走了。

    数据不会丢 —— 租约到期后那条重新可取（RAY-226 已保证）。
    """
    uploader = FakeUploader()  # 卡着不放
    loop = loop_for(uploader)
    loop.start()
    assert uploader.entered.wait(timeout=2.0)

    started = time.monotonic()
    loop.stop()
    elapsed = time.monotonic() - started

    assert elapsed < 1.0, f"stop() 等了 {elapsed:.2f}s —— 说好的不等"
    uploader.block.set()


def test_the_thread_is_a_daemon_so_it_never_holds_the_process_open() -> None:
    uploader = FakeUploader()
    uploader.block.set()
    loop = loop_for(uploader)
    try:
        loop.start()
        assert loop._thread is not None and loop._thread.daemon
    finally:
        loop.stop()


# ── 死掉的线程要看得见 ─────────────────────────────────────────────────────


def test_an_unexpected_failure_does_not_kill_the_loop() -> None:
    """`upload_once` 已兜住传输异常，但 `queue.lease()` 的 sqlite 故障会逃出来。

    不兜住的话线程就死了，而**死掉的线程不报错也不让任何测试变红**。
    """
    uploader = FakeUploader()
    uploader.block.set()
    uploader.error = sqlite_locked = RuntimeError("database is locked")
    loop = loop_for(uploader)
    try:
        loop.start()
        assert wait_until(lambda: uploader.calls >= 3), "一次失败就把循环打死了"
        assert loop.running

        status = loop.status()
        assert status["failures"] >= 1
        assert str(sqlite_locked) in (status["lastError"] or "")
    finally:
        loop.stop()


def test_a_recovery_clears_the_error_so_it_does_not_linger() -> None:
    uploader = FakeUploader()
    uploader.block.set()
    uploader.error = RuntimeError("transient")
    loop = loop_for(uploader)
    try:
        loop.start()
        assert wait_until(lambda: loop.status()["failures"] >= 1)
        uploader.error = None
        assert wait_until(lambda: loop.status()["lastError"] is None)
    finally:
        loop.stop()


def test_a_paused_predicate_that_throws_is_also_survived() -> None:
    """判据由调用方注入，它抛什么这里无从预知 —— 但它不该带走整个循环。"""
    uploader = FakeUploader()
    uploader.block.set()

    def broken() -> bool:
        raise RuntimeError("判据自己炸了")

    loop = loop_for(uploader, paused=broken)
    try:
        loop.start()
        assert wait_until(lambda: loop.status()["failures"] >= 2)
        assert loop.running
    finally:
        loop.stop()


def test_the_snapshot_says_whether_the_drain_thread_is_alive() -> None:
    """没有这个，一次故障杀掉线程之后，界面上与「队列本来就是空的」一模一样。"""
    uploader = FakeUploader()
    uploader.block.set()
    service = TerminalService(uploader=uploader)
    try:
        service.start_background_work()
        summary = service.handle({"id": "1", "method": "snapshot"})["result"]["uploadSummary"]
        assert summary["drain"]["alive"] is True
    finally:
        service.close()


def test_without_an_uploader_the_drain_is_absent_not_dead() -> None:
    """未预配置的终端传不了 —— 那是正常状态，与「配了但死了」必须分得开。"""
    service = TerminalService()
    assert service.drain is None
    summary = service.handle({"id": "1", "method": "snapshot"})["result"]["uploadSummary"]
    # 没有队列时连账都不记；有队列没通路时 drain 为 None。两者都不是「alive: false」。
    assert summary.get("drain") is None


def test_start_twice_does_not_spawn_a_second_thread() -> None:
    uploader = FakeUploader()
    uploader.block.set()
    loop = loop_for(uploader)
    try:
        loop.start()
        first = loop._thread
        loop.start()
        assert loop._thread is first
    finally:
        loop.stop()
