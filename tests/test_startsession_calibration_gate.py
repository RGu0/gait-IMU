"""RAY-754：真实传感器下，没过会话标定就不能开检测。

闸放在 sidecar 的 `startSession`，与登录闸、时钟闸同处。「拦」与「放」成对验 ——
只验「拦」，一个一律拒绝的闸也能全绿；只验「放」，一个不存在的闸也能全绿。
"""

from __future__ import annotations

import time
from pathlib import Path

from gait.app.errors import check_code
from gait.app.replay import ReplayDeviceSource, calibration_clip
from gait.app.service import CALIBRATION_ADMITTED, TerminalService
from gait.app.sessioncalib import STAND_TIMEOUT_S, CalibrationRun
from gait.app.sources import StubDeviceSource
from gait.io.session import list_sessions, read_meta, session_directory

#: 与 `test_session_calibration.JARGON` 同一份词表。
JARGON = ("安装角", "收敛", "零偏", "四元数", "协方差", "主成分", "特征值", "重力方向")

#: 足够细的主机时钟，让时钟闸不挡路 —— 这里只测标定闸。
FINE_CLOCK = 1e-7


class _RealSensor(ReplayDeviceSource):
    """借「真实传感器」这个身份（`binding_required`），字节由合成标定片段供给。"""

    binding_required = True


def _source(*, clip: bool = True) -> _RealSensor:
    return _RealSensor(
        chunks={"L": [], "R": []},
        calibration_loader=calibration_clip if clip else None,
        label="real-sensor-under-test",
        speed=20.0,
    )


def _service(tmp_path: Path, source=None) -> TerminalService:
    return TerminalService(
        source=source if source is not None else _source(),
        session_root=tmp_path,
        host_clock_resolution=lambda: FINE_CLOCK,
    )


def _call(service: TerminalService, method: str, **params) -> dict:
    return service.handle({"id": "1", "method": method, "params": params})


def _calibrate(service: TerminalService, timeout: float = 30.0) -> dict:
    begun = _call(service, "runCalibration", action="begin", fresh=True)["result"]
    if begun["phase"] not in ("stand", "walk", "settle"):
        return begun
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = _call(service, "runCalibration", action="status")["result"]
        if status["phase"] not in ("stand", "walk", "settle"):
            return status
        time.sleep(0.02)
    raise AssertionError("标定没有在时限内结束")


def _assert_refused_without_side_effects(service: TerminalService, tmp_path: Path) -> None:
    sessions_before = list_sessions(tmp_path)
    response = _call(service, "startSession", now=0.0)
    assert response["status"] == "error", response
    assert response["error"]["code"] == "E-CAL-3005"
    # 拒绝发生在任何状态变更之前：不建目录、不写 meta、不包传输、不开会话。
    assert list_sessions(tmp_path) == sessions_before
    assert service.capture is None
    assert service.session_id is None
    assert service.walk is None
    assert not service.session_running


# ── 拦 ──────────────────────────────────────────────────────────────────


def test_blocks_when_calibration_was_never_run(tmp_path: Path) -> None:
    service = _service(tmp_path)
    _assert_refused_without_side_effects(service, tmp_path)


def test_blocks_when_calibration_failed(tmp_path: Path) -> None:
    service = _service(tmp_path)
    # 一次真的失败：两只脚都没有数据，静立步超时（假时钟，不等真实的 20 秒）。
    now = [0.0]
    run = CalibrationRun(clock=lambda: now[0], step_total=lambda: 0)
    run.begin()
    now[0] = STAND_TIMEOUT_S + 1
    run.poll()
    assert run.phase == "failed"
    service._session_calib = run
    _assert_refused_without_side_effects(service, tmp_path)


def test_blocks_when_calibration_was_cancelled(tmp_path: Path) -> None:
    service = _service(tmp_path)
    _call(service, "runCalibration", action="begin", fresh=True)
    assert _call(service, "runCalibration", action="cancel")["result"] == {"phase": "cancelled"}
    _assert_refused_without_side_effects(service, tmp_path)


def test_blocks_while_calibration_is_still_running(tmp_path: Path) -> None:
    """标定还在采就来开检测：没过就是没过。拒绝时也不去动那次还在进行的标定。"""
    service = _service(tmp_path)
    _call(service, "runCalibration", action="begin", fresh=True)
    try:
        _assert_refused_without_side_effects(service, tmp_path)
        assert service._session_calib is not None and service._session_calib.running
    finally:
        _call(service, "runCalibration", action="cancel")


# ── 放 ──────────────────────────────────────────────────────────────────


def test_admits_a_freshly_passed_calibration(tmp_path: Path) -> None:
    service = _service(tmp_path)
    assert _calibrate(service)["phase"] == "passed"
    response = _call(service, "startSession", now=0.0)
    assert response["status"] == "ok", response
    meta = read_meta(session_directory(tmp_path, service.session_id))
    assert meta.calib_snapshot["state"] == "passed"
    _call(service, "stopSession", now=1.0)


def test_admits_a_retest_reusing_the_calibration_without_limit(tmp_path: Path) -> None:
    """「重新检测」不回 P-07：沿用通过的标定，不限次数（用户 2026-10-13 裁定）。"""
    service = _service(tmp_path)
    assert _calibrate(service)["phase"] == "passed"
    first = _call(service, "startSession", now=0.0)["result"]["sessionId"]
    _call(service, "stopSession", now=1.0)
    for _ in range(3):
        response = _call(service, "startSession", now=0.0)
        assert response["status"] == "ok", response
        meta = read_meta(session_directory(tmp_path, service.session_id))
        assert meta.calib_snapshot["reused_from_session"] == first
        _call(service, "stopSession", now=1.0)


def test_admits_a_source_that_cannot_calibrate(tmp_path: Path) -> None:
    """数据源如实说了做不了标定（`unavailable`）：不拦（用户 2026-10-13 裁定）。"""
    service = _service(tmp_path, _source(clip=False))
    assert _calibrate(service)["phase"] == "unavailable"
    response = _call(service, "startSession", now=0.0)
    assert response["status"] == "ok", response
    meta = read_meta(session_directory(tmp_path, service.session_id))
    assert meta.calib_snapshot["state"] == "unavailable"
    _call(service, "stopSession", now=1.0)


# ── 只对真实传感器 ──────────────────────────────────────────────────────


def test_stub_source_is_not_gated(tmp_path: Path) -> None:
    service = TerminalService(source=StubDeviceSource(), session_root=tmp_path)
    response = _call(service, "startSession", now=0.0)
    assert response["status"] == "ok", response
    meta = read_meta(session_directory(tmp_path, service.session_id))
    assert meta.calib_snapshot["state"] == "not_run"
    _call(service, "stopSession", now=1.0)


def test_demo_source_is_not_gated(tmp_path: Path) -> None:
    source = ReplayDeviceSource(
        chunks={"L": [], "R": []}, calibration_loader=calibration_clip, label="synthetic"
    )
    service = TerminalService(source=source, session_root=tmp_path)
    assert _call(service, "startSession", now=0.0)["status"] == "ok"
    _call(service, "stopSession", now=1.0)


# ── 码与文案 ────────────────────────────────────────────────────────────


def test_the_admitted_states_are_exactly_passed_and_unavailable() -> None:
    assert frozenset({"passed", "unavailable"}) == CALIBRATION_ADMITTED


def test_the_refusal_is_registered_and_speaks_in_actions(tmp_path: Path) -> None:
    assert check_code("E-CAL-3005") == "E-CAL-3005"
    error = _call(_service(tmp_path), "startSession", now=0.0)["error"]
    for text in (error["message"], error["action"]):
        assert text.strip()
        for word in JARGON:
            assert word not in text, word

