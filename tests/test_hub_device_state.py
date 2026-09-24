"""RAY-503 工作台跟随真设备源的连接状态。

真机 RC 上：冷启动进工作台，快照不驱动连接、只拉一次，于是一直显示「电量读不到」；
模块在后台连上之后也不会刷新。下面每一条都能失败：把 `_start_connecting_if_idle`
去掉，或让快照不带 `state`、连接中仍报电量问题，都会红。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from gait.app.service import CONNECTING_NOTICE, TerminalService
from gait.app.sources import StubDeviceSource


@dataclass
class _ConnectingSource(StubDeviceSource):
    """有连接状态的设备源：`refresh()` 只记下调用，状态由测试推进。"""

    state: str = "idle"
    refresh_calls: list[float | None] = field(default_factory=list)

    def refresh(self, timeout: float | None = None) -> str:
        self.refresh_calls.append(timeout)
        if self.state in ("idle", "failed"):
            self.state = "connecting"
        return self.state


def _unconnected() -> _ConnectingSource:
    return _ConnectingSource(batteries={"L": None, "R": None})


def _snapshot(service: TerminalService) -> dict:
    return service.handle({"id": "s", "method": "snapshot"})["result"]


def test_idle_source_starts_connecting_without_waiting() -> None:
    source = _unconnected()
    summary = _snapshot(TerminalService(source=source))["deviceSummary"]
    assert source.refresh_calls == [0]  # 非阻塞：快照请求不能被一次蓝牙扫描卡住
    assert summary == {"ready": False, "issues": [CONNECTING_NOTICE], "state": "connecting"}


def test_connecting_is_not_reported_as_an_unreadable_battery() -> None:
    source = _unconnected()
    source.state = "connecting"
    summary = _snapshot(TerminalService(source=source))["deviceSummary"]
    assert summary["issues"] == [CONNECTING_NOTICE]
    assert not any("电量" in issue for issue in summary["issues"])
    assert source.refresh_calls == []  # 已经在连，不再起一次


def test_connected_with_batteries_is_ready() -> None:
    source = _ConnectingSource(state="connected")  # stub 默认电量可读
    summary = _snapshot(TerminalService(source=source))["deviceSummary"]
    assert summary["ready"] is True and summary["state"] == "connected"
    assert source.refresh_calls == []


def test_failed_source_is_not_retried_by_the_snapshot() -> None:
    """失败后自动重试会让工作台开着就无休止地后台扫描；重试归「重新检查设备」。"""
    source = _unconnected()
    source.state = "failed"
    service = TerminalService(source=source)
    summary = _snapshot(service)["deviceSummary"]
    assert source.refresh_calls == []
    assert summary["state"] == "failed" and summary["ready"] is False
    assert any("电量读不到" in issue for issue in summary["issues"])
    service.handle({"id": "r", "method": "recheckDevices"})
    assert source.refresh_calls == [None]


def test_no_connection_is_started_while_a_walk_is_running() -> None:
    source = _ConnectingSource(state="connected")
    service = TerminalService(source=source)
    service.handle({"id": "s", "method": "startSession", "params": {"now": 0.0}})
    assert service.session_running
    source.state = "idle"
    _snapshot(service)
    assert source.refresh_calls == []


def test_sources_without_a_connection_state_keep_the_old_shape() -> None:
    summary = _snapshot(TerminalService(source=StubDeviceSource()))["deviceSummary"]
    assert "state" not in summary and summary["ready"] is True



@dataclass
class _RereadingSource(StubDeviceSource):
    """声明了 `reread_batteries` 的设备源：记下每次 refresh 的参数。"""

    calls: list[bool] = field(default_factory=list)

    def refresh(self, timeout: float | None = None, *, reread_batteries: bool = False) -> str:
        self.calls.append(reread_batteries)
        return "connected"


def test_an_operator_recheck_forces_a_battery_reread_but_preflight_does_not() -> None:
    """RAY-537：主动「重新检查」要真的重读；自检不强制（降速会干扰到达率测量）。"""
    source = _RereadingSource()
    service = TerminalService(source=source)
    service.handle({"id": "r", "method": "recheckDevices"})
    service.handle({"id": "p", "method": "runPreflight"})
    assert source.calls == [True, False]


def test_sources_without_the_parameter_still_recheck() -> None:
    source = _ConnectingSource(state="failed")
    TerminalService(source=source).handle({"id": "r", "method": "recheckDevices"})
    assert source.refresh_calls == [None]
