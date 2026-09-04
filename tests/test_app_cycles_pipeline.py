"""`reportFor` 从会话目录重算出真实周期（RAY-360 `cycles-pipeline`）。

在这之前 `_cycles_for` 恒为空，`reportFor` 在真实会话上永远返回 `E-QLT-5003`。
这里的每一条都是**能失败**的：把接线拆掉，它们回到那个空列表上就变红。
"""

import json
import shutil
import struct
from pathlib import Path

import numpy as np
import pytest
from wt901.protocol.units import (
    ACCEL_FULL_SCALE_G,
    GYRO_FULL_SCALE_DPS,
    INT16_FULL_SCALE,
    STANDARD_GRAVITY,
)
from wt901.recording import RecordedChunk, Recording, write_recording

from gait.app.service import TerminalService
from gait.contracts import SessionMeta
from gait.io.session import create_session, new_session_id, new_subject_uuid, raw_path
from gait.validate.synthetic import NoiseModel, WalkSpec, generate_dual_walk

SECONDS = 20

#: 合成数据加一点器件量级的噪声。恒为 0 的变异系数在真实数据里不存在，
#: 一个完美的 0 反而像占位符 —— 取值与 `cli/mvp.py` 同量级。
NOISE = NoiseModel(accel_density=1.5e-3, gyro_density=3.0e-4, seed=3)


def _to_counts(acc: np.ndarray, gyr: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """SI → int16 码值。**换算常数只从 wt901 取**，不在测试里另抄一份。"""
    a = acc / (ACCEL_FULL_SCALE_G * STANDARD_GRAVITY) * INT16_FULL_SCALE
    g = np.degrees(gyr) / GYRO_FULL_SCALE_DPS * INT16_FULL_SCALE
    return (
        np.clip(a, -32768, 32767).astype(np.int16),
        np.clip(g, -32768, 32767).astype(np.int16),
    )


@pytest.fixture
def recorded_session(tmp_path: Path) -> tuple[Path, str]:
    """一份合成的双足录制会话：真值步长 1.3 m、步频 108 步/分。

    走的是真实的落盘格式（`wt901.recording` 的 JSON Lines + 0x55 0x61 帧），
    所以它测的是**整条读回来的路**，而不是一个绕过磁盘的内存捷径。
    """
    session_id = new_session_id()
    create_session(
        tmp_path,
        SessionMeta(
            session_id=session_id,
            created_at="2026-09-04T00:00:00Z",
            subject_uuid=new_subject_uuid(),
            scenario="walk",
            devices={"L": {"mac": "aa"}, "R": {"mac": "bb"}},
            config_snapshot={"rate_hz": 200},
            calib_snapshot={"L": {"note": "none"}},
            algo_version="test",
            algo_params={"preset": "default"},
            sync_report={"synthetic": True},
            integrity_report={"loss_rate": 0.0},
            protocol_config={"duration_s": SECONDS},
        ),
    )
    dual = generate_dual_walk(WalkSpec(duration_s=float(SECONDS)), noise=NOISE)
    for label, (series, _truth) in dual.items():
        acc, gyr = _to_counts(series.acc, series.gyr)
        chunks = tuple(
            RecordedChunk(
                t=round(index / 200.0, 6),
                data=b"\x55\x61" + struct.pack("<9h", *acc[index], *gyr[index], 0, 0, 0),
            )
            for index in range(len(acc))
        )
        write_recording(
            raw_path(tmp_path, session_id, label),
            Recording(device_id=f"dev-{label}", created_utc="", note="", chunks=chunks),
        )
    return tmp_path, session_id


def _report(root: Path, session_id: str, **params):
    service = TerminalService(session_root=root)
    return service._do_reportFor({"sessionId": session_id, **params})


def test_report_for_recomputes_real_cycles(recorded_session):
    """判据一：产出**真实报告**，指标来自 `run_basic_chain` 的真实计算。"""
    root, session_id = recorded_session
    report = _report(root, session_id, subjectLabel="测试")

    assert not hasattr(report, "code"), getattr(report, "message", report)
    assert report["reportId"] == session_id

    by_key = {metric["key"]: metric for metric in report["metrics"]}
    # 合成真值：步长 1.3 m、步频 108 步/分。数量级对不上就说明链没真的跑。
    assert float(by_key["stride"]["value"]) == pytest.approx(1.30, abs=0.15)
    assert float(by_key["cadence"]["value"]) == pytest.approx(108.0, abs=5.0)
    assert float(by_key["speed"]["value"]) == pytest.approx(1.17, abs=0.2)


def test_metrics_are_never_nan_or_blank(recorded_session):
    """判据二：指标非 NaN/0/空白；不可算项显式「本次不适用」。

    `NaN` 逐字扫整份 payload：JSON 里的裸 `NaN` 不是合法字面量，前端会渲染成
    「NaN」—— 那读起来像「测到了一个叫 NaN 的东西」，而不是「这项没算出来」。
    """
    root, session_id = recorded_session
    report = _report(root, session_id)

    assert "NaN" not in json.dumps(report, ensure_ascii=False)
    for metric in report["metrics"]:
        value = metric["value"]
        assert value not in ("", "0", "N/A", "—", None)
        if metric["grade"] == "uncomputable":
            assert value == "本次不适用"


def test_unreadable_session_is_not_reported_as_a_quality_problem(recorded_session):
    """原始数据读不回来 ≠ 这次采集质量不好。

    落到 `E-QLT-5003`（「质量不足」）会让操作员去重做一场其实采得好好的检测 ——
    而真正的问题是文件没了。两件事必须给不同的码。
    """
    root, session_id = recorded_session
    shutil.rmtree(root / session_id / "raw")

    outcome = _report(root, session_id)
    assert outcome.code == "E-BLE-1021"
    assert outcome.blocking is True


def test_recomputed_report_says_it_has_no_calibration(recorded_session):
    """一份没有标定的报告与一份有标定的报告在版面上长得一模一样。

    读的人无从分辨，除非报告自己说出来。所以就地重算时必须带上那句标注。
    """
    root, session_id = recorded_session
    report = _report(root, session_id)
    assert any("未使用标定参数" in text for text in report["annotations"])


def test_caller_supplied_cycles_are_not_labelled_uncalibrated(recorded_session):
    """调用方直传周期时不加那句：那些周期从哪来本层不知道，替它声明同样是在编。"""
    root, session_id = recorded_session
    service = TerminalService(session_root=root)
    cycles = service._cycles_for({"sessionId": session_id})
    assert cycles, "重算应当产出周期"

    report = service._do_reportFor({"sessionId": session_id, "cycles": cycles})
    assert not any("未使用标定参数" in text for text in report["annotations"])


def test_supplied_cycles_win_over_the_recording(recorded_session):
    """直传优先：明确给了周期的调用方不该被一次磁盘读覆盖掉。"""
    root, session_id = recorded_session
    service = TerminalService(session_root=root)
    everything = service._cycles_for({"sessionId": session_id})
    subset = everything[:3]

    assert service._cycles_for({"sessionId": session_id, "cycles": subset}) == subset


def test_the_recomputed_payload_is_what_the_template_reads(recorded_session):
    """判据四的可执行部分：**重算出来的那份** payload 满足模板的字段要求。

    `test_basic_report.py` 已经对一份手搭的 payload 验过同一件事。这里再验一次，
    验的是不同的东西：那边问「组装层会不会填」，这里问「**从磁盘重算出来的那一份**
    会不会填」—— 中间隔着一整条 ESKF 链，而链上任何一步产不出值都会让某个字段
    落空。

    P-10 的渲染路径（`sidecarTerminalAdapter.reportFor` → `TerminalApp` → 预览）
    JS 侧已经通了；缺的一直是 Python 侧返回一份真报告。**这条断言覆盖的是那一半**
    —— 真正的窗口渲染要 Electron 运行时，本环境取不到它的二进制。
    """
    import re

    root, session_id = recorded_session
    report = _report(root, session_id)

    template = (
        Path(__file__).resolve().parents[1] / "packages/report-template/ReportDocument.jsx"
    )
    consumed = set(re.findall(r"report\.([a-zA-Z]+)", template.read_text(encoding="utf-8")))
    assert consumed, "没能从模板里抓到任何字段，正则该更新了"
    assert not consumed - set(report), f"模板要读但重算的 payload 没有：{sorted(consumed - set(report))}"

    # 它要跨 IPC（RAY-248 契约），不能带任何非 JSON 的东西。
    json.dumps(report, ensure_ascii=False)


def _write_session(root: Path, seconds: float) -> str:
    """把一段合成双足步行写成会话目录，时长可控。

    `recorded_session` fixture 走的是同一条路，这里拆出来只为让「太短」这个条件
    可调 —— 短到只剩一两个周期时，分段层会抛错而不是返回空集。
    """
    session_id = new_session_id()
    create_session(
        root,
        SessionMeta(
            session_id=session_id,
            created_at="2026-09-04T00:00:00Z",
            subject_uuid=new_subject_uuid(),
            scenario="walk",
            devices={"L": {"mac": "aa"}, "R": {"mac": "bb"}},
            config_snapshot={"rate_hz": 200},
            calib_snapshot={"L": {"note": "none"}},
            algo_version="test",
            algo_params={"preset": "default"},
            sync_report={"synthetic": True},
            integrity_report={"loss_rate": 0.0},
            protocol_config={"duration_s": round(seconds)},
        ),
    )
    dual = generate_dual_walk(WalkSpec(duration_s=seconds), noise=NOISE)
    for label, (series, _truth) in dual.items():
        acc, gyr = _to_counts(series.acc, series.gyr)
        chunks = tuple(
            RecordedChunk(
                t=round(index / 200.0, 6),
                data=b"\x55\x61" + struct.pack("<9h", *acc[index], *gyr[index], 0, 0, 0),
            )
            for index in range(len(acc))
        )
        write_recording(
            raw_path(root, session_id, label),
            Recording(device_id=f"dev-{label}", created_utc="", note="", chunks=chunks),
        )
    return session_id


def test_analysis_failure_is_not_reported_as_a_disk_failure(tmp_path):
    """数据读得回来、只是**算不出步态**，不能报成「原始数据读不回来」。

    这条是真机数据逼出来的。RAY-230 的录制里有三个段落回了 `E-BLE-1021`，而那三份
    数据读得好好的（7376~12232 帧全部读出）—— 真正抛的是
    `analysis.segments.SegmentationError`（「剔除策略把所有步都剔掉了：直行段 1 个」），
    而它是 `ValueError` 的子类，被一个包得太宽的 `except (OSError, ValueError)` 归成了
    读盘失败。

    后果是反向的同一个错：一次**分析层的结论**被说成一次**磁盘故障**，操作员会去查
    一个完好的文件。所以分类的依据必须是**失败在哪一段**，不是异常类型。

    这里用一段 4 s 的步行复现同一个结局：数据完好、周期只有两个，分段层的剔除策略
    把它们全剔掉，于是 `SegmentationError` 从链里抛出来 —— 与真机上那三段同一条路。
    """
    session_id = _write_session(tmp_path, seconds=4.0)
    outcome = TerminalService(session_root=tmp_path)._do_reportFor({"sessionId": session_id})

    assert hasattr(outcome, "code"), "太短的会话算不出报告，应当给一个错误"
    assert outcome.code == "E-QLT-5003", (
        f"数据读得回来、只是算不出周期，应当报质量不足，实际 {outcome.code}"
    )


def test_a_missing_recording_still_reports_a_read_failure(tmp_path):
    """另一半：文件真的没了，仍然报 `E-BLE-1021`。

    与上一条成对 —— 两个结局必须**同时**分得开。只有其中一条时，把两者合并成同一个
    码也能让它通过。
    """
    session_id = _write_session(tmp_path, seconds=float(SECONDS))
    shutil.rmtree(tmp_path / session_id / "raw")

    outcome = TerminalService(session_root=tmp_path)._do_reportFor({"sessionId": session_id})
    assert outcome.code == "E-BLE-1021"
