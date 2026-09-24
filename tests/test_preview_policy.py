"""RAY-493 预览策略：出厂标定放行必须**看得见**。

每一条都能失败：把放行写成悄悄变 `pass`、或把标注从进程状态而不是落盘元数据里读，
下面至少有一条会红。
"""

from __future__ import annotations

import uuid
from pathlib import Path

from wt901.recording import RecordedChunk, Recording, write_recording

from gait.app.replay import frames_from_synthetic
from gait.app.service import PreviewPolicy, TerminalService
from gait.app.sources import StubDeviceSource
from gait.calib.store import CalibrationRecord, CalibrationStore
from gait.io.session import raw_path, read_meta, session_directory

WAIVER_NOTE = "预览版：出厂标定参数未匹配，按预览策略放行；数值不作为评估依据。"
DEMO_NOTE = "演示数据（合成/回放），非实测。"


def _factory_cal(service: TerminalService) -> dict:
    items = service.handle({"id": "p", "method": "runPreflight"})["result"]
    return next(item for item in items if item["id"] == "factory-cal")


def _seed_all(root: Path, source: StubDeviceSource) -> None:
    store = CalibrationStore(root)
    for reading in source.device_readings().values():
        store.put(
            CalibrationRecord(
                kind=reading["kind"],
                value=reading["value"],
                provenance=reading["provenance"],
                firmware=reading["firmware"],
                recorded_at="2026-09-04T00:00:00+00:00",
                calib_snapshot={"method": "multi-orientation-magnitude"},
            )
        )


def _preview(**kwargs) -> TerminalService:
    return TerminalService(preview=PreviewPolicy(waive_factory_calibration=True), **kwargs)


def test_without_a_policy_missing_calibration_still_blocks(tmp_path) -> None:
    item = _factory_cal(TerminalService(source=StubDeviceSource(), session_root=tmp_path))
    assert item["status"] == "fail"
    assert item["error"]["code"] == "E-CAL-3001"


def test_policy_turns_the_block_into_a_visible_waiver(tmp_path) -> None:
    item = _factory_cal(_preview(source=StubDeviceSource(), session_root=tmp_path))
    assert item["status"] == "waived"  # 不是 pass：与真通过长得不一样
    assert item["error"] is None
    assert "预览版" in item["hint"]
    assert item["waiver"]["code"] == "E-CAL-3001"
    assert item["waiver"]["reasons"], "放行了什么要说得出来"


def test_policy_does_not_waive_what_is_already_admitted(tmp_path) -> None:
    source = StubDeviceSource()
    _seed_all(tmp_path, source)
    service = _preview(source=source, session_root=tmp_path)
    assert _factory_cal(service)["status"] == "pass"

    session_id = service.handle(
        {"id": "s", "method": "startSession", "params": {"now": 0.0}}
    )["result"]["sessionId"]
    service.handle({"id": "t", "method": "stopSession", "params": {"now": 1.0}})
    meta = read_meta(session_directory(tmp_path, session_id))
    assert meta.extra["preview"]["factory_calibration_waived"] is False


def test_session_meta_records_the_waiver_and_provenance(tmp_path) -> None:
    service = _preview(source=StubDeviceSource(), session_root=tmp_path)
    subject = str(uuid.uuid4())
    session_id = service.handle(
        {"id": "s", "method": "startSession", "params": {"now": 0.0, "subjectUuid": subject}}
    )["result"]["sessionId"]
    service.handle({"id": "t", "method": "stopSession", "params": {"now": 1.0}})

    meta = read_meta(session_directory(tmp_path, session_id))
    assert meta.subject_uuid == subject
    assert meta.extra["preview"] == {
        "waive_factory_calibration": True,
        "factory_calibration_waived": True,
    }
    assert meta.extra["provenance"]["source"] == "stub"


def test_production_meta_has_no_preview_block(tmp_path) -> None:
    service = TerminalService(source=StubDeviceSource(), session_root=tmp_path)
    session_id = service.handle(
        {"id": "s", "method": "startSession", "params": {"now": 0.0}}
    )["result"]["sessionId"]
    service.handle({"id": "t", "method": "stopSession", "params": {"now": 1.0}})
    assert "preview" not in read_meta(session_directory(tmp_path, session_id)).extra


def test_a_non_uuid_subject_never_reaches_the_session_file(tmp_path) -> None:
    """档案号这类明文传错了字段，也不能落进会话文件（FR-02）。"""
    service = TerminalService(source=StubDeviceSource(), session_root=tmp_path)
    session_id = service.handle(
        {"id": "s", "method": "startSession", "params": {"now": 0.0, "subjectUuid": "A-12345"}}
    )["result"]["sessionId"]
    service.handle({"id": "t", "method": "stopSession", "params": {"now": 1.0}})
    stored = read_meta(session_directory(tmp_path, session_id)).subject_uuid
    assert stored != "A-12345"
    uuid.UUID(stored)


def test_snapshot_says_where_the_data_comes_from() -> None:
    production = TerminalService().handle({"id": "1", "method": "snapshot"})["result"]
    assert production["source"] == "stub"
    assert production["preview"] is False
    assert _preview().handle({"id": "1", "method": "snapshot"})["result"]["preview"] is True


def test_list_records_carries_time_completion_and_source(tmp_path) -> None:
    service = TerminalService(source=StubDeviceSource(), session_root=tmp_path)
    service.handle({"id": "s", "method": "startSession", "params": {"now": 0.0}})
    service.handle({"id": "t", "method": "stopSession", "params": {"now": 1.0}})
    # 第二个会话不收尾：元数据停在 pending，`complete` 应当是「不知道」。
    pending = TerminalService(source=StubDeviceSource(), session_root=tmp_path)
    pending.handle({"id": "s", "method": "startSession", "params": {"now": 0.0}})

    records = {r["id"]: r for r in service.handle({"id": "l", "method": "listRecords"})["result"]}
    assert len(records) == 2
    finished = records[service.session_id]
    assert finished["createdAt"]
    assert isinstance(finished["complete"], bool)
    assert finished["source"] == "stub"
    assert records[pending.session_id]["complete"] is None
    assert {"id", "subjectUuid", "protocolSeconds", "algoVersion"} <= set(finished)
    pending._close_capture()


def test_reopened_report_reads_the_annotations_from_disk(tmp_path) -> None:
    """重开历史记录：新进程**没有**预览策略、设备源也换了，标注照样从元数据里来。"""
    service = _preview(source=StubDeviceSource(), session_root=tmp_path)
    session_id = service.handle(
        {"id": "s", "method": "startSession", "params": {"now": 0.0}}
    )["result"]["sessionId"]
    service.handle({"id": "t", "method": "stopSession", "params": {"now": 1.0}})
    # 用一段真能算出步态的合成步行替换掉 stub 的随机字节。
    for label, chunks in frames_from_synthetic(20.0, seed=3, chunk_frames=1).items():
        write_recording(
            raw_path(tmp_path, session_id, label),
            Recording(
                device_id=f"dev-{label}",
                created_utc="",
                note="",
                chunks=tuple(RecordedChunk(t=t, data=data) for t, data in chunks),
            ),
        )

    reopened = TerminalService(session_root=tmp_path)
    report = reopened.handle({"id": "r", "method": "reportFor", "params": {"sessionId": session_id}})
    assert report["status"] == "ok", report
    annotations = report["result"]["annotations"]
    assert any(WAIVER_NOTE in text for text in annotations)
    assert any(DEMO_NOTE in text for text in annotations)


class _Lifecycle(StubDeviceSource):
    refreshed = 0
    closed = 0

    def refresh(self) -> None:
        type(self).refreshed += 1

    def close(self) -> None:
        type(self).closed += 1


def test_optional_refresh_and_close_are_called() -> None:
    service = TerminalService(source=_Lifecycle())
    service.handle({"id": "1", "method": "recheckDevices"})
    service.handle({"id": "2", "method": "runPreflight"})
    service.close()
    assert _Lifecycle.refreshed == 2
    assert _Lifecycle.closed == 1


def test_device_page_says_the_calibration_was_waived_not_missing() -> None:
    """RAY-530：设备页曾在预览放行下仍显示红色「缺少出厂标定」，与自检的 waived 矛盾。"""

    def modules(service: TerminalService) -> list[dict]:
        return service.handle({"id": "d", "method": "deviceSupport"})["result"]["modules"]

    for module in modules(_preview(source=StubDeviceSource())):
        assert module["factoryCalibrated"] is False
        assert module["factoryCalibrationWaived"] is True
    for module in modules(TerminalService(source=StubDeviceSource())):
        assert module["factoryCalibrated"] is False
        assert module["factoryCalibrationWaived"] is False
