"""RAY-759：沿用的会话标定只给它所属的那位受试者。

界面上换人一定会重走 P-07；这里守的是绕过界面的调用方。「换人被拒」与「同人放行」
成对验 —— 只验前者，一个「沿用一律拒绝」的实现也能全绿。
"""

from __future__ import annotations

import time
from pathlib import Path

from gait.app.replay import ReplayDeviceSource, calibration_clip
from gait.app.service import TerminalService
from gait.io.session import list_sessions, read_meta, session_directory

FINE_CLOCK = 1e-7
ALICE = "e8e02054-48ff-402d-ab03-687b57f9140f"
BOB = "e12b17b4-51e4-4f8b-bb26-6caf5803e6ef"


class _RealSensor(ReplayDeviceSource):
    binding_required = True


def _service(tmp_path: Path) -> TerminalService:
    return TerminalService(
        source=_RealSensor(
            chunks={"L": [], "R": []},
            calibration_loader=calibration_clip,
            label="real-sensor-under-test",
            speed=20.0,
        ),
        session_root=tmp_path,
        host_clock_resolution=lambda: FINE_CLOCK,
    )


def _call(service: TerminalService, method: str, **params) -> dict:
    return service.handle({"id": "1", "method": method, "params": params})


def _calibrate(service: TerminalService, timeout: float = 30.0) -> None:
    _call(service, "runCalibration", action="begin", fresh=True)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        phase = _call(service, "runCalibration", action="status")["result"]["phase"]
        if phase not in ("stand", "walk", "settle"):
            assert phase == "passed", phase
            return
        time.sleep(0.02)
    raise AssertionError("标定没有在时限内结束")


def _start(service: TerminalService, **params) -> dict:
    return _call(service, "startSession", now=0.0, **params)


def _used_by(service: TerminalService, subject: str) -> str:
    """标定通过，并已给 `subject` 用过一场。返回那场会话的 id。"""
    _calibrate(service)
    assert _start(service, subjectUuid=subject)["status"] == "ok"
    first = service.session_id
    _call(service, "stopSession", now=1.0)
    return first


def _assert_refused(service: TerminalService, tmp_path: Path, **params) -> None:
    before = list_sessions(tmp_path)
    subject_before = service._subject_uuid
    response = _start(service, **params)
    assert response["status"] == "error", response
    assert response["error"]["code"] == "E-CAL-3005"
    assert list_sessions(tmp_path) == before
    assert service._subject_uuid == subject_before


def test_a_reused_calibration_is_refused_for_another_subject(tmp_path: Path) -> None:
    service = _service(tmp_path)
    _used_by(service, ALICE)
    _assert_refused(service, tmp_path, subjectUuid=BOB)


def test_a_reused_calibration_is_admitted_for_the_same_subject(tmp_path: Path) -> None:
    service = _service(tmp_path)
    first = _used_by(service, ALICE)
    for _ in range(2):
        response = _start(service, subjectUuid=ALICE)
        assert response["status"] == "ok", response
        meta = read_meta(session_directory(tmp_path, service.session_id))
        assert meta.calib_snapshot["reused_from_session"] == first
        assert meta.subject_uuid == ALICE
        _call(service, "stopSession", now=1.0)


def test_a_fresh_calibration_takes_whichever_subject_comes_first(tmp_path: Path) -> None:
    """新通过、还没用过的标定：第一场不论是谁都放行，并从此属于这一位。"""
    service = _service(tmp_path)
    _used_by(service, ALICE)
    # 下一位受试者：界面会重走 P-07，于是有一份新标定。
    _calibrate(service)
    response = _start(service, subjectUuid=BOB)
    assert response["status"] == "ok", response
    meta = read_meta(session_directory(tmp_path, service.session_id))
    assert "reused_from_session" not in meta.calib_snapshot
    _call(service, "stopSession", now=1.0)
    # 这份新标定从此属于 BOB，不再给 ALICE。
    _assert_refused(service, tmp_path, subjectUuid=ALICE)


def test_a_missing_or_invalid_subject_counts_as_different(tmp_path: Path) -> None:
    """不知道是谁，就不能说是同一位。"""
    service = _service(tmp_path)
    _used_by(service, ALICE)
    _assert_refused(service, tmp_path)
    _assert_refused(service, tmp_path, subjectUuid="档案号 2781")


def test_a_rebind_clears_the_owner_with_the_calibration(tmp_path: Path) -> None:
    """RAY-756 作废标定时，归属一并清掉：重新标定后的第一场可以是任何人。"""
    service = _service(tmp_path)
    _used_by(service, ALICE)
    service._discard_calibration()
    assert service._calib_subject is None
    _calibrate(service)
    assert _start(service, subjectUuid=BOB)["status"] == "ok"
    _call(service, "stopSession", now=1.0)
