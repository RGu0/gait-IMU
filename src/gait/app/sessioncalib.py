"""P-07 会话标定的现场编排（RAY-208 `p07-calibration-wiring`）。

`gait/calib/` 早已能从一段静立样本解出零偏与重力方向、从一段直线行走解出安装旋转、
比两次重力方向判松动 —— 但那些都是**纯函数**，产品流程里没有任何东西把现场样本递给
它们。本模块就是那一段递送：从设备源的两条字节通道里收样本，按三步往前推，把结论
与耗时交给 service。

## 三步，由样本推进，不由界面的钟推进

| 步 | 什么时候算完 | 超时 |
|---|---|---|
| `stand` | 双足都收满 5 s，且 `calibrate_still` 在里面找得到 5 s 静止 | 静立失败 |
| `walk` | 设备源的计步（左右合计）比本步开始时多出 10 步 | 照样往下走，交给方向估计判 |
| `settle` | 走完后的样本里双足都找得到 ≥ 2 s 的静止 | 站定失败 |

界面只轮询、只显示。若由界面倒计时、到点再调一次「算吧」，倒计时与数据会对不上：
BLE 链路晚到半秒，就会拿 4.5 s 的样本去做一个要求 5 s 的标定，然后报一个操作员
毫无办法的失败。

`settle` 是用户 2026-10-13 定的：松动的判据是「静止段重力方向相对标定值突变 > 5°」，
P-07 原本只有一个静止段，没有东西可比。走完 10 步后再站定 3 秒，正好量出「走动有没有
把绑带晃松」—— 而那正是绑带会松的方式。

## 耗时是这一步的另一半交付

验收里的「标定链中位耗时 ≤ 90 s」是真人操作时长，合成数据量不出来（用户 2026-10-13
裁定：本 scope 只埋点）。所以每一次尝试的每一步都记时长，随 `calib_snapshot` 落盘；
外场采回来的会话目录直接就能算中位数，不需要再拿秒表。

`chain_seconds` 从**第一次** `begin` 算到通过 —— 重试也是操作员花掉的时间。

## 它不碰的东西

* 不改标定算法：判据都在 `gait/calib/`，这里只调用；
* 不进分析链：报告仍注明「未使用标定参数」（用户 2026-10-13 裁定，另立 scope）；
* 200 Hz 原始样本不出 IPC（R-1）：给界面的只有秒数、步数与结论。
"""

from __future__ import annotations

import math
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Final

import numpy as np
from wt901.models import ImuSample
from wt901.protocol.frames import FrameDecoder, FrameFlag
from wt901.protocol.units import accel_to_m_s2, angular_velocity_to_rad_s

from gait.calib.still import (
    MIN_STILL_SECONDS,
    CalibrationError,
    LoosenessCheck,
    StillCalibration,
    calibrate_still,
    check_looseness,
    verdict,
)
from gait.calib.walk import MountingCalibration, estimate_mounting
from gait.core.alignment import AlignmentError, find_still_window
from gait.device.adapter import to_raw_frame
from gait.device.footseries import NOMINAL_FS

FEET: Final[tuple[str, str]] = ("L", "R")
_SIDE_TEXT: Final[dict[str, str]] = {"L": "左脚", "R": "右脚"}

#: 静立步要收满的时长。与 `calibrate_still` 的下限是同一个数，不另立。
STAND_SECONDS: Final[float] = MIN_STILL_SECONDS
#: 直线行走的步数（左右合计），PRD §6.1。
WALK_STEPS: Final[int] = 10
#: 走完后站定的时长（界面显示的数），与其中至少要找得到的静止长度。
SETTLE_SECONDS: Final[float] = 3.0
SETTLE_MIN_STILL_SECONDS: Final[float] = 2.0

#: 超时。静立给足余量：受试者站好要几秒。行走超时后不判失败，而是拿已有的样本去解
#: 方向 —— 计步器漏数不该让一段好好的直线行走作废，方向估计自己会说它够不够。
STAND_TIMEOUT_S: Final[float] = 20.0
WALK_TIMEOUT_S: Final[float] = 30.0
SETTLE_TIMEOUT_S: Final[float] = 10.0

PHASE_STAND: Final[str] = "stand"
PHASE_WALK: Final[str] = "walk"
PHASE_SETTLE: Final[str] = "settle"
PHASE_PASSED: Final[str] = "passed"
PHASE_FAILED: Final[str] = "failed"
PHASE_CANCELLED: Final[str] = "cancelled"
RUNNING_PHASES: Final[frozenset[str]] = frozenset({PHASE_STAND, PHASE_WALK, PHASE_SETTLE})


@dataclass(frozen=True)
class Failure:
    """一次失败。`code` 进 contract.json；`message` / `action` 是给操作员的成品句子。"""

    reason: str
    code: str
    message: str
    action: str

    def snapshot(self) -> dict[str, str]:
        return {
            "reason": self.reason,
            "code": self.code,
            "message": self.message,
            "action": self.action,
        }


def _sides(feet: list[str]) -> str:
    return "和".join(_SIDE_TEXT[foot] for foot in feet)


def _no_data(feet: list[str]) -> Failure:
    return Failure(
        reason="no_data",
        code="E-CAL-3004",
        message=f"没有收到{_sides(feet)}模块足够的数据。",
        action="请确认模块已开机、离电脑不远，然后重试。",
    )


def _moved(feet: list[str]) -> Failure:
    return Failure(
        reason="moved",
        code="E-CAL-3002",
        message=f"静立时{_sides(feet)}有晃动，没能完成静立校准。",
        action="请让受试者双脚站稳、保持不动，然后重试。",
    )


def _unsettled(feet: list[str]) -> Failure:
    return Failure(
        reason="unsettled",
        code="E-CAL-3002",
        message=f"走完后{_sides(feet)}没有站定。",
        action="请让受试者走完 10 步后原地站定约 3 秒，然后重试。",
    )


def _walk(feet: list[str]) -> Failure:
    return Failure(
        reason="walk",
        code="E-CAL-3003",
        message=f"这一段行走没能用来校准{_sides(feet)}的方向。",
        action="请让受试者沿直线、按平时的速度走 10 步，然后重试。",
    )


def _loose(checks: dict[str, LoosenessCheck]) -> Failure:
    loose = [check for check in checks.values() if check.loose]
    return Failure(
        reason="loose",
        code="E-WEAR-2001",
        # 句子出自 `LoosenessCheck.message` —— 「动作语言」只在 calib 里写一次。
        message="".join(check.message for check in loose),
        action="绑紧后点「重试」，静立与行走会重新做一遍。",
    )


class FootBuffer:
    """一只脚的字节收集器。**回调里只追加字节**，解帧放到轮询时做。

    回调在 BLE 事件循环线程上跑；在那里解帧会把一次轮询的计算量挪进链路热路径。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending: list[bytes] = []
        self._decoder = FrameDecoder()
        self._acc: list[np.ndarray] = []
        self._gyr: list[np.ndarray] = []

    def receive(self, data: bytes) -> None:
        with self._lock:
            self._pending.append(bytes(data))

    def _drain(self) -> None:
        with self._lock:
            pending, self._pending = self._pending, []
        for chunk in pending:
            for frame in self._decoder.feed(chunk):
                if frame.flag is not FrameFlag.DATA:
                    continue  # 寄存器应答混在流里，不是样本
                # 与落盘回读同一条解帧路径（`device/footseries.read_recorded_frames`），
                # 码值切片因此只在 `device/adapter.to_raw_frame` 写一次。
                sample = ImuSample.from_frame(
                    frame, device_id="calibration", t_host=0.0, seq=len(self._acc)
                )
                raw = to_raw_frame(sample)
                self._acc.append(raw.acc_raw)
                self._gyr.append(raw.gyr_raw)

    def count(self) -> int:
        self._drain()
        return len(self._acc)

    def si(self, start: int, end: int | None = None) -> tuple[np.ndarray, np.ndarray]:
        """`[start, end)` 这一段的比力与角速度，SI，模块体系。"""
        self._drain()
        acc_rows, gyr_rows = self._acc[start:end], self._gyr[start:end]
        if not acc_rows:
            empty = np.zeros((0, 3), dtype=np.float64)
            return empty, empty
        to_acc = np.vectorize(accel_to_m_s2, otypes=[np.float64])
        to_gyr = np.vectorize(angular_velocity_to_rad_s, otypes=[np.float64])
        return to_acc(np.stack(acc_rows)), to_gyr(np.stack(gyr_rows))


@dataclass
class Attempt:
    """一次尝试的计时。三步的时长分开记，失败也记 —— 失败花掉的也是操作员的时间。"""

    number: int
    began_at: float
    phase_started_at: float
    durations: dict[str, float] = field(default_factory=dict)
    outcome: str | None = None
    ended_at: float | None = None

    def snapshot(self) -> dict[str, Any]:
        return {
            "attempt": self.number,
            "outcome": self.outcome,
            "stand_seconds": _round(self.durations.get(PHASE_STAND)),
            "walk_seconds": _round(self.durations.get(PHASE_WALK)),
            "settle_seconds": _round(self.durations.get(PHASE_SETTLE)),
            "total_seconds": _round(
                None if self.ended_at is None else self.ended_at - self.began_at
            ),
        }


def _round(value: float | None) -> float | None:
    return None if value is None else round(float(value), 3)


def _finite(value: float) -> float | None:
    """JSON 没有 inf。主导度为 inf 表示横向方差可忽略（最好的情形），落盘记 `None`。"""
    return float(value) if math.isfinite(value) else None


class CalibrationRun:
    """一次 P-07 的标定（含重试）。由 service 持有，靠 `poll()` 往前推。

    `step_total` 取设备源的计步（左右合计），`clock` 取单调时钟；两者都由 service
    注入，测试因此不必等真实的秒数。
    """

    def __init__(
        self,
        *,
        clock: Callable[[], float],
        step_total: Callable[[], int],
        fs: float = NOMINAL_FS,
    ) -> None:
        self._clock = clock
        self._step_total = step_total
        self.fs = float(fs)
        self.buffers: dict[str, FootBuffer] = {foot: FootBuffer() for foot in FEET}
        self.attempts: list[Attempt] = []
        self.first_began_at: float | None = None
        self.passed_at: float | None = None
        self.phase: str = PHASE_CANCELLED
        self.failure: Failure | None = None
        self.stills: dict[str, StillCalibration] = {}
        self.mountings: dict[str, MountingCalibration] = {}
        self.looseness: dict[str, LoosenessCheck] = {}
        self._marks: dict[str, int] = {}
        self._walk_marks: dict[str, int] = {}
        self._walk_steps_at: int = 0

    # ── 生命周期 ─────────────────────────────────────────────────────────

    def begin(self) -> None:
        """开一次尝试。样本缓冲**不清**：起点按下标记，旧样本只是不再被看。"""
        now = self._clock()
        if self.first_began_at is None:
            self.first_began_at = now
        self.attempts.append(Attempt(len(self.attempts) + 1, now, now))
        self.failure = None
        self.stills, self.mountings, self.looseness = {}, {}, {}
        self.passed_at = None
        self._enter(PHASE_STAND, now)

    def cancel(self) -> None:
        if self.phase in RUNNING_PHASES:
            self._finish(PHASE_CANCELLED, "cancelled", self._clock())

    @property
    def running(self) -> bool:
        return self.phase in RUNNING_PHASES

    @property
    def passed(self) -> bool:
        return self.phase == PHASE_PASSED

    def _enter(self, phase: str, now: float) -> None:
        self.phase = phase
        self.attempts[-1].phase_started_at = now
        self._marks = {foot: buffer.count() for foot, buffer in self.buffers.items()}
        if phase == PHASE_WALK:
            self._walk_steps_at = self._step_total()

    def _close_phase(self, now: float) -> None:
        attempt = self.attempts[-1]
        attempt.durations[self.phase] = now - attempt.phase_started_at

    def _finish(self, phase: str, outcome: str, now: float) -> None:
        self._close_phase(now)
        attempt = self.attempts[-1]
        attempt.outcome = outcome
        attempt.ended_at = now
        self.phase = phase
        if phase == PHASE_PASSED:
            self.passed_at = now

    def _fail(self, failure: Failure, now: float) -> None:
        self.failure = failure
        self._finish(PHASE_FAILED, failure.reason, now)

    # ── 推进 ─────────────────────────────────────────────────────────────

    def _collected(self) -> dict[str, int]:
        return {foot: buffer.count() - self._marks[foot] for foot, buffer in self.buffers.items()}

    def poll(self) -> dict[str, Any]:
        """推进一拍并报告现状。给界面的东西全在返回值里，**不含任何样本**。"""
        now = self._clock()
        if self.phase == PHASE_STAND:
            self._poll_stand(now)
        elif self.phase == PHASE_WALK:
            self._poll_walk(now)
        elif self.phase == PHASE_SETTLE:
            self._poll_settle(now)
        return self.status(now)

    def _elapsed(self, now: float) -> float:
        return now - self.attempts[-1].phase_started_at

    def _poll_stand(self, now: float) -> None:
        collected = self._collected()
        need = math.ceil(STAND_SECONDS * self.fs)
        timed_out = self._elapsed(now) >= STAND_TIMEOUT_S
        short = [foot for foot in FEET if collected[foot] < need]
        if short:
            if timed_out:
                self._fail(_no_data(short), now)
            return
        stills: dict[str, StillCalibration] = {}
        unstill: list[str] = []
        for foot in FEET:
            acc, gyr = self.buffers[foot].si(self._marks[foot])
            try:
                stills[foot] = calibrate_still(foot, acc, gyr, self.fs)
            except (CalibrationError, AlignmentError):
                unstill.append(foot)
        if unstill:
            if timed_out:
                self._fail(_moved(unstill), now)
            return
        self.stills = stills
        self._close_phase(now)
        self._enter(PHASE_WALK, now)

    def _walk_steps(self) -> int:
        return max(0, self._step_total() - self._walk_steps_at)

    def _poll_walk(self, now: float) -> None:
        if self._walk_steps() < WALK_STEPS and self._elapsed(now) < WALK_TIMEOUT_S:
            return
        # 行走段从本步起点一直延续到「站定」里找到的静止为止，见 `_poll_settle`。
        self._walk_marks = dict(self._marks)
        self._close_phase(now)
        self._enter(PHASE_SETTLE, now)

    def _poll_settle(self, now: float) -> None:
        collected = self._collected()
        timed_out = self._elapsed(now) >= SETTLE_TIMEOUT_S
        need = math.ceil(SETTLE_SECONDS * self.fs)
        short = [foot for foot in FEET if collected[foot] < need]
        if short:
            if timed_out:
                self._fail(_no_data(short), now)
            return
        minimum = round(SETTLE_MIN_STILL_SECONDS * self.fs)
        windows: dict[str, tuple[int, int]] = {}
        unsettled: list[str] = []
        for foot in FEET:
            acc, gyr = self.buffers[foot].si(self._marks[foot])
            try:
                start, end = find_still_window(
                    acc, gyr, self.fs, minimum_seconds=SETTLE_MIN_STILL_SECONDS
                )
            except AlignmentError:
                unsettled.append(foot)
                continue
            if end - start < minimum:  # pragma: no cover - find_still_window 已保证
                unsettled.append(foot)
                continue
            windows[foot] = (start, end)
        if unsettled:
            if timed_out:
                self._fail(_unsettled(unsettled), now)
            return
        self._conclude(windows, now)

    def _conclude(self, windows: dict[str, tuple[int, int]], now: float) -> None:
        mountings: dict[str, MountingCalibration] = {}
        undirected: list[str] = []
        checks: dict[str, LoosenessCheck] = {}
        for foot in FEET:
            settle_from = self._marks[foot]
            start, end = windows[foot]
            walk_acc, walk_gyr = self.buffers[foot].si(self._walk_marks[foot], settle_from + start)
            try:
                mountings[foot] = estimate_mounting(self.stills[foot], walk_acc, walk_gyr)
            except CalibrationError:
                undirected.append(foot)
            still_acc, _ = self.buffers[foot].si(settle_from + start, settle_from + end)
            checks[foot] = check_looseness(self.stills[foot], still_acc)
        self.looseness = checks
        # 松动先于方向：绑带松了，方向解出来也不可信；而操作员该做的是先绑紧。
        outcome = verdict(self.stills, looseness=checks)
        if not outcome.passed:
            self._fail(_loose(checks), now)
            return
        if undirected:
            self._fail(_walk(undirected), now)
            return
        self.mountings = mountings
        self._finish(PHASE_PASSED, "passed", now)

    # ── 出境 ─────────────────────────────────────────────────────────────

    def status(self, now: float | None = None) -> dict[str, Any]:
        """给界面的现状。**只有秒数、步数与结论** —— 原始样本不出 IPC（R-1）。"""
        now = self._clock() if now is None else now
        payload: dict[str, Any] = {
            "phase": self.phase,
            "attempt": len(self.attempts),
            "standTotal": STAND_SECONDS,
            "walkTotal": WALK_STEPS,
            "settleTotal": SETTLE_SECONDS,
        }
        if self.phase == PHASE_STAND:
            seconds = min(self._collected().values()) / self.fs
            payload["standSeconds"] = round(min(seconds, STAND_SECONDS), 1)
        elif self.phase == PHASE_WALK:
            payload["walkSteps"] = min(self._walk_steps(), WALK_STEPS)
        elif self.phase == PHASE_SETTLE:
            seconds = min(self._collected().values()) / self.fs
            payload["settleSeconds"] = round(min(seconds, SETTLE_SECONDS), 1)
        if self.failure is not None:
            payload["failure"] = self.failure.snapshot()
        if self.passed and self.first_began_at is not None and self.passed_at is not None:
            payload["chainSeconds"] = round(self.passed_at - self.first_began_at, 1)
        return payload

    def timing(self) -> dict[str, Any]:
        chain = (
            None
            if self.passed_at is None or self.first_began_at is None
            else self.passed_at - self.first_began_at
        )
        return {
            "chain_seconds": _round(chain),
            "attempts": [attempt.snapshot() for attempt in self.attempts],
        }

    def snapshot(self) -> dict[str, Any]:
        """进 `SessionMeta.calib_snapshot`。形状见《05 数据格式规范》§3.5。"""
        record: dict[str, Any] = {
            "state": self.phase,
            "issue": "RAY-208",
            "fs_assumed": self.fs,
            # 用户 2026-10-13 裁定：本 scope 不进分析链。写明，免得读 meta 的人以为
            # 报告里的数是用这组参数补偿过的。
            "applied_to_analysis": False,
            "timing": self.timing(),
        }
        if self.failure is not None:
            record["failure"] = self.failure.snapshot()
        if self.passed:
            record["feet"] = {
                foot: {
                    "still": _without_foot(self.stills[foot].snapshot()),
                    "mounting": _mounting_record(self.mountings[foot]),
                    "looseness": _without_foot(self.looseness[foot].snapshot()),
                }
                for foot in FEET
            }
        return record


def _without_foot(record: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in record.items() if key != "foot"}


def _mounting_record(mounting: MountingCalibration) -> dict[str, Any]:
    record = _without_foot(mounting.snapshot())
    record["principal_ratio"] = _finite(mounting.principal_ratio)
    record["peak_asymmetry"] = _finite(mounting.peak_asymmetry)
    return record
