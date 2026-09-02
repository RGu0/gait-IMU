"""RAY-233 `write-failure-safe-stop`：采集中写盘失败即安全停止。

## 在此之前没有人巡检

`device/capture.py` 的模块文档写明：「这是『写盘错误安全停止』的触发点。写线程里的
错误不会自己冒到事件循环，所以必须有人主动看。」而生产代码里没人看 ——
`grep -rn "\\.check()" src/` 只搜得到那句文档本身。

后果不是报错，是**磁盘写满之后倒计时照常走完**：操作员陪着受试者走完三分钟，结束时
才从 `CaptureStatus.problems` 里得知什么都没采到。这违反 PRD §6.1「断连或写盘错误即
安全停止并标记会话不完整」，也违反 UI 设计 §7（写盘错误是阻断级）。

## 怎么造「磁盘满」

沿用 RAY-198 已立的做法（`test_device_capture.py::TestSafeStopOnWriteFailure`）：把
`OSError` 注入 writer。不去造一个真的满磁盘 —— ENOSPC 是操作系统的事，三个平台造法
各不相同（`/dev/full` 只有 Linux 有），而 CI 同时跑 Linux 与 Windows。要验的是**写
失败之后发生什么**，不是操作系统会不会报 ENOSPC。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gait.app import protocol
from gait.app.service import TerminalService
from gait.app.sources import StubDeviceSource, synthetic_frame
from gait.device.capture import recover_recording
from gait.io.session import raw_path, read_meta, session_directory

DISK_FULL = OSError("[Errno 28] No space left on device")


def _running(tmp_path: Path) -> tuple[TerminalService, StubDeviceSource, str]:
    source = StubDeviceSource()
    service = TerminalService(source=source, session_root=tmp_path)
    session_id = service.handle(
        {"id": "s", "method": "startSession", "params": {"now": 0.0}}
    )["result"]["sessionId"]
    return service, source, session_id


def _break_writes(service: TerminalService, foot: str = "L") -> None:
    service.capture._writers[foot].error = DISK_FULL


# ── 巡检确实发生了 ────────────────────────────────────────────────────────


def test_a_healthy_tick_is_still_just_a_tick(tmp_path: Path) -> None:
    service, source, _ = _running(tmp_path)
    for index in range(3):
        source.feed("L", synthetic_frame(index))
    event = service.tick(1.0)
    assert event["topic"] == "session.tick"
    assert set(event["payload"]) == {"remainingSeconds", "steps", "link"}


def test_a_write_failure_turns_the_next_tick_into_an_abort(tmp_path: Path) -> None:
    """这条是整个 scope 的核心：在此之前它会返回一个正常的 tick。"""
    service, _, _ = _running(tmp_path)
    _break_writes(service)
    event = service.tick(1.0)
    assert event["topic"] == "session.aborted"
    assert event["payload"]["error"]["code"] == "E-BLE-1020"


def test_the_abort_says_what_happened_and_what_to_do(tmp_path: Path) -> None:
    service, _, _ = _running(tmp_path)
    _break_writes(service)
    error = service.tick(1.0)["payload"]["error"]
    assert "写盘失败" in error["message"]
    assert "磁盘剩余空间" in error["action"]
    # 文案由 sidecar 给出（RAY-248 验收第二条），渲染端只排版。
    assert error["domain"] == "E-BLE"


def test_the_countdown_cannot_keep_running_after_a_safe_stop(tmp_path: Path) -> None:
    """继续 tick 会让倒计时在一个已经停止的会话上往下走。

    那正是本 scope 要消灭的画面，只是成因换成了「调用方没理会 aborted」。
    """
    service, _, _ = _running(tmp_path)
    _break_writes(service)
    service.tick(1.0)
    with pytest.raises(protocol.ProtocolError, match="终态"):
        service.tick(2.0)


# ── 安全停止的「安全」在于收尾顺序 ────────────────────────────────────────


def test_the_safe_stop_actually_closes_the_capture(tmp_path: Path) -> None:
    """在此之前 `abort()` 只中止 TimedWalk，采集就那么挂着。"""
    service, _, _ = _running(tmp_path)
    _break_writes(service)
    service.tick(1.0)
    assert service.capture is None
    assert service.loop.running is False


def test_data_written_before_the_failure_is_kept(tmp_path: Path) -> None:
    """安全停止不是放弃数据 —— 已经采到的那部分往往正是最该保住的。"""
    service, source, session_id = _running(tmp_path)
    for index in range(6):
        source.feed("R", synthetic_frame(index))
    _break_writes(service, "L")
    service.tick(1.0)

    recording, report = recover_recording(raw_path(tmp_path, session_id, "R"))
    assert report.chunks_recovered == 6
    assert len(recording.chunks) == 6


def test_a_safely_stopped_session_is_distinguishable_from_a_crash(tmp_path: Path) -> None:
    """这是与上一个 scope 的接缝，必须钉住。

    被杀的进程留下 `state: pending`（改写那一步没发生）；被安全停止的会话则留下
    真实的 `complete: False`。两者在磁盘上必须分得开 —— 否则「pending 表示进程没了」
    这个判据就被毁掉了，而上个 scope 的 V-U4/V-U5 全靠它。
    """
    service, _, session_id = _running(tmp_path)
    _break_writes(service)
    service.tick(1.0)

    meta = read_meta(session_directory(tmp_path, session_id))
    assert "state" not in meta.integrity_report, "安全停止不该留下 pending"
    assert meta.integrity_report["complete"] is False
    assert any("写盘失败" in problem for problem in meta.integrity_report["problems"])


# ── 写盘失败要进会话结论，不能只停在事件里 ────────────────────────────────


def test_the_write_failure_reaches_the_session_verdict(tmp_path: Path) -> None:
    """事件是会被错过的；会话结论不会。

    `LinkOutcome.recording_error` 正是为此存在的字段，`summarize_session` 据它
    判会话不完整。
    """
    service, _, _ = _running(tmp_path)
    _break_writes(service)
    service.tick(1.0)

    result = service.handle(
        {"id": "r", "method": "sessionResult", "params": {"wearing": "pass"}}
    )["result"]
    assert result["integrity"]["complete"] is False
    assert any("写盘失败" in problem for problem in result["integrity"]["problems"])
    assert result["overall"] == "invalid"


def test_the_service_does_not_wait_to_be_told(tmp_path: Path) -> None:
    """调用方**没有**传 recordingError，sidecar 自己知道。

    要靠调用方转述，就迟早有人忘了转述 —— 而漏掉的那次正是数据丢了的那次。
    """
    service, _, _ = _running(tmp_path)
    _break_writes(service)
    service.tick(1.0)
    result = service.handle(
        {"id": "r", "method": "sessionResult", "params": {"wearing": "pass"}}
    )["result"]
    left = next(link for link in result["integrity"]["links"] if link["foot"] == "L")
    assert left["recording_error"] is not None
    assert left["clean"] is False


def test_a_clean_session_reports_no_recording_error(tmp_path: Path) -> None:
    """反向：没出事时这些字段必须是干净的，否则上面几条证明不了什么。"""
    service, source, _ = _running(tmp_path)
    for index in range(4):
        source.feed("L", synthetic_frame(index))
    service.handle({"id": "e", "method": "stopSession", "params": {"now": 200.0}})
    result = service.handle(
        {"id": "r", "method": "sessionResult", "params": {"wearing": "pass"}}
    )["result"]
    assert result["integrity"]["complete"] is True
    assert all(link["recording_error"] is None for link in result["integrity"]["links"])
    assert result["overall"] == "valid"
