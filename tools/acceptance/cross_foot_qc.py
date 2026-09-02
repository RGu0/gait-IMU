"""跨脚校验 + 净窗宽闸的验收 —— **钉性质版**，取代 RAY-328 的 `dual_foot_qc_acceptance.py`。

RAY-328 那一版把三个具体的格钉死了：`S1-flat/slow-a` 必须被标记（比值 1.172），
`S1-sport/slow-b`（1.112）与 `S1-sport/mid-a`（1.104）必须紧贴阈下。RAY-339 的事件域
精修把那些周期估计修好之后，24 格的比值全线塌到 1.000~1.022 —— **那三个锚点一个都不
成立了**，脚本从此永远红，而代码本身没有任何问题。

本版不钉任何具体的格，改钉三条性质：

1. **零误伤**：健康数据上没有一格被标记。这是这道闸在正常数据上唯一该有的行为。
2. **闸仍有牙**（阳性对照）：拿真机量到的周期人为拉开一个超阈的比值，它必须被抓出。
   没有这一条，"零误伤"可以靠把闸焊死来满足 —— 那比过时更糟，它看起来在守。
3. **只加票不否决**：被标记的那一格仍然可规划，周期一个不丢。

宽闸那半（RAY-328 判据 2）原样保留 —— 它本来就是钉性质的，从没过时。

## 历史：这份数据曾经有一个真阳性

RAY-328 时 `S1-flat/slow-a` 的跨脚比是 **1.172**，是单脚一致性闸（1.277 < 1.3）放过而
跨脚闸抓住的一格。RAY-339 之后它是 **1.022**。**这道闸在这份数据上再没有已知真阳性**，
它的鉴别力此后只能靠阳性对照（第 2 条）证明，不能靠真实数据证明。要拿真实数据重新
证明，得等病理/不对称步态采集（RAY-337）。

用法（在任一 scope worktree 内）：

    uv run --locked python <本目录>/cross_foot_qc_acceptance.py \\
      "<library>/.../raw/S1-sport" "<library>/.../raw/S1-flat" --out cross_foot_qc_acceptance.json
"""

from __future__ import annotations

import argparse
import json
from dataclasses import replace
from pathlib import Path

from acceptance._dataset import load_walks
from gait.analysis.planning import plan_dual_foot_periods
from gait.config import AlgoConfig
from gait.core.dualfoot import check_cross_foot_period

#: 阳性对照用的比值。取阈的 1.5 倍 —— 远离阈，所以这一条测的是"闸通不通电"，
#: 不是"阈定得准不准"。阈准不准由 Linear 上的判据管，不由对照管。
POSITIVE_CONTROL_RATIO = 1.5
#: 宽闸下参与规划的格，双净覆盖的下限（RAY-328 判据 2 原文）。
MIN_COVERAGE = 0.95


def analyse(trial_dir: Path, cfg: AlgoConfig) -> list[dict]:

    rows: list[dict] = []
    for walk in load_walks(trial_dir, cfg):
        feet, nominal_fs = walk.feet, walk.nominal_fs
        label, name = walk.trial, walk.walk
        result = plan_dual_foot_periods(feet["L"], feet["R"], nominal_fs, cfg)

        # 阳性对照：把右脚的周期人为拉长到超阈，闸必须抓出来。用的是**这一格真机量到
        # 的周期**，不是编的数 —— 所以它同时证明了"闸接在真实数据上也通电"。
        injected = None
        if result.left.period is not None and result.right.period is not None:
            stretched = replace(
                result.right.period,
                period_samples=result.right.period.period_samples
                * POSITIVE_CONTROL_RATIO,
            )
            injected = check_cross_foot_period(
                result.left.period, feet["L"].fs, stretched, feet["R"].fs, cfg
            )

        rows.append(
            {
                "trial": label,
                "walk": name,
                "cross_foot": (
                    result.plan.cross_foot.snapshot() if result.plan.cross_foot else None
                ),
                "window": result.plan.window.snapshot(),
                "plannable": result.plan.plannable,
                "degraded": result.plan.degraded,
                "injected_flagged": (
                    (not injected.agrees) if injected is not None else None
                ),
                "injected_ratio": injected.ratio if injected is not None else None,
            }
        )
    return rows


def judge(rows: list[dict]) -> list[str]:
    failures: list[str] = []
    cfg = AlgoConfig()

    for row in rows:
        cell = (row["trial"], row["walk"])
        cross = row["cross_foot"]
        if cross is None:
            failures.append(f"性质 1：{cell} 没有跨脚校验结果")
            continue

        # ① 零误伤 —— 健康数据上不该有任何一格被标记。
        if not cross["agrees"]:
            failures.append(
                f"性质 1：{cell} 被标记降级（比值 {cross['ratio']:.3f} > "
                f"{cfg.cross_foot_period_ratio_max}），健康数据上不该有"
            )
        # ② 阳性对照 —— 把周期拉到超阈，必须抓出来。
        if row["injected_flagged"] is not True:
            failures.append(
                f"性质 2：{cell} 的阳性对照没有被抓出（注入比值 "
                f"{row['injected_ratio']}）—— 这道闸没有通电"
            )
        # ③ 只加票不否决 —— 标记与否都不该影响可规划性本身。
        if row["degraded"] and not row["plannable"]:
            failures.append(f"性质 3：{cell} 被标记后变得不可规划 —— 那是否决，不是加票")

        # 宽闸那半（RAY-328 判据 2）：拒了要具名，覆盖率必出。
        window = row["window"]
        if "coverage" not in window:
            failures.append(f"判据 2：{cell} 的输出里没有覆盖率")
        usable = not {"left_unusable", "right_unusable"} & set(window["refusals"])
        if usable and window["coverage"] < MIN_COVERAGE:
            failures.append(
                f"判据 2：{cell} 参与规划但双净覆盖 {window['coverage']:.4f} < {MIN_COVERAGE}"
            )
        if not window["plannable"] and not window["refusals"]:
            failures.append(f"判据 2：{cell} 被拒但没有给出理由 —— 静默截断")
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

    print(f"{'趟':22s}{'跨脚比':>8s}{'标记':>6s}{'对照抓出':>10s}{'覆盖率':>9s}{'可规划':>7s}")
    for row in rows:
        cross = row["cross_foot"] or {}
        print(
            f"{row['trial'] + '/' + row['walk']:22s}{cross.get('ratio', float('nan')):>8.3f}"
            f"{('是' if row['degraded'] else '否'):>6s}"
            f"{('是' if row['injected_flagged'] else '否'):>10s}"
            f"{row['window']['coverage']:>9.4f}"
            f"{('是' if row['plannable'] else '否'):>7s}"
        )

    ratios = [r["cross_foot"]["ratio"] for r in rows if r["cross_foot"]]
    print(
        f"\n跨脚比值 {min(ratios):.3f}~{max(ratios):.3f}（阈 "
        f"{cfg.cross_foot_period_ratio_max}）；被标记 "
        f"{sum(1 for r in rows if r['degraded'])}/12；阳性对照抓出 "
        f"{sum(1 for r in rows if r['injected_flagged'])}/12"
    )
    print(
        "注：RAY-328 时 S1-flat/slow-a 的比值是 1.172（真阳性），RAY-339 的事件域精修"
        "把它修到了 1.022。**这份数据上已无已知真阳性**，鉴别力靠阳性对照证明。"
    )

    failures = judge(rows)
    print()
    for line in failures:
        print(f"不达标：{line}")
    print("跨脚校验 + 宽闸：达标" if not failures else f"{len(failures)} 条不达标")

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
