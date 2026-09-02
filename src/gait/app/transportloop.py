"""会话期间承载异步传输的事件循环。

## 为什么 sidecar 需要它

`Transport.connect()` / `disconnect()` 是协程 —— BLE 本来就是异步的。而
`TerminalService.handle()` 是同步的（一条请求一条回应，见 `__main__` 的模块文档）。
两者之间必须有个东西。

选项是三个，这里取第三个：

1. 每次 `asyncio.run(...)`：能跑，但每次建一个循环又关掉。真接上 BLE 之后不成立
   —— BLE 的连接活在循环里，循环一关连接就没了。写它等于写一份注定要重写的代码。
2. 把整个 service 改成异步：改动面远大于本 scope，且 JSON Lines 的一问一答本来
   就不需要异步。
3. **一个跑在后台线程里的常驻循环**，同步侧用 `run_coroutine_threadsafe` 提交并
   等结果。这正是「BLE 回调在循环里跑、落盘在写线程里跑、请求应答在主线程里跑」
   的真实形态，接真设备时不用换。

## 它只在会话期间存在

循环随 `start()` 起、随 `stop()` 落。常驻一个空转的循环没有意义，而一个会话结束后
还连着设备的 sidecar 会让「这次采集已经结束」变得不确定。
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Coroutine
from typing import Any, TypeVar

T = TypeVar("T")

#: 单个传输操作的等待上限。BLE 连接偶尔会卡住，而**卡住的连接与断开的连接对操作员
#: 是两件事**：前者要等，后者要去查设备。给它一个上限，让前者不会无声地变成后者。
OPERATION_TIMEOUT_S = 30.0


class TransportLoop:
    """后台事件循环。`submit()` 从同步侧调用，阻塞到协程完成。"""

    def __init__(self, *, timeout: float = OPERATION_TIMEOUT_S) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._timeout = timeout

    @property
    def running(self) -> bool:
        return self._loop is not None

    def start(self) -> None:
        if self._loop is not None:
            return
        ready = threading.Event()
        loop = asyncio.new_event_loop()

        def run() -> None:
            asyncio.set_event_loop(loop)
            loop.call_soon(ready.set)
            loop.run_forever()

        # daemon：sidecar 被杀时这个线程不该拦着进程退出。数据的安全不靠它活着 ——
        # 靠的是「收到字节的第一动作是写盘」，那一步在写线程里已经完成了。
        thread = threading.Thread(target=run, name="gait-transport-loop", daemon=True)
        thread.start()
        ready.wait(timeout=self._timeout)
        self._loop = loop
        self._thread = thread

    def submit(self, coro: Coroutine[Any, Any, T]) -> T:
        """提交一个协程并等它完成。循环没起来是调用方的错，不是默默不做。"""
        if self._loop is None:
            coro.close()
            raise RuntimeError("传输循环尚未启动")
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(self._timeout)

    def stop(self) -> None:
        loop, thread = self._loop, self._thread
        self._loop = self._thread = None
        if loop is None:
            return
        loop.call_soon_threadsafe(loop.stop)
        if thread is not None:
            thread.join(timeout=self._timeout)
        loop.close()
