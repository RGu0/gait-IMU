"""RAY-343 `drop-xcorr-period-prior` 判据 1、2 的真机验收（需求修订 R1）。

判据 1（R2）：摘掉 T_x 先验后，24 格周期 RMS ≤ 2.0%、最差单格 ≤ 4.5%、步数（对齐后）
RMSE ≤ 0.85、误差绝对值 ≤ 1 的格 ≥ 23/24。
判据 2：φ 反相自检原样保留 —— 24 格 φ/T 仍全部落在 [0.35, 0.65]。

**判据 1 没有逐格单调要求**，离群由"最差单格 ≤ 4.5%"那个上限管。R1 写过"逐格不得
更差"，实测 2 格上移（`sport/slow-a` 两只脚，+0.5%→+1.3% 与 +1.7%→+3.0%），而四个
聚合门全部达标且有余量 —— 那是对"重新分布误差"的改动写了"严格占优"的门，与 RAY-339
R1 判据 2 是同一个错误，紧接着又犯了一次。R2 删掉那句。

"带先验"那一列仍然算并打印出来，因为**改善的证据要看得见**；它只是不再当判据。

## 基线是当场重建的，不是钉在脚本里的

"带先验"那一列由本脚本在**同一次运行里**重建：`detect_stance(period_prior_samples=)`
这个参数保留着（它是 core 的通用入口，不是 L1 专用），所以旧行为可以精确复现。

这一点是本 Issue 存在的理由的直接应用 —— RAY-328 的 `alternation_acceptance.py` 把
逐格周期数钉成了一张 `CYCLES_AFTER_L1` 表，RAY-339 改了周期估计之后那张表整个过时，
12 条失败全部来自它。**钉绝对数就是给自己设保质期。**

用法（在任一 scope worktree 内）：

    uv run --locked python <本目录>/drop_prior_acceptance.py \\
      "<library>/.../raw/S1-sport" "<library>/.../raw/S1-flat" --out drop_prior_acceptance.json
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

from acceptance._dataset import TRUTH_CYCLES, load_walks
from gait.analysis.planning import plan_dual_foot_periods
from gait.config import AlgoConfig
from gait.core.zupt import detect_stance

TRUTH = TRUTH_CYCLES

MAX_RMS = 0.020
MAX_CELL = 0.045
MAX_CYCLE_RMSE = 0.85
MIN_WITHIN_ONE = 23


def analyse(trial_dir: Path, cfg: AlgoConfig) -> list[dict]:

    rows: list[dict] = []
    for walk in load_walks(trial_dir, cfg):
        feet, nominal_fs = walk.feet, walk.nominal_fs
        label, name = walk.trial, walk.walk
        duration = walk.duration_s
        result = plan_dual_foot_periods(feet["L"], feet["R"], nominal_fs, cfg)
        true_period = duration / TRUTH
        # 基线：把 T_x 先验重新喂回去，复现 RAY-328 L1 的行为。同一次运行、同一段数据，
        # 只差这一个参数。
        prior_s = result.phase.period_s if result.phase else None
        for foot, detection in (("L", result.left), ("R", result.right)):
            series = feet[foot]
            with_prior = (
                detect_stance(
                    series.accel,
                    series.gyro,
                    series.fs,
                    cfg,
                    period_prior_samples=prior_s * series.fs,
                )
                if prior_s
                else detection
            )
            rows.append(
                {
                    "cell": f"{label}/{name}/{foot}",
                    "true_period_s": true_period,
                    "period_s": detection.period.period_samples / series.fs,
                    "spanned": detection.period.spanned_cycles,
                    "period_s_with_prior": with_prior.period.period_samples / series.fs,
                    "spanned_with_prior": with_prior.period.spanned_cycles,
                    "phase_fraction": (
                        result.phase.phase_fraction if result.phase else None
                    ),
                    "in_antiphase": result.phase.in_antiphase if result.phase else None,
                    "pool": [name for name, _ in detection.period.estimates],
                }
            )
    return rows


def judge(rows: list[dict]) -> list[str]:
    failures: list[str] = []
    cfg = AlgoConfig()

    error = np.array(
        [(row["period_s"] - row["true_period_s"]) / row["true_period_s"] for row in rows]
    )
    spanned = np.array([row["spanned"] for row in rows], dtype=np.float64)

    rms = float(np.sqrt((error**2).mean()))
    worst = float(abs(error).max())
    cycle_rmse = float(np.sqrt(((spanned - TRUTH) ** 2).mean()))
    within = int((abs(spanned - TRUTH) <= 1).sum())
    if rms > MAX_RMS:
        failures.append(f"判据 1：周期 RMS {rms:.2%} > {MAX_RMS:.1%}")
    if worst > MAX_CELL:
        failures.append(f"判据 1：最差单格 {worst:.2%} > {MAX_CELL:.1%}")
    if cycle_rmse > MAX_CYCLE_RMSE:
        failures.append(f"判据 1：步数 RMSE {cycle_rmse:.2f} > {MAX_CYCLE_RMSE}")
    if within < MIN_WITHIN_ONE:
        failures.append(f"判据 1：误差 ≤ 1 的格 {within} < {MIN_WITHIN_ONE}")

    for row in rows:
        # **没有逐格单调判据**（R1 → R2）。这里只钉"摘干净了"：估计池里不许再出现
        # crosscorrelation —— 那是本 scope 唯一的结构性断言。
        if "crosscorrelation" in row["pool"]:
            failures.append(f"判据 1：{row['cell']} 的估计池里仍有 crosscorrelation")

    for row in rows:
        # 判据 2：φ 那一半必须原样活着。
        if row["in_antiphase"] is not True:
            failures.append(
                f"判据 2：{row['cell']} 的 φ/T = {row['phase_fraction']}，不在反相带内"
            )
        fraction = row["phase_fraction"]
        if fraction is None or not (
            cfg.xcorr_antiphase_min <= fraction <= cfg.xcorr_antiphase_max
        ):
            failures.append(f"判据 2：{row['cell']} 的 φ/T 越出配置的带")
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

    print(f"{'趟/脚':20s}{'带先验':>8s}{'摘掉后':>8s}{'改善':>8s}{'步(对齐)':>9s}{'φ/T':>7s}")
    for row in rows:
        was = (row["period_s_with_prior"] - row["true_period_s"]) / row["true_period_s"]
        now = (row["period_s"] - row["true_period_s"]) / row["true_period_s"]
        print(
            f"{row['cell']:20s}{was:>8.1%}{now:>8.1%}{abs(was) - abs(now):>8.1%}"
            f"{row['spanned']:>9d}{row['phase_fraction']:>7.3f}"
        )

    error = np.array(
        [(row["period_s"] - row["true_period_s"]) / row["true_period_s"] for row in rows]
    )
    before = np.array(
        [
            (row["period_s_with_prior"] - row["true_period_s"]) / row["true_period_s"]
            for row in rows
        ]
    )
    spanned = np.array([row["spanned"] for row in rows], dtype=np.float64)
    spanned_before = np.array(
        [row["spanned_with_prior"] for row in rows], dtype=np.float64
    )
    summary = {
        "rms_with_prior": float(np.sqrt((before**2).mean())),
        "rms_without": float(np.sqrt((error**2).mean())),
        "worst_with_prior": float(before[np.argmax(abs(before))]),
        "worst_without": float(error[np.argmax(abs(error))]),
        "cycle_rmse_with_prior": float(np.sqrt(((spanned_before - TRUTH) ** 2).mean())),
        "cycle_rmse_without": float(np.sqrt(((spanned - TRUTH) ** 2).mean())),
        "within_one_with_prior": int((abs(spanned_before - TRUTH) <= 1).sum()),
        "within_one_without": int((abs(spanned - TRUTH) <= 1).sum()),
    }
    print(
        f"\n周期 RMS {summary['rms_with_prior']:.1%} → {summary['rms_without']:.1%}"
        f"；最差 {summary['worst_with_prior']:+.1%} → {summary['worst_without']:+.1%}"
        f"；步数 RMSE {summary['cycle_rmse_with_prior']:.2f} → {summary['cycle_rmse_without']:.2f}"
        f"；误差≤1 {summary['within_one_with_prior']}/24 → {summary['within_one_without']}/24"
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
                    "config_version": cfg.version,
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
