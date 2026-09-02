"""RAY-339 `boundary-truncation-reporting` 判据 4 的真机验收（需求修订 R3）。

判据 4：算法输出的周期数**含义不变**（仍是观测跨度内的完整周期），但报告里必须说得出
"首/尾各有一个周期被截断"；验收侧拿这个字段去对齐口径，24 格对齐后的周期数偏差
**|偏差| ≤ 0.5**。

两条路里选的是"报出截断"而不是"验收侧把真值取 37"，因为**"报出截断"与"补上那个 1"
是两回事**：前者对不定长的自由行走同样成立，后者只对定长走廊协议成立。本脚本把这件事
量出来 —— 用报告字段对齐（`spanned_cycles`）与用协议常数 +1 对齐，两者的准确度并排列出。

**判据 4 引用的 −1.29 是 scope A 之前的读数**；A 上线后未对齐的偏差是 −1.04。门
（|偏差| ≤ 0.5）不变，本脚本按门判。

用法（在任一 scope worktree 内）：

    uv run --locked python <本目录>/truncation_acceptance.py \\
      "<library>/.../raw/S1-sport" "<library>/.../raw/S1-flat" --out truncation_acceptance.json
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

from acceptance._dataset import TRUTH_CYCLES, load_walks
from gait.analysis.planning import plan_dual_foot_periods
from gait.config import AlgoConfig

TRUTH = TRUTH_CYCLES
MAX_BIAS = 0.5


def analyse(trial_dir: Path, cfg: AlgoConfig) -> list[dict]:

    rows: list[dict] = []
    for walk in load_walks(trial_dir, cfg):
        feet, nominal_fs = walk.feet, walk.nominal_fs
        label, name = walk.trial, walk.walk
        result = plan_dual_foot_periods(feet["L"], feet["R"], nominal_fs, cfg)
        for foot, detection in (("L", result.left), ("R", result.right)):
            period = detection.period
            rows.append(
                {
                    "cell": f"{label}/{name}/{foot}",
                    "cycles": period.cycles,
                    "head": period.head_truncated,
                    "tail": period.tail_truncated,
                    "truncated": period.truncated,
                    "spanned": period.spanned_cycles,
                }
            )
    return rows


def judge(rows: list[dict]) -> list[str]:
    failures: list[str] = []
    spanned = np.array([row["spanned"] for row in rows], dtype=np.float64)
    bias = float((spanned - TRUTH).mean())
    if abs(bias) > MAX_BIAS:
        failures.append(f"判据 4：对齐后的周期数偏差 {bias:+.2f}，超过 ±{MAX_BIAS}")

    for row in rows:
        # 含义不变：`cycles` 仍然只数完整格子，绝不含两头那两截。
        if row["spanned"] < row["cycles"]:
            failures.append(f"判据 4：{row['cell']} 对齐后 {row['spanned']} 少于 cycles {row['cycles']}")
        if row["head"] < 0.0 or row["tail"] < 0.0:
            failures.append(f"判据 4：{row['cell']} 报出了负的截断量")
        if row["truncated"] != (row["head"] + row["tail"] >= 0.5):
            failures.append(f"判据 4：{row['cell']} 的 truncated 与两头之和对不上")
    if not any(row["truncated"] for row in rows):
        failures.append("判据 4：24 格没有一格报出截断 —— 那说明字段没有真的在报")
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

    print(f"{'趟/脚':20s}{'cycles':>7s}{'头':>6s}{'尾':>6s}{'截断':>6s}{'对齐后':>7s}{'误差':>6s}")
    for row in rows:
        print(
            f"{row['cell']:20s}{row['cycles']:>7d}{row['head']:>6.2f}{row['tail']:>6.2f}"
            f"{('是' if row['truncated'] else '否'):>6s}{row['spanned']:>7d}"
            f"{row['spanned'] - TRUTH:>6.0f}"
        )

    cycles = np.array([row["cycles"] for row in rows], dtype=np.float64)
    spanned = np.array([row["spanned"] for row in rows], dtype=np.float64)
    summary = {
        "bias_raw": float((cycles - TRUTH).mean()),
        "bias_spanned": float((spanned - TRUTH).mean()),
        "rmse_spanned": float(np.sqrt(((spanned - TRUTH) ** 2).mean())),
        "within_one_spanned": int((abs(spanned - TRUTH) <= 1).sum()),
        # 对照：定长走廊才成立的那个常数。列出来是为了说明代价 —— 报告字段几乎一样准，
        # 但它不需要那个只对本协议成立的 1。
        "bias_plus_one": float((cycles + 1 - TRUTH).mean()),
        "rmse_plus_one": float(np.sqrt(((cycles + 1 - TRUTH) ** 2).mean())),
        "within_one_plus_one": int((abs(cycles + 1 - TRUTH) <= 1).sum()),
    }
    print(
        f"\n未对齐偏差 {summary['bias_raw']:+.2f}"
        f" → 报告字段对齐 {summary['bias_spanned']:+.2f}"
        f"（RMSE {summary['rmse_spanned']:.2f}，|误差|≤1 {summary['within_one_spanned']}/24）"
        f"\n对照 · 协议常数 +1：偏差 {summary['bias_plus_one']:+.2f}"
        f"（RMSE {summary['rmse_plus_one']:.2f}，|误差|≤1 {summary['within_one_plus_one']}/24）"
    )

    failures = judge(rows)
    print()
    for line in failures:
        print(f"不达标：{line}")
    print("判据 4：达标" if not failures else f"{len(failures)} 条不达标")

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
