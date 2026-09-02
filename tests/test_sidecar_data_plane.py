"""RAY-233 `sidecar-data-plane`：sidecar 拥有数据面，以及 V-U4 数据半边 / V-U5。

## 这两条验收在此之前无物可验

`gait/app/service.py` 里原先没有 `SessionCapture`、没有任何落盘路径。没有落盘就
没有「崩溃时数据完整」可谈 —— 那时能写出来的只会是一个没有数据的空测试，而空测试
给出的绿色比没有测试更危险：它会让 G-04（数据不静默丢失）看起来已经验过。

RAY-198 已经把**文件级**的崩溃恢复测透了（`test_device_crash_recovery.py`）。
这里验的是新的一层：**sidecar 进程**被杀之后，磁盘上留下的会话是否可恢复，
以及它是否**自己说得清楚**它没有正常结束。
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from gait.app.service import TerminalService
from gait.app.sources import StubDeviceSource, synthetic_frame
from gait.device.capture import recover_recording
from gait.io.session import raw_path, read_meta, session_directory

REPO_ROOT = Path(__file__).resolve().parents[1]


# ── 元数据的诚实性 ────────────────────────────────────────────────────────


def _started(tmp_path: Path, **kwargs) -> tuple[TerminalService, StubDeviceSource, str]:
    source = StubDeviceSource(**kwargs)
    service = TerminalService(source=source, session_root=tmp_path)
    result = service.handle({"id": "s", "method": "startSession", "params": {"now": 0.0}})["result"]
    return service, source, result["sessionId"]


def test_session_directory_and_metadata_land_before_any_bytes(tmp_path: Path) -> None:
    """先落盘再计算（原则 6）在这里是一个可被打断并检验的顺序。"""
    _, _, session_id = _started(tmp_path)
    directory = session_directory(tmp_path, session_id)
    assert (directory / "meta.json").is_file()
    assert raw_path(tmp_path, session_id, "L").exists()
    assert raw_path(tmp_path, session_id, "R").exists()


def test_unknowable_fields_say_pending_not_zero(tmp_path: Path) -> None:
    """写 `{"loss_rate": 0.0}` 会更糟：那是一个**看起来已经算过**的结论。"""
    _, _, session_id = _started(tmp_path)
    meta = read_meta(session_directory(tmp_path, session_id))
    assert meta.integrity_report["state"] == "pending"
    assert meta.sync_report["state"] == "pending"
    # 标定还没实现（RAY-208），元数据也照实说，而不是编一组标定参数。
    assert meta.calib_snapshot == {"state": "unimplemented", "issue": "RAY-208"}


def test_a_stub_session_is_self_identifying_on_disk(tmp_path: Path) -> None:
    """一份 stub 会话与一份真机会话在磁盘上长得一模一样。

    没有这个字段，事后没有任何办法把它们分开 —— 而把一份合成字节的会话当成实测
    数据去看，是这条链上最坏的失败。
    """
    _, _, session_id = _started(tmp_path)
    meta = read_meta(session_directory(tmp_path, session_id))
    assert meta.extra["provenance"]["source"] == "stub"
    assert meta.extra["provenance"]["hardware"] is False


def test_metadata_is_rewritten_with_real_numbers_at_close(tmp_path: Path) -> None:
    service, source, session_id = _started(tmp_path)
    for index in range(5):
        source.feed("L", synthetic_frame(index))
    service.handle({"id": "e", "method": "stopSession", "params": {"now": 200.0}})
    meta = read_meta(session_directory(tmp_path, session_id))
    assert "state" not in meta.integrity_report
    assert meta.integrity_report["complete"] is True
    assert meta.integrity_report["chunks_written"]["L"] == 5


def test_bytes_actually_reach_the_disk(tmp_path: Path) -> None:
    """录制钩子装在 `connect()` 里，wrap 之后不 connect 就静悄悄地录不到东西。"""
    service, source, session_id = _started(tmp_path)
    for index in range(7):
        source.feed("R", synthetic_frame(index))
    status = service.handle({"id": "e", "method": "stopSession", "params": {"now": 200.0}})["result"]
    assert status["capture"]["chunks_written"]["R"] == 7
    assert raw_path(tmp_path, session_id, "R").stat().st_size > 0


def test_no_session_root_means_explicitly_not_writing(tmp_path: Path) -> None:
    """不落盘要看得见，而不是悄悄什么都没写。"""
    service = TerminalService(source=StubDeviceSource(), session_root=None)
    service.handle({"id": "s", "method": "startSession", "params": {"now": 0.0}})
    result = service.handle({"id": "e", "method": "stopSession", "params": {"now": 200.0}})["result"]
    assert result["capture"] is None


# ── V-U4 数据半边 / V-U5：写入点被 kill ───────────────────────────────────


def _sidecar(root: Path, *, feed_hz: float) -> subprocess.Popen:
    environment = {
        **os.environ,
        "UV_NO_CONFIG": "1",
        "PYTHONUTF8": "1",
        "GAIT_SESSION_ROOT": str(root),
        "GAIT_STUB_FEED_HZ": str(feed_hz),
    }
    return subprocess.Popen(
        [sys.executable, "-m", "gait.app"],
        cwd=REPO_ROOT,
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _ask(process: subprocess.Popen, message: dict) -> dict:
    process.stdin.write(json.dumps(message) + "\n")
    process.stdin.flush()
    return json.loads(process.stdout.readline())


@pytest.fixture
def killed_session(tmp_path: Path) -> tuple[Path, str]:
    """起一个真 sidecar，让它写起来，然后 `SIGKILL`。

    用真进程而不是在测试里模拟：**要验的正是「进程没了」这件事**，而一个被
    模拟出来的崩溃只能证明模拟本身。
    """
    process = _sidecar(tmp_path, feed_hz=200.0)
    try:
        response = _ask(process, {"id": "1", "method": "startSession", "params": {"now": 0}})
        session_id = response["result"]["sessionId"]
        left = raw_path(tmp_path, session_id, "L")
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if left.exists() and left.stat().st_size > 2_000:
                break
            time.sleep(0.05)
        else:  # pragma: no cover - 只在环境异常时走到
            pytest.fail("sidecar 十秒内没有写出足够的数据")
        os.kill(process.pid, signal.SIGKILL)
        process.wait(timeout=10)
    finally:
        if process.poll() is None:  # pragma: no cover
            process.kill()
    assert process.returncode != 0, "SIGKILL 之后退出码不该是 0"
    return tmp_path, session_id


def test_the_kill_left_data_worth_recovering(killed_session) -> None:
    root, session_id = killed_session
    assert raw_path(root, session_id, "L").stat().st_size > 2_000


def test_data_written_before_the_kill_is_recoverable(killed_session) -> None:
    """V-U4 的数据半边 / V-U5：写入点被杀，已落盘那部分照样取得回来。"""
    root, session_id = killed_session
    for foot in ("L", "R"):
        recording, report = recover_recording(raw_path(root, session_id, foot))
        assert report.chunks_recovered > 0
        assert len(recording.chunks) == report.chunks_recovered


def test_a_killed_session_says_so_itself(killed_session) -> None:
    """一份未完成的会话必须自己说得清楚。

    元数据在会话开始时写成 `pending`，只有正常收尾才会被改写。进程被杀时那次改写
    没有发生 —— 于是磁盘上留下的 `pending` 就是「这次没有正常结束」的证据，
    不需要任何外部记录来判断。
    """
    root, session_id = killed_session
    meta = read_meta(session_directory(root, session_id))
    assert meta.integrity_report["state"] == "pending"
    assert meta.sync_report["state"] == "pending"


def test_the_ui_never_touched_this_data(killed_session) -> None:
    """数据面完全在 sidecar 内（红线 R-1）。

    这条断言的意义在于说明 V-U5 为什么在 sidecar 这一侧验：UI 从来没有碰过这些
    文件，所以「UI 崩溃时已落盘数据可恢复」在结构上是成立的 —— 杀掉 UI 根本
    不经过写盘路径。真正需要被验的是**写盘方自己**被杀时会怎样，也就是上面那条。
    """
    root, session_id = killed_session
    meta = read_meta(session_directory(root, session_id))
    assert meta.extra["provenance"]["source"] == "stub"
    assert set(meta.devices) == {"L", "R"}


def test_a_recovered_session_is_not_silently_complete(killed_session) -> None:
    """救回数据**不等于**当没事发生。

    `recover_recording` 的文档写得很清楚：只救数据不报截断，就正好落回上游想避免
    的那个坑。所以这里断言的是两件事**同时**成立：数据取得回来，且这份录制到底
    完不完整被如实报了出来。

    不断言「一定被截断」—— SIGKILL 可能正好落在行边界上，那时文件是完好的
    （RAY-198 已有一条专门的测试钉住这个边界）。这里要的是报告**说的是实话**：
    `complete` 必须等于 `not truncated`，而不是一个恒为真的乐观值。
    """
    root, session_id = killed_session
    for foot in ("L", "R"):
        recording, report = recover_recording(raw_path(root, session_id, foot))
        assert report.chunks_recovered > 0
        assert report.complete is (not report.truncated)
        assert len(recording.chunks) == report.chunks_recovered

    # 而无论单个文件完不完整，**会话**都没有正常结束 —— 那是元数据的事。
    meta = read_meta(session_directory(root, session_id))
    assert meta.integrity_report["state"] == "pending"


def test_recovery_report_would_catch_a_real_truncation(tmp_path: Path) -> None:
    """把上面那条的判据本身验一遍：截断时它确实报 truncated。

    没有这条，`complete is (not truncated)` 可能只是在一份从未被截断的文件上
    恒真地成立 —— 一个永远不会失败的判据等于没有判据。
    """
    service, source, session_id = _started(tmp_path)
    for index in range(6):
        source.feed("L", synthetic_frame(index))
    service.handle({"id": "e", "method": "stopSession", "params": {"now": 200.0}})

    path = raw_path(tmp_path, session_id, "L")
    intact = path.read_text(encoding="utf-8")
    _, clean = recover_recording(path)
    assert clean.truncated is False

    # 砍掉末行的一半，模拟掉电写到一半。
    path.write_text(intact[: -len(intact.splitlines()[-1]) // 2], encoding="utf-8")
    recording, report = recover_recording(path)
    assert report.truncated is True
    assert report.complete is False
    assert report.chunks_recovered > 0
    assert len(recording.chunks) == report.chunks_recovered
