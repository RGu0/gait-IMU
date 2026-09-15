"""RAY-493 sidecar 事件泵与设备源选择。

事件泵之前没人调 `service.tick`：P-08 的倒计时不动，写盘巡检也从来不跑。这里验
「采集中真的推事件、没在采集时不推、stdin 关了泵就停」。
"""

from __future__ import annotations

import io
import json
import sys
import threading
import time
import types
from collections.abc import Iterator

import pytest

from gait.app import __main__ as entry
from gait.app.errors import TerminalError
from gait.app.replay import ReplayDeviceSource, UnavailableDeviceSource
from gait.app.service import TerminalService
from gait.app.sources import StubDeviceSource
from gait.io.session import create_session


class _SlowStdin:
    """逐行给出请求，行与行之间停一下 —— 让事件泵有时间在两条请求之间 tick。"""

    def __init__(self, steps: list[tuple[dict | None, float]]) -> None:
        self._steps = steps

    def __iter__(self) -> Iterator[str]:
        for message, pause in self._steps:
            if message is not None:
                yield json.dumps(message) + "\n"
            time.sleep(pause)


def _lines(stdout: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in stdout.getvalue().splitlines() if line.strip()]


def _pump_alive() -> bool:
    return any(t.name == "gait-event-pump" and t.is_alive() for t in threading.enumerate())


def test_ticks_flow_between_start_and_stop() -> None:
    stdout = io.StringIO()
    stdin = _SlowStdin(
        [
            ({"id": "s", "method": "startSession", "params": {"now": 100.0}}, 0.3),
            ({"id": "t", "method": "stopSession", "params": {"now": 280.0}}, 0.2),
        ]
    )
    entry.serve(
        stdin,
        stdout,
        TerminalService(source=StubDeviceSource()),
        tick_interval=0.02,
        clock=lambda: 110.0,
    )
    messages = _lines(stdout)
    kinds = [(m["kind"], m.get("id") or m.get("topic")) for m in messages]
    start = kinds.index(("response", "s"))
    stop = kinds.index(("response", "t"))
    ticks = [m for m in messages[start:stop] if m["kind"] == "event"]
    assert ticks, kinds
    assert all(m["topic"] == "session.tick" for m in ticks)
    assert ticks[0]["payload"]["remainingSeconds"] == pytest.approx(170.0)
    assert set(ticks[0]["payload"]) == {"remainingSeconds", "steps", "link"}
    seqs = [m["seq"] for m in ticks]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
    # 停下之后不再 tick —— 在已停的会话上倒计时照走，正是 tick 里那道守卫要防的画面。
    assert not [m for m in messages[stop:] if m["kind"] == "event"]
    assert not _pump_alive(), "stdin EOF 之后事件泵应当已经停下"


def test_no_events_when_no_walk_is_running() -> None:
    stdout = io.StringIO()
    stdin = _SlowStdin([({"id": "d", "method": "describe"}, 0.15)])
    entry.serve(stdin, stdout, TerminalService(), tick_interval=0.02, clock=lambda: 0.0)
    assert [m["kind"] for m in _lines(stdout)] == ["response"]


class _AbortingService(TerminalService):
    def tick(self, now: float) -> dict:
        return self.abort(now, TerminalError("E-BLE-1020", "写盘失败。", "请检查磁盘。"))


def test_aborted_event_is_pushed_once() -> None:
    stdout = io.StringIO()
    stdin = _SlowStdin([({"id": "s", "method": "startSession", "params": {"now": 0.0}}, 0.2)])
    entry.serve(
        stdin, stdout, _AbortingService(source=StubDeviceSource()), tick_interval=0.02
    )
    events = [m for m in _lines(stdout) if m["kind"] == "event"]
    assert [e["topic"] for e in events] == ["session.aborted"]
    assert events[0]["payload"]["error"]["code"] == "E-BLE-1020"


def test_stdout_carries_only_protocol_lines(capsys) -> None:
    entry.service_from_environment({"GAIT_PREVIEW": "1", "GAIT_DEVICE_SOURCE": "nope"})
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "预览策略已启用" in captured.err
    assert "nope" in captured.err


# ── 设备源与配置的选择 ────────────────────────────────────────────────────


def test_default_source_is_the_stub() -> None:
    source = entry.build_source({})
    assert type(source) is StubDeviceSource
    assert entry.build_source({"GAIT_STUB_FEED_HZ": "50"}).autofeed_hz == 50.0


def test_unknown_source_falls_back_to_stub() -> None:
    assert type(entry.build_source({"GAIT_DEVICE_SOURCE": "wat"})) is StubDeviceSource


def test_synthetic_source_is_labelled() -> None:
    source = entry.build_source({"GAIT_DEVICE_SOURCE": "synthetic", "GAIT_PROTOCOL_SECONDS": "60"})
    assert isinstance(source, ReplayDeviceSource)
    assert source.provenance()["source"] == "synthetic"
    assert source.provenance()["hardware"] is False


def test_replay_source_reads_the_session(tmp_path) -> None:
    from wt901.recording import RecordedChunk, Recording, write_recording

    from gait.contracts import SessionMeta
    from gait.io.session import new_session_id, new_subject_uuid, raw_path

    session_id = new_session_id()
    create_session(
        tmp_path,
        SessionMeta(
            session_id=session_id,
            created_at="2026-09-15T00:00:00Z",
            subject_uuid=new_subject_uuid(),
            scenario="walk",
            devices={"L": {"mac": "aa"}, "R": {"mac": "bb"}},
            config_snapshot={"rate_hz": 200},
            calib_snapshot={"L": {"note": "none"}},
            algo_version="test",
            algo_params={"preset": "default"},
            sync_report={"synthetic": True},
            integrity_report={"loss_rate": 0.0},
            protocol_config={"duration_s": 60},
        ),
    )
    for label in ("L", "R"):
        write_recording(
            raw_path(tmp_path, session_id, label),
            Recording(
                device_id=label,
                created_utc="",
                note="",
                chunks=(RecordedChunk(t=0.0, data=b"\x55\x61" + bytes(18)),),
            ),
        )
    source = entry.build_source(
        {"GAIT_DEVICE_SOURCE": "replay", "GAIT_REPLAY_SESSION": str(tmp_path / session_id)}
    )
    assert isinstance(source, ReplayDeviceSource)
    assert source.provenance()["source"] == "replay"
    assert source.chunks["L"] == [(0.0, b"\x55\x61" + bytes(18))]


@pytest.mark.parametrize(
    "env",
    [
        {"GAIT_DEVICE_SOURCE": "replay"},
        {"GAIT_DEVICE_SOURCE": "replay", "GAIT_REPLAY_SESSION": "/nonexistent/20260915T000000Z-deadbeef"},
    ],
)
def test_replay_without_a_readable_session_is_unavailable(env) -> None:
    assert isinstance(entry.build_source(env), UnavailableDeviceSource)


def test_ble_without_the_module_is_unavailable_not_stub(monkeypatch) -> None:
    # `None` 让 import 抛 ImportError —— 与 blesource 尚未交付时同一个结局。
    monkeypatch.setitem(sys.modules, "gait.app.blesource", None)
    source = entry.build_source({"GAIT_DEVICE_SOURCE": "ble"})
    assert isinstance(source, UnavailableDeviceSource)
    assert source.provenance()["source"] == "unavailable"
    items = TerminalService(source=source).handle({"id": "p", "method": "runPreflight"})["result"]
    assert next(i for i in items if i["id"] == "link-l")["error"]["code"] == "E-BLE-1001"


def test_ble_initialisation_failure_is_unavailable(monkeypatch) -> None:
    class Broken:
        @classmethod
        def from_environment(cls, env):
            raise RuntimeError("蓝牙未授权")

    monkeypatch.setitem(
        sys.modules, "gait.app.blesource", types.SimpleNamespace(BleDeviceSource=Broken)
    )
    source = entry.build_source({"GAIT_DEVICE_SOURCE": "ble"})
    assert isinstance(source, UnavailableDeviceSource)
    assert "蓝牙未授权" in source.provenance()["note"]


def test_ble_entrypoint_is_used_when_present(monkeypatch) -> None:
    sentinel = StubDeviceSource()

    class Ble:
        @classmethod
        def from_environment(cls, env):
            assert env["GAIT_DEVICE_SOURCE"] == "ble"
            return sentinel

    monkeypatch.setitem(
        sys.modules, "gait.app.blesource", types.SimpleNamespace(BleDeviceSource=Ble)
    )
    assert entry.build_source({"GAIT_DEVICE_SOURCE": "ble"}) is sentinel


@pytest.mark.parametrize(("raw", "expected"), [("120", 120), ("60", 60), ("90", 180), ("abc", 180), ("", 180)])
def test_protocol_seconds_only_accepts_presets(raw, expected) -> None:
    assert entry.protocol_config_from_environment({"GAIT_PROTOCOL_SECONDS": raw}).duration_s == expected


def test_preview_flag() -> None:
    assert entry.preview_from_environment({}) is None
    assert entry.preview_from_environment({"GAIT_PREVIEW": "0"}) is None
    policy = entry.preview_from_environment({"GAIT_PREVIEW": "1"})
    assert policy is not None and policy.waive_factory_calibration is True
