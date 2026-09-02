"""反相自检的验收 —— **钉性质版**，取代 RAY-328 的 `xcorr_prior_acceptance.py`。

## 被取代的那一半已经不存在了

RAY-328 判据 3 有两半：**T_x 入池不回退**，与 **φ/T 落在反相带内**。

第一半在 RAY-343 scope A 之后**主题消失**：T_x 先验被摘掉了（当时它赢过网格，
RAY-339 之后输给了事件域精修，实测每项指标都更好）。旧脚本因此不是"不达标"，而是
**直接崩了** —— `KeyError: 'seeded'`，那个字段随第二遍一起删了。主题没了的脚本修不了，
只能取代。

第二半原样活着，而且**它才是互相关真正给出的东西**：互相关一次给两个量，峰间距对两条
时间轴的公共偏移免疫、绝对峰位不免疫；被摘掉的是免疫的那个，留下的是不免疫的那个。
听着反直觉，但那是因为 T_x 有了更准的竞争者，而 φ 没有 —— 没有第二个东西能回答
"两脚是不是反相"。

## 钉的三条性质

1. **24 格全部反相**：φ/T 落在 `AlgoConfig` 配的带内。带宽是实测散布的 6 倍，刻意放松
   给病理步态留余地，所以健康数据全过是应有的。
2. **T_x 确实摘干净了**：任何一格的周期估计池里都不许再出现 `crosscorrelation`。
3. **闸仍有牙**（阳性对照）：把同一只脚喂两遍，φ/T 会跑到 0 附近，必须判为**非反相**。
   没有这一条，第 1 条可以靠把判据焊成恒真来满足。

用法（在任一 scope worktree 内）：

    uv run --locked python <本目录>/antiphase_acceptance.py \\
      "<library>/.../raw/S1-sport" "<library>/.../raw/S1-flat" --out antiphase_acceptance.json
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

from acceptance._dataset import load_walks
from gait.analysis.planning import (
    cross_foot_phase,
    plan_dual_foot_periods,
)
from gait.config import AlgoConfig


def analyse(trial_dir: Path, cfg: AlgoConfig) -> list[dict]:

    rows: list[dict] = []
    for walk in load_walks(trial_dir, cfg):
        feet, nominal_fs = walk.feet, walk.nominal_fs
        label, name = walk.trial, walk.walk
        result = plan_dual_foot_periods(feet["L"], feet["R"], nominal_fs, cfg)

        # 阳性对照：把左脚喂两遍。同相，φ/T 跑到 0 附近，必须判为非反相。
        control = None
        if result.phase is not None and result.left.period is not None:
            swing = np.linalg.norm(feet["L"].gyro, axis=1)
            times = np.asarray(feet["L"].arrival, dtype=np.float64)
            seed = result.left.period.period_samples / feet["L"].fs
            control = cross_foot_phase(swing, times, swing, times, seed, cfg, grid_fs=nominal_fs)

        pools = [
            [name for name, _ in detection.period.estimates]
            for detection in (result.left, result.right)
            if detection.period is not None
        ]
        rows.append(
            {
                "trial": label,
                "walk": name,
                "phase": result.phase.snapshot() if result.phase else None,
                "pools": pools,
                "control_in_antiphase": (
                    control.in_antiphase if control is not None else None
                ),
                "control_fraction": (
                    control.phase_fraction if control is not None else None
                ),
            }
        )
    return rows


def judge(rows: list[dict]) -> list[str]:
    failures: list[str] = []
    cfg = AlgoConfig()

    for row in rows:
        cell = (row["trial"], row["walk"])
        phase = row["phase"]
        if phase is None:
            failures.append(f"性质 1：{cell} 没有相位结果")
            continue
        # ① 24 格全部反相。
        if phase["in_antiphase"] is not True:
            failures.append(
                f"性质 1：{cell} 的 φ/T = {phase['phase_fraction']:.3f}，不在反相带 "
                f"[{cfg.xcorr_antiphase_min}, {cfg.xcorr_antiphase_max}] 内"
            )
        # ② T_x 摘干净了。
        for pool in row["pools"]:
            if "crosscorrelation" in pool:
                failures.append(f"性质 2：{cell} 的估计池里仍有 crosscorrelation")
        # ③ 阳性对照：同相必须被判为非反相。
        if row["control_in_antiphase"] is not False:
            failures.append(
                f"性质 3：{cell} 的同相对照没有被判出（φ/T = "
                f"{row['control_fraction']}）—— 这道闸没有通电"
            )
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

    print(f"{'趟':22s}{'φ/T':>7s}{'反相':>6s}{'同相对照 φ/T':>14s}{'对照判出':>10s}{'池含T_x':>9s}")
    for row in rows:
        phase = row["phase"] or {}
        has_tx = any("crosscorrelation" in pool for pool in row["pools"])
        print(
            f"{row['trial'] + '/' + row['walk']:22s}{phase.get('phase_fraction', 0.0):>7.3f}"
            f"{('是' if phase.get('in_antiphase') else '否'):>6s}"
            f"{row['control_fraction'] or 0.0:>14.3f}"
            f"{('是' if row['control_in_antiphase'] is False else '否'):>10s}"
            f"{('是' if has_tx else '否'):>9s}"
        )

    fractions = [r["phase"]["phase_fraction"] for r in rows if r["phase"]]
    print(
        f"\nφ/T {min(fractions):.3f}~{max(fractions):.3f}（带 "
        f"[{cfg.xcorr_antiphase_min}, {cfg.xcorr_antiphase_max}]）；"
        f"同相对照判出 {sum(1 for r in rows if r['control_in_antiphase'] is False)}/12"
    )
    print(
        "注：RAY-328 判据 3 的另一半（T_x 入池不回退）已被 RAY-343 scope A 取代 —— "
        "先验摘掉后每项指标都更好，那一半的主题不复存在。"
    )

    failures = judge(rows)
    print()
    for line in failures:
        print(f"不达标：{line}")
    print("反相自检：达标" if not failures else f"{len(failures)} 条不达标")

    if args.out:
        args.out.write_text(
            json.dumps(
                {"config_version": cfg.version, "rows": rows, "failures": failures},
                ensure_ascii=False,
                indent=1,
            ),
            encoding="utf-8",
        )
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
