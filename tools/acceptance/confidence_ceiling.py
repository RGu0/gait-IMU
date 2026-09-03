"""软零速置信上限该由什么决定 —— RAY-347 判据 1、2、3。

## 背景

`consistent`（几个周期估计彼此差多少）在 RAY-339 之前是**开关**：不一致就把周期退回
自相关。此后事件域精修成了主估计，`detect_stance` 里那段代码自己写着「一致性闸从
开关降级为**读数**……它不再左右结果」。

但 `_period_confidence` 还在按旧含义读它：

```python
ceiling = 0.25 if period.consistent else 0.125
```

于是真机 24 格里有 4 格被减半，而那 4 格的最终周期误差是 +2.5% / −2.0% / −0.8% /
+0.4% —— 全表（−4.3% ~ +3.0%）里偏好的一半。**降的是一个已经不成立的理由。**

RAY-347 把两件事拆成两个字段：`consistent` 仍是读数，`fallback` 说「这个周期是退化
路径给的」，而 `_period_confidence` 改看后者。

## 钉什么

1. **精修被采纳的格，一格都不许被减半**（`fallback` 为假）。实测 24/24 采纳。
2. **阳性对照：减半那条路还活着。** 同一批真机数据禁用精修
   （`period_refine_min_intervals` 拉到 10**6）之后，`fallback` 必须回到
   `not consistent`，且**至少有一格**真的为真。

   没有这一条，第 1 条可以靠把减半整个删掉来满足 —— 那会把一条真降级悄悄摘掉，
   而且看起来一切正常。这里用的是**真机数据**，不是构造的信号：那 4 格的估计分歧
   本来就是真的。
3. **读数没被这次改动碰。** 钉的是它的**定义**而不是它的值：
   `ratio == max(估计)/min(估计)`、`consistent == (ratio < 阈值)`。钉定义不会因为
   数据或上游演进而过期，钉值会（RAY-328 的 `CYCLES_AFTER_L1` 就是钉值烂掉的）。

   外加一条**空转守卫**：不一致的格数必须 > 0。全都一致的话，第 1、2 条比较的是
   两个恒等的东西，会安静地全绿。

用法见 `tools/run_acceptance.py`。
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from acceptance._dataset import TRUTH_CYCLES, load_walks, parse_args, report
from gait.config import AlgoConfig
from gait.core.zupt import detect_stance

#: 让 `_refine_from_events` 必定不采纳，用来打开退化路径。与 `tests/test_zupt.py`
#: 的 `test_too_few_intervals_means_no_adoption` 同一个手法。
NO_REFINEMENT = 10**6


def _snapshot(foot, cfg: AlgoConfig) -> dict | None:
    detection = detect_stance(foot.accel, foot.gyro, foot.fs, cfg)
    period = detection.period
    if period is None:
        return None
    values = [value for _, value in period.estimates]
    return {
        "ratio": round(float(period.ratio), 4),
        "consistent": bool(period.consistent),
        "fallback": bool(period.fallback),
        "refined": "events" in dict(period.estimates),
        "period_samples": float(period.period_samples),
        # 判据 3 钉的是定义，所以把定义要用的两个量也带出来。
        "ratio_of_estimates": round(max(values) / min(values), 4),
        "n_estimates": len(values),
        # 判据 4 要问「退化时采用的是 seed 还是中位数」，两个都带出来。
        "seed": float(dict(period.estimates)["autocorrelation"]),
        "median": float(sorted(values)[len(values) // 2]) if len(values) % 2 else
                  float(sum(sorted(values)[len(values) // 2 - 1:len(values) // 2 + 1]) / 2),
    }


def analyse(trial_dir: Path, cfg: AlgoConfig) -> list[dict]:
    #: 阳性对照跑同一批数据，只关掉精修。
    without_refinement = replace(cfg, period_refine_min_intervals=NO_REFINEMENT)
    rows: list[dict] = []
    for walk in load_walks(trial_dir, cfg):
        true_period = walk.duration_s / TRUTH_CYCLES
        for label, foot in walk.feet.items():
            live = _snapshot(foot, cfg)
            if live is None:
                continue
            live["error_pct"] = round(
                (live["period_samples"] / foot.fs - true_period) / true_period * 100.0, 1
            )
            rows.append(
                {
                    "trial": walk.trial,
                    "walk": walk.walk,
                    "foot": label,
                    "live": live,
                    "control": _snapshot(foot, without_refinement),
                }
            )
    return rows


def judge(rows: list[dict]) -> list[str]:
    failures: list[str] = []
    cfg = AlgoConfig()
    inconsistent = 0

    for row in rows:
        cell = f"{row['trial']}/{row['walk']}/{row['foot']}"
        live = row["live"]
        if not live["consistent"]:
            inconsistent += 1

        # ① 精修被采纳 ⟹ 不是退化路径 ⟹ 不减半。
        if live["refined"] and live["fallback"]:
            failures.append(
                f"性质 1：{cell} 精修被采纳，却仍被记成退化路径 —— 置信度会按一个"
                f"已经不成立的理由减半（比值 {live['ratio']:.3f}）"
            )
        # ③ 读数的定义不许被碰。
        if live["ratio"] != live["ratio_of_estimates"]:
            failures.append(
                f"性质 3：{cell} 的 ratio {live['ratio']} 不等于估计池的 max/min "
                f"{live['ratio_of_estimates']} —— 读数的定义变了"
            )
        if live["consistent"] is not (live["ratio"] < cfg.stance_period_consistency_max):
            failures.append(
                f"性质 3：{cell} 的 consistent 与 ratio < "
                f"{cfg.stance_period_consistency_max} 不一致 —— 读数的定义变了"
            )

        # ② 阳性对照：关掉精修，退化路径必须回来。
        control = row["control"]
        if control is None:
            failures.append(f"性质 2：{cell} 关掉精修后连周期都没有了，对照无从谈起")
            continue
        if control["refined"]:
            failures.append(f"性质 2：{cell} 的对照仍然采纳了精修 —— 这个开关没起作用")
        if control["fallback"] is not (not control["consistent"]):
            failures.append(
                f"性质 2：{cell} 关掉精修后 fallback={control['fallback']} 而 "
                f"consistent={control['consistent']} —— 第一遍那句 "
                f"`median if consistent else seed` 与 fallback 对不上了"
            )
        # ④ 退化时采用的必须是 seed，不是中位数。
        #
        # 这一条本来打算用 `shuffle`（合成拖步步态）那个历史真阳性来钉 —— 代码注释里
        # 记的是"中位数给 259 而真值 400"。**实测复现不出来**：今天的 shuffle 比值只有
        # 1.015，一致，压根不走退化路径。所以改用真机数据钉同一件事，而且更硬：
        # 关掉精修之后那几格是**真的**不一致，采用值必须等于 seed。
        if control["fallback"] and control["period_samples"] != control["seed"]:
            failures.append(
                f"性质 4：{cell} 退化路径采用了 {control['period_samples']:.1f} 而 seed 是 "
                f"{control['seed']:.1f} —— 不一致时该退回自相关，不是拌一个中位数"
                f"（中位数 {control['median']:.1f}）"
            )

    # 空转守卫。
    if inconsistent == 0:
        failures.append(
            f"空转：{len(rows)} 格全部一致，性质 1、2 比较的是两个恒等的东西。"
            f"换一批有分歧的数据，否则这条判据只是在自我确认"
        )
    if not any(row["control"] and row["control"]["fallback"] for row in rows):
        failures.append(
            "性质 2：关掉精修之后**一格都没有**走退化路径 —— 减半那条路已经无法被"
            "这份数据证明还活着"
        )
    return failures


def _yes(flag: bool) -> str:
    return "是" if flag else "否"


def main() -> int:
    args = parse_args(__doc__ or "")
    cfg = AlgoConfig()
    rows = [row for trial in args.trials for row in analyse(trial, cfg)]
    if not rows:
        return report(rows, ["没有可用的趟次"], "软零速置信上限", args.out)

    print(f"{'格':26s}{'比值':>8s}{'一致':>5s}{'精修':>5s}{'退化':>5s}"
          f"{'周期误差':>10s}{'对照退化':>10s}")
    for row in rows:
        live, control = row["live"], row["control"]
        print(
            f"{row['trial'] + '/' + row['walk'] + '/' + row['foot']:26s}"
            f"{live['ratio']:>8.3f}{_yes(live['consistent']):>4s}"
            f"{_yes(live['refined']):>4s}{_yes(live['fallback']):>4s}"
            f"{live['error_pct']:>+9.1f}%"
            f"{(_yes(control['fallback']) if control else '—'):>10s}"
        )

    halved = sum(1 for row in rows if row["live"]["fallback"])
    control_halved = sum(1 for row in rows if row["control"] and row["control"]["fallback"])
    spread = [row["live"]["ratio"] for row in rows]
    print(
        f"\n{len(rows)} 格：比值 {min(spread):.3f}~{max(spread):.3f}（闸 "
        f"{cfg.stance_period_consistency_max}）；不一致 "
        f"{sum(1 for r in rows if not r['live']['consistent'])} 格；"
        f"精修采纳 {sum(1 for r in rows if r['live']['refined'])} 格"
        f"\n被减半：**{halved} 格**；关掉精修后被减半：{control_halved} 格（阳性对照）"
    )

    return report(
        rows,
        judge(rows),
        "软零速置信上限",
        args.out,
        extra={"halved": halved, "control_halved": control_halved},
    )


if __name__ == "__main__":
    raise SystemExit(main())
