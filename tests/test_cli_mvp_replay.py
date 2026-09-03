"""`cli.mvp --replay` —— 回放路径的端到端验收（RAY-345）。

把一个合成双足行走**量化回 int16 码值**写进录制，再经 `--replay` 走完整条闭环
（读录制 → FootSeries → 基础链 → report.html）。这条守的是「回放路径真的通」——
它是 Issue 验收第二条的可执行版。
"""

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
from gait.io.session import create_session, new_session_id, raw_path
from gait.contracts import SessionMeta
from gait.validate.synthetic import WalkSpec, generate_dual_walk


def _to_module_raw(acc: np.ndarray, gyr: np.ndarray, label: str) -> tuple[np.ndarray, np.ndarray]:
    """足部系 → 模块体系（重排的逆），再 SI → int16 码值。"""
    sign = 1.0 if label == "L" else -1.0
    module_acc = np.stack([sign * acc[:, 1], acc[:, 0], acc[:, 2]], axis=1)
    module_gyr = np.stack([sign * gyr[:, 1], gyr[:, 0], gyr[:, 2]], axis=1)
    acc_raw = np.rint(module_acc * INT16_FULL_SCALE / (ACCEL_FULL_SCALE_G * STANDARD_GRAVITY))
    gyr_raw = np.rint(np.degrees(module_gyr) * INT16_FULL_SCALE / GYRO_FULL_SCALE_DPS)
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
