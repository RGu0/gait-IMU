"""`gait.cli.linktest`：无硬件下证明压测工具本身是对的。

分两层，因为这两件事的失败方式不同：

1. **测量链路的正确性**（工具对空洞的读数是否等于注入值）—— 用**确定性的到达
   时刻数组**验证。它必须是确定性的：这是整个实验的信任根，不能取决于宿主的
   定时器精度。早先这一条挂在按原时序回放上，结果在 Windows CI 上把一段干净的
   录制读成 18% 丢包 —— 定时器粒度（15.6 ms）粗于 20 ms 的包节拍，回放复现不出
   节拍。那次失败是对的：它说明**时序断言不能建在 sleep 精度上**。
2. **采集通路的接线**（字节 → 帧 → 样本 → 到达时刻 → 报告文件）—— 走 wt901 的
   回放传输，只断言计数与产物，不断言任何时序派生量。

其余关切（残留样本过滤、录制失败进判定、时钟分辨率守卫）直接对内部函数与
`wt901.transport.memory.MemoryTransport` 白盒测试。
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
    CLOCK_RESOLUTION_RATIO,
    BenchEnvironment,
    DeviceRun,
    _consume,
    _finalize,
    _verdict,
    host_clock_resolution,
    run_bench,
    sustained_undersampling,
)
from gait.device.ble import StreamConfig
from gait.sync.integrity import assess

#: 一个足够细的时钟分辨率，用于把「时钟守卫」从其他断言里隔离出来。
_FINE_CLOCK = 1e-7

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


def _synthetic_arrivals(
    *, seconds: float, fs: float = 200.0, gap_at: float | None = None, lost: int = 0
) -> np.ndarray:
    """一条确定性的到达时刻序列：`fs` 均匀采样，可在 `gap_at` 处抠掉 `lost` 个。

    真机的到达时刻是成簇的（一次通知多个样本），但空洞检测只在包边界上找台阶，
    而均匀序列的每个样本都是边界 —— 对被测的判据而言这是更严格的输入，不是
    更宽松的。
    """
    total = int(seconds * fs)
    times = np.arange(total, dtype=np.float64) / fs
    if gap_at is None or lost <= 0:
        return times
    cut = int(gap_at * fs)
    return np.concatenate((times[:cut], times[cut + lost :]))


def test_injected_gap_is_measured_exactly_and_fails_criterion() -> None:
    """信任根：注入 N 个丢失，工具必须读出 N 个，并据此判不达标。

    走 `_finalize` + `_verdict` —— 与真机跑完之后完全相同的那条统计路径，
    只是到达时刻是构造的而不是量出来的，因此结果在任何宿主上都一样。
    """
    for lost in (4, 8, 20):
        arrival = _synthetic_arrivals(seconds=60, gap_at=30.0, lost=lost)
        run = DeviceRun(device_id="d0")
        run.arrivals = list(arrival)

        _finalize(run, 200.0)

        assert run.integrity is not None
        assert run.integrity.lost_samples == lost, (
            f"注入丢 {lost} 样本，读数 {run.integrity.lost_samples}"
        )
        assert len(run.integrity.gaps) == 1

        verdict = _verdict(
            [run], timing_valid=True, nominal_fs=200.0, clock_resolution=_FINE_CLOCK
        )
        # 12000 个样本里丢 4 个 = 0.033%，低于判据；丢 8/20 个也仍低于 0.5%。
        # 判据是**比率**，所以这里断言的是比率算对了，而不是「有空洞就失败」。
        expected_rate = lost / (run.integrity.received + lost)
        assert abs((run.loss_rate or 0.0) - expected_rate) < 1e-12
        assert verdict["pass"] is (expected_rate < 0.005)


def test_loss_rate_above_criterion_fails_verdict() -> None:
    """缺失率跨过 0.5% 时判定必须翻面。"""
    # 10 s @200 Hz = 2000 样本；丢 20 个 = 0.99% > 0.5%。
    arrival = _synthetic_arrivals(seconds=10, gap_at=5.0, lost=20)
    run = DeviceRun(device_id="d0")
    run.arrivals = list(arrival)
    _finalize(run, 200.0)

    assert run.integrity is not None
    assert run.integrity.lost_samples == 20
    assert (run.loss_rate or 0.0) > 0.005

    verdict = _verdict(
        [run], timing_valid=True, nominal_fs=200.0, clock_resolution=_FINE_CLOCK
    )
    assert not verdict["pass"]
    assert any("缺失率" in problem for problem in verdict["problems"])


def test_coarse_host_clock_invalidates_the_round() -> None:
    """时钟粗于采样周期 1/10 时，本轮结论不可用 —— 哪怕链路本身干净。

    这正是 Windows + Python 3.12 的处境（`time.monotonic()` = 15.6 ms，
    而 200 Hz 的周期是 5 ms）。
    """
    arrival = _synthetic_arrivals(seconds=10)
    run = DeviceRun(device_id="d0")
    run.arrivals = list(arrival)
    _finalize(run, 200.0)
    assert run.integrity is not None and run.integrity.lost_samples == 0

    coarse = _verdict(
        [run], timing_valid=True, nominal_fs=200.0, clock_resolution=0.015625
    )
    assert not coarse["pass"]
    assert coarse["clock_adequate"] is False
    assert coarse["timing_valid"] is False  # 时钟不够细，时序指标一并作废
    assert any("时钟分辨率" in problem for problem in coarse["problems"])

    # 边界：恰好等于 周期/比例 时算合格。
    exact = _verdict(
        [run],
        timing_valid=True,
        nominal_fs=200.0,
        clock_resolution=1.0 / 200.0 / CLOCK_RESOLUTION_RATIO,
    )
    assert exact["clock_adequate"] is True
    assert exact["pass"]


def test_this_host_clock_is_adequate_for_200hz() -> None:
    """本机（跑测试的这台）能不能测 200 Hz —— 失败即说明该换宿主或 Python。

    这条会在 Windows + Python 3.12 上失败，那不是测试的毛病：在那台机器上
    真机压测本来就测不出可信的到达率。
    """
    resolution = host_clock_resolution()
    assert resolution * CLOCK_RESOLUTION_RATIO <= 1.0 / 200.0, (
        f"本机 time.monotonic() 分辨率 {resolution * 1e3:.4g} ms，"
        "粗于 200 Hz 采样周期 5 ms 的 1/10；在这台机器上跑 RAY-200 压测，"
        "量到的缺失率是时钟的假象。Windows 需 Python ≥ 3.13。"
    )


def test_replay_wires_bytes_through_to_report_files(tmp_path: Path) -> None:
    """接线测试：字节 → 帧 → 样本 → 报告产物。**不断言任何时序派生量**。"""
    path_a = tmp_path / "a.jsonl"
    path_b = tmp_path / "b.jsonl"
    expected_a = _write_synthetic_recording(path_a, chunks=60)
    expected_b = _write_synthetic_recording(path_b, chunks=60)
    out_dir = tmp_path / "out"

    report = _run(out_dir, [path_a, path_b], speed=None)

    devices = report["devices"]
    assert [d["samples"] for d in devices] == [expected_a, expected_b]
    assert [d["device_stats"]["dropped_samples"] for d in devices] == [0, 0]
    assert [d["device_stats"]["resync_count"] for d in devices] == [0, 0]
    assert all(d["disconnected_at"] is None for d in devices)
    assert all(d["recording_error"] is None for d in devices)
    # 回放一律不认时序指标，无论倍速。
    assert report["verdict"]["timing_valid"] is False
    # 逐秒数组不进 report.json（在 per_second_*.csv 里，避免重复几千行）。
    assert "per_second_rate" not in json.dumps(devices[0]["integrity"])
    on_disk = json.loads((out_dir / "report.json").read_text(encoding="utf-8"))
    assert on_disk["verdict"] == report["verdict"]
    assert (out_dir / "report.md").exists()
    assert "时序指标无效" in (out_dir / "report.md").read_text(encoding="utf-8")


def test_replay_reports_host_clock_in_the_record(tmp_path: Path) -> None:
    """时钟分辨率必须进报告 —— 它是判断这份数据可不可信的前提条件。"""
    path = tmp_path / "a.jsonl"
    _write_synthetic_recording(path, chunks=30)
    out_dir = tmp_path / "out"

    report = _run(out_dir, [path], speed=None)

    clock = report["host_clock"]
    assert clock["monotonic_resolution_s"] == host_clock_resolution()
    assert clock["sample_period_s"] == 1.0 / 200.0
    assert clock["required_ratio"] == CLOCK_RESOLUTION_RATIO
    assert "主机单调时钟分辨率" in (out_dir / "report.md").read_text(encoding="utf-8")


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

    verdict = _verdict(
        [run], timing_valid=True, nominal_fs=200.0, clock_resolution=_FINE_CLOCK
    )

    assert not verdict["pass"]
    assert any("写盘失败" in problem for problem in verdict["problems"])
