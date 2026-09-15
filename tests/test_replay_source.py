"""RAY-493 合成/回放设备源：字节真的流过落盘路径，并且能算出一份报告。

与 `StubDeviceSource` 的差别就在这里 —— stub 推的随机帧只能证明「写盘路径通了」，
算不出步态；这里要证明的是**整条** 自检 → 采集 → 结论 → 报告 在没有硬件时走得通，
并且报告自己说出这是演示数据。
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from gait.app.replay import (
    DEFAULT_CHUNK_FRAMES,
    ReplayDeviceSource,
    UnavailableDeviceSource,
    frames_from_session,
    frames_from_synthetic,
)
from gait.app.service import PreviewPolicy, TerminalService
from gait.config import ProtocolConfig

SECONDS = 20.0


@pytest.fixture(scope="module")
def synthetic_chunks() -> dict:
    return frames_from_synthetic(SECONDS, seed=3)


def test_synthetic_chunks_are_200_hz_frames_in_small_groups(synthetic_chunks) -> None:
    assert set(synthetic_chunks) == {"L", "R"}
    for chunks in synthetic_chunks.values():
        times = [t for t, _ in chunks]
        assert times == sorted(times)
        # 每帧 20 字节，10 帧一段；最后一段可以不满。
        assert all(len(data) == 20 * DEFAULT_CHUNK_FRAMES for _, data in chunks[:-1])
        assert all(data[:2] == b"\x55\x61" for _, data in chunks)
        frames = sum(len(data) for _, data in chunks) // 20
        assert frames == pytest.approx(SECONDS * 200, abs=2)
        assert times[-1] == pytest.approx(SECONDS, abs=0.01)


def _wait_until_fed(source: ReplayDeviceSource, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while source._feeder is not None and source._feeder.is_alive():
        if time.monotonic() > deadline:
            raise AssertionError("回放在超时内没有推完")
        time.sleep(0.02)


def _walk_and_report(root: Path, source: ReplayDeviceSource) -> tuple[TerminalService, str, dict]:
    service = TerminalService(
        source=source,
        session_root=root,
        config=ProtocolConfig(duration_s=60),
        preview=PreviewPolicy(waive_factory_calibration=True),
    )
    items = service.handle({"id": "p", "method": "runPreflight"})["result"]
    assert {i["id"]: i["status"] for i in items}["factory-cal"] == "waived"
    assert all(i["status"] in ("pass", "waived") for i in items), items

    started = service.handle({"id": "s", "method": "startSession", "params": {"now": 0.0}})
    session_id = started["result"]["sessionId"]
    _wait_until_fed(source)
    stopped = service.handle({"id": "t", "method": "stopSession", "params": {"now": 60.0}})
    assert stopped["result"]["capture"]["complete"] is True
    result = service.handle(
        {"id": "r", "method": "sessionResult", "params": {"wearing": "pass"}}
    )["result"]
    assert result["report"]["status"] == "ready"
    report = service.handle({"id": "x", "method": "reportFor", "params": {"sessionId": session_id}})
    assert report["status"] == "ok", report
    return service, session_id, report["result"]


def test_synthetic_walk_produces_a_labelled_report(tmp_path, synthetic_chunks) -> None:
    source = ReplayDeviceSource(chunks=synthetic_chunks, label="synthetic", speed=50.0)
    service, _, report = _walk_and_report(tmp_path, source)

    by_key = {metric["key"]: metric for metric in report["metrics"]}
    # 合成真值：步长 1.3 m、步频 108 步/分。对不上说明字节没有完整流过落盘路径。
    assert float(by_key["stride"]["value"]) == pytest.approx(1.30, abs=0.15)
    assert float(by_key["cadence"]["value"]) == pytest.approx(108.0, abs=5.0)

    annotations = report["annotations"]
    assert any("预览版：出厂标定参数未匹配" in text for text in annotations)
    assert any("演示数据（合成/回放），非实测。" in text for text in annotations)

    record = service.handle({"id": "l", "method": "listRecords"})["result"][0]
    assert record["source"] == "synthetic"
    assert record["complete"] is True


def test_a_recorded_session_replays_into_a_new_one(tmp_path, synthetic_chunks) -> None:
    """回放一份会话，得到的新会话照样算得出报告，且来源写的是 replay。"""
    first = ReplayDeviceSource(chunks=synthetic_chunks, label="synthetic", speed=50.0)
    _, original, _ = _walk_and_report(tmp_path / "a", first)

    chunks = frames_from_session(tmp_path / "a" / original)
    assert set(chunks) == {"L", "R"}
    again = ReplayDeviceSource(chunks=chunks, label="replay", speed=50.0)
    service, _, report = _walk_and_report(tmp_path / "b", again)
    assert report["metrics"]
    assert service.handle({"id": "s", "method": "snapshot"})["result"]["source"] == "replay"


def test_step_counts_are_cosmetic_and_bounded(synthetic_chunks) -> None:
    clock = [0.0]
    source = ReplayDeviceSource(
        chunks=synthetic_chunks, label="synthetic", speed=1e9, clock=lambda: clock[0]
    )
    assert source.step_counts() == {"L": 0, "R": 0}
    source.begin_stream()
    _wait_until_fed(source)
    clock[0] = 10.0  # 远超合成时长：计步停在总时长上，不会一直涨
    counts = source.step_counts()
    assert counts["L"] == counts["R"] == int(SECONDS * 1.8 / 2)
    source.end_stream()
    assert source.step_counts() == {"L": 0, "R": 0}


def test_a_loader_does_not_block_construction() -> None:
    def slow() -> dict:
        time.sleep(0.3)
        return {"L": [(0.0, b"")], "R": [(0.0, b"")]}

    began = time.monotonic()
    source = ReplayDeviceSource(loader=slow, label="synthetic")
    assert time.monotonic() - began < 0.2
    assert source._ready.wait(2.0)
    assert source.provenance() == {
        "source": "synthetic",
        "hardware": False,
        "note": "演示数据（合成/回放），非实测。",
    }


def test_unavailable_source_fails_preflight_honestly() -> None:
    source = UnavailableDeviceSource(reason="BLE 模块缺席")
    items = TerminalService(source=source).handle({"id": "p", "method": "runPreflight"})["result"]
    link = next(item for item in items if item["id"] == "link-l")
    assert link["status"] == "fail"
    assert link["error"]["code"] == "E-BLE-1001"
    assert source.provenance() == {
        "source": "unavailable",
        "hardware": False,
        "note": "BLE 模块缺席",
    }
