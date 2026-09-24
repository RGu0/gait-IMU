"""RAY-532：报告的测试条件与检测日期读**那一次**会话的元数据，不读进程状态。

真机会话重开报告时写成「有效时长 0 秒（0%）」，而 meta 里记着 63 秒。原因是
`_do_reportFor` 取进程里的 `self.walk` / `self.config` / `now()`。下面每条都能失败：
把任一项改回进程状态都会红。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from pathlib import Path

from wt901.recording import RecordedChunk, Recording, write_recording

from gait.app.replay import frames_from_synthetic
from gait.app.service import TerminalService
from gait.app.sources import StubDeviceSource
from gait.config import ProtocolConfig
from gait.io.session import raw_path, read_meta, session_directory, write_meta


def _walk(root: Path, *, stop_at: float, duration_s: int = 60) -> str:
    """走一次（stub 计时），再把原始数据换成算得出步态的合成步行。返回 session_id。"""
    service = TerminalService(
        source=StubDeviceSource(), session_root=root, config=ProtocolConfig(duration_s=duration_s)
    )
    session_id = service.handle(
        {"id": "s", "method": "startSession", "params": {"now": 0.0}}
    )["result"]["sessionId"]
    service.handle({"id": "t", "method": "stopSession", "params": {"now": stop_at}})
    for label, chunks in frames_from_synthetic(20.0, seed=3, chunk_frames=1).items():
        write_recording(
            raw_path(root, session_id, label),
            Recording(
                device_id=f"dev-{label}",
                created_utc="",
                note="",
                chunks=tuple(RecordedChunk(t=t, data=data) for t, data in chunks),
            ),
        )
    return session_id


def _conditions(service: TerminalService, session_id: str) -> tuple[dict[str, str], dict]:
    response = service.handle(
        {"id": "r", "method": "reportFor", "params": {"sessionId": session_id}}
    )
    assert response["status"] == "ok", response
    report = response["result"]
    return {c["label"]: c["value"] for c in report["conditions"]}, report


def test_reopened_report_reads_duration_and_valid_time_from_the_session(tmp_path) -> None:
    session_id = _walk(tmp_path, stop_at=45.0, duration_s=60)
    # 相当于 app 重启、且当前设置换成了 180 秒。
    reopened = TerminalService(session_root=tmp_path, config=ProtocolConfig(duration_s=180))
    conditions, report = _conditions(reopened, session_id)
    assert conditions["时长配置"] == "60 秒"
    assert conditions["有效时长"] == "45 秒（75%）"
    assert report["protocolSeconds"] == 60


def test_a_session_without_valid_time_says_not_recorded_not_zero(tmp_path) -> None:
    session_id = _walk(tmp_path, stop_at=45.0)
    directory = session_directory(tmp_path, session_id)
    meta = read_meta(directory)
    protocol = {k: v for k, v in meta.protocol_config.items() if k != "valid_seconds"}
    write_meta(directory, replace(meta, protocol_config=protocol))

    conditions, _ = _conditions(TerminalService(session_root=tmp_path), session_id)
    assert conditions["有效时长"] == "未记录"


def test_the_assessment_date_is_the_session_date_in_local_time(tmp_path) -> None:
    session_id = _walk(tmp_path, stop_at=45.0)
    directory = session_directory(tmp_path, session_id)
    created = "2026-09-24T05:23:39+00:00"  # 美西本地是 9-23 22:23
    write_meta(directory, replace(read_meta(directory), created_at=created))

    _, report = _conditions(TerminalService(session_root=tmp_path), session_id)
    expected = datetime.fromisoformat(created).astimezone().date().isoformat()
    assert report["assessedAt"] == expected


def test_another_walk_in_the_process_does_not_leak_into_an_old_report(tmp_path) -> None:
    old = _walk(tmp_path, stop_at=30.0)
    service = TerminalService(
        source=StubDeviceSource(), session_root=tmp_path, config=ProtocolConfig(duration_s=60)
    )
    service.handle({"id": "s", "method": "startSession", "params": {"now": 0.0}})
    service.handle({"id": "t", "method": "stopSession", "params": {"now": 55.0}})

    conditions, _ = _conditions(service, old)
    assert conditions["有效时长"] == "30 秒（50%）"
