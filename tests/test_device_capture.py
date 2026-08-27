"""RAY-198 `session-record-and-replay`：会话级落盘、安全停止与回放桥接。

验收对应关系写在各测试类的 docstring 里。全部用合成字节与内存传输，无需真机。
"""

from __future__ import annotations

import asyncio
import struct
from pathlib import Path

import pytest
from wt901 import WT901Device
from wt901.transport.memory import MemoryTransport

from gait.contracts import SessionMeta
from gait.device.adapter import to_raw_frame
from gait.device.capture import (
    CaptureError,
    SessionCapture,
    payload_equal,
    replay_raw_frames,
    replay_session_foot,
)
from gait.io.session import create_session, new_session_id, new_subject_uuid, raw_path


def _frame(seed: int) -> bytes:
    """一帧 0x55 0x61 运动数据，9 个 int16 计数。"""
    return b"\x55\x61" + struct.pack("<9h", *range(seed, seed + 9))


FRAMES = [_frame(i * 10) for i in range(6)]


def _meta(session_id: str) -> SessionMeta:
    return SessionMeta(
        session_id=session_id,
        created_at="2026-08-26T00:00:00Z",
        subject_uuid=new_subject_uuid(),
        scenario="walk",
        devices={"L": {"mac": "AA:BB:CC:DD:EE:01"}, "R": {"mac": "AA:BB:CC:DD:EE:02"}},
        config_snapshot={"rate": 11, "bandwidth": 3, "algorithm": 1},
        calib_snapshot={"L": {"bias": [0.0, 0.0, 0.0]}},
        algo_version="test",
        algo_params={"zupt_threshold": 0.1},
        sync_report={"anchors": 0},
        integrity_report={"loss_rate": 0.0},
        protocol_config={"duration_s": 60},
    )


@pytest.fixture
def session(tmp_path: Path) -> tuple[Path, str]:
    session_id = new_session_id()
    create_session(tmp_path, _meta(session_id))
    return tmp_path, session_id


class TestLandsOnTheSessionLayout:
    """验收 2：双足各自落到 raw/left.raw 与 raw/right.raw。"""

    def test_each_foot_writes_to_its_own_path(self, session):
        root, session_id = session

        async def scenario() -> None:
            with SessionCapture(root, session_id) as capture:
                for foot in ("L", "R"):
                    inner = MemoryTransport(device_id=f"dev-{foot}")
                    recording = capture.wrap(foot, inner)
                    await recording.connect()
                    for frame in FRAMES:
                        inner.feed(frame)
                    await recording.disconnect()

        asyncio.run(scenario())
        for foot in ("L", "R"):
            assert raw_path(root, session_id, foot).exists()

    def test_the_layout_comes_from_io_session_not_from_here(self, session):
        # 布局只有一个来源：io.session.raw_path。这条测试钉住「本模块不另定路径」。
        root, session_id = session
        capture = SessionCapture(root, session_id)
        inner = MemoryTransport()
        capture.wrap("L", inner)
        capture.close()
        assert raw_path(root, session_id, "L").exists()

    def test_a_missing_session_directory_is_refused_not_created(self, tmp_path: Path):
        # 建目录归 io.session.create_session；两个来源会让「会话已登记」含糊。
        with pytest.raises(CaptureError, match="raw 目录不存在"):
            SessionCapture(tmp_path, new_session_id())

    def test_one_foot_cannot_be_registered_twice(self, session):
        root, session_id = session
        capture = SessionCapture(root, session_id)
        capture.wrap("L", MemoryTransport())
        with pytest.raises(CaptureError, match="已经登记过"):
            capture.wrap("L", MemoryTransport())
        capture.close()

    @pytest.mark.parametrize("bad", ["l", "left", "X"])
    def test_a_bad_foot_label_is_refused(self, session, bad):
        root, session_id = session
        capture = SessionCapture(root, session_id)
        with pytest.raises(CaptureError, match="foot"):
            capture.wrap(bad, MemoryTransport())
        capture.close()


class TestSafeStopOnWriteFailure:
    """验收 3：写盘出错时会话安全停止并标为不完整，不静默继续。"""

    def test_check_raises_so_the_caller_can_stop(self, session):
        root, session_id = session
        capture = SessionCapture(root, session_id)
        capture.wrap("L", MemoryTransport())
        capture.wrap("R", MemoryTransport())
        capture._writers["L"].error = OSError("disk full")

        with pytest.raises(CaptureError, match="必须安全停止"):
            capture.check()
        capture.close()

    def test_a_healthy_session_check_is_silent(self, session):
        root, session_id = session
        capture = SessionCapture(root, session_id)
        capture.wrap("L", MemoryTransport())
        capture.wrap("R", MemoryTransport())
        capture.check()
        capture.close()

    def test_a_write_failure_marks_the_session_incomplete(self, session):
        root, session_id = session
        capture = SessionCapture(root, session_id)
        capture.wrap("L", MemoryTransport())
        capture.wrap("R", MemoryTransport())
        capture._writers["R"].error = OSError("disk full")
        status = capture.close()

        assert not status.complete
        assert any("R 的原始数据写盘失败" in p for p in status.problems)

    def test_a_single_foot_session_is_incomplete(self, session):
        # 双足会话缺一只即不完整 —— 算法照常能出结果，原始数据却缺一半。
        root, session_id = session
        capture = SessionCapture(root, session_id)
        capture.wrap("L", MemoryTransport())
        status = capture.close()

        assert not status.complete
        assert any("未登记的脚" in p for p in status.problems)

    def test_both_feet_healthy_is_complete(self, session):
        root, session_id = session
        capture = SessionCapture(root, session_id)
        capture.wrap("L", MemoryTransport())
        capture.wrap("R", MemoryTransport())
        status = capture.close()

        assert status.complete
        assert status.problems == ()

    def test_status_before_close_is_refused_rather_than_guessed(self, session):
        root, session_id = session
        capture = SessionCapture(root, session_id)
        with pytest.raises(CaptureError, match="尚未结束"):
            _ = capture.status
        capture.close()

    def test_exiting_the_context_closes_writers_even_after_an_exception(
        self, session
    ):
        # 一次异常中止的会话，已经采到的那部分往往正是最该保住的。
        root, session_id = session
        capture = SessionCapture(root, session_id)

        async def scenario() -> None:
            with pytest.raises(RuntimeError), capture:
                    inner = MemoryTransport()
                    recording = capture.wrap("L", inner)
                    await recording.connect()
                    inner.feed(FRAMES[0])
                    raise RuntimeError("boom")

        asyncio.run(scenario())
        assert raw_path(root, session_id, "L").read_text().strip()
        assert capture.status.chunks_written["L"] == 1


class TestReplayBridge:
    """验收 4：回放工具能把落盘数据重新喂给下游。"""

    def _record_then_replay(self, root: Path, session_id: str) -> list:
        async def scenario() -> list:
            with SessionCapture(root, session_id) as capture:
                inner = MemoryTransport(device_id="dev-L")
                recording = capture.wrap("L", inner)
                await recording.connect()
                for frame in FRAMES:
                    inner.feed(frame)
                await recording.disconnect()
            return [
                frame
                async for frame in replay_raw_frames(raw_path(root, session_id, "L"))
            ]

        return asyncio.run(scenario())

    def test_replay_yields_contract_raw_frames(self, session):
        root, session_id = session
        frames = self._record_then_replay(root, session_id)
        assert len(frames) == len(FRAMES)
        assert frames[0].acc_raw.tolist() == [0, 1, 2]

    def test_replay_by_session_id_uses_the_same_layout(self, session):
        root, session_id = session
        self._record_then_replay(root, session_id)

        async def scenario() -> list:
            return [
                frame
                async for frame in replay_session_foot(root, session_id, "L")
            ]

        assert len(asyncio.run(scenario())) == len(FRAMES)

    def test_a_bad_foot_label_is_refused_on_replay(self, session):
        root, session_id = session

        async def scenario() -> None:
            async for _ in replay_session_foot(root, session_id, "nope"):
                pass

        with pytest.raises(CaptureError, match="foot"):
            asyncio.run(scenario())


class TestLiveReplayEquivalence:
    """验收 5：回放结果与实时处理一致（载荷一致，时序**不**一致）。"""

    @staticmethod
    def _live(frames: list[bytes]) -> list:
        async def scenario() -> list:
            inner = MemoryTransport(device_id="dev-L")
            device = WT901Device(inner)
            await device.open()
            for frame in frames:
                inner.feed(frame)
            await device.close()
            return [to_raw_frame(s) async for s in device.samples()]

        return asyncio.run(scenario())

    @staticmethod
    def _replayed(root: Path, session_id: str, frames: list[bytes]) -> list:
        async def scenario() -> list:
            with SessionCapture(root, session_id) as capture:
                inner = MemoryTransport(device_id="dev-L")
                recording = capture.wrap("L", inner)
                await recording.connect()
                for frame in frames:
                    inner.feed(frame)
                await recording.disconnect()
            return [
                frame
                async for frame in replay_raw_frames(raw_path(root, session_id, "L"))
            ]

        return asyncio.run(scenario())

    def test_the_same_bytes_give_the_same_payload_both_ways(self, session):
        root, session_id = session
        assert payload_equal(
            self._live(FRAMES), self._replayed(root, session_id, FRAMES)
        )

    def test_payload_equality_notices_a_changed_frame(self, session):
        # 否则上面那条断言可能只是"两边都空"。
        root, session_id = session
        other = [*FRAMES[:-1], _frame(999)]
        assert not payload_equal(
            self._live(FRAMES), self._replayed(root, session_id, other)
        )

    def test_payload_equality_notices_a_missing_frame(self, session):
        root, session_id = session
        assert not payload_equal(
            self._live(FRAMES), self._replayed(root, session_id, FRAMES[:-1])
        )

    def test_t_host_is_replay_time_not_capture_time(self, session):
        """这条是刻意钉住一个**限制**而不是一个能力。

        回放产出的 t_host 是回放那一刻的时钟。任何从 t_host 算出来的指标
        （到达率、空洞、缺失率）在回放数据上都不成立 —— cli/linktest.py 在回放
        模式下把 timing_valid 置 False 就是同一条口径。反过来的假设不会报错：
        算出来的到达率看着完全正常，只是描述的是回放机器的调度。
        """
        root, session_id = session
        live = self._live(FRAMES)
        replayed = self._replayed(root, session_id, FRAMES)

        assert payload_equal(live, replayed)
        assert [f.t_host for f in live] != [f.t_host for f in replayed]


class TestReplayNeverLosesSamplesSilently:
    """消费者慢于喂入时，设备层队列满会丢**最旧**的样本，且不报错。

    对实时采集那是对的（阻塞 BLE 回调会让丢失发生在协议栈里，看不见也数不着），
    但在回放上同一条机制会静悄悄地让「与实时一致」不成立。

    注意 wt901 已经挡住了朴素情形：`ReplayTransport._feed` 即使全速也每块
    `await asyncio.sleep(0)`，正是为了不让整段录制在一个事件循环轮次里喂完
    （它的注释写着「丢出来的是回放的假象」）。这里守的是另一半 —— **下游每帧
    处理耗时**时喂入仍会跑在前面。
    """

    @staticmethod
    def _record(root: Path, session_id: str, frames: list[bytes]) -> Path:
        async def scenario() -> None:
            with SessionCapture(root, session_id) as capture:
                inner = MemoryTransport(device_id="dev-L")
                recording = capture.wrap("L", inner)
                await recording.connect()
                for frame in frames:
                    inner.feed(frame)
                await recording.disconnect()

        asyncio.run(scenario())
        return raw_path(root, session_id, "L")

    def test_a_slow_consumer_losing_samples_is_reported_not_swallowed(self, session):
        root, session_id = session
        many = [_frame(i) for i in range(200)]
        path = self._record(root, session_id, many)

        async def scenario() -> list:
            seen = []
            async for frame in replay_raw_frames(path, queue_size=8):
                await asyncio.sleep(0.001)  # 下游每帧要干点活
                seen.append(frame)
            return seen

        with pytest.raises(CaptureError, match="回放丢了"):
            asyncio.run(scenario())

    def test_wt901_already_prevents_the_naive_full_speed_case(self, session):
        # 小队列 + 快消费者并不丢：_feed 每块都让出控制权。这条钉住那个上游保证，
        # 若将来 wt901 去掉那个 sleep(0)，这里会先红。
        root, session_id = session
        many = [_frame(i) for i in range(200)]
        path = self._record(root, session_id, many)

        async def scenario() -> list:
            return [f async for f in replay_raw_frames(path, queue_size=8)]

        assert len(asyncio.run(scenario())) == len(many)

    def test_a_big_enough_queue_replays_everything(self, session):
        root, session_id = session
        many = [_frame(i) for i in range(200)]
        path = self._record(root, session_id, many)

        async def scenario() -> list:
            return [f async for f in replay_raw_frames(path, queue_size=4096)]

        assert len(asyncio.run(scenario())) == len(many)

    def test_breaking_out_early_is_not_reported_as_loss(self, session):
        # 消费者自己不要了，不是丢失。
        root, session_id = session
        path = self._record(root, session_id, [_frame(i) for i in range(200)])

        async def scenario() -> int:
            seen = 0
            async for _ in replay_raw_frames(path, queue_size=8):
                await asyncio.sleep(0.001)
                seen += 1
                if seen == 3:
                    break
            return seen

        assert asyncio.run(scenario()) == 3
