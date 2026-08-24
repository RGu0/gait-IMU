"""`gait.cli.linktest`：无硬件下证明压测工具本身是对的。

端到端走 wt901 的回放传输：合成一段 200 Hz 的 0x61 字节流（含一个已知大小的
空洞），录成文件，再让 `run_bench` 以回放模式跑完整条采集→统计→报告通路。
工具对空洞的读数必须与注入值一致 —— 否则真机压测测出的任何数字都不可信。
"""

from __future__ import annotations

import asyncio
import json
import struct
from pathlib import Path

import numpy as np
from wt901.recording import RecordedChunk, Recording, write_recording

from gait.cli.linktest import (
    BenchEnvironment,
    run_bench,
    sustained_undersampling,
)
from gait.device.ble import StreamConfig

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

    report = _run(out_dir, [path_a, path_b], speed=None)

    devices = report["devices"]
    assert [d["samples"] for d in devices] == [expected_a, expected_b]
    assert [d["device_stats"]["dropped_samples"] for d in devices] == [0, 0]
    assert report["verdict"]["pass"], report["verdict"]["problems"]
    # 报告文件齐全，report.json 与返回值一致。
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
