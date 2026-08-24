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
import pytest
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
    analyze_recordings,
    host_clock_resolution,
    run_bench,
    sustained_undersampling,
)
from gait.device.ble import StreamConfig
from gait.sync.integrity import assess

#: 一个足够细的时钟分辨率，用于把「时钟守卫」从其他断言里隔离出来。
_FINE_CLOCK = 1e-7


class _FakeDiscovered:
    """`scan()` 结果的替身，只需要 name/address 两个字段。"""

    def __init__(self, name: str) -> None:
        self.name = name
        self.address = f"addr-{name}"
        self.rssi = -40
        self.handle = None

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


def test_verdict_classifies_this_host_clock_consistently() -> None:
    """用**本机真实时钟**跑一遍守卫，断言分类与实测分辨率一致。

    实测值（CI 已确认）：macOS = 41.7 ns ✅；**Windows + Python 3.12 =
    15.62 ms ❌**（粗于 200 Hz 的 5 ms 周期）。

    这里不把「本机测不了 200 Hz」判成测试失败 —— 那是宿主的属性，不是代码的
    缺陷，让它长红只会被人静音。拦住不可信实验的是**工具运行时的守卫**
    （`_verdict` 里那条，已由上面几条确定性用例覆盖）；这条负责证明守卫接的是
    真实时钟，并在宿主不合格时以 skip 理由把事实喊出来。
    """
    arrival = _synthetic_arrivals(seconds=10)
    run = DeviceRun(device_id="d0")
    run.arrivals = list(arrival)
    _finalize(run, 200.0)

    resolution = host_clock_resolution()
    verdict = _verdict(
        [run], timing_valid=True, nominal_fs=200.0, clock_resolution=resolution
    )
    adequate = resolution * CLOCK_RESOLUTION_RATIO <= 1.0 / 200.0
    assert verdict["clock_adequate"] is adequate

    if not adequate:
        pytest.skip(
            f"本机 time.monotonic() 分辨率 {resolution * 1e3:.4g} ms，粗于 200 Hz "
            "采样周期 5 ms 的 1/10 —— 在这台机器上跑 RAY-200 压测，量到的缺失率是"
            "时钟的假象。Windows 需 Python ≥ 3.13。工具会在运行时拒绝出结论。"
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


def test_analyze_recovers_exact_timing_from_a_recording(tmp_path: Path) -> None:
    """离线补算必须采信录制里记的到达时刻 —— 那是抢救被打断一轮的全部依据。

    构造一段带已知空洞的录制（包间隔 20 ms，每包 4 帧 = 200 Hz），补算出的
    丢失数必须精确等于注入值，且 `timing_valid` 为真（与回放相反）。
    """
    path = tmp_path / "interrupted.jsonl"
    dropped = (60, 61)  # 连丢 2 包 = 8 个样本
    _write_synthetic_recording(path, chunks=125, dropped=dropped)

    report = analyze_recordings(
        paths=[path],
        out_dir=tmp_path / "out",
        env=_ENV,
        nominal_fs=200.0,
        echo=lambda *_: None,
    )

    device = report["devices"][0]
    assert device["samples"] == (125 - len(dropped)) * _FRAMES_PER_CHUNK
    assert device["integrity"]["lost_samples"] == len(dropped) * _FRAMES_PER_CHUNK
    # 与 --replay 的关键区别：录制里的 t 是真时刻，所以时序指标有效。
    assert report["verdict"]["timing_valid"] is True
    assert report["source"] == "analysis"
    assert report["analyzed"] == [str(path)]
    md = (tmp_path / "out" / "report.md").read_text(encoding="utf-8")
    assert "离线补算" in md
    assert "时序指标**有效**" in md


def test_analyze_and_live_reports_are_structurally_identical(tmp_path: Path) -> None:
    """两条路径的报告必须逐字段同构，否则评审无法直接比对。"""
    path = tmp_path / "a.jsonl"
    _write_synthetic_recording(path, chunks=40)

    analyzed = analyze_recordings(
        paths=[path],
        out_dir=tmp_path / "an",
        env=_ENV,
        nominal_fs=200.0,
        echo=lambda *_: None,
    )
    replayed = _run(tmp_path / "rp", [path], speed=None)

    # 共有字段必须同构；分析路径可以多带自己的溯源字段（trimmed 只对它有意义）。
    assert set(replayed) <= set(analyzed)
    assert set(analyzed) - set(replayed) == {"trimmed"}
    assert set(analyzed["devices"][0]) == set(replayed["devices"][0])
    assert set(analyzed["verdict"]) == set(replayed["verdict"])
    assert set(analyzed["host_clock"]) == set(replayed["host_clock"])


def test_streaming_span_trims_only_the_edges() -> None:
    """录制首尾的 10 Hz 段（电量/配置阶段）要裁掉，中间的低速段绝不能裁。"""
    from gait.cli.linktest import _streaming_span

    # 首 3 包与末 2 包是 1 帧/包（10 Hz），中间是 8 帧/包（200 Hz），
    # 其中第 10 包是一次真实的链路劣化 —— 它必须留在区间内。
    counts = [1, 1, 1] + [8] * 6 + [1] + [8] * 6 + [1, 1]
    first, last = _streaming_span(counts)

    assert (first, last) == (3, len(counts) - 3)
    assert first < 10 < last, "中间的低速段被裁掉了 —— 那是要测的东西"


def test_streaming_span_handles_a_recording_with_no_streaming() -> None:
    from gait.cli.linktest import _streaming_span

    assert _streaming_span([]) == (0, -1)
    assert _streaming_span([0, 0, 0]) == (0, 2)


def test_analyze_trims_the_low_rate_tail_before_measuring(tmp_path: Path) -> None:
    """回归：真机 round-2 补算时，卡死期间 11 分钟的 10 Hz 尾巴把缺失率顶到 37.8%。"""
    path = tmp_path / "with_tail.jsonl"
    frame = _FRAME
    chunks = [
        RecordedChunk(t=i * _CHUNK_INTERVAL, data=frame * _FRAMES_PER_CHUNK)
        for i in range(200)
    ]
    # 尾部 40 段 10 Hz（每包 1 帧，间隔 100 ms）——- 就是电量复读/卡死那一段。
    tail_start = chunks[-1].t
    chunks += [
        RecordedChunk(t=tail_start + 0.1 * (i + 1), data=frame) for i in range(40)
    ]
    write_recording(
        path,
        Recording(
            device_id="tail",
            created_utc="2026-08-24T00:00:00+00:00",
            note="",
            chunks=tuple(chunks),
        ),
    )

    report = analyze_recordings(
        paths=[path],
        out_dir=tmp_path / "out",
        env=_ENV,
        nominal_fs=200.0,
        echo=lambda *_: None,
    )

    device = report["devices"][0]
    assert device["samples"] == 200 * _FRAMES_PER_CHUNK, "尾巴没被裁掉"
    assert report["trimmed"][0]["trailing_s"] > 3.0
    assert device["integrity"]["lost_samples"] == 0
    assert report["verdict"]["pass"]


def test_scan_retries_until_both_devices_appear() -> None:
    """真机实测：单次扫描常只看到一台，且每次是不同的那台。必须重试。"""
    from gait.cli import linktest as mod

    calls: list[float] = []
    rounds = [
        [_FakeDiscovered("A")],  # 只看到 A
        [_FakeDiscovered("B")],  # 只看到 B
        [_FakeDiscovered("A"), _FakeDiscovered("B")],  # 终于都看到
    ]

    async def fake_scan(timeout: float, **_: object) -> list[object]:
        calls.append(timeout)
        return rounds[len(calls) - 1]

    async def scenario() -> list[object]:
        original = mod.scan
        mod.scan = fake_scan  # type: ignore[assignment]
        try:
            return await mod._scan_for(2, 20.0, 4, lambda *_: None)
        finally:
            mod.scan = original  # type: ignore[assignment]

    found = asyncio.run(scenario())
    assert len(found) == 2
    assert len(calls) == 3, "应当一直重试到扫够为止"


def test_scan_gives_up_after_the_attempt_budget() -> None:
    """重试不是无限的：扫不够就把最后一次的结果交回去，由调用方报错退出。"""
    from gait.cli import linktest as mod

    calls: list[float] = []

    async def fake_scan(timeout: float, **_: object) -> list[object]:
        calls.append(timeout)
        return [_FakeDiscovered("A")]

    async def scenario() -> list[object]:
        original = mod.scan
        mod.scan = fake_scan  # type: ignore[assignment]
        try:
            return await mod._scan_for(2, 5.0, 3, lambda *_: None)
        finally:
            mod.scan = original  # type: ignore[assignment]

    found = asyncio.run(scenario())
    assert len(found) == 1
    assert len(calls) == 3


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
