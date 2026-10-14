"""RAY-756：重新配对模块后，上一次的会话标定作废。

换了模块，标定量的就是另一台的零偏与安装方向。配对**成功**才作废、**失败**不动 ——
两条成对验：只验前者，一个「什么都作废」的实现也能全绿。
"""

from __future__ import annotations

import time
from pathlib import Path

from gait.app.bindings import BindingStore, IdentifyFailure
from gait.app.replay import ReplayDeviceSource, calibration_clip
from gait.app.service import TerminalService
from gait.device.binding import DeviceIdentity
from gait.device.identity import MAC_PROVENANCE
from gait.io.session import read_meta, session_directory

FINE_CLOCK = 1e-7
NEW_LEFT = "F3:33:33:33:33:33"


class _RealSensor(ReplayDeviceSource):
    """真实传感器身份（`binding_required`），字节来自合成标定片段，配对可以成功或失败。"""

    binding_required = True
    identify_fails: bool = False

    def identify_for_binding(self, foot: str, *, exclude=None) -> DeviceIdentity:
        if self.identify_fails:
            raise IdentifyFailure(
                "同时开着两台模块，认不出要配的是哪一台。", "请只打开要配对的那一台。"
            )
        return DeviceIdentity(kind="mac", value=NEW_LEFT, provenance=MAC_PROVENANCE)


class _UnwritableStore(BindingStore):
    """磁盘写不进去的绑定存放处：识别成功了，保存失败。"""

    def bind(self, foot, identity):
        raise OSError("磁盘只读")


def _service(
    tmp_path: Path, *, store: BindingStore | None = None
) -> tuple[TerminalService, _RealSensor]:
    source = _RealSensor(
        chunks={"L": [], "R": []},
        calibration_loader=calibration_clip,
        label="real-sensor-under-test",
        speed=20.0,
    )
    service = TerminalService(
        source=source,
        session_root=tmp_path / "sessions",
        bindings=store or BindingStore(tmp_path / "config"),
        host_clock_resolution=lambda: FINE_CLOCK,
    )
    return service, source


def _call(service: TerminalService, method: str, **params) -> dict:
    return service.handle({"id": "1", "method": method, "params": params})


def _calibrate(service: TerminalService, timeout: float = 30.0) -> str:
    _call(service, "runCalibration", action="begin", fresh=True)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        phase = _call(service, "runCalibration", action="status")["result"]["phase"]
        if phase not in ("stand", "walk", "settle"):
            return phase
        time.sleep(0.02)
    raise AssertionError("标定没有在时限内结束")


def _passed_and_used_once(service: TerminalService, tmp_path: Path) -> str:
    """标定通过并已随一场会话落盘 —— 于是有了「沿用来源」。返回那场会话的 id。"""
    assert _calibrate(service) == "passed"
    assert _call(service, "startSession", now=0.0)["status"] == "ok"
    first = service.session_id
    _call(service, "stopSession", now=1.0)
    return first


def test_a_successful_rebind_discards_the_calibration(tmp_path: Path) -> None:
    service, _ = _service(tmp_path)
    _passed_and_used_once(service, tmp_path)

    assert _call(service, "bindFoot", foot="L")["status"] == "ok"

    assert service._calib_snapshot() == {"state": "not_run", "issue": "RAY-208"}
    assert service._calib_first_session is None


def test_a_failed_rebind_keeps_the_calibration(tmp_path: Path) -> None:
    service, source = _service(tmp_path)
    first = _passed_and_used_once(service, tmp_path)
    source.identify_fails = True

    response = _call(service, "bindFoot", foot="L")
    assert response["error"]["code"] == "E-BLE-1031"

    snapshot = service._calib_snapshot()
    assert snapshot["state"] == "passed"
    assert snapshot["reused_from_session"] == first


def test_after_a_rebind_a_session_needs_a_new_calibration(tmp_path: Path) -> None:
    service, _ = _service(tmp_path)
    _passed_and_used_once(service, tmp_path)
    assert _call(service, "bindFoot", foot="L")["status"] == "ok"

    refused = _call(service, "startSession", now=0.0)
    assert refused["error"]["code"] == "E-CAL-3005"

    assert _calibrate(service) == "passed"
    admitted = _call(service, "startSession", now=0.0)
    assert admitted["status"] == "ok", admitted
    meta = read_meta(session_directory(tmp_path / "sessions", service.session_id))
    assert meta.calib_snapshot["state"] == "passed"
    # 新标定的第一场会话，不是沿用。
    assert "reused_from_session" not in meta.calib_snapshot
    _call(service, "stopSession", now=1.0)


def test_a_rebind_during_a_running_calibration_stops_its_stream(tmp_path: Path) -> None:
    """配对时标定还在采：作废它，也收掉它的流，不留一个挂着的收集器。"""
    service, source = _service(tmp_path)
    _call(service, "runCalibration", action="begin", fresh=True)
    assert source._feeder is not None

    assert _call(service, "bindFoot", foot="L")["status"] == "ok"

    assert service._calib_snapshot()["state"] == "not_run"
    assert source._feeder is None
    assert not service.loop.running


def test_a_rebind_that_fails_to_save_keeps_the_calibration(tmp_path: Path) -> None:
    """识别成功、保存失败：模块没换成，标定照旧有效。"""
    service, _ = _service(tmp_path, store=_UnwritableStore(tmp_path / "config"))
    first = _passed_and_used_once(service, tmp_path)

    response = _call(service, "bindFoot", foot="L")
    assert response["error"]["code"] == "E-BLE-1030"

    snapshot = service._calib_snapshot()
    assert snapshot["state"] == "passed"
    assert snapshot["reused_from_session"] == first
