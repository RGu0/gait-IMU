"""航向漂移的**双向哨兵** —— RAY-356 判据 1~5。

## 它守的不是"够好"，是"还在已知的那个带里"

真机直行一趟的航向合计漂 **103°~1655°**（T-230-03 每趟单向直行，转身真值 **0**）。
逐步 |Δ航向| 中位 3.2~33.5°，而转身判据是 25°/步 —— 慢档因此 8/8 格判出假转身，
中段步从 33 剔到 6，下游的双支撑期占比读到 −6.2（RAY-354 / RAY-351）。

**本脚本不判"航向够不够好"** —— 它现在不够好，而修法还没选定（三个候选都未验证，
见 Issue 正文）。它做的是把这个缺陷**变成一个被守住、可度量的量**：在此之前
没有任何东西在量航向漂移，而那正是 RAY-350 的覆盖图记下的那类空白。

## 为什么每条都是双向的

被守的量目前**都是坏的**。钉"不得更坏"会把坏值当基线供起来 —— 下一个人看到绿灯，
以为这里没问题。

双向哨兵在它**变好**时也红，那时该来把判据收紧。这与 `tests/test_v1a_regression.py`
的 `TestLowSpeedIsAKnownFailure` 同一形状，理由也同一条：**一个被跳过的限制不会被
人再想起。**

## 钉什么

1. **航向漂移**：24 格的逐步 |Δ航向| 中位全部落在 **[2, 40]°**（实测 3.2~33.5）。
2. **转身误报**：真值是 0，所以判出的转身全是误报。总数落在 **[20, 80]**（实测 38）。
3. **速度依赖**：slow 档的中位 **≥** fast 档的（实测 12.6 vs 6.8）。它消失就说明
   有人修好了机制。
4. **机制**：ZUPT 占比与自由积分窗和航向 p50 的相关 |r| **≥ 0.4**（实测 0.555 / 0.541）。

   这一条守的是**解释**而不是病本身 —— 相关塌了说明机制变了，那时 RAY-356 的
   结论要重新审视，而不是默默继续用。
5. **阳性对照**：把**合成**步态喂进同一条链，判据 1 的**下限门**必须红。

## 阳性对照为什么走下限那一头：上限门够不着

第一版对照想的是「注入更大的陀螺零偏，把中位顶出 40° 上限」。在最干净的一格
（`S1-sport/fast-b`，基线 L 8.8° / R 3.2°）上量了下面这些，**没有一条能让上限门
变红**：

| 注入 | L / R 航向中位 | 结果 |
| -- | -- | -- |
| 恒定陀螺 z 零偏，**全程** | —— | `AlignmentError`：零偏把开头的静止段也转起来，对准先崩 |
| 恒定零偏，**对准窗之后** 8 / 16 / 24 °/s | 9.1 / 16.5 / **22.8°**（R 7.9 / 14.4 / 19.6） | 全部在带内，而且**随注入成比例上涨** |
| 同上，40 / 80 °/s | —— | `SegmentationError`：`trim=1` 把直行段的步全剔光 |
| 斜坡零偏 0→40 °/s（模拟温漂） | 17.4° | 在带内 |
| 斜坡零偏 0→80 °/s | —— | `SegmentationError` |
| GLRT 阈 ×3 / ×10（观测变**密**） | 8.4 / 8.4°，ZUPT 16.4% → 17.0 / 17.1% | 几乎不动 |
| GLRT 阈 ÷5 以下（饿死观测） | —— | `AlignmentError`：初始对准先崩 |

**上限门测不了的原因在第二、三行之间**：中位随零偏成比例爬到 22.8° 还在带内，
再往上加，`separate` 已经找不出直行段（每一步都像转身，阈 25°），
`select_middle_steps` 一步不剩 —— **分段死在中位越线之前**，中间没有窗口。

**所以上限门是一道没有阳性对照的「不得更坏」门，这里明写出来，而不是假装它通了电。**

真正通电的是**下限门**，而那恰好是这个哨兵存在的理由 —— 它要在漂移被**修好**时响。
合成步态的同一量实测 **0.07~0.21°**，真机 3.2~33.5°：相差两个数量级，所以这道门
既有牙、又不会被噪声偶然触发。

### 这张表**不**支持的两个说法

写它的时候我先下过两条结论，扫得更宽之后都不成立，记在这里免得再被重新发明：

* **「ESKF 的零偏状态把恒定零偏吸收掉了。」不成立。** 我最初只扫到 16 °/s，把低端
  （1 / 2 / 4 °/s，响应还埋在噪声里）的非单调读成了吸收。扫到 24 °/s 就看得出中位
  跟着零偏成比例走。**零偏残余是不是根因，本表给不出独立证据** —— RAY-356 第六轮
  的结论仍然只由那一轮自己支撑。
* **「把观测调密复证了判据 4 的机制方向。」不成立。** 现实量级的干预（阈 ×3 / ×10）
  只把 ZUPT 从 16.4% 推到 17.1%，中位从 8.8° 动到 8.4° —— 证不了任何方向。
  阈 ×100 确实能把 ZUPT 顶到 27.2%、中位压到 7.0°，但那个量级已经不是"调一下"，
  用它当因果证据是把结论想要的方向当成了实验设计。**判据 4 的证据仍然只有那 24 格
  上的相关，而相关不是因果** —— 判据 4 守的本来也就是"这个相关还在"。

用法见 `tools/run_acceptance.py`。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from acceptance._dataset import load_walks, parse_args, report
from acceptance.chain_metrics import LEAD_S, _foot_series
from gait.analysis.segments import heading_change_per_cycle
from gait.cloud.chain import _yaw_rate, run_basic_chain
from gait.config import AlgoConfig
from gait.contracts import FootSeries, Quality
from gait.validate.synthetic import NoiseModel, WalkSpec, generate_dual_walk

#: 逐步 |Δ航向| 中位允许的带，度。实测 3.2~33.5 —— 两端都留了余量，而**两端都是门**。
#: 上限没有阳性对照（见模块头），下限有。
HEADING_BAND = (2.0, 40.0)
#: 24 格的转身误报总数允许的带。实测 38；真值是 0，所以判出的全是误报。
#: 带取实测值的约 0.5× ~ 2× —— 两头都留着门，而不是把当前值当成基线供起来。
FALSE_TURN_BAND = (20, 80)
#: 机制解释力的下限：ZUPT 观测密度与航向漂移的相关。实测 0.555 / 0.541。
MIN_MECHANISM_R = 0.4
#: 阳性对照的合成趟长，秒。取两档是为了让对照不依赖某一个特定时长 ——
#: 一条只在 20 s 上成立的"对照"证明不了带有牙。
CONTROL_DURATIONS = (20.0, 40.0)


def _measure(chain, label: str, fs: float) -> dict:
    outcome = chain.feet[label]
    navigation = outcome.navigation
    change = np.abs(
        heading_change_per_cycle(outcome.cycles, navigation.t, _yaw_rate(navigation))
    )
    marked = np.flatnonzero(navigation.zupt)
    gaps = np.diff(marked)
    free = gaps[gaps > 1] / fs
    return {
        "heading_p50": float(np.median(change)) if change.size else float("nan"),
        "turns": int(outcome.segmentation.turns),
        "cycles": len(outcome.cycles),
        "zupt_fraction": float(navigation.zupt.mean()),
        "free_run_p50": float(np.median(free)) if free.size else float("nan"),
    }


def analyse(trial_dir: Path, cfg: AlgoConfig) -> list[dict]:
    """本趟的逐格读数，**外加阳性对照**。

    对照与趟次无关，所以每趟都会重跑一遍 —— 那是故意的。`tools/run_acceptance.py`
    要的是 `analyse(trial, cfg)` 这个纯函数；用模块级缓存"只跑第一趟"会让同一次
    调用因为调用顺序不同而返回不同的东西，那是个比两次合成解算贵得多的陷阱。
    合成 20 s + 40 s 相对 12 趟真机数据是噪声量级的开销。
    """
    rows: list[dict] = []
    for walk in load_walks(trial_dir, cfg, lead_s=LEAD_S):
        series = {label: _foot_series(label, foot) for label, foot in walk.feet.items()}
        chain = run_basic_chain(series, cfg, sync_quality={"acceptance": True})
        for label in sorted(series):
            rows.append(
                {
                    "kind": "cell",
                    "trial": walk.trial,
                    "walk": walk.walk,
                    "foot": label,
                    "speed": walk.walk.split("-")[0],
                    **_measure(chain, label, series[label].fs),
                }
            )
    return rows + control(cfg)


def control(cfg: AlgoConfig) -> list[dict]:
    """阳性对照：合成步态走同一条链，航向近乎无漂。

    走**产品自己的入口**（`run_basic_chain`），而不是另写一份"等价"的积分 ——
    RAY-356 的溯因里，自写探针量出的 6875° 是探针自己的 bug。对照要证明的是
    "这道门在这条链上通电"，那就必须是同一条链。
    """
    rows: list[dict] = []
    for duration in CONTROL_DURATIONS:
        pair = generate_dual_walk(
            WalkSpec(duration_s=duration), noise=NoiseModel.bs_bt91()
        )
        feet = {}
        for label, (synth, _truth) in pair.items():
            count = len(synth.t)
            feet[label] = FootSeries(
                label=label,
                t=synth.t,
                acc=synth.acc,
                gyr=synth.gyr,
                quality=np.full(count, Quality.NONE, dtype=np.uint8),
                segments=[(0, count)],
                fs=synth.fs,
            )
        chain = run_basic_chain(feet, cfg, sync_quality={"control": True})
        for label in sorted(feet):
            rows.append(
                {
                    "kind": "control",
                    "duration_s": duration,
                    "foot": label,
                    **_measure(chain, label, feet[label].fs),
                }
            )
    return rows


def _correlation(rows: list[dict], key: str) -> float:
    x = np.array([row[key] for row in rows], dtype=np.float64)
    y = np.array([row["heading_p50"] for row in rows], dtype=np.float64)
    usable = np.isfinite(x) & np.isfinite(y)
    if usable.sum() < 3:
        return float("nan")
    return float(np.corrcoef(x[usable], y[usable])[0, 1])


def judge(everything: list[dict]) -> list[str]:
    rows = [row for row in everything if row["kind"] == "cell"]
    controls = [row for row in everything if row["kind"] == "control"]
    failures: list[str] = []
    low, high = HEADING_BAND

    # ① 航向漂移落在已知的带里 —— 两端都是门。
    for row in rows:
        cell = f"{row['trial']}/{row['walk']}/{row['foot']}"
        value = row["heading_p50"]
        if not np.isfinite(value):
            failures.append(f"性质 1：{cell} 没有算出航向变化")
        elif value > high:
            failures.append(
                f"性质 1：{cell} 逐步 |Δ航向| 中位 {value:.1f}° > {high}° —— 变坏了"
            )
        elif value < low:
            failures.append(
                f"性质 1：{cell} 逐步 |Δ航向| 中位 {value:.1f}° < {low}° —— **变好了**，"
                f"该来把这条判据收紧，而不是让一个已知缺陷静静地消失"
            )

    # ② 转身误报：真值 0，判出的全是误报。
    total = sum(row["turns"] for row in rows)
    lo, hi = FALSE_TURN_BAND
    if total > hi:
        failures.append(f"性质 2：转身误报共 {total} 次 > {hi} —— 变坏了（真值是 0）")
    elif total < lo:
        failures.append(
            f"性质 2：转身误报共 {total} 次 < {lo} —— **变好了**，该来更新这条判据"
        )

    # ③ 速度依赖：慢档不该比快档好。
    def band(name: str) -> float:
        values = [r["heading_p50"] for r in rows if r["speed"] == name]
        return float(np.median(values)) if values else float("nan")

    slow, fast = band("slow"), band("fast")
    if np.isfinite(slow) and np.isfinite(fast) and slow < fast:
        failures.append(
            f"性质 3：慢档中位 {slow:.1f}° < 快档 {fast:.1f}° —— 速度依赖翻转或消失了，"
            f"RAY-356 的机制结论要重新审视"
        )

    # ④ 机制：观测密度确实解释着漂移。
    for key, name in (("zupt_fraction", "ZUPT 占比"), ("free_run_p50", "自由积分窗")):
        r = _correlation(rows, key)
        if not np.isfinite(r) or abs(r) < MIN_MECHANISM_R:
            failures.append(
                f"性质 4：{name}与航向 p50 的相关 r = {r:.3f}，|r| < {MIN_MECHANISM_R}"
                f" —— 机制变了，本 Issue 的解释不再成立"
            )

    # ⑤ 阳性对照：合成步态必须把下限门顶红。它不红，说明这道带没通电。
    if not controls:
        failures.append("性质 5：阳性对照没跑出任何一格")
    for row in controls:
        cell = f"合成 {row['duration_s']:.0f}s/{row['foot']}"
        value = row["heading_p50"]
        if not np.isfinite(value) or value >= low:
            failures.append(
                f"性质 5：{cell} 的中位 {value:.2f}° 没有低于下限 {low}° —— "
                f"合成数据航向近乎无漂，它都触不红这道门，那门就没有通电"
            )
    return failures


def main() -> int:
    args = parse_args(__doc__ or "")
    cfg = AlgoConfig()
    everything = [row for trial in args.trials for row in analyse(trial, cfg)]
    rows = [row for row in everything if row["kind"] == "cell"]
    if not rows:
        return report(everything, ["没有可用的趟次"], "航向漂移哨兵", args.out)
    # 对照每趟重跑一次（见 `analyse`），逐格读数一样，这里只展示一份。
    controls = [row for row in everything if row["kind"] == "control"]
    controls = list({(r["duration_s"], r["foot"]): r for r in controls}.values())

    print(f"{'格':26s}{'航向p50':>9s}{'转身':>5s}{'ZUPT%':>8s}{'自由积分':>10s}")
    for row in rows:
        print(
            f"{row['trial'] + '/' + row['walk'] + '/' + row['foot']:26s}"
            f"{row['heading_p50']:>8.1f}°{row['turns']:>5d}"
            f"{100 * row['zupt_fraction']:>7.1f}%{row['free_run_p50']:>9.3f}s"
        )

    heads = [row["heading_p50"] for row in rows]
    print(
        f"\n{len(rows)} 格：逐步 |Δ航向| 中位 {min(heads):.1f}~{max(heads):.1f}°"
        f"（带 {HEADING_BAND[0]:.0f}~{HEADING_BAND[1]:.0f}，**两端都是门**）"
        f"\n转身误报共 {sum(r['turns'] for r in rows)} 次（带 {FALSE_TURN_BAND}，真值 0）"
    )
    for name in ("slow", "mid", "fast"):
        band = [r["heading_p50"] for r in rows if r["speed"] == name]
        if band:
            print(f"  {name:5s} 中位 {np.median(band):5.1f}°")
    for key, name in (("zupt_fraction", "ZUPT 占比"), ("free_run_p50", "自由积分窗")):
        print(f"  {name}与航向 p50 的相关 r = {_correlation(rows, key):+.3f}")

    print(f"\n阳性对照（合成步态走同一条链，必须低于下限 {HEADING_BAND[0]:.0f}°）：")
    for row in controls:
        print(
            f"  合成 {row['duration_s']:>4.0f}s/{row['foot']}  中位 "
            f"{row['heading_p50']:5.2f}°  转身 {row['turns']}  周期 {row['cycles']}"
        )

    return report(
        everything,
        judge(everything),
        "航向漂移哨兵",
        args.out,
        extra={
            "heading_band": list(HEADING_BAND),
            "false_turn_band": list(FALSE_TURN_BAND),
            "control_durations": list(CONTROL_DURATIONS),
        },
    )


if __name__ == "__main__":
    raise SystemExit(main())
