"""航向漂移的**双向哨兵** —— RAY-356 判据 1~5，上限门的对照来自 RAY-362。

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
5. **两道门各自有阳性对照**：合成步态触红**下限门**；往真机注入斜坡零偏触红
   **上限门**，并配一个必须**留在带内**的阴性对照。

## 两道门各自的阳性对照

**下限门**：合成步态走同一条链，航向近乎无漂（实测 0.07~0.21°），必须触红下限
2°。真机同一量是 3.2~33.5° —— 相差两个数量级，所以这道门既有牙、又不会被噪声
偶然触发。

**上限门**：往真机 `S1-sport/slow-b` 注入斜坡陀螺零偏，必须把左脚中位顶过 40°。
配一个**阴性对照**：同一格换小峰值，必须**留在带内**。

### RAY-356 曾写"上限门够不着、没有阳性对照" —— 那是错的（RAY-362）

那个结论来自两条**一维**扫描：一条固定 onset=4.0 扫峰值，一条固定 onset=3.0 扫
峰值。两条线各自都撞上了 `SegmentationError`，于是各自得出"分段死在中位越线之前，
中间没有窗口"。

**补上 peak × onset 的二维网格之后，上限门够得着**，而且慢档存在一片连续的稳健区：

    S1-sport/slow-b，左脚中位，行 peak °/s，列 onset s
           0.5   1.0   1.5   2.0   2.5   3.0   3.5   4.0   4.5
      20    23    23    24    25    25    25    25    26    26   <- 阴性对照取这一行
      40    48    48    48    48    48    47    47    47    52
      50    67    65    61    61    63    57    60    58    58   <- 阳性对照取这一行
      60    71    75    74    72    63    63    63    64    64
      80    84   Seg    90    88    78    77    80    81    83

同一张网格在快档 `fast-b` 上是**红 9/54、崩 39/54** —— 红格散落在崩溃海里。
**散的是快档，不是这条路。** 机制与判据 4 一致：慢档 ZUPT 稀（2.8~4.6% vs 快档
15~16%），注入的零偏没有观测去压，中等峰值就能顶过门而周期结构还完好；快档要顶到
40° 得把峰值推到 80+，那时每一步都像转身，`separate` 找不出直行段，分段先死。

### 为什么判左脚、为什么峰值和起点都写死

**判左脚。** 右脚在 onset **2.9** 有一道 **0.1 s 宽的悬崖**：2.8 给 64.8°，
2.9 掉到 38.7° —— 落回带内。而左脚穿过它只从 57.2° 走到 57.3°。既然已知同类结构
只有 0.1 s 宽，而我们大部分区域只按 0.5 步长采过，"两足都红"在这份数据上**无法
被证明稳健**；左脚可以。

**起点写死 1.5 而不是给一个区间**，否则下一个人会取到 2.85。1.2~1.8 已按 0.1
步长验过（L 55.3~61.2，R 60.8~64.3，七格两足全红）。

**注入的构造逐行写死，包括 `t` 怎么来**：两个会话曾用两种等价写法 —— 起点取整到
样本 vs 起点是浮点 —— 斜坡起点相差**不到一个样本**，结果左脚差 0.0047°，
**右脚差 3.2474°**。悬崖之后的右脚会把规格里任何没写明的细节放大成读数差。
（别把它写成一个"放大倍数"：那个比值的分母接近浮点噪声，本身是病态的。）

### 这里的"余量"是上界，不是区域性质

左脚的观测最薄余量：0.5 步长 9 个点上是 +17.0°，加密到 0.1 步长再取 7 个点就变成
**+15.3°**（L 在 onset 1.3 下凹到 55.3°）。**两次加密两次下降，而且这是单调性质
不是巧合** —— 观测最小值只能随采样变密而降，永远不会升。所以下面那个 15.3° 是
**16 个采样点上的观测最小值**，不是"这一区的余量"。再加密它大概率还会降。

### 三种结局要分开报

`run_acceptance.py` 把"崩溃"与"不达标"分开报（RAY-343）。这里还有第三种：

* **正常**：链子跑完，读数在期望的一侧；
* **崩溃**：抛 `SegmentationError` / `AlignmentError` —— 至少会被报成"崩了"；
* **静默失效**：链子跑完，但注入没能把中位顶过门（如 onset 2.9 的右脚）。

第三种最难查，因为它读起来就像"这道门失效了"。**它不是门坏了，是注入在这一格
没起作用** —— 混报会让下一个人去查一个没坏的门。

### 这个对照会随修法失效，那是好消息

它的前提是"慢档 ZUPT 稀"。三个候选修法里按 `‖ω‖` 筛支撑相区间那条**正是要把慢档
ZUPT 变密**。它一旦生效，peak 50 可能不再触红。**那是修法生效的信号，不是回归** ——
届时该把峰值抬上去，而不是去查哨兵。

用法见 `tools/run_acceptance.py`。
"""

from __future__ import annotations

from dataclasses import replace
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
#: 下限门阳性对照的合成趟长，秒。取两档是为了让对照不依赖某一个特定时长 ——
#: 一条只在 20 s 上成立的"对照"证明不了带有牙。
CONTROL_DURATIONS = (20.0, 40.0)

#: 上限门对照钉死的那一格。慢档 ZUPT 稀，注入的零偏没有观测去压 —— 见模块头
#: 「为什么判左脚」。换格要重跑整张 peak × onset 网格，不能只验几个点。
UPPER_CELL = ("S1-sport", "slow-b")
#: 判上限门的那只脚。右脚在 onset 2.9 有一道 0.1 s 宽的悬崖，左脚穿过它只动 0.1°。
UPPER_FOOT = "L"
#: 斜坡注入的起点，秒。**写死一个值，不写区间** —— 写区间下一个人会取到 2.85，
#: 而 2.9 就是悬崖。1.2~1.8 已按 0.1 步长验过。
UPPER_ONSET_S = 1.5
#: 阳性对照的峰值，°/s。实测左脚 61.2°，离门 +21.2°；离最近的崩溃行（80）也隔着
#: 一整行。**两边都有余量**是选它而不是选 80 的理由。
UPPER_PEAK_DPS = 50.0
#: 阴性对照的峰值，°/s。实测两足 23~26 / 17~18°，全程留在带内。
#: 一个只会变红的"对照"什么也不证明 —— 这一条守的是注入装置本身没坏。
NULL_PEAK_DPS = 20.0


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
        if (walk.trial, walk.walk) == UPPER_CELL:
            rows.extend(gate_controls(walk, cfg))
    return rows + control(cfg)


def _inject_ramp(walk, peak_dps: float) -> dict[str, FootSeries]:
    """把斜坡陀螺 z 零偏注入这一趟的两只脚。

    **每一行都是判据的一部分**，不是实现细节。两个会话曾各写一种等价写法 ——
    起点取整到样本 vs 起点用浮点算比例 —— 斜坡起点因此相差不到一个样本，
    左脚读数差 0.0047°，**右脚差 3.2474°**。所以这里逐项钉死：

    * 时间轴**逐脚**由 `_foot_series` 给出（`np.arange(n) / foot.fs`），不是共享
      时间轴、也不是标称 200 Hz —— 两足的样本数与实测 fs 都不同；
    * 起点 `int(UPPER_ONSET_S * fs)`，**取整到样本**；
    * 斜坡线性升到**序列最后一个样本**才到峰值；
    * 序列本身由 `load_walks(..., lead_s=LEAD_S)` 切出，`LEAD_S` 不能改。
    """
    series: dict[str, FootSeries] = {}
    for label, foot in walk.feet.items():
        base = _foot_series(label, foot)
        gyr = np.array(base.gyr, dtype=np.float64, copy=True)
        onset = int(UPPER_ONSET_S * base.fs)
        gyr[onset:, 2] += np.linspace(0.0, np.radians(peak_dps), len(gyr) - onset)
        series[label] = replace(base, gyr=gyr)
    return series


def gate_controls(walk, cfg: AlgoConfig) -> list[dict]:
    """上限门的阳性对照 + 阴性对照，都走产品自己的 `run_basic_chain`。

    崩溃不吞掉：抛出来的异常类型原样记进行里，`judge` 会把它与"没顶过门"分开报。
    **那两件事不一样** —— 崩溃是"这次没测出来"，没顶过门是"注入在这一格失效"，
    而两者都不是"这道门坏了"。
    """
    rows: list[dict] = []
    for kind, peak in (("upper", UPPER_PEAK_DPS), ("null", NULL_PEAK_DPS)):
        series = _inject_ramp(walk, peak)
        row = {
            "kind": f"{kind}_control",
            "trial": walk.trial,
            "walk": walk.walk,
            "peak_dps": peak,
            "onset_s": UPPER_ONSET_S,
        }
        try:
            chain = run_basic_chain(series, cfg, sync_quality={kind: True})
        except Exception as error:  # noqa: BLE001 —— 类型本身就是要报的读数
            rows.append({**row, "outcome": "crashed", "error": type(error).__name__})
            continue
        for label in sorted(series):
            rows.append(
                {
                    **row,
                    "foot": label,
                    "outcome": "ran",
                    **_measure(chain, label, series[label].fs),
                }
            )
    return rows


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

    # ⑥ 上限门的阳性对照。三种结局分开报 —— 混报会让人去查一个没坏的门。
    cell_name = "/".join(UPPER_CELL)
    upper = [row for row in everything if row["kind"] == "upper_control"]
    if not upper:
        failures.append(
            f"性质 6：上限门的阳性对照**没跑** —— 它钉在 {cell_name} 上，"
            f"而这次的趟次里没有这一格。这不是「门没通电」，是这次没测"
        )
    for row in upper:
        if row["outcome"] == "crashed":
            failures.append(
                f"性质 6：上限门对照在 {cell_name} 上抛了 {row['error']} —— "
                f"**崩了，不是不达标**：这一格没测出来，而不是这里测不出来"
            )
            continue
        if row["foot"] != UPPER_FOOT:
            continue
        value = row["heading_p50"]
        if not np.isfinite(value) or value <= high:
            failures.append(
                f"性质 6：{cell_name}/{UPPER_FOOT} 注入 {row['peak_dps']:.0f}°/s "
                f"（起点 {row['onset_s']}s）之后中位 {value:.1f}°，没有越过上限 {high}° —— "
                f"**这是注入在这一格失效，不是上限门坏了**。先核注入构造有没有被改动"
                f"（见 `_inject_ramp` 的文档），再考虑是不是慢档 ZUPT 变密了 —— "
                f"后者说明有人修好了漂移，那时该把峰值抬上去"
            )

    # ⑦ 阴性对照。只会变红的"对照"什么也不证明。
    null_rows = [row for row in everything if row["kind"] == "null_control"]
    if upper and not null_rows:
        failures.append(f"性质 7：阴性对照没跑 —— {cell_name} 的阳性对照跑了而它没跑")
    for row in null_rows:
        if row["outcome"] == "crashed":
            failures.append(
                f"性质 7：阴性对照在 {cell_name} 上抛了 {row['error']} —— 崩了，不是不达标"
            )
            continue
        value = row["heading_p50"]
        if not np.isfinite(value) or not low < value <= high:
            failures.append(
                f"性质 7：{cell_name}/{row['foot']} 注入 {row['peak_dps']:.0f}°/s 之后"
                f"中位 {value:.1f}°，没有留在带 [{low}, {high}] 内 —— "
                f"**注入装置本身有问题**：一个把什么都染红的注入，证不了上限门通电"
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

    print(f"\n下限门阳性对照（合成步态走同一条链，必须低于下限 {HEADING_BAND[0]:.0f}°）：")
    for row in controls:
        print(
            f"  合成 {row['duration_s']:>4.0f}s/{row['foot']}  中位 "
            f"{row['heading_p50']:5.2f}°  转身 {row['turns']}  周期 {row['cycles']}"
        )

    cell_name = "/".join(UPPER_CELL)
    print(
        f"\n上限门对照（{cell_name}，斜坡零偏起点 {UPPER_ONSET_S}s）："
        f"阳性判 {UPPER_FOOT} 脚 > {HEADING_BAND[1]:.0f}°，阴性须留在带内"
    )
    gate = [row for row in everything if row["kind"].endswith("_control")]
    if not gate:
        print(f"  —— **没跑**：这次的趟次里没有 {cell_name}")
    for row in gate:
        name = "阳性" if row["kind"] == "upper_control" else "阴性"
        if row["outcome"] == "crashed":
            print(f"  {name} {row['peak_dps']:>3.0f}°/s      **崩溃** {row['error']}")
            continue
        print(
            f"  {name} {row['peak_dps']:>3.0f}°/s /{row['foot']}  中位 "
            f"{row['heading_p50']:6.1f}°  转身 {row['turns']:>2d}  周期 {row['cycles']}"
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
            "upper_cell": list(UPPER_CELL),
            "upper_foot": UPPER_FOOT,
            "upper_onset_s": UPPER_ONSET_S,
            "upper_peak_dps": UPPER_PEAK_DPS,
            "null_peak_dps": NULL_PEAK_DPS,
        },
    )


if __name__ == "__main__":
    raise SystemExit(main())
