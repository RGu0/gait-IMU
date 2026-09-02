"""RAY-339 `event-interval-estimator` 判据 1、2 的真机验收（需求修订 R2）。

真值是**受控**的：T-230-03 每趟每脚 38 个步态周期，采集时现场计数并严格控制，24 格
一致（`evidence/ray-337/protocol-truth/README.md`）。所以每一格的真周期也已知 ——
`趟时长 / 38` —— 而这让**周期估计本身**可以被直接打分，不只是打步数的分。

现状基线由本脚本**当场重算**，不抄任何一份历史读数：把 `period_refine_min_intervals`
调到不可能达到的值就关掉了采纳，那条路径与精修上线之前逐比特相同。两条路径在同一次
运行里跑同一段数据，中间没有任何可能漂掉的中间产物。

用法（在任一 scope worktree 内）：

    uv run --locked python <本目录>/event_interval_acceptance.py \\
      "<library>/.../raw/S1-sport" "<library>/.../raw/S1-flat" --out event_interval_acceptance.json
"""

from __future__ import annotations

import argparse
import json
from dataclasses import replace
from pathlib import Path

import numpy as np

from acceptance._dataset import TRUTH_CYCLES, load_walks
from gait.analysis.planning import plan_dual_foot_periods
from gait.config import AlgoConfig

TRUTH = TRUTH_CYCLES
#: 判据 1、2 的门（R2）。
MAX_RMS = 0.025
MAX_CELL = 0.050
MAX_CYCLE_ERROR = 3


def analyse(trial_dir: Path, cfg: AlgoConfig) -> list[dict]:
    """签名与套件里其余脚本一致 —— `tools/run_acceptance.py` 按这个签名统一调用。

    `cfg` 就是"精修开着"的那一套；"关掉"的那一套由它派生，所以调用方只需给一个。
    """
    on = cfg
    # 关掉采纳 = 精修上线之前的行为。**基线当场重算**，不抄历史读数 —— 两条路径在同一次
    # 运行里跑同一段数据，只差 `period_refine_min_intervals` 这一个参数。
    off = replace(on, period_refine_min_intervals=10**6)
    # 两套配置各装一遍：`load_walks` 会按 cfg 建时基与完整性报告，不能共用。
    walks = {tag: load_walks(trial_dir, cfg) for tag, cfg in (("before", off), ("after", on))}

    rows: list[dict] = []
    for index in range(len(walks["after"])):
        measured: dict[str, dict] = {}
        duration = 0.0
        label = walks["after"][index].trial
        name = walks["after"][index].walk
        for tag, variant in (("before", off), ("after", on)):
            walk = walks[tag][index]
            feet, duration = walk.feet, walk.duration_s
            result = plan_dual_foot_periods(feet["L"], feet["R"], walk.nominal_fs, variant)
            measured[tag] = {
                foot: {
                    "period_s": detection.period.period_samples / feet[foot].fs,
                    "cycles": detection.period.cycles,
                    "adopted": "events" in dict(detection.period.estimates),
                }
                for foot, detection in (("L", result.left), ("R", result.right))
            }
        true_period = duration / TRUTH
        for foot in ("L", "R"):
            before, after = measured["before"][foot], measured["after"][foot]
            rows.append(
                {
                    "cell": f"{label}/{name}/{foot}",
                    "true_period_s": true_period,
                    "before": before,
                    "after": after,
                    "error_before": (before["period_s"] - true_period) / true_period,
                    "error_after": (after["period_s"] - true_period) / true_period,
                }
            )
    return rows


def judge(rows: list[dict]) -> list[str]:
    failures: list[str] = []
    errors = np.array([row["error_after"] for row in rows], dtype=np.float64)
    rms = float(np.sqrt((errors**2).mean()))
    if rms > MAX_RMS:
        failures.append(f"判据 1：24 格周期 RMS {rms:.2%} > {MAX_RMS:.1%}")

    for row in rows:
        # 判据 1 的最差单格与判据 2 的上限是同一个数：分布收窄的判据只有一个上限，
        # 不要求逐格单调改善 —— 本改动是把误差重新分布，不是严格占优（见 R2 变更说明）。
        if abs(row["error_after"]) > MAX_CELL:
            failures.append(
                f"判据 1/2：{row['cell']} 周期误差 {row['error_after']:+.1%}，"
                f"超过 {MAX_CELL:.1%}"
            )
        error = row["after"]["cycles"] - TRUTH
        if abs(error) > MAX_CYCLE_ERROR:
            failures.append(
                f"判据 2：{row['cell']} 步数 {row['after']['cycles']}，"
                f"与真值 {TRUTH:.0f} 差 {error:+.0f}，超过 ±{MAX_CYCLE_ERROR}"
            )
        if not row["after"]["adopted"]:
            # 不采纳本身合法（支持度不够），但 24 格全是正常步行，一格都不该落到那里。
            failures.append(f"判据 1：{row['cell']} 没有采纳事件域估计")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trials", nargs="+", type=Path)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()

    cfg = AlgoConfig()
    rows: list[dict] = []
    for trial in args.trials:
        rows.extend(analyse(trial, cfg))

    print(f"{'趟/脚':22s}{'真 T':>7s}{'现状':>8s}{'新':>8s}{'步 旧→新':>10s}{'采纳':>5s}")
    for row in rows:
        print(
            f"{row['cell']:22s}{row['true_period_s']:>7.2f}"
            f"{row['error_before']:>8.1%}{row['error_after']:>8.1%}"
            f"{f'{row['before']['cycles']}→{row['after']['cycles']}':>10s}"
            f"{('是' if row['after']['adopted'] else '否'):>5s}"
        )

    before = np.array([row["error_before"] for row in rows], dtype=np.float64)
    after = np.array([row["error_after"] for row in rows], dtype=np.float64)
    cycles_before = np.array([row["before"]["cycles"] for row in rows], dtype=np.float64)
    cycles_after = np.array([row["after"]["cycles"] for row in rows], dtype=np.float64)
    summary = {
        "rms_before": float(np.sqrt((before**2).mean())),
        "rms_after": float(np.sqrt((after**2).mean())),
        "worst_before": float(before[np.argmax(abs(before))]),
        "worst_after": float(after[np.argmax(abs(after))]),
        "cycles_before": [int(cycles_before.min()), int(cycles_before.max())],
        "cycles_after": [int(cycles_after.min()), int(cycles_after.max())],
        "regressed_cells": int((abs(after) > abs(before)).sum()),
    }
    print(
        f"\n周期 RMS {summary['rms_before']:.1%} → {summary['rms_after']:.1%}；"
        f"最差 {summary['worst_before']:+.1%} → {summary['worst_after']:+.1%}；"
        f"步数 {summary['cycles_before']} → {summary['cycles_after']}；"
        f"逐格上移 {summary['regressed_cells']}/24（R2 允许，见变更说明）"
    )

    failures = judge(rows)
    print()
    for line in failures:
        print(f"不达标：{line}")
    print("判据 1、2：达标" if not failures else f"{len(failures)} 条不达标")

    if args.out:
        args.out.write_text(
            json.dumps(
                {
                    "config_version": AlgoConfig().version,
                    "truth_cycles": TRUTH,
                    "summary": summary,
                    "rows": rows,
                    "failures": failures,
                },
                ensure_ascii=False,
                indent=1,
            ),
            encoding="utf-8",
        )
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
