"""周期数与速度档一致性 —— 取代 RAY-325 的 `period_stance_field.py`。

守 RAY-325 判据 1（真机检出进入可接受范围，且不需要按速度分档调参）与判据 4
（慢速不过检、快速不漏检）。

## 原件为什么必须被取代

它**已经崩了**：

    ImportError: cannot import name '_runs' from 'gait.core.zupt'

改名发生在 `30df59f` —— RAY-325 **自己的第二个 scope** 把 `_runs` 提成公开的
`runs`。前一个 scope 的验收脚本当场失效，同一个 Issue 内部，没人发现。原件住在
云端证据库：不进版本、不过 lint、不过审查，没有任何东西逼它保持能跑。

这是同一个病的第三例（RAY-328 的 `KeyError('seeded')`、RAY-328 判据 4 的过时对照
表、这一个）。三次都不是算法出错。

## 钉什么

原件把「原型基准 34~43」印在报告里当参照，判据本身却停留在 Linear 上，脚本不判。
本版直接判，并且钉的是**区间**不是逐格的数：

1. **周期数全部落在 [35, 40]** —— 真值 38，实测 36~38。区间上下限管离群，
   **不要求逐格单调**：那是 RAY-339 R1 与 RAY-343 R1 连犯两次的错误形状，对一个
   会重新分布误差的改动写严格占优的门。
2. **慢档与快档的误差不得反号** —— 这才是判据 4 要挡的那种失效。阈值法在慢速过检、
   快速漏检，误差随速度换号；周期分段法不该有这个性质。
3. **阳性对照**：把同一趟真机数据**砍掉后半段**再跑一遍，周期数必须掉出 [35, 40]。
   没有这一条，「全部落在区间内」可以靠把区间放到 [0, 999] 来满足 —— 那比崩掉更糟，
   崩掉至少看得见。

用的是**真机数据的一半**，不是编的信号：走一半路就该只有一半周期，这个断言不依赖
任何模型假设。

## 24 格实测（`fe25aa7`）

| 档位 | 周期数 | 误差 |
| --- | --- | --- |
| slow | 36~37 | −5% ~ −3% |
| mid | 37~38 | −3% ~ +0% |
| fast | 36~39 | −5% ~ +3% |

合计 **36~39**，原型基准是 34~43；RAY-339 的事件域精修与 RAY-343 摘掉 T_x 先验都
传导到了这里，而 RAY-325 关闭判据 1、4 时记的还是原型档的数。

**比原件多出一格 39。** 原件按**每只脚各自的** `arrival[0]` 切趟，本版走
`_dataset.load_walks`，两只脚用**共同原点**（`gait_metrics_field.py` 当年就论证过
共同原点才对：各减各的零点，两足之间就再没有共同零点可言）。两足首包差约 15 ms
= 3 个样本，而周期数是 `round(时长/周期)` —— `S1-sport/fast-b/L` 正好压在进位边上，
38 变 39。**两个读数都不算错，共同原点那个更有依据**；判据钉的是区间 [35, 40]，
它对这 3 个样本不敏感，这正是钉区间而不钉逐格数的意义。

用法见 `tools/run_acceptance.py`，或单独跑：

    uv run --locked python tools/acceptance/period_cycles.py <采集目录>... --out out.json
"""

from __future__ import annotations

from pathlib import Path

from acceptance._dataset import TRUTH_CYCLES, load_walks, parse_args, report
from gait.config import AlgoConfig
from gait.core.zupt import detect_stance

#: 周期数的可接受区间。真值 38，实测 36~38 —— 上下各留 2~3 个周期的余量。
#: 下限比上限离实测远一点，因为漏检（丢周期）比过检更常见：丢包造成的空洞会劈开
#: 支撑相，而周期栅格是按 `round(时长/周期)` 定的，栅格本身不会凭空多出周期来。
CYCLE_RANGE = (35, 40)

#: 阳性对照保留原趟的比例。取一半 —— 远离判据区间，所以这一条测的是「区间检查通不通
#: 电」，不是「区间定得准不准」。区间准不准由 Linear 上的判据管，不由对照管。
CONTROL_KEEP = 0.5


def _cycles(foot, cfg: AlgoConfig, keep: float = 1.0) -> int:
    """一只脚一趟的周期数。`keep < 1` 时只喂前一段，用作阳性对照。"""
    stop = max(int(len(foot.accel) * keep), 1)
    detection = detect_stance(foot.accel[:stop], foot.gyro[:stop], foot.fs, cfg)
    return detection.period.cycles if detection.period is not None else 0


def analyse(trial_dir: Path, cfg: AlgoConfig) -> list[dict]:
    rows: list[dict] = []
    for walk in load_walks(trial_dir, cfg):
        # 速度档从趟次名里取：`slow-a` / `mid-b` / `fast-a`。判据 4 比的是档与档之间，
        # 不是趟与趟之间。
        speed = walk.walk.split("-")[0]
        for label, foot in walk.feet.items():
            cycles = _cycles(foot, cfg)
            rows.append(
                {
                    "trial": walk.trial,
                    "walk": walk.walk,
                    "foot": label,
                    "speed": speed,
                    "cycles": cycles,
                    "error_pct": round((cycles - TRUTH_CYCLES) / TRUTH_CYCLES * 100.0, 1),
                    # 阳性对照与正样本走的是同一条代码路径，只是输入短一半。
                    "control_cycles": _cycles(foot, cfg, CONTROL_KEEP),
                }
            )
    return rows


def judge(rows: list[dict]) -> list[str]:
    failures: list[str] = []
    low, high = CYCLE_RANGE

    for row in rows:
        cell = f"{row['trial']}/{row['walk']}/{row['foot']}"
        # ① 周期数落在区间内。
        if not low <= row["cycles"] <= high:
            failures.append(
                f"性质 1：{cell} 周期数 {row['cycles']} 不在 [{low}, {high}] 内"
                f"（真值 {TRUTH_CYCLES:.0f}）"
            )
        # ③ 阳性对照：只走一半路，周期数必须掉出区间。
        if low <= row["control_cycles"] <= high:
            failures.append(
                f"性质 3：{cell} 的阳性对照没被抓出 —— 只喂了前 "
                f"{CONTROL_KEEP:.0%} 的数据，周期数仍是 {row['control_cycles']}，"
                f"落在 [{low}, {high}] 内。这个区间检查没有通电"
            )

    # ② 两端误差不得反号。整档在零的两侧才算反号 —— 一格擦过零不算，那是噪声不是失效。
    bands = {
        speed: [row["error_pct"] for row in rows if row["speed"] == speed]
        for speed in ("slow", "mid", "fast")
    }
    slow, fast = bands.get("slow") or [], bands.get("fast") or []
    if slow and fast:
        reversed_up = min(slow) > 0 and max(fast) < 0
        reversed_down = max(slow) < 0 and min(fast) > 0
        if reversed_up or reversed_down:
            failures.append(
                f"性质 2：慢档误差 {min(slow):+.0f}%~{max(slow):+.0f}%，"
                f"快档 {min(fast):+.0f}%~{max(fast):+.0f}% —— 两端反号，"
                f"这正是阈值法那种「慢速过检、快速漏检」的失效"
            )
    return failures


def main() -> int:
    args = parse_args(__doc__ or "")
    cfg = AlgoConfig()
    rows = [row for trial in args.trials for row in analyse(trial, cfg)]

    print(f"{'格':26s}{'周期数':>8s}{'误差':>8s}{'对照(半趟)':>12s}")
    for row in rows:
        print(
            f"{row['trial'] + '/' + row['walk'] + '/' + row['foot']:26s}"
            f"{row['cycles']:>8d}{row['error_pct']:>+7.0f}%{row['control_cycles']:>12d}"
        )

    counts = [row["cycles"] for row in rows]
    control = [row["control_cycles"] for row in rows]
    print(
        f"\n{len(rows)} 格：周期数 {min(counts)}~{max(counts)}"
        f"（真值 {TRUTH_CYCLES:.0f}，判据区间 {CYCLE_RANGE[0]}~{CYCLE_RANGE[1]}）；"
        f"阳性对照 {min(control)}~{max(control)}"
    )
    print("按速度档（判据 4：两端误差不得反号）")
    for speed in ("slow", "mid", "fast"):
        band = [row["error_pct"] for row in rows if row["speed"] == speed]
        if band:
            print(f"  {speed:5s} 误差 {min(band):+5.0f}% ~ {max(band):+5.0f}%")

    return report(
        rows,
        judge(rows),
        "周期数与速度档一致性",
        args.out,
        extra={"cycle_range": list(CYCLE_RANGE), "control_keep": CONTROL_KEEP},
    )


if __name__ == "__main__":
    raise SystemExit(main())
