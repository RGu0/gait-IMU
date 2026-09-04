"""把 RAY-230 的真机录制切成会话目录，跑 `reportFor`，对照受控真值。

## 为什么需要它

采集端的 `reportFor` 只认**会话目录**（`meta.json` + `raw/left.raw` / `raw/right.raw`）。
而 RAY-230 的真机数据是按**趟次目录**存的：一份 16 分钟的连续录制 + 一份
`walk_segments.json` 说明哪几个时间窗是步行。两者只差一层外壳 —— 录制文件本身就是
`wt901.recording` 的 JSON Lines，与采集端落盘的**同一个格式**。

这个脚本补的就是那层外壳。它让「真机数据跑一遍产品链路」成为一条**可复现**的命令，
而不是一次性的手工操作。

## 它不是验收脚本

`tools/acceptance/` 里的脚本是**回归判据**，被 `tests/test_acceptance_suite.py` 钉住、
每次改共享算法路径都要全跑。这个不是：它是一个取证工具，产出的数字进证据库，
判据不在它身上。所以它放在 `tools/` 而不是 `tools/acceptance/`。

## 读数怎么解读

受控真值：走廊 45.148 m、每趟每脚 38 个周期 ⇒ **步长恒为 1.188 m**（步数与路长都
受控，所以速度档之间只差步频，步长是常量）。

但**这条链此刻跑的是未标定数据**：没有出厂加计标定（RAY-207 未交付），坐标重排用
的是恒等映射而非逐会话实测的安装角，时轴是标称 200 Hz。所以偏差里同时含着标定误差、
安装角误差与时基误差 —— **不要拿这里的数字下精度结论**，那归 RAY-230 / RAY-207。

它回答的是另一个问题：**这条链在真机数据上跑不跑得通、跑多久**。
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

import numpy as np
from wt901.recording import RecordedChunk, Recording, open_recording, write_recording

from gait.app.service import TerminalService
from gait.contracts import SessionMeta
from gait.io.session import create_session, new_session_id, new_subject_uuid, raw_path

#: 走廊长度与每趟每脚的周期数（RAY-230 受控条件）。
CORRIDOR_M = 45.148
TRUTH_CYCLES = 38
TRUTH_STRIDE_M = CORRIDOR_M / TRUTH_CYCLES


def _offsets(trial: Path) -> dict[str, float]:
    """两足各自的录制起点相对公共原点的偏移，s。

    `walk_segments.json` 的时刻是相对**两足公共原点**（较早的那个第一到达时刻）的，
    而每份录制文件的 `t` 是相对**它自己**第一段字节的。差的就是这个偏移。
    """
    bundle = np.load(trial / "arrivals.npz", allow_pickle=False)
    origin = min(float(bundle["left_arrival"][0]), float(bundle["right_arrival"][0]))
    return {
        "L": float(bundle["left_arrival"][0]) - origin,
        "R": float(bundle["right_arrival"][0]) - origin,
    }


def build_session(trial: Path, walk_index: int, root: Path) -> tuple[str, float]:
    """把一趟步行切出来装成会话目录。返回 `(session_id, 时长秒)`。"""
    info = json.loads((trial / "trial.json").read_text(encoding="utf-8"))
    walks = json.loads((trial / "walk_segments.json").read_text(encoding="utf-8"))
    window = walks["feet"]["L"]["walks"][walk_index]
    offsets = _offsets(trial)

    session_id = new_session_id()
    create_session(
        root,
        SessionMeta(
            session_id=session_id,
            created_at=info["captured_utc"],
            # 真机趟次没有受试者 uuid（那是采集端建档时才有的），这里现生成一个：
            # FR-02 只要求它是 uuid 且不含身份明文，而这份数据本来就不含。
            subject_uuid=new_subject_uuid(),
            scenario="walk",
            devices={"L": {"mac": info["left_device"]}, "R": {"mac": info["right_device"]}},
            config_snapshot={"rate_hz": info["nominal_fs"]},
            calib_snapshot={"L": {"note": "未标定"}, "R": {"note": "未标定"}},
            algo_version="field-replay",
            algo_params={"preset": "default"},
            # 原趟次自己算出的同步与完整性报告原样带上 —— 它们是这份数据的来历。
            sync_report=info["anchor"],
            integrity_report=info["integrity"],
            protocol_config={"duration_s": round(window["duration_s"])},
        ),
    )
    for label in ("L", "R"):
        key = "left_device" if label == "L" else "right_device"
        source = trial / f"raw_{label}_{info[key]}.jsonl"
        low = window["start_s"] - offsets[label]
        high = window["end_s"] - offsets[label]
        with open_recording(source, tolerate_truncated_tail=True) as reader:
            device_id = reader.header.device_id
            kept = tuple(
                RecordedChunk(t=round(chunk.t - low, 6), data=chunk.data)
                for chunk in reader
                if low <= chunk.t <= high
            )
        write_recording(
            raw_path(root, session_id, label),
            Recording(
                device_id=device_id,
                created_utc=info["captured_utc"],
                note=f"{info['label']} walk#{walk_index}",
                chunks=kept,
            ),
        )
    return session_id, float(window["duration_s"])


def run(trial: Path, walk_index: int, root: Path) -> dict[str, Any]:
    """建会话 → `reportFor` → 读数与耗时。"""
    session_id, duration = build_session(trial, walk_index, root)
    service = TerminalService(session_root=root)
    started = time.monotonic()
    report = service._do_reportFor({"sessionId": session_id})
    elapsed = time.monotonic() - started

    row: dict[str, Any] = {
        "trial": trial.name,
        "walk": walk_index,
        "duration_s": duration,
        "truth_speed_m_s": CORRIDOR_M / duration,
        "elapsed_s": elapsed,
    }
    if hasattr(report, "code"):
        row["error"] = report.code
        return row
    metrics = {item["key"]: item for item in report["metrics"]}
    stride = float(metrics["stride"]["value"])
    row.update(
        stride_m=stride,
        stride_ratio=stride / TRUTH_STRIDE_M,
        speed_m_s=float(metrics["speed"]["value"]),
        cadence_spm=float(metrics["cadence"]["value"]),
        grade=report["qualityFooter"]["overall"],
    )
    return row


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trials", nargs="+", type=Path, help="RAY-230 的趟次目录")
    parser.add_argument("--root", type=Path, required=True, help="会话目录的落点")
    parser.add_argument("--json", type=Path, help="逐行读数的 JSON 落盘路径")
    args = parser.parse_args(argv)

    print(f"受控真值：步长 {TRUTH_STRIDE_M:.3f} m（{CORRIDOR_M} m ÷ {TRUTH_CYCLES} 周期）\n")
    header = f"{'趟次':10s} {'段':>4s} {'时长s':>7s} {'真值步速':>9s} {'步长m':>7s} {'倍数':>7s} {'步频':>7s} {'耗时':>7s}"
    print(header)
    rows = []
    for trial in args.trials:
        walks = json.loads((trial / "walk_segments.json").read_text(encoding="utf-8"))
        for index in range(len(walks["feet"]["L"]["walks"])):
            row = run(trial, index, args.root)
            rows.append(row)
            if "error" in row:
                print(f"{row['trial']:10s} {row['walk']:>4d} {row['duration_s']:7.1f} "
                      f"{row['truth_speed_m_s']:9.3f}   {row['error']}   （{row['elapsed_s']:.2f}s）")
            else:
                print(f"{row['trial']:10s} {row['walk']:>4d} {row['duration_s']:7.1f} "
                      f"{row['truth_speed_m_s']:9.3f} {row['stride_m']:7.2f} "
                      f"{row['stride_ratio']:6.2f}x {row['cadence_spm']:7.1f} {row['elapsed_s']:6.2f}s")
    if args.json:
        args.json.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
