"""`cli.mvp --replay` —— 回放路径的端到端验收（RAY-345）。

把一个合成双足行走**量化回 int16 码值**写进录制，再经 `--replay` 走完整条闭环
（读录制 → FootSeries → 基础链 → report.html）。这条守的是「回放路径真的通」——
它是 Issue 验收第二条的可执行版。
"""

import json
import struct

import numpy as np
from wt901.protocol.units import (
    ACCEL_FULL_SCALE_G,
    GYRO_FULL_SCALE_DPS,
    INT16_FULL_SCALE,
    STANDARD_GRAVITY,
)
from wt901.recording import RecordedChunk, Recording, write_recording

from gait.cli.mvp import main
from gait.contracts import SessionMeta
from gait.io.session import create_session, new_session_id, raw_path
from gait.report.wording import reason_text
from gait.validate.synthetic import WalkSpec, generate_dual_walk


def _to_module_raw(acc: np.ndarray, gyr: np.ndarray, label: str) -> tuple[np.ndarray, np.ndarray]:
    """足部系 → 模块体系（重排的逆），再 SI → int16 码值。

    **重排现在是恒等**（RAY-390：实测确认模块系与足部系是同一个系），所以它的逆也是
    恒等，这里不再换轴。本函数原先写的是 `[sign*acc[:,1], acc[:,0], acc[:,2]]` ——
    那是旧的轴交换的逆，RAY-390 之后它不再是任何东西的逆，只是在**引入**一次错乱。

    没有测试因此变红，因为本文件唯一那条用例只断言 `report.html` 生成得出来、里面
    没有 `NaN` —— 一份轴错乱的数据照样满足它。所以顺手把这条也补上：下面新增的
    两条用例会读 `report.json` 里的指标。
    """
    del label  # 两只脚同一个映射（RAY-390）；参数留着是为了调用点仍然读得懂。
    acc_raw = np.rint(acc * INT16_FULL_SCALE / (ACCEL_FULL_SCALE_G * STANDARD_GRAVITY))
    gyr_raw = np.rint(np.degrees(gyr) * INT16_FULL_SCALE / GYRO_FULL_SCALE_DPS)
    return np.clip(acc_raw, -32768, 32767).astype(int), np.clip(gyr_raw, -32768, 32767).astype(int)


def _frame(ax: int, ay: int, az: int, wx: int, wy: int, wz: int) -> bytes:
    return b"\x55\x61" + struct.pack("<9h", ax, ay, az, wx, wy, wz, 0, 0, 0)


def _record(path, series, label: str) -> None:
    acc_raw, gyr_raw = _to_module_raw(series.acc, series.gyr, label)
    chunks = tuple(
        RecordedChunk(
            t=float(i) / series.fs,
            data=_frame(
                int(acc_raw[i, 0]), int(acc_raw[i, 1]), int(acc_raw[i, 2]),
                int(gyr_raw[i, 0]), int(gyr_raw[i, 1]), int(gyr_raw[i, 2]),
            ),
        )
        for i in range(len(series.t))
    )
    write_recording(
        path,
        Recording(
            device_id=f"dev-{label}",
            created_utc="2026-09-01T00:00:00Z",
            note="synthetic",
            chunks=chunks,
        ),
    )


def _make_session(root) -> None:
    session_id = new_session_id()
    meta = SessionMeta(
        session_id=session_id,
        created_at="2026-09-01T00:00:00Z",
        subject_uuid="3f2a6d0e-0000-4000-8000-000000000001",
        scenario="walk",
        devices={"L": {"mac": "AA"}, "R": {"mac": "BB"}},
        config_snapshot={"rate_hz": 200},
        calib_snapshot={"L": {}, "R": {}},
        algo_version="basic-0.0.0",
        algo_params={"preset": "default"},
        sync_report={"anchors": 0},
        integrity_report={"loss_rate": 0.0},
        protocol_config={"duration_s": 20, "version": "2.3"},
    )
    create_session(root, meta)
    dual = generate_dual_walk(WalkSpec(duration_s=20.0))
    for label, (series, _truth) in dual.items():
        _record(raw_path(root, session_id, label), series, label)
    return session_id


def test_replay_produces_html_and_json(tmp_path):
    session_id = _make_session(tmp_path)
    session_dir = tmp_path / session_id

    code = main(["--replay", str(session_dir), "--out", str(tmp_path / "out")])
    assert code == 0

    html_path = tmp_path / "out" / "report.html"
    json_path = tmp_path / "out" / "report.json"
    assert html_path.is_file()
    assert json_path.is_file()

    markup = html_path.read_text(encoding="utf-8")
    for heading in ("步态检测报告", "核心指标", "左右对比", "测试条件", "报告编号"):
        assert heading in markup
    assert "NaN" not in markup and "Infinity" not in markup


def _metrics(json_path) -> dict:
    return {
        item["key"]: item
        for item in json.loads(json_path.read_text(encoding="utf-8"))["metrics"]
    }


def test_replay_does_not_claim_a_sync_quality_it_never_computed(tmp_path):
    """回放路径**没算过**同步质量，就不能让跨足指标带着一个干净的等级出场。

    回放路径的时轴是标称 200 Hz（MVP 桥），跨足同步从未被计算。第一版这里填了
    `{"determinate": True, "flagged": False}`，等于告诉定级层「这次同步是好的」——
    实测（RAY-395）那份占位把双支撑期从 `low` + `missing_sync_quality` 抬成了
    `normal` + 无理由：**连「这项没有同步依据」都看不见了**。

    留空之后它落到 `uncomputable` 并带上原因。这条断言钉的就是「不出数」——
    把占位填回去，双支撑期立刻带上一个值，本条变红。
    """
    session_id = _make_session(tmp_path)
    code = main(["--replay", str(tmp_path / session_id), "--out", str(tmp_path / "out")])
    assert code == 0

    ds = _metrics(tmp_path / "out" / "report.json")["ds"]
    assert ds["grade"] != "normal", "没算过同步质量的双支撑期不该是「良好」"
    assert "value" not in ds, f"回放路径不该给出双支撑期的数值，实际 {ds}"
    # 原因必须**从标注自己的 reasons 翻出来**，不是一句写死的话。
    #
    # 这里的实际 reason 是 `not_computable`：`cloud/chain.py` 在没有 `sync_quality`
    # 时**根本不算**双支撑期（`if left and right and sync_quality is not None`），
    # 于是标注拿到 `computable=False`。
    #
    # 等号右边走 `wording.reason_text` 而不是抄一句中文：写死的译文会在表改了之后
    # 继续通过。`assemble` 若退回那句硬编码的「本次有效步数不足」，本条当场变红 ——
    # 那句话对这个案子是**具体而错误**的解释，读的人会去补步数。
    assert ds["reason"] == reason_text(["not_computable"]), (
        f"不可算的原因该由 reasons 翻出来，实际 {ds['reason']!r}"
    )


def test_synthetic_keeps_its_sync_quality_because_there_it_is_true(tmp_path):
    """合成路径**保留**那份同步质量 —— 在那条路上它不是占位，是真的。

    `generate_dual_walk` 的两只脚共用同一个精确时轴、无抖动、无丢包。两条路径从此
    在这一点上不同，而**这条断言就是那个「不同」的锚**：若哪天有人图省事把两条路径
    又合并成同一个常量，两条测试里必有一条变红。
    """
    code = main(["--synthetic", "--seconds", "20", "--out", str(tmp_path / "out")])
    assert code == 0

    ds = _metrics(tmp_path / "out" / "report.json")["ds"]
    assert "value" in ds, f"合成数据的同步质量是确定的，双支撑期该出数，实际 {ds}"
