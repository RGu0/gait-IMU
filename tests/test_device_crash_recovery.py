"""RAY-198 `crash-recovery`：崩溃截断后无静默丢失，且截断可观测。

注入方式是**真的 kill -9 一个正在写盘的子进程**，而不是手工构造一个残行文件：
手工构造的截断是「我认为崩溃长什么样」，`kill -9` 是崩溃本身。两者在这个格式上
应当一致，但只有后者能证明这一点。
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import struct
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from gait.device.capture import (
    RecoveryReport,
    recover_recording,
    replay_recording,
)
from gait.device.orchestration import LinkOutcome, summarize_session

_FRAME = b"\x55\x61" + struct.pack("<9h", *range(9))


def _writer_program(path: Path) -> str:
    """一个不停写录制、等着被杀掉的子进程。"""
    return textwrap.dedent(f"""
        import sys, time
        from pathlib import Path
        sys.path.insert(0, {str(Path("src").resolve())!r})
        from gait.device.recorder import ThreadedRecordingWriter
        frame = {_FRAME!r}
        w = ThreadedRecordingWriter(Path({str(path)!r}), device_id="dev-L")
        print("ready", flush=True)
        while True:
            w.write(frame)
            time.sleep(0.001)
    """)


@pytest.fixture
def killed_recording(tmp_path: Path) -> Path:
    """一份被 kill -9 打断的录制。"""
    path = tmp_path / "left.raw"
    program = _writer_program(path)
    proc = subprocess.Popen(
        [sys.executable, "-c", program],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdout is not None
        assert proc.stdout.readline().strip() == "ready"
        # 让它写够几百行再杀，确保「救回来的部分」不是空的。
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if path.exists() and len(path.read_text().splitlines()) > 200:
                break
            time.sleep(0.05)
        os.kill(proc.pid, signal.SIGKILL)
    finally:
        proc.wait(timeout=10)
        if proc.stdout:
            proc.stdout.close()
        if proc.stderr:
            proc.stderr.close()
    return path


class TestKillDashNine:
    """验收 6：kill -9 / 断电注入后无静默丢失。"""

    def test_the_kill_actually_produced_a_file_worth_recovering(
        self, killed_recording: Path
    ):
        # 前置断言：如果子进程根本没写出东西，下面几条就是在验空气。
        lines = killed_recording.read_text().splitlines()
        assert len(lines) > 200, "注入没写够数据，后续断言不成立"

    def test_recovery_returns_the_data_before_the_partial_line(
        self, killed_recording: Path
    ):
        recording, report = recover_recording(killed_recording)
        assert report.chunks_recovered > 100
        assert len(recording.chunks) == report.chunks_recovered

    def test_nothing_is_lost_silently(self, killed_recording: Path):
        """救回的块数必须等于文件里**完好**的数据行数 —— 一行不多一行不少。

        这才是「无静默丢失」的可执行含义：不是「读出来了」，而是「读出来的
        正好是还在的那些」。
        """
        lines = [
            line for line in killed_recording.read_text().splitlines() if line.strip()
        ]
        intact = 0
        for line in lines[1:]:  # 跳过头部
            try:
                json.loads(line)
            except json.JSONDecodeError:
                break
            intact += 1

        _recording, report = recover_recording(killed_recording)
        assert report.chunks_recovered == intact

    def test_the_recovered_data_still_replays_into_contract_frames(
        self, killed_recording: Path
    ):
        recording, report = recover_recording(killed_recording)

        async def scenario() -> list:
            return [f async for f in replay_recording(recording, queue_size=8192)]

        frames = asyncio.run(scenario())
        assert len(frames) == report.chunks_recovered
        assert frames[0].acc_raw.tolist() == [0, 1, 2]


class TestTruncationIsObservable:
    """验收 7：截断可观测，会话据此标为不完整。"""

    def test_kill_dash_nine_leaves_the_file_at_a_line_boundary(
        self, killed_recording: Path
    ):
        """实测结论：`kill -9` **不会**留下残行，因此不报截断。

        `ThreadedRecordingWriter` 用行缓冲（`buffering=1`），每行在换行处就已经
        交给内核；进程被杀时字节已经不在用户态缓冲区里了。8 次注入实测 0 次残行。

        所以「进程被杀」这个失败模式**已经被落盘侧的行缓冲选择挡住了** ——
        本条断言的是那个设计确实成立，而不是恢复能力。真正会造出残行的是
        **断电**（页缓存尚未落盘），见 `test_a_power_loss_style_truncation_...`。

        这条测试若哪天变红，说明有人把行缓冲改掉了 —— 那是个需要知道的改动。
        """
        _recording, report = recover_recording(killed_recording)
        assert not report.truncated
        assert report.complete
        assert killed_recording.read_text().endswith("\n")

    def test_a_power_loss_style_truncation_is_reported(
        self, killed_recording: Path, tmp_path: Path
    ):
        """断电的代理注入：在行中间截断。

        断电没法在测试里真的注入 —— 行缓冲只保证字节到了**内核**，没保证到
        **盘**，而那段窗口正是断电会丢的东西。这里用「砍掉半行」作代理，它
        与断电在文件上的表现一致。
        """
        forced = tmp_path / "forced.raw"
        text = killed_recording.read_text()
        forced.write_text(text[: text.rindex("\n") + 1] + '{"hex":"5561000')

        _recording, report = recover_recording(forced)
        assert report.truncated
        assert not report.complete

    def test_an_intact_recording_is_not_reported_as_truncated(self, tmp_path: Path):
        from gait.device.recorder import ThreadedRecordingWriter

        path = tmp_path / "clean.raw"
        writer = ThreadedRecordingWriter(path, device_id="dev-L")
        for _ in range(10):
            writer.write(_FRAME)
        writer.close()

        _recording, report = recover_recording(path)
        assert not report.truncated
        assert report.complete

    def test_truncation_reaches_the_session_outcome(self):
        """截断必须一路传到会话元数据 —— 停在读取那一步就等于没观测到。"""
        outcome = summarize_session(
            (LinkOutcome(foot="L", recording_truncated=True), LinkOutcome(foot="R"))
        )
        assert not outcome.complete
        assert any("崩溃截断" in p for p in outcome.problems)
        assert outcome.snapshot()["links"][0]["recording_truncated"] is True

    def test_truncation_and_write_failure_are_different_problems(self):
        """写盘当场失败 vs 进程被杀 —— 采集时前者有迹象、后者没有。"""
        truncated = summarize_session(
            (LinkOutcome(foot="L", recording_truncated=True), LinkOutcome(foot="R"))
        ).problems
        failed = summarize_session(
            (LinkOutcome(foot="L", recording_error="disk full"), LinkOutcome(foot="R"))
        ).problems
        assert truncated != failed

    def test_a_truncated_link_is_not_clean(self):
        assert not LinkOutcome(foot="L", recording_truncated=True).clean


class TestRecoveryIsNotSilent:
    def test_the_report_snapshot_carries_everything_needed(self, tmp_path: Path):
        report = RecoveryReport(path=tmp_path / "x.raw", truncated=True, chunks_recovered=7)
        snap = report.snapshot()
        assert snap["truncated"] is True
        assert snap["chunks_recovered"] == 7

    def test_middle_line_corruption_is_still_refused(self, tmp_path: Path):
        """只容忍末行。中间行坏说明文件被改过或拼接过，与崩溃无关。"""
        from gait.device.recorder import ThreadedRecordingWriter

        path = tmp_path / "corrupt.raw"
        writer = ThreadedRecordingWriter(path, device_id="dev-L")
        for _ in range(6):
            writer.write(_FRAME)
        writer.close()

        lines = path.read_text().splitlines()
        lines[3] = "{not json"
        path.write_text("\n".join(lines) + "\n")

        with pytest.raises(ValueError):
            recover_recording(path)
