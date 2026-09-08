"""常驻的待传队列排空线程。RAY-416。

上传这条链路此前差最后一环：队列语义有了（RAY-226），会话收尾即入队有了（RAY-233），
会发包的 HTTP 客户端也有了（RAY-355）—— **但没有任何人调用 `drain()`**，
`SessionUploader` 在 `src/` 里从未被构造过。数据安全地排在本地队列里，永远排着。

## 为什么线程在这里，而不在 `SessionUploader` 里

`SessionUploader` 的模块文档写明它**不自带线程**：

> 一次只推进一个条目，而不是自己开线程循环……那是调用方的调度问题，
> **把线程藏在这里会让调用方失去对时机的控制**。

那句话反对的是「藏在**它自己**里」，不是反对有线程。本模块就是那个「调用方」：
它住在 app 层（与 `transportloop.py` 同一层），`cloud/upload.py` 一行不改。

于是「什么时候传」这个决定看得见、改得动、测得了 —— 而不是散在一个上传类的内部。

## 三条产品决定，都在这个文件里落地

**① 采集中停传**（RAY-416 待确认 2，2026-09-08 拍板）。

PRD §6.1 只说采集中不**显示**上传进度，没说不传。选保守，理由是代价不对称：
§18 已把链路余量列为已知薄弱项（悬崖在 2 与 2.5 m 之间），而占带宽干扰一旦发生，
表现是到达率下降、**这次采集直接废掉** —— 那远大于晚传几分钟。

判据取 `paused()` 回调而不是自己去看会话状态：本模块不该知道什么叫「采集」。

**② 退出不等**（待确认 3）。daemon 线程 + `stop()` 只给极短的 join。
数据不会丢 —— 租约到期后那条重新可取，下次开机接着传（RAY-226 已保证）。
`transportloop.TransportLoop` 的 daemon 注释写的是同一件事。

**③ 不阻塞 IPC。** 这是本 Issue 存在的理由：sidecar 主循环是
`__main__.py` 的 `for line in stdin:`，单线程阻塞读。一件 4 MiB 在慢网上可以耗到
`PART_TIMEOUT` 的 300 秒 —— 那 300 秒里渲染进程的每一次调用都没有回应。

## 一个死掉的排空线程是看不见的

它不报错、不让任何测试变红，只是队列从此再也不动 —— **而那正是本 Issue 要修的
毛病本身**，换了个更隐蔽的形态回来。

`SessionUploader.upload_once` 已经兜住了传输异常（RAY-233），但兜的是 `_transfer`
那一段：`queue.lease()` 的 sqlite 故障、打包时的读盘错误仍然会逃出来。所以这里再兜
一层，并且**把它变成看得见的东西**：`status()` 带出 `alive` / `last_error` /
`consecutive_failures`，由 `snapshot()` 随 `uploadSummary` 报给 P-01。

靠「应该不会抛」是不行的 —— 那正是上一轮让队列只进不出的那种假设。
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any, Final, Protocol

#: 队列空时的轮询间隔。诊室里等下一位受试者是以分钟计的，秒级轮询没有意义，
#: 而它每一次都要开一次 sqlite。
IDLE_INTERVAL_S: Final[float] = 5.0

#: 采集中的复查间隔。比空闲短 —— 采集一结束就该接着传，那正是「等下一位」的窗口。
PAUSED_INTERVAL_S: Final[float] = 2.0

#: 连续失败后的退避上限。队列自己对**单个条目**已有退避（15 min 上限），
#: 这一条防的是另一回事：整个循环层面的反复失败（比如 sqlite 一直打不开）。
ERROR_BACKOFF_S: Final[float] = 30.0

#: `stop()` 等线程收尾的时间。**刻意很短** —— 待确认 3 拍板为「不等，直接退」。
STOP_JOIN_S: Final[float] = 0.5


class Uploader(Protocol):
    """本模块只用得到这一个动作。"""

    def upload_once(self, *, now: float | None = None) -> Any: ...


class UploadLoop:
    """把待传队列排空的常驻线程。

    **一次只推进一个条目**（`upload_once` 而不是 `drain`）：暂停判据因此每传完一件
    就复查一次，采集一开始最多再传一件就停下。用 `drain(limit=100)` 的话，一批传到
    一半采集开始了，它还会继续传下去 —— 而那正是待确认 2 要避免的事。
    """

    def __init__(
        self,
        uploader: Uploader,
        *,
        paused: Callable[[], bool] = lambda: False,
        idle_interval: float = IDLE_INTERVAL_S,
        paused_interval: float = PAUSED_INTERVAL_S,
        error_backoff: float = ERROR_BACKOFF_S,
    ) -> None:
        self._uploader = uploader
        self._paused = paused
        self._idle = idle_interval
        self._paused_interval = paused_interval
        self._error_backoff = error_backoff
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._last_error: str | None = None
        self._failures = 0
        self._uploads = 0

    # ── 生命周期 ────────────────────────────────────────────────────────────

    @property
    def running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    def start(self) -> None:
        """起线程。已经在跑就什么都不做 —— 重复 start 不该起出第二个。"""
        if self.running:
            return
        self._stop.clear()
        # daemon：sidecar 退出时这个线程不该拦着进程走。待确认 3 拍板为「不等，
        # 直接退」，而数据的安全不靠它活着 —— 靠的是租约到期后条目重新可取。
        thread = threading.Thread(target=self._run, name="gait-upload-drain", daemon=True)
        self._thread = thread
        thread.start()

    def stop(self, *, timeout: float = STOP_JOIN_S) -> None:
        """请线程停下，**最多等 `timeout`**。

        不等它把手上那一件传完：那一件最长 300 s（`PART_TIMEOUT`），而诊室下班时
        人已经要走了。没传完的那条会在租约到期后重新可取。
        """
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=timeout)

    # ── 可见性 ──────────────────────────────────────────────────────────────

    def status(self) -> dict[str, Any]:
        """让「线程还活着吗」成为一个能被看见的事实。

        没有这个，一次 sqlite 故障杀掉线程之后，界面上的表现与「队列本来就是空的」
        一模一样 —— 而那正是本 Issue 要修的毛病换了个形态。
        """
        with self._lock:
            return {
                "alive": self.running,
                "uploads": self._uploads,
                "failures": self._failures,
                "lastError": self._last_error,
            }

    # ── 循环 ────────────────────────────────────────────────────────────────

    def _run(self) -> None:
        delay = 0.0
        while not self._stop.wait(delay):
            delay = self._tick()

    def _tick(self) -> float:
        """推进一步，返回下一次之前该等多久。"""
        try:
            if self._paused():
                # 采集中停传（待确认 2）。**判据每一件之后都复查**，所以采集一开始
                # 最多再传一件就停。
                return self._paused_interval
        except Exception as exc:  # noqa: BLE001 - 判据由调用方注入，它抛什么这里无从预知
            return self._record_failure(f"暂停判据抛出 {type(exc).__name__}：{exc}")

        try:
            outcome = self._uploader.upload_once()
        except Exception as exc:  # noqa: BLE001 - 见模块文档「一个死掉的排空线程是看不见的」
            # `upload_once` 自己已经兜住了传输异常（RAY-233），所以能到这里的是
            # 别的东西：`queue.lease()` 的 sqlite 故障、打包时的读盘错误。
            # 不兜住的话线程就死了，而**死掉的线程不报错也不让测试变红**。
            return self._record_failure(f"{type(exc).__name__}：{exc}")

        with self._lock:
            self._failures = 0
            self._last_error = None
            if getattr(outcome, "result", None) != "idle":
                self._uploads += 1

        # 队列空了就慢下来；刚传完一条就立刻接着传下一条 —— 积压是要清完的，
        # 不是每 5 秒挪一格。
        return self._idle if getattr(outcome, "result", None) == "idle" else 0.0

    def _record_failure(self, detail: str) -> float:
        with self._lock:
            self._failures += 1
            self._last_error = detail
        return self._error_backoff


def build_uploader(
    session_root: Any, access_root: Any
) -> Any:  # pragma: no cover - 由 `__main__` 在真实环境里走
    """按预配置造一个真的 `SessionUploader`。**造不出来就返回 None，并说明原因。**

    放在这里而不是 `TerminalService.__init__`：构造它需要 `AccessStore`（预配置目录
    与密钥库），而那是**进程入口**的事 —— service 本身不该知道环境变量长什么样。
    """
    from gait.cloud.ingest_http import HttpIngestionClient
    from gait.cloud.tenancy import AccessError, AccessStore
    from gait.cloud.upload import SessionUploader, UploadQueue

    if session_root is None or access_root is None:
        return None
    try:
        client = HttpIngestionClient.from_access_store(AccessStore(access_root))
    except (AccessError, OSError):
        # 未预配置的终端传不了 —— 这不是错误，是一种正常状态（v1 的接入模型是
        # 服务方安装时写入）。返回 None，由调用方决定怎么说。
        return None
    from pathlib import Path

    return SessionUploader(UploadQueue(Path(session_root) / "upload-queue.sqlite3"), client)


__all__ = [
    "ERROR_BACKOFF_S",
    "IDLE_INTERVAL_S",
    "PAUSED_INTERVAL_S",
    "STOP_JOIN_S",
    "UploadLoop",
    "Uploader",
    "build_uploader",
]
