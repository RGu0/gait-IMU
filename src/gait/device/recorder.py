"""原始字节落盘的热路径安全写入。契约 §1 的 `device/recorder.py`（F1.4 → RAY-198）。

PRD §6.1：「每个 BLE Notify 记录主机高精度接收时刻，随原始帧第一时间落盘
（接收回调第一动作是写盘）」。

## 录制传输层直接用 wt901 的

wt901 自带 `wt901.transport.recording.RecordingTransport`：包住任意 Transport，
把下行字节抄一份给 writer 再向上转发，`disconnect` 时无论是否异常都关闭 writer。
本仓库不重复它 —— 本模块只补它缺的一块：**热路径安全的 writer**。

## 为什么写盘必须移出 BLE 回调线程

两台设备的 bleak 通知回调串行跑在同一个事件循环线程上，而 `ImuSample.t_host`
在回调链**之后**才打点。同步写盘意味着设备 A 的一次磁盘停顿（Windows 上杀毒/
文件系统 10–50 ms 的停顿是常态）会原样推迟设备 B 的到达时刻 —— 200 Hz 下 20 ms
就是 4 个样本被推过秒边界，逐秒到达率被工具自己的 I/O 压低。压测量的是链路，
不能让测量器污染被测量。

所以「第一动作」拆成两半：回调里只做**取时刻 + 入队**（微秒级），格式化与
磁盘 I/O 由专属线程完成。时刻在回调里取，落盘的 `t` 才是真实到达时刻而不是
写线程排到它的时刻。

## 写失败不抛回调

磁盘满 / 句柄失效发生在写线程里，记进 :attr:`ThreadedRecordingWriter.error`，
之后的数据不再写盘但**继续向上转发** —— bleak 回调里抛异常只会进事件循环的
异常处理器（谁也看不见），停不下任何东西。调用方在轮结束时检查 ``error``，
把该轮标记为录制不完整。会话级的「写盘错误安全停止」是 RAY-198 的完整交付，
不在本最小实现。
"""

from __future__ import annotations

import queue
import threading
import time
from pathlib import Path

from wt901.recording import RecordingWriter

__all__ = ["ThreadedRecordingWriter"]

_CLOSE = object()


class ThreadedRecordingWriter:
    """把 `wt901.recording.RecordingWriter` 的磁盘 I/O 移到专属线程。

    与 `RecordingTransport` 的 writer 协议兼容（``write`` / ``close`` /
    ``chunks_written``），格式与 wt901 完全一致 —— 复用它的写入器，只是喂给
    它的时钟改为「回调时刻的回放」：入队时取 ``time.monotonic()``，写线程把
    该时刻交给 RecordingWriter 的 ``clock``。
    """

    def __init__(self, path: Path, *, device_id: str, note: str = "") -> None:
        # 行缓冲：每行是一段完整字节，尽快离开用户态缓冲区 —— 崩溃时丢的只有
        # 队列里还没写的部分。反正 I/O 已在专属线程，不再有热路径代价。
        handle = path.open("w", encoding="utf-8", buffering=1)
        self._pending_t = 0.0
        self._writer = RecordingWriter(
            handle, device_id=device_id, note=note, clock=lambda: self._pending_t
        )
        self._queue: queue.SimpleQueue[object] = queue.SimpleQueue()
        self._written = 0
        self._closed = False
        self.error: BaseException | None = None
        """写线程遇到的第一个异常。非 ``None`` 表示录制不完整。"""
        self._thread = threading.Thread(
            target=self._drain, name=f"recording-{device_id}", daemon=True
        )
        self._thread.start()

    @property
    def chunks_written(self) -> int:
        return self._written

    def write(self, data: bytes) -> None:
        """BLE 回调的第一动作：取时刻、入队、立即返回。"""
        if self._closed or self.error is not None:
            return
        self._queue.put((time.monotonic(), bytes(data)))

    def close(self) -> None:
        """排空队列并关闭文件。可重复调用。"""
        if self._closed:
            return
        self._closed = True
        self._queue.put(_CLOSE)
        self._thread.join(timeout=10.0)

    def _drain(self) -> None:
        while True:
            item = self._queue.get()
            if item is _CLOSE:
                break
            if self.error is not None:
                continue  # 只排空，不再写。
            t, data = item  # type: ignore[misc]
            try:
                self._pending_t = t
                self._writer.write(data)
                self._written += 1
            except BaseException as exc:  # noqa: BLE001 - 记录任何写失败
                self.error = exc
        try:
            self._writer.close()
        except BaseException as exc:  # noqa: BLE001
            if self.error is None:
                self.error = exc
