"""预览版的设备源：合成步行与会话回放（RAY-493 `sidecar-preview-runtime`）。

## 它为什么存在

Preview 要在**没有两只模块在手上**的机器上把整条流程走通：自检 → 采集 → 结论 →
报告。`StubDeviceSource` 推的是结构合法的随机字节（`sources.synthetic_frame`），
落盘路径是真的，但从那些字节算不出任何步态 —— `reportFor` 必然回 `E-QLT-5003`。

这里补的是**字节的内容**，不是判定：

* `synthetic`：`validate/synthetic.generate_dual_walk` 的双足步行（真值步长 1.3 m、
  步频 108 步/分），换算成 0x55 0x61 帧；
* `replay`：把一份已落盘会话的 `raw/left.raw` / `raw/right.raw` 按原到达时刻重新推一遍。

读数之后的每一步（写盘、重算、报告）照旧走真实代码。**两者都不是实测**，所以
`provenance()` 的 `hardware` 恒为 `False`，报告据此打上「演示数据」标注 —— 一份
合成步行出的报告与一份真机报告在版面上长得一模一样，不说出来就分不开。
"""

from __future__ import annotations

import struct
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
from wt901.protocol.units import (
    ACCEL_FULL_SCALE_G,
    GYRO_FULL_SCALE_DPS,
    INT16_FULL_SCALE,
    STANDARD_GRAVITY,
)
from wt901.recording import read_recording

from gait.app.sources import StubDeviceSource
from gait.io.session import raw_path
from gait.validate.synthetic import NoiseModel, WalkSpec, generate_dual_walk

#: 一段字节：`(相对到达时刻 s, 载荷)`。形状与 `wt901.recording.RecordedChunk` 对齐。
Chunk = tuple[float, bytes]
Chunks = dict[str, list[Chunk]]

SAMPLE_RATE_HZ = 200.0

#: 每段打包多少帧。逐帧推要 200 次/秒 × 2 只脚的线程唤醒；10 帧一段是 20 次/秒，
#: 与真机 BLE Notify 一次带多帧的形态也更接近。
DEFAULT_CHUNK_FRAMES = 10

#: 合成步行的器件量级噪声。恒为 0 的变异系数在真实数据里不存在 —— 取值与
#: `cli/mvp.py` 同量级（原先写在 `tests/test_app_cycles_pipeline.py` 里）。
ACCEL_NOISE_DENSITY = 1.5e-3
GYRO_NOISE_DENSITY = 3.0e-4

#: 演示用的步频（步/秒，双足合计）：108 步/分。只用于 P-08 的计步显示，不进任何指标。
DEMO_STEPS_PER_SECOND = 1.8

DEMO_NOTE = "演示数据（合成/回放），非实测。"


def to_counts(acc: np.ndarray, gyr: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """SI → int16 码值。**换算常数只从 wt901 取**，不在这里另抄一份。"""
    a = acc / (ACCEL_FULL_SCALE_G * STANDARD_GRAVITY) * INT16_FULL_SCALE
    g = np.degrees(gyr) / GYRO_FULL_SCALE_DPS * INT16_FULL_SCALE
    return (
        np.clip(a, -32768, 32767).astype(np.int16),
        np.clip(g, -32768, 32767).astype(np.int16),
    )


def frames_from_synthetic(
    duration_s: float, seed: int = 0, *, chunk_frames: int = DEFAULT_CHUNK_FRAMES
) -> Chunks:
    """一段合成双足步行，按 200 Hz 打成 0x55 0x61 帧、再分段。

    每段的 `t` 取段内**最后一帧**的时刻：字节是攒够一段才到的，取首帧时刻会让回放
    提前把还没「采到」的样本推出去。
    """
    if chunk_frames < 1:
        raise ValueError(f"chunk_frames 至少为 1，收到 {chunk_frames}")
    dual = generate_dual_walk(
        WalkSpec(duration_s=float(duration_s)),
        noise=NoiseModel(
            accel_density=ACCEL_NOISE_DENSITY,
            gyro_density=GYRO_NOISE_DENSITY,
            seed=seed,
        ),
    )
    chunks: Chunks = {}
    for label, (series, _truth) in dual.items():
        acc, gyr = to_counts(series.acc, series.gyr)
        frames = [
            b"\x55\x61" + struct.pack("<9h", *acc[index], *gyr[index], 0, 0, 0)
            for index in range(len(acc))
        ]
        chunks[label] = [
            (
                round((min(start + chunk_frames, len(frames)) - 1) / SAMPLE_RATE_HZ, 6),
                b"".join(frames[start : start + chunk_frames]),
            )
            for start in range(0, len(frames), chunk_frames)
        ]
    return chunks


def frames_from_session(session_dir: Path) -> Chunks:
    """读回一份已落盘会话的双足录制，保留原到达时刻。

    路径经 `io.session.raw_path` 定，不在这里另拼一遍。末行残行容忍 —— 与
    `device/footseries.read_recorded_frames` 同一口径：掉电的会话照样能回放到断点。
    """
    directory = Path(session_dir)
    chunks: Chunks = {}
    for label in ("L", "R"):
        recording = read_recording(
            raw_path(directory.parent, directory.name, label),
            tolerate_truncated_tail=True,
        )
        chunks[label] = [(chunk.t, chunk.data) for chunk in recording.chunks]
    return chunks


@dataclass
class ReplayDeviceSource(StubDeviceSource):
    """按时刻把预先备好的字节推进双足 `MemoryTransport`。

    读数（电量、到达率、身份）沿用 stub 的默认值 —— 回放拿不到当时的电量，编一个
    「当时的读数」比承认用的是占位读数更糟。**出厂标定照样按参数库判**，于是在没有
    参数库的预览机上它会失败，只能由预览策略显式放行（见 `service.PreviewPolicy`）。
    """

    chunks: Chunks = field(default_factory=dict)
    #: 生成字节很贵（190 s 合成步行约 2.4 s）时给一个工厂：构造时在后台线程里备好，
    #: 进程入口不必为它阻塞，第一条 `describe` 也不必等。
    loader: Callable[[], Chunks] | None = None
    label: str = "replay"
    speed: float = 1.0
    clock: Callable[[], float] = time.monotonic
    _ready: threading.Event = field(default_factory=threading.Event)
    _stream_started_at: float | None = None
    _stream_seconds: float = 0.0

    def __post_init__(self) -> None:
        if self.speed <= 0:
            raise ValueError(f"speed 必须为正，收到 {self.speed}")
        if self.loader is None:
            self._ready.set()
            return
        loader = self.loader

        def load() -> None:
            try:
                self.chunks = loader()
            finally:
                # 失败也要放行等待者：卡住的采集比一次空的采集更难查。
                self._ready.set()

        threading.Thread(target=load, name="gait-replay-load", daemon=True).start()

    def begin_stream(self) -> None:
        if self._feeder is not None:
            return
        self._stop.clear()
        self.transports()

        def pump() -> None:
            self._ready.wait()
            # 只按 `(t, label)` 排，靠排序的稳定性保住每只脚的录制顺序（RAY-538）。
            # 并列的 t 很常见：Windows + Python 3.12 的 monotonic 粒度约 15.6 ms，
            # 一个刻度里能录下十几段。键里带上 `data` 时，并列段按载荷字节重排，
            # 回放出的是原会话的一个排列 —— 步态被打乱，报告有时还算得出来。
            timeline = sorted(
                (
                    (t, label, data)
                    for label, items in self.chunks.items()
                    for t, data in items
                ),
                key=lambda item: (item[0], item[1]),
            )
            self._stream_seconds = timeline[-1][0] if timeline else 0.0
            start = self.clock()
            self._stream_started_at = start
            for t, label, data in timeline:
                delay = start + t / self.speed - self.clock()
                if delay > 0 and self._stop.wait(delay):
                    return
                if self._stop.is_set():
                    return
                self.feed(label, data)

        self._feeder = threading.Thread(target=pump, name="gait-replay-feed", daemon=True)
        self._feeder.start()

    def end_stream(self) -> None:
        super().end_stream()
        self._stream_started_at = None

    def step_counts(self) -> dict[str, int]:
        """**装饰性的**计步：按已推送时长 × 108 步/分估。

        P-08 只拿它显示「走了多少步」，任何指标都不从这里取 —— 报告里的步数来自
        重算链。推完之后停在总时长上，不会一直往上涨。
        """
        started = self._stream_started_at
        if started is None:
            return dict(self.steps)
        elapsed = min((self.clock() - started) * self.speed, self._stream_seconds)
        per_foot = int(max(elapsed, 0.0) * DEMO_STEPS_PER_SECOND / 2)
        return {"L": per_foot, "R": per_foot}

    def provenance(self) -> dict[str, Any]:
        return {"source": self.label, "hardware": False, "note": DEMO_NOTE}


@dataclass
class UnavailableDeviceSource(StubDeviceSource):
    """要真设备、但真设备那条路起不来（BLE 模块缺席或初始化失败）。

    它**不退回 stub**：退回去自检会全绿，操作员以为模块连上了。这里的读数全是
    「读不到」，于是 `runPreflight` 如实给出 `E-BLE-1001`，原因写进 provenance。
    """

    reason: str = "真设备通路不可用。"

    def __post_init__(self) -> None:
        self.batteries = {"L": None, "R": None}
        self.arrival = {"L": 0.0, "R": 0.0}
        self.links = {"L": "bad", "R": "bad"}

    def provenance(self) -> dict[str, Any]:
        return {"source": "unavailable", "hardware": False, "note": self.reason}
