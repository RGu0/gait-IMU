"""原始字节落盘。契约 §1 的 `device/recorder.py`（F1.4 → RAY-198）。

PRD §6.1：「每个 BLE Notify 记录主机高精度接收时刻，随原始帧第一时间落盘
（接收回调第一动作是写盘）」。

本模块目前只有 RAY-200 压测所需的最小实现：一个包在任意 Transport 外面的
录制层，把每段下行字节先写盘、再交给上层解析。完整的会话落盘（目录布局、
崩溃恢复、磁盘满处理）仍归 RAY-198。

## 为什么包装 Transport 而不是挂设备回调

`WT901Device` 拿到字节的入口已经是解析入口 —— 在设备层再落盘，「第一动作」
就排到解析之后了。解析崩了数据还在：压测要回答的是链路问题，采集工具自身的
bug 不该让一轮 30 分钟的实验作废。

## 为什么沿用 wt901 的录制格式

JSONL：首行文件头，其后逐行 ``{"t": 相对首段字节的秒, "hex": 原始字节}``。
`wt901.transport.replay.ReplayTransport` 直接可读 —— 无硬件自测靠的就是它。
wt901 声明 recording/replay 「接口不承诺稳定」，但本仓库把 wt901 钉在 commit
上（pyproject `[tool.uv.sources]`），升级是显式动作，届时这里是唯一需要跟进
的位置。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import IO

from wt901 import Transport
from wt901.recording import RecordingWriter

__all__ = ["RecordingTransport", "open_recording_writer"]


def open_recording_writer(
    path: Path, *, device_id: str, note: str = ""
) -> RecordingWriter:
    """在 ``path`` 打开一个录制文件。

    时钟固定用 ``time.monotonic`` —— 与 wt901 给 ``ImuSample.t_host`` 打点的
    时钟同源，录制里的 ``t`` 才能与样本时刻互相印证。
    """
    handle: IO[str] = path.open("w", encoding="utf-8")
    return RecordingWriter(
        handle, device_id=device_id, note=note, clock=time.monotonic
    )


class RecordingTransport(Transport):
    """把下行字节先落盘、再转发的 Transport 包装层。

    上层（`WT901Device`）照常 ``on_data`` 注册回调，感知不到录制的存在；
    写盘失败会让异常沿 BLE 回调冒出来 —— PRD §6.1 要求写盘错误安全停止，
    静默吞掉写不进去的数据与之相悖。
    """

    def __init__(self, inner: Transport, writer: RecordingWriter) -> None:
        super().__init__()
        self._inner = inner
        self._writer = writer
        inner.on_data(self._tee)
        inner.on_disconnect(self._emit_disconnect)

    @property
    def device_id(self) -> str:
        return self._inner.device_id

    @property
    def is_connected(self) -> bool:
        return self._inner.is_connected

    async def connect(self) -> None:
        await self._inner.connect()

    async def disconnect(self) -> None:
        await self._inner.disconnect()

    async def write(self, data: bytes) -> None:
        await self._inner.write(data)

    def _tee(self, data: bytes) -> None:
        self._writer.write(data)
        self._emit_data(data)
