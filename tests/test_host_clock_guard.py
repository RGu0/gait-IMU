"""RAY-545 `product-clock-guard`：真实传感器模式下，主机时钟不够细就不静默出数。

Windows + Python 3.12 的 `time.monotonic()` 是 15.6 ms 的台阶，200 Hz 的周期是 5 ms。
`python-313` 把安装包换成了 3.13；这里守的是「万一又落回一个粗时钟」—— 那时自检要说清楚
并拦住开会话，而不是照常采一份到达时刻被量化的数据。

时钟一律**注入**：测试机的真实时钟有多细与这里要证明的东西无关。
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from gait.app.errors import check_code
from gait.app.service import TerminalService
from gait.app.sources import StubDeviceSource
from gait.cli import linktest, v3prime
from gait.device import hostclock
from gait.io.session import read_meta

WINDOWS_PY312 = 0.015625  # GetTickCount64
QPC = 1e-7  # QueryPerformanceCounter（Windows + 3.13 实测）


class _RealSensorStub(StubDeviceSource):
    """只借「真实传感器模式」这一个身份（`binding_required`），不碰蓝牙。"""

    binding_required = True


def _call(service: TerminalService, method: str, **params):
    return service.handle({"id": "1", "method": method, "params": params})


def _clock_item(service: TerminalService) -> dict | None:
    items = _call(service, "runPreflight")["result"]
    return next((item for item in items if item["id"] == "host-clock"), None)


# ── 判据只有一处 ─────────────────────────────────────────────────────────


def test_the_criterion_lives_in_one_place() -> None:
    # linktest、v3prime 与产品路径曾各写一个 10；现在两个 CLI 都指向同一个对象。
    assert linktest.CLOCK_RESOLUTION_RATIO is hostclock.CLOCK_RESOLUTION_RATIO
    assert v3prime.CLOCK_RESOLUTION_RATIO is hostclock.CLOCK_RESOLUTION_RATIO
    for module in (linktest, v3prime):
        source = inspect.getsource(module)
        assert "CLOCK_RESOLUTION_RATIO = 10" not in source, module.__name__


def test_the_criterion_itself() -> None:
    assert not hostclock.is_adequate(WINDOWS_PY312, 200.0)  # 差 31 倍
    assert hostclock.is_adequate(QPC, 200.0)
    assert hostclock.is_adequate(hostclock.limit_for(200.0), 200.0)  # 边界含等号
    assert hostclock.is_adequate(None, 200.0)  # 录制里没检出量化痕迹 = 够细
    assert hostclock.limit_for(200.0) == pytest.approx(0.0005)


def test_the_product_path_takes_the_coarser_of_declared_and_measured(monkeypatch) -> None:
    # 申报值说够细、实测跳变却粗 —— 信粗的那个。
    monkeypatch.setattr(hostclock, "declared_resolution", lambda: QPC)
    monkeypatch.setattr(hostclock, "measured_resolution", lambda samples=200: WINDOWS_PY312)
    assert hostclock.effective_resolution() == WINDOWS_PY312


# ── 自检 ────────────────────────────────────────────────────────────────


def test_a_coarse_clock_fails_preflight_in_real_sensor_mode() -> None:
    service = TerminalService(source=_RealSensorStub(), host_clock_resolution=lambda: WINDOWS_PY312)
    item = _clock_item(service)
    assert item is not None and item["status"] == "fail"
    error = item["error"]
    assert error["code"] == "E-BLE-1040"
    check_code(error["code"])  # 已在 contract.json 登记
    assert "15.6 ms" in error["message"]
    assert "演示模式" in error["action"]  # 动作语言：操作员能做的下一步


def test_a_fine_clock_passes_preflight() -> None:
    service = TerminalService(source=_RealSensorStub(), host_clock_resolution=lambda: QPC)
    item = _clock_item(service)
    assert item is not None and item["status"] == "pass"
    assert item["error"] is None


def test_demo_mode_has_no_clock_item_even_on_a_coarse_clock() -> None:
    # 演示数据不经过主机时钟打时刻，没有东西可失真。
    service = TerminalService(source=StubDeviceSource(), host_clock_resolution=lambda: WINDOWS_PY312)
    assert _clock_item(service) is None


# ── 开会话 ──────────────────────────────────────────────────────────────


def test_start_session_is_refused_on_a_coarse_clock(tmp_path: Path) -> None:
    service = TerminalService(
        source=_RealSensorStub(), session_root=tmp_path, host_clock_resolution=lambda: WINDOWS_PY312
    )
    response = _call(service, "startSession")
    assert response["status"] == "error", response
    assert response["error"]["code"] == "E-BLE-1040"
    # 先于任何状态变更拦住：不留半开的会话目录（上传队列库随 service 建立，与会话无关）。
    assert [p for p in tmp_path.iterdir() if p.is_dir()] == []
    assert service.session_id is None


def test_demo_mode_starts_on_a_coarse_clock(tmp_path: Path) -> None:
    service = TerminalService(
        source=StubDeviceSource(), session_root=tmp_path, host_clock_resolution=lambda: WINDOWS_PY312
    )
    assert _call(service, "startSession")["status"] == "ok"


# ── 落盘 ────────────────────────────────────────────────────────────────


def test_a_real_sensor_session_records_its_clock(tmp_path: Path) -> None:
    service = TerminalService(
        source=_RealSensorStub(), session_root=tmp_path, host_clock_resolution=lambda: QPC
    )
    assert _call(service, "startSession")["status"] == "ok"
    meta = read_meta(tmp_path / service.session_id)
    clock = meta.extra["host_clock"]
    assert clock["monotonic_resolution_s"] == QPC
    assert clock["adequate"] is True
    assert clock["nominal_fs"] == 200.0
    assert clock["required_resolution_s"] == pytest.approx(0.0005)


def test_a_demo_session_does_not_claim_a_host_clock(tmp_path: Path) -> None:
    service = TerminalService(source=StubDeviceSource(), session_root=tmp_path)
    assert _call(service, "startSession")["status"] == "ok"
    meta = read_meta(tmp_path / service.session_id)
    assert "host_clock" not in meta.extra
