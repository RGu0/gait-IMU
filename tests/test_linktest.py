"""`gait.cli.linktest`：无硬件下证明压测工具本身是对的。

端到端走 wt901 的回放传输：合成一段 200 Hz 的 0x61 字节流（含一个已知大小的
空洞），录成文件，再让 `run_bench` 以回放模式跑完整条采集→统计→报告通路。
工具对空洞的读数必须与注入值一致 —— 否则真机压测测出的任何数字都不可信。

其余关切（残留样本过滤、非 1.0 倍速时序失效、录制失败进判定）绕过 BLE，
直接对内部函数与 `wt901.transport.memory.MemoryTransport` 白盒测试。
"""

from __future__ import annotations

import asyncio
import json
import struct
from pathlib import Path

import numpy as np
from wt901 import WT901Device
from wt901.recording import RecordedChunk, Recording, write_recording
from wt901.transport.memory import MemoryTransport

from gait.cli.linktest import (
    BenchEnvironment,
    DeviceRun,
    _consume,
    _verdict,
    run_bench,
    sustained_undersampling,
)
from gait.device.ble import StreamConfig
from gait.sync.integrity import assess

_FRAME = b"\x55\x61" + struct.pack("<9h", *range(9))
_ENV = BenchEnvironment(
    label="selftest", distance_m=None, occlusion="无", note="合成回放"
)

#: 4 帧一包、每 20 ms 一包 = 200 Hz，与真机 50 Hz 以上打包传输的形态一致。
_FRAMES_PER_CHUNK = 4
_CHUNK_INTERVAL = 0.02


def _write_synthetic_recording(
    path: Path, *, chunks: int, dropped: tuple[int, ...] = ()
) -> int:
    """写一段合成录制，返回其中的样本数。``dropped`` 里的包被整包丢弃。"""
    recorded = tuple(
        RecordedChunk(t=index * _CHUNK_INTERVAL, data=_FRAME * _FRAMES_PER_CHUNK)
        for index in range(chunks)
        if index not in dropped
    )
    write_recording(
        path,
        Recording(
            device_id=f"replay-{path.stem}",
            created_utc="2026-08-23T00:00:00+00:00",
            note="synthetic",
            chunks=recorded,
        ),
    )
    return len(recorded) * _FRAMES_PER_CHUNK


def _run(out_dir: Path, replay_files: list[Path], speed: float | None) -> dict:
    return asyncio.run(
        run_bench(
            duration=30.0,
            out_dir=out_dir,
            env=_ENV,
            config=StreamConfig(),
            nominal_fs=200.0,
            replay_files=replay_files,
            replay_speed=speed,
            echo=lambda *_: None,
        )
    )


def test_sustained_undersampling_flags_real_deficit_not_jitter() -> None:
    steady = np.full(120, 1.0)
    assert sustained_undersampling(steady) == []

    # 单秒毛刺（integrity.py 实测无丢包时逐秒最低 0.94）不该报。
    jitter = steady.copy()
    jitter[30] = 0.94
    jitter[31] = 1.06
    assert sustained_undersampling(jitter) == []

    # 持续 30 s 跑在 97%：真实的欠采，必须报，且起点落在低速段附近。
    deficit = steady.copy()
    deficit[40:80] = 0.97
    windows = sustained_undersampling(deficit)
    assert windows
    assert 10 <= windows[0] <= 41

    # 整轮不足一个窗口时退化为整轮均值判定。
    assert sustained_undersampling(np.full(5, 0.9)) == [0]
    assert sustained_undersampling(np.full(5, 1.0)) == []


def test_clean_replay_passes_and_counts_every_sample(tmp_path: Path) -> None:
    path_a = tmp_path / "a.jsonl"
    path_b = tmp_path / "b.jsonl"
    expected_a = _write_synthetic_recording(path_a, chunks=60)
    expected_b = _write_synthetic_recording(path_b, chunks=60)
    out_dir = tmp_path / "out"

    report = _run(out_dir, [path_a, path_b], speed=1.0)

    devices = report["devices"]
    assert [d["samples"] for d in devices] == [expected_a, expected_b]
    assert [d["device_stats"]["dropped_samples"] for d in devices] == [0, 0]
    assert all(d["disconnected_at"] is None for d in devices)
    assert all(d["recording_error"] is None for d in devices)
    assert report["verdict"]["pass"], report["verdict"]["problems"]
    assert report["verdict"]["timing_valid"] is True
    # 逐秒数组不进 report.json（在 per_second_*.csv 里，避免重复几千行）。
    assert "per_second_rate" not in json.dumps(devices[0]["integrity"])
    on_disk = json.loads((out_dir / "report.json").read_text(encoding="utf-8"))
    assert on_disk["verdict"] == report["verdict"]
    assert (out_dir / "report.md").exists()


def test_injected_gap_is_measured_and_fails_criterion(tmp_path: Path) -> None:
    """按原时序回放 2.5 s，丢 2 个整包（8 样本）：缺失率 1.6%，必须判不达标。"""
    path = tmp_path / "gap.jsonl"
    samples = _write_synthetic_recording(path, chunks=125, dropped=(60, 61))
    out_dir = tmp_path / "out"

    report = _run(out_dir, [path], speed=1.0)

    device = report["devices"][0]
    assert device["samples"] == samples
    integrity = device["integrity"]
    lost = integrity["lost_samples"]
    # 注入 8 个：空洞检测的读数要对得上（integrity.py 声明其估计精确）。
    assert 6 <= lost <= 10, f"注入丢 8 样本，读数 {lost}"
    assert integrity["gaps"], "应检测到空洞"
    loss_rate = device["loss_rate"]
    assert loss_rate is not None and loss_rate > 0.005
    assert not report["verdict"]["pass"]
    assert any("缺失率" in problem for problem in report["verdict"]["problems"])
    # 逐秒曲线落了盘。
    per_second = (out_dir / "per_second_0.csv").read_text(encoding="utf-8")
    assert per_second.startswith("second,arrival_rate,lost_samples")
    # 判定写进了人读的报告。
    assert "不达标" in (out_dir / "report.md").read_text(encoding="utf-8")


def test_full_speed_replay_is_flagged_timing_invalid(tmp_path: Path) -> None:
    """全速回放压缩了到达时刻，时序类指标不构成链路结论，必须显式标注。"""
    path = tmp_path / "a.jsonl"
    _write_synthetic_recording(path, chunks=60)
    out_dir = tmp_path / "out"

    report = _run(out_dir, [path], speed=None)

    assert report["verdict"]["timing_valid"] is False
    assert "⚠️" in (out_dir / "report.md").read_text(encoding="utf-8")


def test_consume_drops_samples_older_than_started() -> None:
    """配置/电量阶段的残留样本必须被过滤，否则会污染统计（见模块 docstring）。

    `WT901Device.connect()` 只接受 BLE target；这里直接构造设备并 `open()`
    一个 `MemoryTransport`，绕开 BLE 单独测这条过滤逻辑。
    """

    async def scenario() -> None:
        transport = MemoryTransport(device_id="dev")
        device = WT901Device(transport)
        await device.open()
        run = DeviceRun(device_id=device.device_id)
        loop = asyncio.get_running_loop()

        transport.feed(_FRAME)  # 配置阶段的残留：晚点才会被判定为"过早"。
        await asyncio.sleep(0.02)
        started = loop.time()
        transport.feed(_FRAME)  # started 之后的真实样本。

        consumer = asyncio.ensure_future(_consume(device, run, started))
        await asyncio.sleep(0.05)
        consumer.cancel()
        await asyncio.gather(consumer, return_exceptions=True)
        await device.close()

        assert len(run.arrivals) == 1, "只有 started 之后的样本应被计入"

    asyncio.run(scenario())


def test_verdict_flags_recording_error_alongside_integrity_problems() -> None:
    """写盘失败（磁盘满等）必须体现在判定里，即使到达率本身达标。"""
    arrival = np.arange(400, dtype=np.float64) / 200.0  # 2 s 干净的 200 Hz。
    run = DeviceRun(device_id="d0")
    run.arrivals = list(arrival)
    run.arrival_array = arrival
    run.integrity = assess(arrival, 200.0)
    run.sustained_windows = []
    run.recording_error = "OSError('磁盘已满')"

    verdict = _verdict([run], timing_valid=True)

    assert not verdict["pass"]
    assert any("写盘失败" in problem for problem in verdict["problems"])
