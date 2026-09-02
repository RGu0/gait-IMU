"""交替解码的验收 —— **钉性质版**，取代 RAY-328 的 `alternation_acceptance.py`。

RAY-328 那一版硬编码了一张 `CYCLES_AFTER_L1` 表（当时的逐格周期数），用来核"L2 不碰
周期估计"。RAY-339 改了周期估计之后那张表整个过时，**12 条失败全部来自它** —— 而被
守护的性质不但仍然成立，还变好了（冲突从 5 处降到 2 处）。

同一个脚本里的另一条判据 —— 账目恒等式 `槽 = 检出 − 合并 + 补槽` —— **到今天仍然
一字不差地成立**。这就是钉性质与钉绝对数的差别，两者在同一个文件里同时存在过，
结局完全不同。

## 钉的四条性质

1. **破缺全部有主**：解码后剩下的每一处同足相邻，都必须在 `conflicts` 里有对应的一条。
   不达标只有这一种情形 —— 破缺发生了而没人记。
2. **账目自洽**：`len(slots) == detected − merged + inferred`。一段检出都不会凭空消失。
3. **周期数落在真值附近**：逐格 `|cycles − 38| ≤ 3`。真值 38 是**受控实测**
   （`evidence/ray-337/protocol-truth/`），不是从这一版代码读出来的基线 —— 所以它
   不会随代码演进而过时，这正是它取代 `CYCLES_AFTER_L1` 的理由。
4. **闸仍有牙**（阳性对照）：从右脚删掉一串支撑相，解码器必须察觉 —— 要么补槽，
   要么记冲突，`inferred + same_foot_adjacencies` 必须比基线多。

用法（在任一 scope worktree 内）：

    uv run --locked python <本目录>/alternation_decode_acceptance.py \\
      "<library>/.../raw/S1-sport" "<library>/.../raw/S1-flat" --out alternation_decode_acceptance.json
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

from acceptance._dataset import TRUTH_CYCLES, load_walks
from gait.analysis.planning import (
    _net_stance_spans,
    plan_dual_foot_periods,
)
from gait.config import AlgoConfig
from gait.core.dualfoot import decode_alternation

TRUTH = TRUTH_CYCLES
MAX_CYCLE_ERROR = 3
#: 阳性对照：从右脚连着删掉这么多个支撑相。
CONTROL_DROP = 3


def analyse(trial_dir: Path, cfg: AlgoConfig) -> list[dict]:

    rows: list[dict] = []
    for walk in load_walks(trial_dir, cfg):
        feet, nominal_fs = walk.feet, walk.nominal_fs
        label, name = walk.trial, walk.walk
        result = plan_dual_foot_periods(feet["L"], feet["R"], nominal_fs, cfg)
        decoding = result.alternation

        # 阳性对照：从右脚中段连着删掉几个支撑相再解一次。解码器必须察觉 ——
        # 要么补槽（间隔是整数个 stride），要么记冲突（不是）。
        control = None
        if decoding is not None:
            spans = {
                foot: _net_stance_spans(feet[foot], det, result.plan.window)
                for foot, det in (("L", result.left), ("R", result.right))
            }
            middle = len(spans["R"]) // 2
            punched = spans["R"][:middle] + spans["R"][middle + CONTROL_DROP :]
            control = decode_alternation(spans["L"], punched, decoding.stride_s, cfg)

        rows.append(
            {
                "trial": label,
                "walk": name,
                "cycles": [
                    result.left.period.cycles if result.left.period else None,
                    result.right.period.cycles if result.right.period else None,
                ],
                "decoding": decoding.snapshot() if decoding else None,
                "control_noticed": (
                    (
                        control.inferred + control.same_foot_adjacencies
                        > decoding.inferred + decoding.same_foot_adjacencies
                    )
                    if control is not None and decoding is not None
                    else None
                ),
            }
        )
    return rows


def judge(rows: list[dict]) -> list[str]:
    failures: list[str] = []
    for row in rows:
        cell = (row["trial"], row["walk"])
        decoding = row["decoding"]
        if decoding is None:
            failures.append(f"性质 1：{cell} 没有解码结果")
            continue

        # ① 破缺全部有主。
        if decoding["same_foot_adjacencies"] != len(decoding["conflicts"]):
            failures.append(
                f"性质 1：{cell} 破缺 {decoding['same_foot_adjacencies']} 处但只记了 "
                f"{len(decoding['conflicts'])} 条冲突 —— 有破缺被静默吞掉"
            )
        # ② 账目自洽。
        expected = decoding["detected"] - decoding["merged"] + decoding["inferred"]
        if decoding["slots"] != expected:
            failures.append(
                f"性质 2：{cell} 槽位账目对不上 —— {decoding['slots']} ≠ "
                f"{decoding['detected']} − {decoding['merged']} + {decoding['inferred']}"
            )
        # ③ 周期数落在受控真值附近。锚是真值，不是上一版代码的读数。
        for foot, cycles in zip(("L", "R"), row["cycles"], strict=True):
            if cycles is None:
                failures.append(f"性质 3：{cell}/{foot} 没有周期数")
            elif abs(cycles - TRUTH) > MAX_CYCLE_ERROR:
                failures.append(
                    f"性质 3：{cell}/{foot} 周期数 {cycles}，与受控真值 {TRUTH:.0f} 差 "
                    f"{cycles - TRUTH:+.0f}，超过 ±{MAX_CYCLE_ERROR}"
                )
        # ④ 阳性对照。
        if row["control_noticed"] is not True:
            failures.append(
                f"性质 4：{cell} 删掉 {CONTROL_DROP} 个支撑相后解码器没有察觉 —— "
                "这道闸没有通电"
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

    print(
        f"{'趟':22s}{'周期 L/R':>10s}{'检出':>6s}{'合并':>6s}{'补槽':>6s}"
        f"{'槽位':>6s}{'破缺':>6s}{'冲突':>6s}{'对照察觉':>10s}"
    )
    for row in rows:
        decoding = row["decoding"] or {}
        print(
            f"{row['trial'] + '/' + row['walk']:22s}"
            f"{f'{row['cycles'][0]}/{row['cycles'][1]}':>10s}"
            f"{decoding.get('detected', 0):>6d}{decoding.get('merged', 0):>6d}"
            f"{decoding.get('inferred', 0):>6d}{decoding.get('slots', 0):>6d}"
            f"{decoding.get('same_foot_adjacencies', 0):>6d}"
            f"{len(decoding.get('conflicts', [])):>6d}"
            f"{('是' if row['control_noticed'] else '否'):>10s}"
        )

    cycles = np.array([c for row in rows for c in row["cycles"]], dtype=np.float64)
    print(
        f"\n周期数 {int(cycles.min())}~{int(cycles.max())}（受控真值 {TRUTH:.0f}，"
        f"门 ±{MAX_CYCLE_ERROR}）；冲突合计 "
        f"{sum(len((r['decoding'] or {}).get('conflicts', [])) for r in rows)} 处；"
        f"对照察觉 {sum(1 for r in rows if r['control_noticed'])}/12"
    )
    print(
        "注：本版不再钉 RAY-328 的 CYCLES_AFTER_L1 对照表 —— 那张表随周期估计演进而"
        "过时，12 条失败全出自它。锚改成受控真值 38，它不随代码变。"
    )

    failures = judge(rows)
    print()
    for line in failures:
        print(f"不达标：{line}")
    print("交替解码：达标" if not failures else f"{len(failures)} 条不达标")

    if args.out:
        args.out.write_text(
            json.dumps(
                {
                    "config_version": cfg.version,
                    "truth_cycles": TRUTH,
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
