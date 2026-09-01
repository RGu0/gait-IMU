"""`gait.sync.anchor` 的物理对碰锚点（RAY-212，工程模式）。

验收标准：**20 次对碰的时刻差标准差可量化输出**。

真机对碰属 RAY-213 的 V3′ 实验，这里全部用合成冲击注入：已知真值 offset 下
生成双侧冲击波形 + 传感器噪声，验证恢复精度。目标：插值后每对估计误差
< 1 个采样周期（5 ms @200 Hz），均值误差 ±2~3 ms。

## 合成模型里"真值"的定义

冲击在绝对时刻 T 同时作用于两台设备。设备各自以自己的晶振采样（两台的实际
采样率刻意不同），样本经 BLE 延迟（固有 `latency_min` + 单边长尾抖动）到达
主机。工具应恢复的真值是

    delta = latency_min_L − latency_min_R

—— 物理事件时刻 T 在两侧相减时消掉，剩下的正是主机侧同步方案不可观测、
需要物理锚点才能量出的那一项（`anchor.py` 模块文档）。
"""

import json
import math
import struct

import numpy as np
import pytest
from wt901.protocol.units import STANDARD_GRAVITY as G
from wt901.recording import RecordedChunk, Recording, write_recording

from gait.config import AlgoConfig, ConfigError
from gait.sync.anchor import (
    AnchorError,
    AnchorEvent,
    FootSignal,
    ImpactPeak,
    detect_impacts,
    measure_offsets,
    pair_events,
)

NOMINAL_FS = 200.0

#: 两侧固有链路延迟。不可观测、不相等 —— 其差就是要恢复的真值。
LATENCY_L = 0.012
LATENCY_R = 0.025
TRUE_DELTA = LATENCY_L - LATENCY_R


def hann_pulse(t: np.ndarray, centre: float, width: float, amp: float) -> np.ndarray:
    """cos² 脉冲：支撑宽度 `width`，峰在 `centre`。

    形状选它不是为了像真实冲击（真实冲击更陡），而是为了**可控**：峰位置解析
    已知、二阶可导，抛物线插值的误差全部来自方法本身而不是波形毛刺。
    """
    x = (t - centre) / width
    out = np.zeros_like(t)
    inside = np.abs(x) < 0.5
    out[inside] = amp * np.cos(np.pi * x[inside]) ** 2
    return out


def arrival_stream(
    t_true: np.ndarray,
    *,
    latency: float,
    rng: np.random.Generator,
    per_packet: int = 4,
    jitter_mean: float = 0.004,
) -> np.ndarray:
    """逐样本到达时刻。`synth_foot` 与 CLI fixture 共用**同一个**链路模型。

    性质与 `test_timebase.simulate_arrival` 一致：整包到达、延迟单边为正且
    长尾、有序交付。独立成函数是为了让"改到达模型"只有一处可改 —— 两份
    略有出入的副本正是合成真值测试要预防的那类漂移。
    """
    n = t_true.size
    arrival = np.empty(n)
    previous = -math.inf
    for begin in range(0, n, per_packet):
        end = min(begin + per_packet, n)
        moment = t_true[end - 1] + latency + rng.exponential(jitter_mean)
        moment = max(moment, previous)  # 有序交付
        arrival[begin:end] = moment
        previous = moment
    return arrival


def synth_foot(
    impacts,
    *,
    latency: float,
    fs_true: float,
    n: int = 6000,
    start: float = 0.0,
    per_packet: int = 4,
    jitter_mean: float = 0.004,
    noise: float = 0.05,
    amp: float = 8 * G,
    width: float = 0.02,
    clip_at: float | None = None,
    seed: int = 0,
) -> FootSignal:
    """一台设备的合成流。`impacts` 是绝对时刻列表，元素可为 `(时刻, 幅值)`。"""
    rng = np.random.default_rng(seed)
    t_true = start + np.arange(n) / fs_true
    magnitude = G + rng.normal(0.0, noise, n)
    for item in impacts:
        centre, this_amp = item if isinstance(item, tuple) else (item, amp)
        magnitude += hann_pulse(t_true, centre, width, this_amp)
    clipped = None
    if clip_at is not None:
        clipped = magnitude >= clip_at
        magnitude = np.minimum(magnitude, clip_at)
    arrival = arrival_stream(
        t_true, latency=latency, rng=rng, per_packet=per_packet, jitter_mean=jitter_mean
    )
    return FootSignal(magnitude=magnitude, arrival=arrival, clipped=clipped)


def dual_feet(impacts_l, impacts_r=None, **overrides):
    left = synth_foot(impacts_l, latency=LATENCY_L, fs_true=200.3, seed=1, **overrides)
    right = synth_foot(
        impacts_r if impacts_r is not None else impacts_l,
        latency=LATENCY_R,
        fs_true=199.8,
        seed=2,
        **overrides,
    )
    return left, right


# --- 冲击峰检测与插值 --------------------------------------------------------


def test_parabolic_interpolation_reaches_subsample_precision():
    """峰落在两个采样点之间时，插值应把它找回来 —— 这是"亚采样周期"的字面验证。"""
    fs = NOMINAL_FS
    t = np.arange(1000) / fs
    true_centre = 2.5017  # 刻意不落在采样格点上
    magnitude = G + hann_pulse(t, true_centre, 0.02, 8 * G)
    peaks = detect_impacts(magnitude, fs)
    assert len(peaks) == 1
    assert peaks[0].interpolated
    recovered = peaks[0].index / fs
    assert abs(recovered - true_centre) < 0.5 / fs  # 半个采样周期以内


def test_detection_ignores_walking_level_signal():
    """3 g 阈值下，1 g 基线加噪声不产生任何峰 —— 误报会污染配对。"""
    rng = np.random.default_rng(0)
    magnitude = G + rng.normal(0.0, 0.3, 4000)
    assert detect_impacts(magnitude, NOMINAL_FS) == []


def test_rebound_within_merge_window_is_one_event():
    """主峰后 60 ms 的回弹（次级冲击）并入同一事件，取主峰。"""
    fs = NOMINAL_FS
    t = np.arange(2000) / fs
    magnitude = (
        G
        + hann_pulse(t, 5.0, 0.02, 8 * G)
        + hann_pulse(t, 5.06, 0.02, 4 * G)
    )
    peaks = detect_impacts(magnitude, fs)
    assert len(peaks) == 1
    assert abs(peaks[0].index / fs - 5.0) < 1.0 / fs


def test_taps_beyond_merge_window_stay_separate():
    fs = NOMINAL_FS
    t = np.arange(3000) / fs
    magnitude = G + hann_pulse(t, 5.0, 0.02, 8 * G) + hann_pulse(t, 5.5, 0.02, 8 * G)
    assert len(detect_impacts(magnitude, fs)) == 2


def test_clipped_plateau_uses_midpoint_and_flags():
    """削顶平台取中点：对称削顶下峰时刻仍应有界，且 `clipped` 可见。"""
    fs = NOMINAL_FS
    t = np.arange(2000) / fs
    true_centre = 5.0025
    magnitude = G + hann_pulse(t, true_centre, 0.03, 30 * G)
    clip_level = 15.5 * G
    clipped = magnitude >= clip_level
    magnitude = np.minimum(magnitude, clip_level)
    assert clipped.sum() >= 2  # 前提：确实形成了平台
    peaks = detect_impacts(magnitude, fs, clipped=clipped)
    assert len(peaks) == 1
    assert peaks[0].clipped
    assert not peaks[0].interpolated
    assert abs(peaks[0].index / fs - true_centre) < 2.0 / fs


def test_double_clipped_plateau_uses_run_containing_peak():
    """主峰与回弹**都**削顶时，峰时刻取包含最大值的那个平台的中点 ——
    不是整个事件首尾削顶样本的中点（那会落在两平台之间，偏差几十毫秒）。"""
    fs = NOMINAL_FS
    t = np.arange(2000) / fs
    true_centre = 5.0
    magnitude = (
        G
        + hann_pulse(t, true_centre, 0.03, 30 * G)
        + hann_pulse(t, 5.06, 0.03, 20 * G)
    )
    clip_level = 15.5 * G
    clipped = magnitude >= clip_level
    magnitude = np.minimum(magnitude, clip_level)
    # 前提自检：确实是两个不相连的削顶平台。
    runs = np.flatnonzero(np.diff(np.flatnonzero(clipped)) > 1)
    assert runs.size >= 1
    peaks = detect_impacts(magnitude, fs, clipped=clipped)
    assert len(peaks) == 1
    assert peaks[0].clipped
    assert abs(peaks[0].index / fs - true_centre) < 2.0 / fs


def test_clipped_rebound_does_not_displace_unclipped_peak():
    """只有回弹样本被标削顶、主峰本身未削顶：峰时刻留在主峰，不挪去削顶段。"""
    fs = NOMINAL_FS
    t = np.arange(2000) / fs
    magnitude = G + hann_pulse(t, 5.0, 0.02, 12 * G) + hann_pulse(t, 5.06, 0.02, 9 * G)
    clipped = np.zeros_like(magnitude, dtype=bool)
    rebound_peak = round(5.06 * fs)
    clipped[rebound_peak - 1 : rebound_peak + 2] = True  # 模拟单轴满量程的回弹
    peaks = detect_impacts(magnitude, fs, clipped=clipped)
    assert len(peaks) == 1
    assert peaks[0].clipped  # 降级仍可见
    assert abs(peaks[0].index / fs - 5.0) < 1.0 / fs


def test_pairing_dense_events_is_optimal_not_greedy():
    """密集事件的反例：贪心（含单步前瞻）只配出 1 对并制造 2 个假落单，
    非交叉最优匹配配出全部 2 对。"""

    def event(t: float) -> AnchorEvent:
        peak = ImpactPeak(
            index=0.0, magnitude=100.0, clipped=False, interpolated=True, width_samples=3
        )
        return AnchorEvent(peak=peak, t_host=t, t_device=t)

    pairs, lone_left, lone_right = pair_events(
        [event(0.00), event(0.10)], [event(0.06), event(0.09)]
    )
    assert len(pairs) == 2
    assert not lone_left and not lone_right
    # 同数配对取 |Δ| 总和最小：0.00↔0.06 与 0.10↔0.09。
    assert abs(pairs[0].delta - (-0.06)) < 1e-12
    assert abs(pairs[1].delta - 0.01) < 1e-12


def test_invalid_inputs_are_refused():
    with pytest.raises(AnchorError, match="一维"):
        detect_impacts(np.zeros((3, 3)), NOMINAL_FS)
    with pytest.raises(AnchorError, match="fs"):
        detect_impacts(np.zeros(10), 0.0)
    with pytest.raises(AnchorError, match="NaN"):
        detect_impacts(np.full(10, np.nan), NOMINAL_FS)
    with pytest.raises(AnchorError, match="clipped"):
        detect_impacts(np.zeros(10), NOMINAL_FS, clipped=np.zeros(5, dtype=bool))


# --- 已知真值下的恢复精度（本 Issue 的核心验证）------------------------------


def test_known_offset_recovered_within_one_sample_period():
    """已知真值 −13 ms：每对误差 < 5 ms（1 个采样周期），均值误差 < 3 ms。"""
    taps = [3.0 + 1.5 * k for k in range(12)]
    left, right = dual_feet(taps)
    report = measure_offsets(left, right, NOMINAL_FS)
    assert len(report.pairs) == len(taps)
    assert not report.unpaired_left and not report.unpaired_right
    errors = report.deltas - TRUE_DELTA
    assert np.abs(errors).max() < 0.005, f"逐对误差 {errors * 1e3} ms"
    assert abs(report.offset_mean - TRUE_DELTA) < 0.003
    assert report.offset_std < 0.003


def test_acceptance_twenty_taps_std_is_quantified():
    """RAY-212 验收原文：20 次对碰的时刻差标准差可量化输出。"""
    taps = [3.0 + 1.2 * k for k in range(20)]
    left, right = dual_feet(taps, n=7000)
    report = measure_offsets(left, right, NOMINAL_FS)
    snapshot = report.snapshot()
    assert snapshot["offset"]["count"] == 20
    assert math.isfinite(snapshot["offset"]["std_s"])
    assert snapshot["offset"]["std_s"] < 0.003
    assert math.isfinite(snapshot["offset"]["drift_s_per_min"])


def test_light_tap_below_threshold_on_one_side_goes_unpaired():
    """轻碰单侧漏检：那一对不硬配，落进 unpaired 可见。"""
    taps = [3.0, 4.5, 6.0, 7.5]
    # 第 3 次对碰右侧只有 1 g 的脉冲（峰模值约 2 g）—— 3 g 阈值之下。
    impacts_r = [(t, 1 * G) if t == 6.0 else t for t in taps]
    left, right = dual_feet(taps, impacts_r)
    report = measure_offsets(left, right, NOMINAL_FS)
    assert len(report.pairs) == 3
    assert len(report.unpaired_left) == 1
    assert not report.unpaired_right
    assert abs(report.unpaired_left[0].t_host - (6.0 + LATENCY_L)) < 0.01


def test_spurious_extra_peak_pairs_to_nearest():
    """右侧多出一个假峰（间隔超合并窗但在配对窗内）：真峰配对，假峰落单。"""
    left, right = dual_feet([5.0], [5.0, (5.15, 6 * G)])
    report = measure_offsets(left, right, NOMINAL_FS)
    assert len(report.pairs) == 1
    assert abs(report.pairs[0].delta - TRUE_DELTA) < 0.005
    assert len(report.unpaired_right) == 1


def test_clipped_pair_is_degraded_but_still_bounded():
    taps = [3.0, 5.0, 7.0]
    left, right = dual_feet(taps, amp=30 * G, width=0.03, clip_at=15.5 * G)
    report = measure_offsets(left, right, NOMINAL_FS)
    assert len(report.pairs) == 3
    assert all(pair.degraded for pair in report.pairs)
    assert np.abs(report.deltas - TRUE_DELTA).max() < 0.01


def test_no_taps_gives_empty_report_not_error():
    left, right = dual_feet([])
    report = measure_offsets(left, right, NOMINAL_FS)
    assert report.pairs == ()
    assert math.isnan(report.offset_mean)
    assert math.isnan(report.offset_std)
    assert math.isnan(report.drift_s_per_min)


def test_mismatched_signal_shapes_are_refused():
    left, right = dual_feet([3.0])
    broken = FootSignal(magnitude=left.magnitude[:-1], arrival=left.arrival)
    with pytest.raises(AnchorError, match="形状"):
        measure_offsets(broken, right, NOMINAL_FS)


def test_anchor_config_must_be_positive():
    for name in (
        "anchor_threshold_m_s2",
        "anchor_merge_window_s",
        "anchor_pairing_window_s",
    ):
        with pytest.raises(ConfigError, match=name):
            AlgoConfig(**{name: 0.0})


# --- CLI：消费 wt901 录制文件（RAY-198/200 的落盘格式）------------------------


def _write_recording(path, device_id, magnitude_axis, arrival_abs, per_packet=4):
    """把 (轴向加速度, 绝对到达时刻) 写成 wt901 录制文件。

    帧负载是本测试真正要造的东西：`0x55 0x61` + 9×int16 LE（加速度 3、角速度 3、
    角度 3），重力放 z 轴、冲击放 x 轴 —— 模值 = hypot(x, 0, z)，与 CLI 的解析
    路径完全一致。文件级序列化交给 `wt901.recording.write_recording`：格式与
    版本头由格式的主人产出，wt901 收紧头部校验时本测试跟着走而不是悄悄漂移。
    `t` 按 wt901 语义**归零到首段字节**；返回该文件的 epoch（首段绝对时刻）。
    """
    scale = 32768.0 / (16.0 * G)
    counts_x = np.clip(np.rint(np.asarray(magnitude_axis) * scale), -32768, 32767).astype(int)
    count_z = round(G * scale)
    epoch = float(arrival_abs[0])
    n = len(magnitude_axis)
    chunks = []
    for begin in range(0, n, per_packet):
        end = min(begin + per_packet, n)
        payload = b"".join(
            bytes([0x55, 0x61])
            + struct.pack("<9h", int(counts_x[k]), 0, count_z, 0, 0, 0, 0, 0, 0)
            for k in range(begin, end)
        )
        chunks.append(RecordedChunk(t=round(arrival_abs[end - 1] - epoch, 6), data=payload))
    write_recording(
        path,
        Recording(
            device_id=device_id,
            created_utc="2026-08-25T00:00:00+00:00",
            note="synthetic anchor test",
            chunks=tuple(chunks),
        ),
    )
    return epoch


def _cli_fixture(tmp_path):
    """两份合成录制 + 各自 epoch + 真值对碰时刻。"""
    taps = [2.0, 4.0, 6.0, 8.0]
    files = {}
    epochs = {}
    for label, latency, fs_true, start, seed in (
        ("left", LATENCY_L, 200.3, 0.0, 11),
        ("right", LATENCY_R, 199.8, 0.4, 12),  # 开流晚 0.4 s：零点差就此出现
    ):
        rng = np.random.default_rng(seed)
        n = 2200
        t_true = start + np.arange(n) / fs_true
        axis = rng.normal(0.0, 0.05, n)
        for tap in taps:
            axis += hann_pulse(t_true, tap, 0.02, 8 * G)
        arrival = arrival_stream(t_true, latency=latency, rng=rng)
        path = tmp_path / f"{label}.raw"
        epochs[label] = _write_recording(path, f"AA:BB:{label}", axis, arrival)
        files[label] = path
    return files, epochs, taps


def test_cli_with_epochs_recovers_absolute_offset(tmp_path, capsys):
    from gait.cli.anchor import main

    files, epochs, taps = _cli_fixture(tmp_path)
    out = tmp_path / "out"
    code = main(
        [
            "--left", str(files["left"]),
            "--right", str(files["right"]),
            "--left-epoch", str(epochs["left"]),
            "--right-epoch", str(epochs["right"]),
            "--out", str(out),
        ]
    )
    assert code == 0
    report = json.loads((out / "anchor_report.json").read_text(encoding="utf-8"))
    assert report["common_clock"] is True
    assert report["offset"]["count"] == len(taps)
    assert abs(report["offset"]["mean_s"] - TRUE_DELTA) < 0.003
    assert report["offset"]["std_s"] < 0.003
    assert "跨足偏移" in capsys.readouterr().out


def test_cli_without_epochs_aligns_and_marks_relative_clock(tmp_path):
    """不给 epoch：零点差（此处 0.4 s 开流时差）冲破配对窗，必须靠粗对齐救回；
    对齐量进报告，均值不再是绝对偏移。"""
    from gait.cli.anchor import main

    files, epochs, taps = _cli_fixture(tmp_path)
    out = tmp_path / "out"
    code = main(
        ["--left", str(files["left"]), "--right", str(files["right"]), "--out", str(out)]
    )
    assert code == 0
    report = json.loads((out / "anchor_report.json").read_text(encoding="utf-8"))
    assert report["common_clock"] is False
    assert report["offset"]["count"] == len(taps)
    # 粗对齐吃掉的正是"零点差常数 + 真值偏移"——它被明示而不是被吞掉……
    constant = epochs["right"] - epochs["left"]
    assert report["alignment_applied_s"] is not None
    assert abs(report["alignment_applied_s"] - (TRUE_DELTA + constant)) < 0.005
    # ……于是均值按构造在零附近，而标准差不受常数平移影响 ——
    # RAY-212 的验收量在无 epoch 时依然成立。
    assert abs(report["offset"]["mean_s"]) < 0.005
    assert report["offset"]["std_s"] < 0.003


def test_coarse_alignment_recovers_axis_offset_in_core():
    """核心层：右侧时间轴整体平移 3 s（模拟各自归零），粗对齐后逐对精度不变。"""
    taps = [3.0 + 1.5 * k for k in range(8)]
    left, right = dual_feet(taps)
    shifted = FootSignal(
        magnitude=right.magnitude, arrival=right.arrival - 3.0, clipped=right.clipped
    )
    report = measure_offsets(left, shifted, NOMINAL_FS, coarse_align=True)
    assert len(report.pairs) == len(taps)
    assert report.alignment_applied_s is not None
    # 对齐量 = 轴平移 + 真值偏移（真值被一并吃进对齐，所以均值才不再可读）。
    assert abs(report.alignment_applied_s - (3.0 + TRUE_DELTA)) < 0.005
    assert abs(report.offset_mean) < 0.003
    assert report.offset_std < 0.003


def test_cli_session_layout_and_epoch_pairing_errors(tmp_path):
    from gait.cli.anchor import main

    files, _epochs, _ = _cli_fixture(tmp_path)
    raw = tmp_path / "session" / "raw"
    raw.mkdir(parents=True)
    (raw / "left.raw").write_bytes(files["left"].read_bytes())
    (raw / "right.raw").write_bytes(files["right"].read_bytes())
    assert main(["--session", str(tmp_path / "session")]) == 0

    with pytest.raises(SystemExit):
        main(["--left", str(files["left"])])  # 缺 --right
    with pytest.raises(SystemExit):
        main(
            [
                "--left", str(files["left"]),
                "--right", str(files["right"]),
                "--left-epoch", "5.0",  # 单边 epoch
            ]
        )
    with pytest.raises(SystemExit):
        # --session 与 --right 并用：静默忽略 --right 会分析错误的录制。
        main(["--session", str(tmp_path / "session"), "--right", str(files["right"])])


def test_cli_invalid_threshold_fails_cleanly(tmp_path, capsys):
    """--threshold-g 0 走"锚点分析失败"路径（退出码 2），不裸栈。"""
    from gait.cli.anchor import main

    files, _epochs, _ = _cli_fixture(tmp_path)
    code = main(
        ["--left", str(files["left"]), "--right", str(files["right"]), "--threshold-g", "0"]
    )
    assert code == 2
    assert "锚点分析失败" in capsys.readouterr().err


def test_cli_missing_file_fails_cleanly(tmp_path, capsys):
    from gait.cli.anchor import main

    code = main(
        ["--left", str(tmp_path / "no.raw"), "--right", str(tmp_path / "no2.raw")]
    )
    assert code == 2
    assert "锚点分析失败" in capsys.readouterr().err


def test_tap_window_excludes_walking_impacts():
    """对碰窗把步行段的足跟着地挡在锚点之外。

    由来是一次真实采集：对碰段内 5 对的 Δ 全在 ±8 ms，而步行段两次**不相关**的
    着地被硬配成一对，给出 −223 ms 的假 Δ，把 |Δ| 90 分位从 6.5 ms 抬到 115 ms。
    着地冲击（实测 3~6.7 g）与轻碰在幅值上重叠，靠阈值分不开；调用方本来就知道
    对碰段是哪一段，用时间窗比猜形状可靠。
    """
    taps = [2.0, 4.0, 6.0]
    # 晚期一对：左右错开 200 ms —— 两次不相关的着地，落在配对窗（250 ms）内，
    # 不限窗就会被硬配成一对。
    left, right = dual_feet([*taps, 22.0], [*taps, 22.2])

    wide = measure_offsets(left, right, NOMINAL_FS)
    windowed = measure_offsets(
        left, right, NOMINAL_FS, window=(left.arrival[0], left.arrival[0] + 10.0)
    )

    assert len(wide.pairs) == len(taps) + 1      # 假配对混了进来
    assert len(windowed.pairs) == len(taps)      # 限窗后只剩真对碰
    assert wide.offset_max_abs > 0.15            # 假 Δ 是几百毫秒量级
    # 窗内留下的都是真对碰：逐对相对真值（注入的固有延迟差 −13 ms）误差在
    # 一个采样周期内，而不限窗时那个假配对把最大值推到了 200 ms 以上。
    assert np.abs(windowed.deltas - TRUE_DELTA).max() < 0.005
    assert windowed.offset_max_abs < abs(TRUE_DELTA) + 0.005


def test_window_keeps_timebase_on_full_data():
    """限窗只筛锚点候选，时基仍用整段拟合 —— 样本越多回归越稳。"""
    left, right = dual_feet([3.0, 4.5, 6.0])
    full = measure_offsets(left, right, NOMINAL_FS)
    windowed = measure_offsets(
        left, right, NOMINAL_FS, window=(left.arrival[0], left.arrival[0] + 7.0)
    )
    assert windowed.left_sync.samples == full.left_sync.samples
    assert windowed.left_sync.fs == pytest.approx(full.left_sync.fs)
