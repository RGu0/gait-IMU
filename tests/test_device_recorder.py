"""`gait.device.recorder`：落盘是接收回调的第一动作，且录制可被回放读回。"""

from __future__ import annotations

import asyncio
from pathlib import Path

from wt901 import Transport
from wt901.recording import read_recording

from gait.device.recorder import RecordingTransport, open_recording_writer


class _StubInner(Transport):
    """只会把喂进来的字节向上抛的传输层。"""

    def __init__(self) -> None:
        super().__init__()
        self._connected = False
        self.written: list[bytes] = []

    @property
    def device_id(self) -> str:
        return "stub"

    @property
    def is_connected(self) -> bool:
        return self._connected

    async def connect(self) -> None:
        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False

    async def write(self, data: bytes) -> None:
        self.written.append(bytes(data))

    def feed(self, data: bytes) -> None:
        self._emit_data(data)


class _OrderProbe:
    """记录「写盘」与「转发」谁先发生的假 writer。"""

    def __init__(self, order: list[str]) -> None:
        self._order = order

    def write(self, data: bytes) -> None:
        self._order.append("disk")


def test_disk_write_happens_before_forwarding() -> None:
    order: list[str] = []
    inner = _StubInner()
    transport = RecordingTransport(inner, _OrderProbe(order))
    transport.on_data(lambda data: order.append("forward"))

    inner.feed(b"\x55\x61" + bytes(18))

    assert order == ["disk", "forward"]


def test_delegates_lifecycle_and_writes() -> None:
    inner = _StubInner()
    transport = RecordingTransport(inner, _OrderProbe([]))

    async def scenario() -> None:
        await transport.connect()
        assert transport.is_connected
        await transport.write(b"\xff\xaa\x27\x64\x00")
        await transport.disconnect()
        assert not transport.is_connected

    asyncio.run(scenario())
    assert inner.written == [b"\xff\xaa\x27\x64\x00"]
    assert transport.device_id == "stub"


def test_disconnect_propagates() -> None:
    inner = _StubInner()
    transport = RecordingTransport(inner, _OrderProbe([]))
    seen: list[bool] = []
    transport.on_disconnect(lambda: seen.append(True))

    inner._emit_disconnect()

    assert seen == [True]


def test_recording_round_trips_through_wt901_reader(tmp_path: Path) -> None:
    """写出的文件必须能被 wt901 的读取端读回 —— 回放自测靠的就是这一点。"""
    path = tmp_path / "raw.jsonl"
    writer = open_recording_writer(path, device_id="AA:BB", note="round-1")
    inner = _StubInner()
    transport = RecordingTransport(inner, writer)
    transport.on_data(lambda data: None)

    first = b"\x55\x61" + bytes(range(18))
    second = b"\x55\x61" + bytes(18)
    inner.feed(first)
    inner.feed(second)
    writer.close()

    recording = read_recording(path)
    assert recording.device_id == "AA:BB"
    assert recording.note == "round-1"
    assert [chunk.data for chunk in recording.chunks] == [first, second]
    assert recording.chunks[0].t == 0.0
