"""会话级采集编排与回放桥接。契约 §1 的 `device/`（F1.4 → RAY-198）。

PRD §6.1：「每个 BLE Notify 记录主机高精度接收时刻，随原始帧第一时间落盘」；
原则 6：原始数据先落盘，算法在另一线程消费副本。

## 本模块与 `recorder.py` / `io/session.py` 的分工

三者已经各自解决了一部分，这里只把它们接起来：

* `io/session.py` 定了会话目录布局 —— `raw_path()` 的 docstring 就写着「落盘由
  RAY-198 实现，**路径在这里定**」。本模块因此不重新发明布局。
* `recorder.py` 的 `ThreadedRecordingWriter` 解决了热路径安全（回调里只取时刻 +
  入队，磁盘 I/O 在专属线程）。
* wt901 的 `RecordingTransport` 解决了「抄一份字节再向上转发」，且断连时无论是否
  异常都会关闭 writer。

缺的是**会话这一层**：两只脚各自落到哪、其中一只写盘失败了整个会话该怎么办、
以及把落盘数据重新喂回下游。

## 为什么不用 `RecordingTransport.to_file`

那个便捷构造会自己 `path.open()` 并配一个 `time.monotonic` 时钟的
`RecordingWriter` —— 也就是**同步写盘**，正好抵消 `ThreadedRecordingWriter`
存在的理由。两台设备的 bleak 回调串行跑在同一个事件循环线程上，一次磁盘停顿会
原样推迟另一台的到达时刻。所以这里直接构造 `RecordingTransport(inner, writer)`，
把我们自己的线程化 writer 交给它（协议兼容：`write` / `close` /
`chunks_written`）。

## 写盘出错为什么必须停会话，而不是继续录

`ThreadedRecordingWriter` 遇错后不再写盘但**继续向上转发** —— 那是它那一层唯一
正确的选择，因为 bleak 回调里抛异常只会进事件循环的异常处理器，谁也看不见，
停不下任何东西。

但会话这一层看得见，也必须管：继续采下去会得到一份**算法照常出结果、原始数据
却缺了一段**的会话。而原始数据是唯一不可再生的东西 —— 算法可以重跑，那段字节
永远回不来。所以 `SessionCapture.check()` 一旦发现错误就要求调用方安全收尾，并
把会话标记为不完整。

## 回放不保 `t_host`，这一条必须显式

`ImuSample.t_host` 是**主机接收时刻**，由设备层在收到字节时现打。回放时打的是
回放那一刻的时钟，不是原始采集时刻 —— 录制文件里的 `RecordedChunk.t` 只用来
控制喂入节奏，不会回填进 `t_host`。

所以「回放结果与实时处理一致」指的是**载荷**一致（`acc_raw` / `gyr_raw` /
`ang_raw` / `saturated` 以及样本的数量与顺序），**不含时序**。`cli/linktest.py`
已经确立了同一条口径：它在回放模式下把 `timing_valid` 置为 `False`，让到达率、
空洞、缺失率这些从 `t_host` 算出来的指标整体作废。

把这条写在这里，是因为反过来的假设不会报错：拿回放数据算出来的到达率看起来
完全正常，只是描述的是回放机器的调度而不是那条 BLE 链路。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Self

from wt901 import OutputMode, WT901Device
from wt901.device import DEFAULT_QUEUE_SIZE
from wt901.recording import Recording, read_recording
from wt901.transport.base import Transport
from wt901.transport.recording import RecordingTransport
from wt901.transport.replay import ReplayTransport

from gait.contracts import FootLabel, RawFrame
from gait.device.adapter import to_raw_frame
from gait.device.recorder import ThreadedRecordingWriter
from gait.io.session import RAW_FILENAMES, raw_path

__all__ = [
    "CaptureError",
    "CaptureStatus",
    "RecoveryReport",
    "SessionCapture",
    "payload_equal",
    "recover_recording",
    "replay_raw_frames",
    "replay_recording",
    "replay_session_foot",
]


class CaptureError(RuntimeError):
    """会话采集的编排错误（不是写盘错误本身 —— 那个记在 `CaptureStatus` 里）。"""


@dataclass(frozen=True, slots=True)
class CaptureStatus:
    """一次会话采集结束时的状态。

    `complete` 为假即表示**这份会话的原始数据有缺口**，必须随会话元数据一起
    留痕。调用方不该只看异常有没有抛 —— 写盘错误发生在写线程里，不会抛到这里。
    """

    complete: bool
    chunks_written: dict[str, int]
    problems: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.complete and self.problems:
            raise CaptureError("complete 为真时不应带 problems —— 那会让调用方两头猜")


def _check_foot(foot: str) -> FootLabel:
    if foot not in RAW_FILENAMES:
        raise CaptureError(f"foot 应为 'L' 或 'R'，收到 {foot!r}")
    return foot  # type: ignore[return-value]


class SessionCapture:
    """把双足的原始字节落到一个会话目录里。

    典型用法::

        with SessionCapture(root, session_id) as capture:
            left = capture.wrap("L", left_transport)
            right = capture.wrap("R", right_transport)
            ...                      # 采集期间定期 capture.check()
        status = capture.status      # 退出后才有终态

    退出 `with` 会关闭全部 writer（排空队列后落盘），无论是否有异常 —— 一次
    异常中止的会话，已经采到的那部分往往正是最该保住的。
    """

    def __init__(self, root: Path, session_id: str, *, note: str = "") -> None:
        self._paths = {
            foot: raw_path(root, session_id, foot) for foot in RAW_FILENAMES
        }
        for path in self._paths.values():
            if not path.parent.is_dir():
                raise CaptureError(
                    f"会话的 raw 目录不存在：{path.parent}。"
                    "先用 io.session.create_session 建立会话目录 —— "
                    "本模块不建目录，避免把「会话已登记」这件事变成两个来源。"
                )
        self._note = note
        self._writers: dict[str, ThreadedRecordingWriter] = {}
        self._closed = False
        self._status: CaptureStatus | None = None

    @property
    def status(self) -> CaptureStatus:
        """终态。只有在采集结束（`close()` 之后）才有意义。"""
        if self._status is None:
            raise CaptureError("采集尚未结束；先退出 with 或调用 close()")
        return self._status

    def wrap(self, foot: str, inner: Transport) -> RecordingTransport:
        """给一只脚的传输套上录制层。同一只脚只能套一次。"""
        label = _check_foot(foot)
        if self._closed:
            raise CaptureError("采集已结束，不能再登记设备")
        if label in self._writers:
            raise CaptureError(
                f"{label} 已经登记过传输了。一只脚对应一台设备、一个文件 —— "
                "两次登记会让两条流写进同一个文件而互相交错。"
            )
        writer = ThreadedRecordingWriter(
            self._paths[label], device_id=inner.device_id, note=self._note
        )
        self._writers[label] = writer
        return RecordingTransport(inner, writer)

    def failures(self) -> tuple[str, ...]:
        """当前已经发生的写盘错误，每只脚一条。空元组表示一切正常。"""
        return tuple(
            f"{label} 的原始数据写盘失败：{writer.error!r}。"
            "该时刻之后的原始字节没有落盘。"
            for label, writer in sorted(self._writers.items())
            if writer.error is not None
        )

    def check(self) -> None:
        """采集期间的巡检：有写盘错误就抛，让调用方走安全收尾。

        这是「写盘错误安全停止」的触发点。写线程里的错误不会自己冒到事件循环，
        所以必须有人主动看 —— 见模块文档。
        """
        problems = self.failures()
        if problems:
            raise CaptureError(
                "原始数据落盘失败，会话必须安全停止：" + "；".join(problems)
            )

    def close(self) -> CaptureStatus:
        """关闭全部 writer 并定下终态。可重复调用。"""
        if self._closed:
            return self.status
        self._closed = True
        for writer in self._writers.values():
            writer.close()
        problems = list(self.failures())
        missing = sorted(set(RAW_FILENAMES) - set(self._writers))
        if missing:
            problems.append(
                f"未登记的脚：{missing}。双足会话缺一只即不完整。"
            )
        self._status = CaptureStatus(
            complete=not problems,
            chunks_written={
                label: writer.chunks_written
                for label, writer in sorted(self._writers.items())
            },
            problems=tuple(problems),
        )
        return self._status

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


async def replay_raw_frames(
    path: Path, *, speed: float | None = None, queue_size: int = DEFAULT_QUEUE_SIZE
) -> AsyncIterator[RawFrame]:
    """把一份录制重新喂给下游，产出契约 `RawFrame`。

    默认 ``speed=None``（全速不等待）：算法开发与排障要的是那串数据，不是重新
    体验一遍 30 分钟。要复现时序相关的行为时传 ``speed=1.0``。

    **产出的 `t_host` 是回放时刻，不是原始采集时刻** —— 见模块文档。任何从
    `t_host` 算出来的指标（到达率、空洞、缺失率）在回放数据上都不成立。

    ## 喂完之后必须有人关掉设备

    `device.samples()` 迭代到 `close()` 推的哨兵为止。回放喂完不会自己关，所以
    这里挂一个后台任务等 `wait_exhausted()` 再关 —— 否则 `async for` 永远挂着。

    ## 消费者慢于喂入时会静默丢样本

    设备层的样本队列满时**丢最旧的**（`WT901Device._offer`）—— 对实时采集这是
    对的（阻塞 BLE 回调会让丢失发生在协议栈里，那里看不见也数不着），但在回放
    上同一条机制会静悄悄地让「与实时一致」不成立。

    wt901 已经挡住了朴素情形：`ReplayTransport._feed` 即使全速也每块
    ``await asyncio.sleep(0)``，正是为了不让整段录制在一个事件循环轮次里喂完。
    这里守的是另一半 —— **下游每帧处理耗时**时，喂入仍会跑在消费前面。

    所以迭代正常结束后检查 `stats.dropped_samples`，非零即抛。消费者中途 `break`
    不算 —— 那是它自己不要了。要处理慢下游可以调大 `queue_size`。
    """
    recording = read_recording(Path(path))
    transport = ReplayTransport(recording, speed=speed)
    device = WT901Device(transport, queue_size=queue_size, output_mode=OutputMode.MOTION)
    await device.open()

    async def _close_when_fed() -> None:
        await transport.wait_exhausted()
        await device.close()

    closer = asyncio.ensure_future(_close_when_fed())
    try:
        async for sample in device.samples():
            yield to_raw_frame(sample)
    finally:
        closer.cancel()
        await device.close()

    dropped = device.stats.dropped_samples
    if dropped:
        raise CaptureError(
            f"回放丢了 {dropped} 个样本：喂入快过消费，样本队列（{queue_size}）"
            "满后丢弃了最旧的样本。这份回放与实时结果不再一致 —— "
            "调大 queue_size，或用 speed=1.0 按原速回放。"
        )


async def replay_session_foot(
    root: Path, session_id: str, foot: str, *, speed: float | None = None
) -> AsyncIterator[RawFrame]:
    """按会话 id 与脚标回放，路径由 `io.session.raw_path` 决定。

    存在的理由是让调用方不必自己拼路径 —— 布局只有一个来源。
    """
    label = _check_foot(foot)
    async for frame in replay_raw_frames(
        raw_path(root, session_id, label), speed=speed
    ):
        yield frame


def payload_equal(left: Iterable[RawFrame], right: Iterable[RawFrame]) -> bool:
    """两串 `RawFrame` 的载荷是否逐帧相等（**不比 `t_host`**）。

    「回放结果与实时处理一致」的可执行定义。不比 `t_host` 不是放松要求，而是
    因为它在回放路径上必然不同 —— 把它算进去，这个判据就永远为假，于是没人会用。
    """
    left_list, right_list = list(left), list(right)
    if len(left_list) != len(right_list):
        return False
    return all(
        a.saturated == b.saturated
        and (a.acc_raw == b.acc_raw).all()
        and (a.gyr_raw == b.gyr_raw).all()
        and (a.ang_raw == b.ang_raw).all()
        for a, b in zip(left_list, right_list, strict=True)
    )


@dataclass(frozen=True, slots=True)
class RecoveryReport:
    """一份可能被崩溃截断的录制，救回了什么。

    `truncated` 为真即表示**有数据没了，而且丢了多少无从得知** —— 残行本身就是
    坏的。所以它不是「小瑕疵」，是「这次会话不完整」的证据，必须一路传到会话
    元数据里去（见 `orchestration.LinkOutcome.recording_truncated`）。
    """

    path: Path
    truncated: bool
    chunks_recovered: int

    @property
    def complete(self) -> bool:
        return not self.truncated

    def snapshot(self) -> dict[str, object]:
        return {
            "path": str(self.path),
            "truncated": self.truncated,
            "chunks_recovered": self.chunks_recovered,
        }


def recover_recording(path: Path) -> tuple[Recording, RecoveryReport]:
    """读一份**可能被崩溃截断**的录制，救回末行之前的全部数据。

    进程被 `kill -9`、掉电、或写到一半被打断时，文件的最后一行必然是残行。
    严格解析会让此前全部完好的数据一起失效 —— 真机上一份 30 分钟 200 Hz 的录制
    截断后，前 633 行完好却一行都取不出来（WT901 RAY-280 的起因就是本仓库报的
    这个复现）。

    ## 为什么这里明写 `tolerate_truncated_tail=True`，而不改上游默认

    上游默认严格是对的：静默容忍会把「这份文件坏了」变成一个没人注意到的事实。
    容忍要由**知道自己在读一份崩溃残留**的调用方明写出来 —— 也就是这里。

    但容忍**不等于**当没事发生：`RecoveryReport.truncated` 必须被消费。只救数据
    不报截断，就正好落回上游想避免的那个坑，只是换了个位置。

    ## 只容忍末行，这个边界是准确的而不是保守的

    上游的实现只容忍**最后一行的 JSON 解析失败**。中间行损坏说明文件被改过或
    拼接过，与崩溃无关，照旧拒绝；末行若能解析出 JSON 但时刻倒退、hex 非法，
    那是损坏不是截断，同样拒绝。

    依据是这个格式的一条性质：数据行形如 ``{"hex":"…","t":…}``，它的任何真前缀
    都不是合法 JSON。所以「末行解析不了」恰好等价于「末行被截断」。
    """
    path = Path(path)
    recording = read_recording(path, tolerate_truncated_tail=True)
    return recording, RecoveryReport(
        path=path,
        truncated=recording.truncated,
        chunks_recovered=len(recording.chunks),
    )


async def replay_recording(
    recording: Recording,
    *,
    speed: float | None = None,
    queue_size: int = DEFAULT_QUEUE_SIZE,
) -> AsyncIterator[RawFrame]:
    """把一份**已经读进来的**录制喂给下游，产出契约 `RawFrame`。

    与 `replay_raw_frames` 的唯一区别是入参已是 `Recording` —— 崩溃恢复要先读
    一次才知道有没有截断，不该为了回放再读第二次。语义（含 `t_host` 是回放时刻、
    丢样本检查）完全相同。
    """
    transport = ReplayTransport(recording, speed=speed)
    device = WT901Device(transport, queue_size=queue_size, output_mode=OutputMode.MOTION)
    await device.open()

    async def _close_when_fed() -> None:
        await transport.wait_exhausted()
        await device.close()

    closer = asyncio.ensure_future(_close_when_fed())
    try:
        async for sample in device.samples():
            yield to_raw_frame(sample)
    finally:
        closer.cancel()
        await device.close()

    dropped = device.stats.dropped_samples
    if dropped:
        raise CaptureError(
            f"回放丢了 {dropped} 个样本：喂入快过消费，样本队列（{queue_size}）"
            "满后丢弃了最旧的样本。这份回放与实时结果不再一致 —— "
            "调大 queue_size，或用 speed=1.0 按原速回放。"
        )
