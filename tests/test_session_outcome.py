"""RAY-496 会话结局必须落在盘上，而不是只活在进程里。

在此之前，`protocol_config` 是**建会话时**写下的快照 —— `state` 恒为 `walking`、
`elapsed_seconds` 恒为 0。于是走满 60 秒与第 5 秒手动停止在磁盘上一模一样，
而检测记录列表里两者都显示「完成」，因为它读的是 `integrity_report.complete`，
那个字段说的是「写队列没丢块」，不是「协议走完了」。

下面每一条都能失败：把收尾时的重新快照去掉、或把 `complete` 改成协议完成标志，
都会红。
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from gait.app.service import TerminalService
from gait.app.sources import StubDeviceSource
from gait.io.session import read_meta, session_directory, write_meta


def _run(root: Path, *, stop_at: float, source: StubDeviceSource | None = None) -> tuple[TerminalService, str]:
    """开一次会话并在 `stop_at` 停表。返回服务与 session_id。"""
    service = TerminalService(source=source or StubDeviceSource(), session_root=root)
    session_id = service.handle(
        {"id": "s", "method": "startSession", "params": {"now": 0.0}}
    )["result"]["sessionId"]
    service.handle({"id": "t", "method": "stopSession", "params": {"now": stop_at}})
    return service, session_id


def _session(root: Path, *, stop_at: float) -> str:
    return _run(root, stop_at=stop_at)[1]


def test_meta_records_the_walk_as_it_actually_ended(tmp_path) -> None:
    protocol = read_meta(
        session_directory(tmp_path, _session(tmp_path, stop_at=60.0))
    ).protocol_config
    assert protocol["state"] == "finished"
    assert protocol["elapsed_seconds"] == 60.0


def test_list_records_carries_the_outcome_and_not_only_write_integrity(tmp_path) -> None:
    """列表要能自己回答「走了多久」—— 否则渲染端只能拿 `complete` 硬凑。"""
    service, session_id = _run(tmp_path, stop_at=5.0)
    record = next(
        item
        for item in service.handle({"id": "l", "method": "listRecords"})["result"]
        if item["id"] == session_id
    )
    assert record["protocolState"] == "finished"
    assert record["elapsedSeconds"] == 5.0
    assert record["validSeconds"] == 5.0
    # 「走了多久 / 配了多久」这对比较是列表区分「完成」与「已停止」的全部依据。
    assert record["elapsedSeconds"] < record["protocolSeconds"]


def test_the_display_only_step_count_stays_out_of_the_session_metadata(tmp_path) -> None:
    """钉住 `StepCounter` 的那句话：这个数不进会话元数据。

    手边唯一的步数是 `source.step_counts()`，而它的文档写明「仅供显示…从不进报告、
    不进会话元数据，任何指标都不该从它算」—— 它数的是陀螺模长的上升沿，不是步态
    算法的步。把它落进元数据、再填进列表的「有效步数」列，是拿采集界面用的粗数冒充
    分析口径的有效步：空着是实话，填错了是假话，而且事后看不出来。

    本 Issue 的需求里写了「『有效步数』列一并接上」，开工后判定**不能这么接** ——
    真正的有效步数在 `core/` 里，要跑离线分析才有。理由与去向见验收记录。
    """
    service, session_id = _run(tmp_path, stop_at=60.0, source=StubDeviceSource(steps={"L": 41, "R": 40}))
    assert "session_outcome" not in read_meta(session_directory(tmp_path, session_id)).extra
    record = next(
        item
        for item in service.handle({"id": "l", "method": "listRecords"})["result"]
        if item["id"] == session_id
    )
    assert "validSteps" not in record


def test_write_integrity_stays_write_integrity(tmp_path) -> None:
    """钉住口径：`complete` 不是协议完成标志。

    第 5 秒就停的会话**照样**可以写盘完整。把 `complete` 顺手改成「走满了没有」，
    这条会红 —— 那正是要拦的那次改动：真出现丢块时就再也报不出来了。
    """
    short = read_meta(session_directory(tmp_path, _session(tmp_path, stop_at=5.0)))
    full = read_meta(session_directory(tmp_path, _session(tmp_path, stop_at=180.0)))
    assert short.integrity_report["complete"] is True
    assert full.integrity_report["complete"] is True
    # 而协议这一侧分得开。
    assert short.protocol_config["elapsed_seconds"] == 5.0
    assert full.protocol_config["elapsed_seconds"] == 180.0


def test_a_safely_stopped_session_does_not_look_like_a_killed_process(tmp_path) -> None:
    """写盘失败 → 安全停止。磁盘上要认得出「中断了，原因是这个」。

    `abort()` 的文档写明它存在的理由就是不让「被安全停止」与「进程被杀」在磁盘上长得
    一样。但它先 `_close_capture()` 再 `walk.abort()` —— 收尾时流程还没进中止态，
    落下的仍是开走前那份 `walking`，于是这两件事又长回一样了。
    """
    source = StubDeviceSource()
    service = TerminalService(source=source, session_root=tmp_path)
    session_id = service.handle(
        {"id": "s", "method": "startSession", "params": {"now": 0.0}}
    )["result"]["sessionId"]
    service.capture._writers["L"].error = OSError("[Errno 28] No space left on device")
    assert service.tick(1.0)["topic"] == "session.aborted"

    protocol_config = read_meta(session_directory(tmp_path, session_id)).protocol_config
    assert protocol_config["state"] == "aborted"
    assert "写盘失败" in protocol_config["abort_reason"]


def test_a_session_from_before_this_change_reads_as_unknown_not_as_zero(tmp_path) -> None:
    """旧会话磁盘上没有这些键。读出 `None`（不知道），不是 0（走了 0 秒）。"""
    session_id = _session(tmp_path, stop_at=60.0)
    directory = session_directory(tmp_path, session_id)
    meta = read_meta(directory)
    write_meta(
        directory,
        replace(meta, protocol_config={"duration_s": meta.protocol_config["duration_s"]}),
    )

    service = TerminalService(source=StubDeviceSource(), session_root=tmp_path)
    record = next(
        item
        for item in service.handle({"id": "l", "method": "listRecords"})["result"]
        if item["id"] == session_id
    )
    assert record["protocolState"] is None
    assert record["elapsedSeconds"] is None
    assert record["validSeconds"] is None
    assert record["abortReason"] is None


def test_list_records_skips_a_directory_without_meta(tmp_path) -> None:
    """建了目录却没写成 meta 的残骸不弄坏整张检测记录（真机 RC 上旧版留下过一个）。"""
    session_id = _session(tmp_path, stop_at=60.0)
    (tmp_path / "20260921T075528Z-dd9dba80" / "raw").mkdir(parents=True)
    service = TerminalService(source=StubDeviceSource(), session_root=tmp_path)
    response = service.handle({"id": "l", "method": "listRecords"})
    assert [item["id"] for item in response["result"]] == [session_id]
