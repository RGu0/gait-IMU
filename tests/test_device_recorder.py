"""`gait.device.recorder.ThreadedRecordingWriter`：写盘移出 BLE 回调线程。

热路径关切：`write()` 只做取时刻+入队，磁盘 I/O 在专属线程；写失败被捕获成
`error` 而不是抛出（BLE 回调里抛异常没人接得住）；与 wt901 的
`transport.recording.RecordingTransport` 协议兼容，端到端 tee 行为不变。
"""

from __future__ import annotations

import time
from pathlib import Path

from wt901.recording import RecordingWriter, read_recording
from wt901.transport.memory import MemoryTransport
from wt901.transport.recording import RecordingTransport

from gait.device.recorder import ThreadedRecordingWriter


def test_recording_round_trips_through_wt901_reader(tmp_path: Path) -> None:
    path = tmp_path / "raw.jsonl"
    writer = ThreadedRecordingWriter(path, device_id="AA:BB", note="round-1")

    first = b"\x55\x61" + bytes(range(18))
    second = b"\x55\x61" + bytes(18)
    writer.write(first)
    writer.write(second)
    writer.close()

    assert writer.error is None
    assert writer.chunks_written == 2
    recording = read_recording(path)
    assert recording.device_id == "AA:BB"
    assert recording.note == "round-1"
    assert [chunk.data for chunk in recording.chunks] == [first, second]
    assert recording.chunks[0].t == 0.0


def test_recorded_time_reflects_when_write_was_called_not_when_drained(
    tmp_path: Path,
) -> None:
    """`t` 必须是入队（回调发生）的时刻，不是写线程排到它的时刻。"""
    path = tmp_path / "raw.jsonl"
    writer = ThreadedRecordingWriter(path, device_id="dev")

    writer.write(b"\x55\x61" + bytes(18))
    time.sleep(0.05)
    writer.write(b"\x55\x61" + bytes(18))
    writer.close()

    recording = read_recording(path)
    assert recording.chunks[1].t >= 0.04


def test_close_is_idempotent(tmp_path: Path) -> None:
    writer = ThreadedRecordingWriter(tmp_path / "raw.jsonl", device_id="dev")
    writer.write(b"\x55\x61" + bytes(18))
    writer.close()
    writer.close()  # 不应抛异常或挂起。


def test_write_failure_is_captured_not_raised_and_further_writes_are_dropped(
    tmp_path: Path, monkeypatch
) -> None:
    """写盘异常发生在专属线程里；调用方（BLE 回调）永远看不到它。"""
    path = tmp_path / "raw.jsonl"
    writer = ThreadedRecordingWriter(path, device_id="dev")

    def _boom(self: RecordingWriter, _data: bytes) -> None:
        raise OSError("磁盘已满")

    # RecordingWriter 是 __slots__ 类，实例属性不可写；改在类上打补丁。
    monkeypatch.setattr(RecordingWriter, "write", _boom)

    writer.write(b"\x55\x61" + bytes(18))  # 不抛：write() 本身只是入队。

    deadline = time.monotonic() + 2.0
    while writer.error is None and time.monotonic() < deadline:
        time.sleep(0.005)
    assert isinstance(writer.error, OSError)

    writer.write(b"\x55\x61" + bytes(18))  # 出错后：静默丢弃，不再入队。
    assert writer._queue.qsize() == 0

    writer.close()  # 仍能正常关闭，不挂起。


def test_compatible_with_wt901_recording_transport(tmp_path: Path) -> None:
    """本类只是 wt901 `RecordingTransport` 的 writer 参数，端到端 tee 行为不变。"""
    path = tmp_path / "raw.jsonl"
    writer = ThreadedRecordingWriter(path, device_id="mem")
    inner = MemoryTransport(device_id="mem")
    transport = RecordingTransport(inner, writer)
    received: list[bytes] = []
    transport.on_data(received.append)

    async def scenario() -> None:
        await transport.connect()
        inner.feed(b"\x55\x61" + bytes(18))
        await transport.disconnect()  # 关闭 writer。

    import asyncio

    asyncio.run(scenario())

    assert received == [b"\x55\x61" + bytes(18)]
    assert writer.error is None
    recording = read_recording(path)
    assert len(recording.chunks) == 1
