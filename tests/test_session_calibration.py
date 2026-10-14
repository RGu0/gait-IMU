"""RAY-208 `p07-calibration-wiring`：P-07 会话标定接进 sidecar。

`gait/calib/` 的判据已由 `still-calibration` / `walk-calibration` 两个 scope 验过；这里验
的是**接线**：现场字节能不能按「静立 → 行走 → 站定」推到一个结论，结论与耗时有没有
真的落进会话元数据，以及失败时给操作员的话是不是动作语言。

松动的检出与不误报照旧**成对**验：只验检出，把阈值调到 0 也能全绿。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np
import pytest

from gait.app import protocol
from gait.app.replay import (
    CALIBRATION_STAND_S,
    CALIBRATION_WALK_S,
    DEMO_STEPS_PER_SECOND,
    SAMPLE_RATE_HZ,
    ReplayDeviceSource,
    _chunked,
    calibration_clip,
)
from gait.app.service import TerminalService
from gait.app.sessioncalib import (
    SETTLE_TIMEOUT_S,
    STAND_TIMEOUT_S,
    WALK_STEPS,
    CalibrationRun,
)
from gait.app.sources import StubDeviceSource
from gait.io.session import IDENTITY_KEY_FRAGMENTS, read_meta, session_directory
from gait.validate.synthetic import NoiseModel, WalkSpec, generate_dual_walk

#: 给操作员的话里不许出现的算法词（与 `test_still_calibration` 同一份口径）。
JARGON = ("安装角", "收敛", "零偏", "四元数", "协方差", "主成分", "特征值", "重力方向")


# ── 片段：静立 → 走 → 站定，三段都可单独改 ──────────────────────────────────


def _segments(seed: int = 0) -> dict[str, dict[str, tuple[np.ndarray, np.ndarray]]]:
    dual = generate_dual_walk(
        WalkSpec(
            duration_s=CALIBRATION_STAND_S + CALIBRATION_WALK_S,
            still_lead_s=CALIBRATION_STAND_S,
        ),
        noise=NoiseModel(accel_density=1.5e-3, gyro_density=3.0e-4, seed=seed),
    )
    lead = round(CALIBRATION_STAND_S * SAMPLE_RATE_HZ)
    out = {}
    for label, (series, _truth) in dual.items():
        out[label] = {
            "stand": (series.acc[:lead], series.gyr[:lead]),
            "walk": (series.acc[lead:], series.gyr[lead:]),
        }
    return out


def _rotate_x(vectors: np.ndarray, degrees: float) -> np.ndarray:
    angle = np.radians(degrees)
    cos, sin = np.cos(angle), np.sin(angle)
    matrix = np.array([[1, 0, 0], [0, cos, -sin], [0, sin, cos]])
    return vectors @ matrix.T


def _clip(*, stand=None, walk=None, settle=None) -> dict:
    """按段拼一段片段。每段可替换为 `(foot, acc, gyr) -> (acc, gyr)` 的变换。"""
    parts = _segments()
    chunks = {}
    for label, segs in parts.items():
        stand_acc, stand_gyr = segs["stand"]
        walk_acc, walk_gyr = segs["walk"]
        settle_acc, settle_gyr = stand_acc[: round(6 * SAMPLE_RATE_HZ)], stand_gyr[: round(6 * SAMPLE_RATE_HZ)]
        if stand is not None:
            stand_acc, stand_gyr = stand(label, stand_acc, stand_gyr)
        if walk is not None:
            walk_acc, walk_gyr = walk(label, walk_acc, walk_gyr)
        if settle is not None:
            settle_acc, settle_gyr = settle(label, settle_acc, settle_gyr)
        acc = np.concatenate([stand_acc, walk_acc, settle_acc])
        gyr = np.concatenate([stand_gyr, walk_gyr, settle_gyr])
        chunks[label] = _chunked(acc, gyr, 10)
    return chunks


class _Feed:
    """按假时钟把片段推给一次标定。真实的秒数一秒都不等。"""

    def __init__(self, chunks: dict, *, steps_per_second: float = DEMO_STEPS_PER_SECOND) -> None:
        self.now = 0.0
        self.timeline = sorted(
            ((t, label, data) for label, items in chunks.items() for t, data in items),
            key=lambda item: (item[0], item[1]),
        )
        self.index = 0
        self.offset = 0.0
        self.steps_per_second = steps_per_second
        self.run = CalibrationRun(clock=lambda: self.now, step_total=self.steps)

    def steps(self) -> int:
        return int((self.now - self.offset) * self.steps_per_second)

    def restart(self) -> None:
        """重试：片段从头再推一遍（同回放源的行为）。"""
        self.index = 0
        self.offset = self.now

    def drive(self, limit: float = 60.0, step: float = 0.25) -> dict:
        status = self.run.status()
        stop = self.now + limit
        while self.now < stop:
            self.now += step
            while (
                self.index < len(self.timeline)
                and self.timeline[self.index][0] + self.offset <= self.now
            ):
                _t, label, data = self.timeline[self.index]
                self.run.buffers[label].receive(data)
                self.index += 1
            status = self.run.poll()
            if not self.run.running:
                break
        return status


def _passing() -> _Feed:
    feed = _Feed(calibration_clip())
    feed.run.begin()
    return feed


# ── 通过：三步都推得动，结论与耗时都在 ────────────────────────────────────


def test_the_demo_clip_passes_all_three_steps() -> None:
    feed = _passing()
    seen: list[str] = []
    while feed.run.running:
        seen.append(feed.drive(limit=0.25)["phase"])
    assert feed.run.passed, feed.run.failure
    # 三步按顺序出现过 —— 不是一步跳到通过。
    order = [phase for index, phase in enumerate(seen) if index == 0 or seen[index - 1] != phase]
    assert order[:3] == ["stand", "walk", "settle"]
    assert order[-1] == "passed"


def test_mounting_and_looseness_come_out_right_on_the_demo_clip() -> None:
    """合成数据本身就在足部系：安装旋转应接近单位阵，松动应接近 0°。"""
    feed = _passing()
    feed.drive()
    for foot in ("L", "R"):
        rotation = feed.run.mountings[foot].rotation
        assert np.allclose(rotation, np.eye(3), atol=1e-2), foot
        assert feed.run.looseness[foot].deviation_deg < 0.5


def test_timing_is_recorded_per_step_and_for_the_chain() -> None:
    feed = _passing()
    feed.drive()
    timing = feed.run.timing()
    (attempt,) = timing["attempts"]
    assert attempt["outcome"] == "passed"
    for key in ("stand_seconds", "walk_seconds", "settle_seconds"):
        assert attempt[key] is not None and attempt[key] > 0
    parts = attempt["stand_seconds"] + attempt["walk_seconds"] + attempt["settle_seconds"]
    assert attempt["total_seconds"] == pytest.approx(parts, abs=1e-6)
    assert timing["chain_seconds"] == pytest.approx(attempt["total_seconds"])
    # 静立按样本推进：5 s 的样本一到就走，不会提前。
    assert attempt["stand_seconds"] >= 5.0


def test_walk_ends_on_steps_not_on_a_timer() -> None:
    """计步到 10 就进站定；计步更快，行走这一步就更短。"""
    slow, fast = _Feed(calibration_clip(), steps_per_second=1.0), _Feed(calibration_clip(), steps_per_second=4.0)
    for feed in (slow, fast):
        feed.run.begin()
        feed.drive()
        assert feed.run.passed, feed.run.failure
    walk = lambda feed: feed.run.attempts[0].durations["walk"]
    assert walk(fast) < walk(slow)
    assert walk(slow) == pytest.approx(WALK_STEPS / 1.0, abs=0.3)


# ── 松动：检出与不误报成对 ────────────────────────────────────────────────


@pytest.mark.parametrize("degrees", [6.0, 8.0, 15.0])
def test_looseness_after_the_walk_blocks(degrees: float) -> None:
    """走完后重力方向偏了 > 5° —— 绑带被走松了。必须在 P-07 当场拦下。"""
    loosened = lambda foot, acc, gyr: (
        (_rotate_x(acc, degrees), gyr) if foot == "R" else (acc, gyr)
    )
    feed = _Feed(_clip(settle=loosened))
    feed.run.begin()
    status = feed.drive()
    assert status["phase"] == "failed"
    failure = status["failure"]
    assert failure["reason"] == "loose"
    assert failure["code"] == "E-WEAR-2001"
    assert "右脚" in failure["message"] and "左脚" not in failure["message"]
    assert feed.run.looseness["R"].deviation_deg == pytest.approx(degrees, abs=0.5)


@pytest.mark.parametrize("degrees", [1.0, 3.0, 4.5])
def test_small_settling_changes_do_not_block(degrees: float) -> None:
    """不误报：≤ 5° 的变化不拦。只有上一条时，把阈值调到 0 也能全绿。"""
    shifted = lambda foot, acc, gyr: (_rotate_x(acc, degrees), gyr)
    feed = _Feed(_clip(settle=shifted))
    feed.run.begin()
    feed.drive()
    assert feed.run.passed, feed.run.failure


# ── 其余失败：各有各的话 ────────────────────────────────────────────────


def test_no_data_names_the_silent_foot() -> None:
    clip = calibration_clip()
    feed = _Feed({"L": clip["L"], "R": []})
    feed.run.begin()
    status = feed.drive(limit=STAND_TIMEOUT_S + 1)
    assert status["failure"]["reason"] == "no_data"
    assert status["failure"]["code"] == "E-CAL-3004"
    assert "右脚" in status["failure"]["message"]
    assert "左脚" not in status["failure"]["message"]


def test_moving_during_the_stand_fails_after_the_timeout() -> None:
    """静立时一直在动：等到超时才判，而不是第一拍没找到静止就判。"""
    walking = lambda foot, acc, gyr: _segments()[foot]["walk"]
    feed = _Feed(_clip(stand=walking))
    feed.run.begin()
    status = feed.drive(limit=STAND_TIMEOUT_S + 1)
    assert status["failure"]["reason"] == "moved"
    assert status["failure"]["code"] == "E-CAL-3002"
    assert feed.run.attempts[0].durations["stand"] >= STAND_TIMEOUT_S


def test_never_settling_after_the_walk_fails() -> None:
    walking = lambda foot, acc, gyr: _segments()[foot]["walk"]
    feed = _Feed(_clip(settle=walking))
    feed.run.begin()
    status = feed.drive(limit=60)
    assert status["failure"]["reason"] == "unsettled"
    assert status["failure"]["code"] == "E-CAL-3002"
    assert feed.run.attempts[0].durations["settle"] >= SETTLE_TIMEOUT_S


def test_a_walk_with_no_direction_fails_instead_of_guessing() -> None:
    """原地抖动而不是直线走：方向估计认输，这里就判失败，不拿噪声定前向。"""
    rng = np.random.default_rng(3)

    def shuffle(foot, acc, gyr):
        n = acc.shape[0]
        jitter = rng.normal(0, 2.0, (n, 3))
        jitter[:, 2] = 0.0
        return acc[:1].repeat(n, axis=0) + jitter, rng.normal(0, 1.0, (n, 3))

    feed = _Feed(_clip(walk=shuffle))
    feed.run.begin()
    status = feed.drive()
    assert status["failure"]["reason"] == "walk"
    assert status["failure"]["code"] == "E-CAL-3003"


def test_every_failure_speaks_in_actions() -> None:
    from gait.app import sessioncalib
    from gait.app.errors import check_code
    from gait.calib.still import LoosenessCheck

    failures = [
        sessioncalib._no_data(["L"]),
        sessioncalib._moved(["L", "R"]),
        sessioncalib._unsettled(["R"]),
        sessioncalib._walk(["L"]),
        sessioncalib._loose({"L": LoosenessCheck("L", 9.0, 5.0, True)}),
    ]
    for failure in failures:
        check_code(failure.code)  # 未登记的码在前端只能显示成一个数字
        for text in (failure.message, failure.action):
            assert text.strip()
            for word in JARGON:
                assert word not in text, (failure.reason, word)


# ── 重试与计时 ──────────────────────────────────────────────────────────


def test_a_retry_counts_toward_the_chain_time() -> None:
    """链耗时从**第一次**开始算：重试花掉的也是操作员的时间。"""
    clip = calibration_clip()
    feed = _Feed({"L": clip["L"], "R": []})
    feed.run.begin()
    feed.drive(limit=STAND_TIMEOUT_S + 1)
    assert feed.run.phase == "failed"
    # 操作员处理好之后重试，这回两只脚都有数据。
    feed.timeline = _Feed(clip).timeline
    feed.restart()
    feed.run.begin()
    feed.drive()
    assert feed.run.passed
    timing = feed.run.timing()
    assert [a["outcome"] for a in timing["attempts"]] == ["no_data", "passed"]
    first, second = timing["attempts"]
    assert timing["chain_seconds"] == pytest.approx(
        first["total_seconds"] + second["total_seconds"], abs=0.3
    )


# ── 出境：IPC 不带样本，元数据是合法 JSON 且无身份键 ─────────────────────


def test_status_carries_no_samples() -> None:
    feed = _passing()
    while feed.run.running:
        protocol.reject_bulk_payload(feed.drive(limit=0.5))


def test_snapshot_is_strict_json_without_identity_keys() -> None:
    feed = _passing()
    feed.drive()
    record = feed.run.snapshot()
    text = json.dumps(record, allow_nan=False)  # inf / nan 会在这里炸
    assert record["state"] == "passed"
    assert record["applied_to_analysis"] is False
    assert set(record["feet"]) == {"L", "R"}

    def keys(value):
        if isinstance(value, dict):
            for key, inner in value.items():
                yield key
                yield from keys(inner)
        elif isinstance(value, list):
            for inner in value:
                yield from keys(inner)

    for key in keys(json.loads(text)):
        for fragment in IDENTITY_KEY_FRAGMENTS:
            assert fragment not in key.lower(), key


# ── service：真的经过设备源、真的落进 meta.json ───────────────────────────


def _poll_until_done(service: TerminalService, timeout: float = 30.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = service.handle(
            {"id": "s", "method": "runCalibration", "params": {"action": "status"}}
        )
        assert response["status"] == "ok", response
        if response["result"]["phase"] not in ("stand", "walk", "settle"):
            return response["result"]
        time.sleep(0.02)
    raise AssertionError("标定没有在时限内结束")


def _synthetic_source() -> ReplayDeviceSource:
    return ReplayDeviceSource(
        chunks={"L": [], "R": []},
        calibration_loader=calibration_clip,
        label="synthetic",
        speed=20.0,
    )


def test_service_runs_the_flow_and_writes_calib_snapshot(tmp_path: Path) -> None:
    """标定结果要**真的走一次 `create_session`**（《05》§5 的教训：只在内存里看过的
    快照，第一次落盘才暴露问题）。"""
    service = TerminalService(source=_synthetic_source(), session_root=tmp_path)
    begun = service.handle(
        {"id": "1", "method": "runCalibration", "params": {"action": "begin", "fresh": True}}
    )
    assert begun["result"]["phase"] == "stand"
    result = _poll_until_done(service)
    assert result["phase"] == "passed", result
    assert result["chainSeconds"] >= 0

    started = service.handle({"id": "2", "method": "startSession", "params": {"now": 0}})
    session_id = started["result"]["sessionId"]
    meta = read_meta(session_directory(tmp_path, session_id))
    assert meta.calib_snapshot["state"] == "passed"
    assert meta.calib_snapshot["timing"]["chain_seconds"] is not None
    assert set(meta.calib_snapshot["feet"]) == {"L", "R"}
    service.handle({"id": "3", "method": "stopSession", "params": {"now": 1}})

    assert "reused_from_session" not in meta.calib_snapshot

    # 「重新检测」不回 P-07，沿用这份标定 —— 并写明沿用自哪场，免得算中位数时数两遍。
    again = service.handle({"id": "4", "method": "startSession", "params": {"now": 0}})
    meta = read_meta(session_directory(tmp_path, again["result"]["sessionId"]))
    assert meta.calib_snapshot["state"] == "passed"
    assert meta.calib_snapshot["reused_from_session"] == session_id
    service.handle({"id": "5", "method": "stopSession", "params": {"now": 1}})

    # 下一位受试者重新进 P-07：新的标定，不再是沿用。
    service.handle(
        {"id": "6", "method": "runCalibration", "params": {"action": "begin", "fresh": True}}
    )
    assert _poll_until_done(service)["phase"] == "passed"
    third = service.handle({"id": "7", "method": "startSession", "params": {"now": 0}})
    meta = read_meta(session_directory(tmp_path, third["result"]["sessionId"]))
    assert "reused_from_session" not in meta.calib_snapshot
    service.handle({"id": "8", "method": "stopSession", "params": {"now": 1}})


def test_a_source_without_a_calibration_clip_says_so(tmp_path: Path) -> None:
    """回放旧会话：开头几秒不是为静立准备的。如实说做不了，而不是报一个莫名的失败。"""
    source = ReplayDeviceSource(chunks={"L": [], "R": []}, label="replay")
    service = TerminalService(source=source, session_root=tmp_path)
    begun = service.handle(
        {"id": "1", "method": "runCalibration", "params": {"action": "begin", "fresh": True}}
    )
    assert begun["result"]["phase"] == "unavailable"
    assert "标定片段" in begun["result"]["message"]
    started = service.handle({"id": "2", "method": "startSession", "params": {"now": 0}})
    meta = read_meta(session_directory(tmp_path, started["result"]["sessionId"]))
    assert meta.calib_snapshot["state"] == "unavailable"
    service.handle({"id": "3", "method": "stopSession", "params": {"now": 1}})


def test_cancel_releases_the_stream() -> None:
    source = StubDeviceSource(autofeed_hz=50.0)
    service = TerminalService(source=source)
    service.handle(
        {"id": "1", "method": "runCalibration", "params": {"action": "begin", "fresh": True}}
    )
    assert source._feeder is not None
    cancelled = service.handle(
        {"id": "2", "method": "runCalibration", "params": {"action": "cancel"}}
    )
    assert cancelled["result"] == {"phase": "cancelled"}
    assert source._feeder is None
    assert not service.loop.running


def test_protocol_misuse_is_a_protocol_error() -> None:
    service = TerminalService()
    with pytest.raises(protocol.ProtocolError, match="先 begin"):
        service.handle({"id": "1", "method": "runCalibration", "params": {"action": "status"}})
    with pytest.raises(protocol.ProtocolError, match="action"):
        service.handle({"id": "2", "method": "runCalibration", "params": {}})


def test_a_late_status_poll_does_not_stop_the_session_stream(tmp_path: Path) -> None:
    """标定结束后界面晚到的一拍轮询，不能把已经开始的检测的流收掉。"""
    source = ReplayDeviceSource(
        chunks={"L": [], "R": []},
        calibration_loader=calibration_clip,
        label="synthetic",
        speed=20.0,
    )
    service = TerminalService(source=source, session_root=tmp_path)
    service.handle(
        {"id": "1", "method": "runCalibration", "params": {"action": "begin", "fresh": True}}
    )
    assert _poll_until_done(service)["phase"] == "passed"
    service.handle({"id": "2", "method": "startSession", "params": {"now": 0}})
    assert source._feeder is not None
    late = service.handle(
        {"id": "3", "method": "runCalibration", "params": {"action": "status"}}
    )
    assert late["result"]["phase"] == "passed"
    assert source._feeder is not None, "晚到的轮询收掉了会话的流"
    assert service.loop.running
    service.handle({"id": "4", "method": "stopSession", "params": {"now": 1}})


def test_a_begin_that_breaks_halfway_leaves_nothing_open() -> None:
    """开流时出了别的错：不留一个挂着收集器、开着循环的半截标定。"""

    class Broken(ReplayDeviceSource):
        def begin_calibration_stream(self) -> None:
            raise RuntimeError("片段生成失败")

    source = Broken(chunks={"L": [], "R": []}, label="synthetic")
    service = TerminalService(source=source)
    with pytest.raises(RuntimeError, match="片段生成失败"):
        service.handle(
            {"id": "1", "method": "runCalibration", "params": {"action": "begin", "fresh": True}}
        )
    assert service._session_calib is not None and not service._session_calib.running
    assert not service.loop.running
    assert source._feeder is None
