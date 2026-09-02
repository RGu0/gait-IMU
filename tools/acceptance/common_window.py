"""RAY-339 `common-interval-count-check` 判据 3 的真机验收（需求修订 R3）。

判据 3（R3 重写）：

* 数**支撑相槽**（`decode_alternation` 的输出），**不是网格格子** —— 网格只铺到首/末
  摆动峰之间，两脚范围不同时裁剪不对称，数出来的差是伪影；
* 公共窗内 **|N_L − N_R| ≥ 2 即标记**，阈由反相推出，无可调参数；
* **S1-sport/fast-a 必须被抓出**（槽 33/30，差 3，而跨脚比仅 1.010）；其余 **11 格零误伤**；
* 与跨脚比值**并列上报**，不互相替代。

脚本一个数都不自己算：`plan_dual_foot_periods` 的 `common_window` 与 `plan.cross_foot`
就是交付件的输出。**两道闸的盲区不同**这件事也直接在结果里读得出来 —— 被整数约束抓出
的那一格，它的跨脚比值必须离阈很远。

用法（在任一 scope worktree 内）：

    uv run --locked python <本目录>/common_window_acceptance.py \\
      "<library>/.../raw/S1-sport" "<library>/.../raw/S1-flat" --out common_window_acceptance.json
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from acceptance._dataset import load_walks
from gait.analysis.planning import plan_dual_foot_periods
from gait.config import AlgoConfig

#: 判据 3 点名必须被抓出的那一格。
MUST_FLAG = ("S1-sport", "fast-a")


def analyse(trial_dir: Path, cfg: AlgoConfig) -> list[dict]:

    rows: list[dict] = []
    for walk in load_walks(trial_dir, cfg):
        feet, nominal_fs = walk.feet, walk.nominal_fs
        label, name = walk.trial, walk.walk
        result = plan_dual_foot_periods(feet["L"], feet["R"], nominal_fs, cfg)
        rows.append(
            {
                "trial": label,
                "walk": name,
                "common_window": (
                    result.common_window.snapshot() if result.common_window else None
                ),
                "cross_foot": (
                    result.plan.cross_foot.snapshot() if result.plan.cross_foot else None
                ),
            }
        )
    return rows


def judge(rows: list[dict]) -> list[str]:
    failures: list[str] = []
    cfg = AlgoConfig()
    flagged: list[tuple[str, str]] = []

    for row in rows:
        cell = (row["trial"], row["walk"])
        count = row["common_window"]
        if count is None:
            failures.append(f"判据 3：{cell} 没有公共窗计数")
            continue
        if not count["agrees"]:
            flagged.append(cell)
        # 阈是推出来的：差 ≥ 2 才标记，差 ≤ 1 是反相下的构造性余量。
        if count["agrees"] != (count["difference"] <= 1):
            failures.append(
                f"判据 3：{cell} 的 agrees 与 difference 对不上："
                f"{count['agrees']} vs 差 {count['difference']}"
            )

    if flagged != [MUST_FLAG]:
        failures.append(
            f"判据 3：被标记的格应当恰好是 {MUST_FLAG}，实际是 {sorted(flagged)}"
        )

    target = next((row for row in rows if (row["trial"], row["walk"]) == MUST_FLAG), None)
    if target is None or target["common_window"] is None:
        failures.append(f"判据 3：找不到 {MUST_FLAG}")
    else:
        # 「两道闸盲区不同」不是一句说明，是一条可断言的事实：被整数约束抓出的那一格，
        # 比值闸必须离阈很远 —— 否则这道闸只是在重复比值闸已经说过的话。
        ratio = (target["cross_foot"] or {}).get("ratio")
        if ratio is None or ratio >= cfg.cross_foot_period_ratio_max:
            failures.append(
                f"判据 3：{MUST_FLAG} 的跨脚比值是 {ratio}，没有低于阈 "
                f"{cfg.cross_foot_period_ratio_max} —— 那样整数约束就没有给出新信息"
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

    print(f"{'趟':18s}{'槽 L/R':>9s}{'差':>4s}{'标记':>6s}{'公共窗 s':>10s}{'跨脚比':>8s}{'比值闸':>7s}")
    for row in rows:
        count = row["common_window"] or {}
        ratio = (row["cross_foot"] or {}).get("ratio")
        window = count.get("window", [0.0, 0.0])
        print(
            f"{row['trial'] + '/' + row['walk']:18s}"
            f"{f'{count.get("left")}/{count.get("right")}':>9s}"
            f"{count.get('difference', 0):>4d}"
            f"{('是' if not count.get('agrees', True) else '否'):>6s}"
            f"{window[1] - window[0]:>10.1f}"
            f"{ratio if ratio is not None else float('nan'):>8.3f}"
            f"{('过' if ratio is not None and ratio <= cfg.cross_foot_period_ratio_max else '标记'):>7s}"
        )

    failures = judge(rows)
    print()
    for line in failures:
        print(f"不达标：{line}")
    print("判据 3：达标" if not failures else f"{len(failures)} 条不达标")

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
