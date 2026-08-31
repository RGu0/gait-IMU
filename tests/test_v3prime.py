"""`gait.validate.v3prime` 与 `gait.cli.v3prime` 的 V3′ 实验工装（RAY-213）。

真机实验（≥5 人 × ≥3 趟）不在这里 —— 这个文件验的是**工装本身**：注入一个已知
的跨足偏差 Δ，看管线能不能把它量出来、能不能算对它造成的指标偏差、以及 R1 判据
是不是真的按 06 v1.1 §5 的两个数执行。

一条贯穿始终的自检：RAY-264 实测的 `SI ≈ 2Δ / 步时`。它既是判据的推导依据，也是
本文件用来交叉验证"指标偏差算对了没有"的独立参照 —— 如果管线算出来的 SI 偏差与
这个闭式不符，那么不是管线错了，就是那条依据错了，两者都必须当场知道。
"""

import dataclasses
import json
import math
from dataclasses import replace

import numpy as np
import pytest

from gait.analysis.events import segment_cycles
from gait.config import AlgoConfig
from gait.core.zupt import detect_stance
from gait.sync.anchor import AnchorEvent, AnchorPair, AnchorReport, ImpactPeak
from gait.sync.timebase import SyncReport
from gait.validate.synthetic import WalkSpec, generate_dual_walk
from gait.validate.v3prime import (
    NEGLIGIBLE_MEDIAN_S,
    NEGLIGIBLE_P90_S,
    V3PrimeError,
    Verdict,
    evaluate_trial,
    paired_double_support,
    shift_cycles,
    summarize,
)

CFG = AlgoConfig()
QUALITY = {"offset_estimate": None, "flagged": False, "reasons": []}


def _sync_report(offset: float = 0.0, fs: float = 200.0) -> SyncReport:
    return SyncReport(
        offset=offset, fs=fs, nominal_fs=200.0, samples=4000, packets=1000,
        samples_per_packet=4.0, anchors=40, residual_rms=0.001, residual_p95=0.002,
        residual_max=0.004, fs_window_spread=1e-5, fs_windows=2,
    )


def _anchor_report(deltas, *, aligned: float | None = None) -> AnchorReport:
    """造一份锚点报告：逐对 Δ 由 `deltas` 给定，其余字段取无关紧要的合法值。

    直接构造而不是跑 `measure_offsets`：本文件要验的是"拿到 Δ 之后做什么"，
    Δ 怎么测出来的已由 `tests/test_anchor.py` 守着。
    """
    peak = ImpactPeak(index=1.0, magnitude=80.0, clipped=False, interpolated=True, width_samples=3)
    pairs = tuple(
        AnchorPair(
            left=AnchorEvent(peak=peak, t_host=3.0 + index + delta, t_device=3.0 + index),
            right=AnchorEvent(peak=peak, t_host=3.0 + index, t_device=3.0 + index),
        )
        for index, delta in enumerate(deltas)
    )
    return AnchorReport(
        pairs=pairs, unpaired_left=(), unpaired_right=(),
        left_sync=_sync_report(), right_sync=_sync_report(),
        alignment_applied_s=aligned,
    )


def _cycles(spec: WalkSpec | None = None):
    """一对双足步态周期。`position=None`：跨足时序量只依赖事件时刻。"""
    data = generate_dual_walk(spec or WalkSpec(duration_s=24.0))
    out = {}
    for foot in ("L", "R"):
        series = data[foot][0]
        stance = detect_stance(series.acc, series.gyr, series.fs, CFG)
        cycles, _ = segment_cycles(
            series.label, series.t, series.acc, series.gyr, stance.stances,
            position=None, cfg=CFG,
        )
        out[foot] = cycles
    return out["L"], out["R"]


# --- 判据：它必须只有一处家，且就是 06 v1.1 §5 的两个数 -----------------------


def test_criterion_constants_match_the_frozen_document():
    """判据自 06 v1.1 起冻结。这条测试是"有没有人跑完之后动过判据"的哨兵。"""
    assert NEGLIGIBLE_MEDIAN_S == 0.0055
    assert NEGLIGIBLE_P90_S == 0.010


@pytest.mark.parametrize(
    ("deltas", "expected"),
    [
        ([0.004, -0.003, 0.005, 0.002], True),          # 中位与 90 分位都合格
        ([0.004, 0.005, 0.006, 0.030], False),          # 中位合格、尾部散开 → 不合格
        ([0.008, -0.009, 0.008, 0.009], False),         # 中位就超了
    ],
    ids=["合格", "尾部散开", "中位超标"],
)
def test_verdict_applies_both_halves_of_the_criterion(deltas, expected):
    """90 分位那一条不是装饰：中位合格而尾部散开的会话必须判不合格。"""
    verdict = summarize([_trial(deltas)])
    assert verdict.negligible is expected


def _trial(deltas):
    left, right = _cycles()
    return evaluate_trial("T", _anchor_report(deltas), left, right, sync_quality=QUALITY)


def test_empty_sample_is_not_negligible_but_unknown():
    """没数据不是"合格"。返回 None 而不是 False/True —— 两者都会被读成结论。"""
    verdict = Verdict(deltas=np.zeros(0), trials=0, taps=0)
    assert verdict.negligible is None
    assert "无数据" in verdict.decision
    assert math.isnan(verdict.median_abs)


def test_decision_leaves_the_firmware_choice_to_a_human():
    """不可忽略时，三选一的后两条取决于商务可得性 —— 数据答不了，不能替人选。"""
    verdict = summarize([_trial([0.030] * 5)])
    assert verdict.negligible is False
    assert "取决于厂商" in verdict.decision
    assert "固件" in verdict.decision and "不交付" in verdict.decision


# --- 校正：平移够不着足内的量 -------------------------------------------------


def test_shift_moves_instants_but_not_within_foot_durations():
    """恒定平移改变时刻、不改变时长。改了时长等于凭空制造足内差异（RAY-263 §4）。"""
    left, _ = _cycles()
    shifted = shift_cycles(left, 0.020)
    for before, after in zip(left, shifted, strict=True):
        assert after.t_ic == pytest.approx(before.t_ic - 0.020)
        assert after.t_to == pytest.approx(before.t_to - 0.020)
        assert after.t_ic_next == pytest.approx(before.t_ic_next - 0.020)
        assert after.stride_time == before.stride_time
        assert after.stance_time == before.stance_time
        assert after.stance_ratio == before.stance_ratio


# --- 指标偏差：与 RAY-264 的闭式独立对照 --------------------------------------


@pytest.mark.parametrize("injected", [0.010, 0.020, 0.030])
def test_step_time_symmetry_bias_matches_ray264_closed_form(injected):
    """注入 Δ 后，管线算出的 SI 偏差应与 `SI ≈ 2Δ/步时` 相符。

    这条测试同时守着判据的推导依据：管线与闭式对不上，两者必有一错。
    """
    left, right = _cycles()
    # 注入：把左足事件整体推后 Δ，模拟主机时基下的跨足偏差。
    biased_left = shift_cycles(left, -injected)
    trial = evaluate_trial(
        "T", _anchor_report([injected] * 8), biased_left, right, sync_quality=QUALITY
    )
    symmetry = next(m for m in trial.metrics if m.name == "step_time_symmetry")
    step_time = float(np.median([c.stride_time for c in right])) / 2.0
    predicted = 2.0 * injected / step_time
    # 校正后应回到接近零的本底；主机时基读数应接近闭式预测。
    assert symmetry.corrected < 0.001
    # 容差 1%：实测全档（5~50 ms）相对差 0.0%。松容差会让「管线与判据依据脱节」
    # 这件事悄悄溜过去，而那正是这条测试唯一的用途。
    assert symmetry.host == pytest.approx(predicted, rel=0.01)


def test_correction_removes_the_injected_bias():
    """校正后的读数应当与"从未注入偏差"的读数一致 —— 这是校正正确性的定义。"""
    left, right = _cycles()
    clean = evaluate_trial("clean", _anchor_report([0.0] * 8), left, right, sync_quality=QUALITY)
    biased = evaluate_trial(
        "biased", _anchor_report([0.025] * 8), shift_cycles(left, -0.025), right,
        sync_quality=QUALITY,
    )
    for a, b in zip(clean.metrics, biased.metrics, strict=True):
        assert b.corrected == pytest.approx(a.corrected, abs=1e-9)
    symmetry = next(m for m in biased.metrics if m.name == "step_time_symmetry")
    assert abs(symmetry.bias) > 0.05  # 前提自检：确实注入了可见的偏差


# --- 守卫：粗对齐过的 Δ 不能用来校正 ------------------------------------------


def test_coarse_aligned_report_is_refused():
    """对齐把 Δ 的绝对值吃掉了，用它校正等于用一个被定义为零的量去量偏差。"""
    left, right = _cycles()
    with pytest.raises(V3PrimeError, match="粗对齐"):
        evaluate_trial(
            "T", _anchor_report([0.004] * 5, aligned=0.4), left, right, sync_quality=QUALITY
        )


def test_trial_without_taps_is_refused():
    left, right = _cycles()
    with pytest.raises(V3PrimeError, match="没有配对"):
        evaluate_trial("T", _anchor_report([]), left, right, sync_quality=QUALITY)


def test_snapshot_is_valid_json_without_bare_nan():
    """裸 NaN 不是合法 JSON；报告要给非 Python 的读取方看。"""
    trial = _trial([0.004, 0.005])
    payload = {"trial": trial.snapshot(), "verdict": summarize([trial]).snapshot()}
    text = json.dumps(payload, ensure_ascii=False)
    assert "NaN" not in text
    assert json.loads(text)["verdict"]["criterion"]["median_abs_s"] == NEGLIGIBLE_MEDIAN_S


def test_cross_check_reports_none_when_selfcheck_cannot_estimate():
    """差分法判定 offset 不可估时，互证项必须是 None，不能凑一个数。"""
    left, right = _cycles()
    trial = evaluate_trial(
        "T", _anchor_report([0.004] * 4), left, right,
        sync_quality=QUALITY, delta_selfcheck=None,
    )
    assert trial.cross_check is None
    assert trial.snapshot()["cross_check_s"] is None


def test_cross_check_is_the_difference_of_two_methods():
    left, right = _cycles()
    trial = evaluate_trial(
        "T", _anchor_report([0.006] * 4), left, right,
        sync_quality=QUALITY, delta_selfcheck=0.005,
    )
    assert trial.cross_check == pytest.approx(0.001)


def test_metric_bias_sign_says_which_way_the_reading_moved():
    bias = replace(_trial([0.02] * 4).metrics[0], host=0.21, corrected=0.19)
    assert bias.bias == pytest.approx(0.02)


# --- 配对双支撑差：对 Δ 敏感，且知道自己什么时候不可比 ------------------------


@pytest.mark.parametrize("injected", [0.005, 0.010, 0.020, 0.050])
def test_paired_double_support_bias_equals_twice_delta(injected):
    """恒定 Δ 下配对差的偏差**精确等于 2Δ**（RAY-211/263 的机制）。

    这是双支撑期里真正承载同步偏差的量 —— 与它并列输出的 `fraction`（产品口径）
    对 Δ 几乎免疫，见下一条。
    """
    left, right = _cycles()
    trial = evaluate_trial(
        "T", _anchor_report([injected] * 8), shift_cycles(left, -injected), right,
        sync_quality=QUALITY,
    )
    paired = next(m for m in trial.metrics if m.name == "double_support_leading_difference")
    assert paired.comparable
    assert paired.bias == pytest.approx(2 * injected, abs=1e-6)


def test_double_support_fraction_is_nearly_blind_to_offset():
    """产品口径（均值/占比）对恒定 Δ 几乎免疫 —— 一类相位 +Δ、另一类 −Δ，均值抵消。

    这条测试的用途是**防止误读**：V3′ 若只报这个口径，会得出「双支撑期不受同步
    误差影响」的错误结论，而配对口径同时显示 40 ms 的偏差。
    """
    left, right = _cycles()
    trial = evaluate_trial(
        "T", _anchor_report([0.020] * 8), shift_cycles(left, -0.020), right,
        sync_quality=QUALITY,
    )
    fraction = next(m for m in trial.metrics if m.name == "double_support_fraction")
    paired = next(m for m in trial.metrics if m.name == "double_support_leading_difference")
    assert abs(fraction.bias) < 1e-4
    assert abs(paired.bias) == pytest.approx(0.040, abs=1e-6)


def test_structure_change_reports_nan_with_a_reason_not_a_wrong_number():
    """相位结构在校正下改变时，两次读数不是同一个量 —— 必须报 nan 并说明原因。

    实测触发点：合成步态的两足事件落在同一采样网格上，校正的浮点回程（1 ulp）
    就能翻转包含判定。真机上两足各有时间轴，罕见 —— 但「罕见」不是「不会」，
    而给一个错数比给 nan 危险得多：nan 会被追问，错数会被引用。
    """
    left, right = _cycles()
    trial = evaluate_trial(
        "T", _anchor_report([0.030] * 8), shift_cycles(left, -0.030), right,
        sync_quality=QUALITY,
    )
    paired = next(m for m in trial.metrics if m.name == "double_support_leading_difference")
    assert not paired.comparable
    assert math.isnan(paired.bias)
    assert "相位结构" in paired.note
    snapshot = next(
        m for m in trial.snapshot()["metrics"]
        if m["name"] == "double_support_leading_difference"
    )
    assert snapshot["bias"] is None
    assert snapshot["comparable"] is False


def test_paired_support_reports_its_phase_structure():
    """结构三元组要能被读出来 —— 它是「两次读数可不可比」的判据本身。"""
    left, right = _cycles()
    paired = paired_double_support(left, right)
    assert paired.structure == (paired.left_phases, paired.right_phases, paired.contained)
    assert paired.left_phases > 0 and paired.right_phases > 0
    assert math.isfinite(paired.difference)


def test_paired_support_with_no_overlap_is_nan_not_zero():
    """两足全程不重叠时没有双支撑相位。返回 nan —— 0 会被读成「无偏差」。"""
    left, right = _cycles()
    far = shift_cycles(right, -100.0)
    paired = paired_double_support(left, far)
    assert math.isnan(paired.difference)
    assert paired.left_phases == 0 or paired.right_phases == 0


# --- CLI：不需要硬件的那几条路径 ---------------------------------------------


def _fake_capture(tmp_path, delta: float, seconds: float = 36.0):
    """造一趟"已采集"的 arrivals.npz：两足共钟，左足到达时刻整体推后 delta。

    共钟是关键前提 —— 在线采集给的就是这个（同进程、同 `time.monotonic()`），
    而这正是 `replay` 能算出 Δ 绝对值的原因。
    """
    fs = 200.0
    n = int(seconds * fs)
    rng = np.random.default_rng(7)
    taps = [4.0 + 1.5 * k for k in range(20)]
    out = {}
    for prefix, latency in (("left", delta), ("right", 0.0)):
        t_true = np.arange(n) / fs
        magnitude = np.zeros((n, 3))
        magnitude[:, 2] = 9.80665 + rng.normal(0.0, 0.05, n)
        for tap in taps:
            x = (t_true - tap) / 0.02
            inside = np.abs(x) < 0.5
            magnitude[inside, 0] += 8 * 9.80665 * np.cos(np.pi * x[inside]) ** 2
        arrival = np.empty(n)
        previous = -math.inf
        for begin in range(0, n, 4):
            end = min(begin + 4, n)
            moment = max(t_true[end - 1] + 0.012 + latency + rng.exponential(0.004), previous)
            arrival[begin:end] = moment
            previous = moment
        out[f"{prefix}_arrival"] = arrival
        out[f"{prefix}_accel"] = magnitude
        out[f"{prefix}_gyro"] = np.zeros((n, 3))
    trial_dir = tmp_path / "S1-1"
    trial_dir.mkdir(parents=True)
    np.savez(
        trial_dir / "arrivals.npz", **out,
        label=np.asarray("S1-1"), nominal_fs=np.asarray(fs),
        left_device=np.asarray("AA:L"), right_device=np.asarray("AA:R"),
    )
    return trial_dir


def test_cli_replay_recovers_the_injected_delta(tmp_path, capsys):
    """`replay` 从共钟到达时刻复算 Δ —— 不需要硬件，也不需要 epoch。"""
    from gait.cli.v3prime import main

    trial_dir = _fake_capture(tmp_path, delta=0.013)
    assert main(["replay", "--trial-dir", str(trial_dir)]) == 0
    payload = json.loads((trial_dir / "trial.json").read_text(encoding="utf-8"))
    offset = payload["anchor"]["offset"]
    assert offset["count"] == 20
    assert offset["median_s"] == pytest.approx(0.013, abs=0.003)
    assert "跨足偏差" in capsys.readouterr().out


def test_cli_verdict_applies_the_frozen_criterion(tmp_path, capsys):
    """`verdict` 汇总多趟并按 R1 判据下结论，且把判据出处印出来。"""
    from gait.cli.v3prime import main

    dirs = []
    for index, delta in enumerate((0.013, 0.014)):
        trial_dir = _fake_capture(tmp_path / f"run{index}", delta=delta)
        main(["replay", "--trial-dir", str(trial_dir)])
        dirs.append(str(trial_dir))
    out_path = tmp_path / "verdict.json"
    assert main(["verdict", "--trials", *dirs, "--out", str(out_path)]) == 0
    summary = json.loads(out_path.read_text(encoding="utf-8"))
    verdict = summary["verdict"]
    assert verdict["trials"] == 2
    assert verdict["taps"] == 40
    assert verdict["criterion"]["median_abs_s"] == NEGLIGIBLE_MEDIAN_S
    assert verdict["criterion"]["p90_abs_s"] == NEGLIGIBLE_P90_S
    # 13~14 ms 远超 5.5 ms 的门槛 —— 这一档必须判不可忽略。
    assert verdict["negligible"] is False
    text = capsys.readouterr().out
    assert "06 测试与验证方案 v1.1 §5" in text and "R1" in text


def test_cli_missing_trial_fails_cleanly(tmp_path, capsys):
    from gait.cli.v3prime import main

    assert main(["replay", "--trial-dir", str(tmp_path / "nope")]) == 2
    assert "V3′ 工装失败" in capsys.readouterr().err


def test_clock_gate_refuses_a_coarse_clock(monkeypatch):
    """时钟粗于采样周期的 1/10 就拒绝开跑 —— 那时测到的是时钟台阶，不是链路。"""
    from gait.cli import v3prime as cli

    monkeypatch.setattr(cli, "host_clock_resolution", lambda *a, **k: 0.0156)
    with pytest.raises(cli.HarnessError, match="时钟"):
        cli.require_adequate_clock(200.0, echo=lambda *_: None)


def test_clock_gate_accepts_a_fine_clock():
    from gait.cli import v3prime as cli

    # 真实的 macOS/Linux 时钟是纳秒级；这条同时守着"探测函数本身能跑"。
    assert cli.require_adequate_clock(200.0, echo=lambda *_: None) < 0.0005


def test_replay_preserves_capture_metadata(tmp_path):
    """复算不得丢掉采集元数据 —— `replay` 会整份重写 trial.json。

    `foot_assignment` 尤其要紧：它是判断偏差**方向**可不可信的依据（左右足按扫描
    顺序定时，Δ 与所有偏差可能整体反号）。它活在 npz 里而不是只活在 json 里，
    正是为了经得起复算。
    """
    from gait.cli.v3prime import main

    trial_dir = _fake_capture(tmp_path, delta=0.013)
    # 夹具模拟 live 落盘：把元数据一并写进 npz。
    data = dict(np.load(trial_dir / "arrivals.npz", allow_pickle=False))
    data["foot_assignment"] = np.asarray("explicit_mac")
    data["captured_utc"] = np.asarray("2026-08-27T00:00:00+00:00")
    np.savez(trial_dir / "arrivals.npz", **data)

    assert main(["replay", "--trial-dir", str(trial_dir)]) == 0
    payload = json.loads((trial_dir / "trial.json").read_text(encoding="utf-8"))
    assert payload["foot_assignment"] == "explicit_mac"
    assert payload["captured_utc"] == "2026-08-27T00:00:00+00:00"


def test_missing_foot_assignment_is_none_not_guessed(tmp_path, capsys):
    """没记录左右足来源的数据显示为"未记录"，不被猜成 scan_order。

    猜一个默认值会让"这份数据没记"与"这份数据记了扫描顺序"看起来一样，而两者
    对偏差方向的可信度含义不同。
    """
    from gait.cli.v3prime import main

    trial_dir = _fake_capture(tmp_path, delta=0.013)  # 夹具不写元数据
    assert main(["replay", "--trial-dir", str(trial_dir)]) == 0
    payload = json.loads((trial_dir / "trial.json").read_text(encoding="utf-8"))
    assert payload["foot_assignment"] is None
    assert "未记录" in capsys.readouterr().out


# --- live 路径的接口契约（无硬件可验，防的是"上机才发现调错 API"）-----------


def test_live_path_api_contract():
    """`live` 用到的每个外部符号都必须存在且签名兼容。

    这条测试的由来是一次真实事故：`StreamConfig(rate_hz=...)` 与
    `device.stream()` 两处都是凭印象写的 API，而 `live` 需要硬件、CI 覆盖不到，
    于是错误一直等到受试者与两台模块都就位时才暴露，代价是一次上机。

    这些调用**不需要硬件也能验**：字段名、方法名、签名都是静态的。所以它们不该
    靠上机来发现。
    """
    import inspect

    from wt901 import BleTransport, ReturnRate, WT901Device, scan
    from wt901.models import ImuSample, Vec3
    from wt901.transport.recording import RecordingTransport

    from gait.device.ble import StreamConfig, configure_streaming
    from gait.device.recorder import ThreadedRecordingWriter

    # 取样：是 samples() 不是 stream()，且必须是异步生成器（`async for` 用它）。
    assert inspect.isasyncgenfunction(WT901Device.samples), "device.samples() 必须是异步生成器"
    assert not hasattr(WT901Device, "stream"), "接口已变：出现了 stream()，请复核 _consume"

    # 流配置：字段名是 rate（ReturnRate 值），不是 rate_hz。
    fields = {f.name for f in dataclasses.fields(StreamConfig)}
    assert {"rate", "bandwidth", "algorithm"} <= fields, f"StreamConfig 字段变了：{fields}"
    StreamConfig(rate=int(ReturnRate.HZ_200))  # 关键字与取值都必须被接受

    # 下发：按位置传 (device, config)，所以验的是"能接住两个位置参数"，不是参数名。
    assert len(inspect.signature(configure_streaming).parameters) >= 2

    # 样本字段：`_consume` 逐样本取这三项。
    sample_fields = {f.name for f in dataclasses.fields(ImuSample)}
    assert {"t_host", "accel", "gyro"} <= sample_fields, sample_fields
    assert {f.name for f in dataclasses.fields(Vec3)} >= {"x", "y", "z"}

    # 连接与落盘链路的构造签名。
    # scan 与 writer 走关键字，验名字；两个 transport 走位置，只验能接住几个。
    assert "timeout" in inspect.signature(scan).parameters
    assert len(inspect.signature(BleTransport.__init__).parameters) >= 2  # self + 目标设备
    assert len(inspect.signature(RecordingTransport.__init__).parameters) >= 3  # self + 传输 + writer
    writer_params = inspect.signature(ThreadedRecordingWriter.__init__).parameters
    assert {"path", "device_id", "note"} <= set(writer_params), list(writer_params)


@pytest.mark.parametrize(("fs", "expected"), [(200.0, 11), (100.0, 9), (50.0, 8)])
def test_return_rate_maps_supported_rates(fs, expected):
    from gait.cli.v3prime import _return_rate

    assert _return_rate(fs) == expected


def test_unsupported_rate_fails_loudly_instead_of_snapping():
    """器件不支持的速率当场失败，不取最近的一档。

    取最近一档会让采集速率与 `--nominal-fs` 悄悄不一致 —— 没有任何东西会报错，
    只会让整趟数据的时间轴系统性地错。
    """
    from gait.cli.v3prime import HarnessError, _return_rate

    with pytest.raises(HarnessError, match="不支持"):
        _return_rate(150.0)


# --- 时基不可信的趟次不得进入判定 --------------------------------------------


def _mark_unstable(trial_dir):
    """把一趟的 trial.json 改成"时基不稳"，模拟丢包严重的链路。"""
    path = trial_dir / "trial.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["timebase_trustworthy"] = False
    payload["timebase_note"] = "测试注入"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_unstable_timebase_trial_is_excluded_from_verdict(tmp_path, capsys):
    """时基不稳的趟次必须被排除，且**排除本身要出现在输出里**。

    静默丢弃会让「5 人 × 3 趟」变成谎话：样本量看起来永远是满的，而判定其实
    只用了其中几趟。
    """
    from gait.cli.v3prime import main

    good = _fake_capture(tmp_path / "g", delta=0.013)
    bad = _fake_capture(tmp_path / "b", delta=0.013)
    main(["replay", "--trial-dir", str(good)])
    main(["replay", "--trial-dir", str(bad)])
    _mark_unstable(bad)

    out_path = tmp_path / "verdict.json"
    assert main(["verdict", "--trials", str(good), str(bad), "--out", str(out_path)]) == 0
    summary = json.loads(out_path.read_text(encoding="utf-8"))
    assert summary["verdict"]["trials"] == 1  # 只有好的那一趟进了判定
    text = capsys.readouterr().out
    assert "已排除 1 趟" in text and "时基不稳" in text


def test_untrustworthy_timebase_blocks_metric_bias(tmp_path):
    """时基不可信时不算指标偏差 —— 校正量本身就来自那条跑偏的时间轴。"""
    from gait.cli.v3prime import load_trial_dir

    trial_dir = _fake_capture(tmp_path, delta=0.013)
    payload = load_trial_dir(trial_dir)
    # 合成夹具的链路是干净的，应当判为可信 —— 这条同时守着「别把好数据也拒了」。
    assert payload["timebase_trustworthy"] is True
    assert payload["trial"] is not None


def test_consume_discards_pre_capture_backlog():
    """早于采集起点的样本必须丢弃 —— 它们是配置阶段积压在队列里的残留。

    这条测试的由来是一次真实采集：没有这道过滤时，arrival 跨度比名义采集时长
    多出 5.5 s，时基回归据此把实测采样率读成 191/178 Hz（真值 198），残差 p95
    涨到 0.5~1.3 s，整趟被判 unusable —— 而同一时段 linktest 测得链路缺失率
    只有 0.02%，链路本身是好的。
    """
    import asyncio

    from gait.cli.v3prime import FootCapture, _consume

    class FakeVec:
        x = y = z = 1.0

    class FakeSample:
        def __init__(self, t):
            self.t_host = t
            self.accel = self.gyro = FakeVec()

    class FakeDevice:
        async def samples(self):
            # 前三个是积压（早于 started=100.0），后两个是真正的采集样本。
            for t in (97.5, 98.0, 99.9, 100.1, 100.2):
                yield FakeSample(t)

    capture = FootCapture(foot="L", device_id="x", arrival=[], accel=[], gyro=[])
    asyncio.run(_consume(FakeDevice(), capture, 100.0))
    assert capture.arrival == [100.1, 100.2]
    assert len(capture.accel) == 2 and len(capture.gyro) == 2
