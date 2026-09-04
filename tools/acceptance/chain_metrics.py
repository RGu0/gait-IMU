"""产品链路印在报告上的两个指标 —— RAY-351 判据 1、2、3。

## 它守的是**链路**，不是事件层

`stance_intervals.py`（RAY-346）量的是 `analysis/events` 两条路径的差别。本脚本量的是
**产品链路真正印进报告的那两个数**：`report/assemble.py` 第 213 行的支撑相占比与
第 217 行的双支撑期占比，都取自 `cloud/chain.py::run_basic_chain` 的产出。

两者曾经不一致：事件层早就有了 `detect_stance_intervals`（RAY-325），而链路
**没有切过去** —— `segment_cycles` 少传一个 `stance_edges=`。真机上那两个指标因此是
废数（支撑相占比 1~16%、双支撑期占比 −0.9），而**合成数据上旧路径是对的**
（+0.260 / 60%），所以现有测试一条都抓不到。

## 钉什么

1. **双支撑期占比：记录 + 反向守卫，不设达标门**（R3）。

   实测 **−6.248 ~ +0.062**，8/12 趟为负 —— 但**根因不在支撑相路径**：分段把单向直行
   判成 6 次转身，中段步从 33 剔到 6，左右两只脚的集合不再对应，配对随即失去意义
   （一个相位读到 −27.5 秒）。分段判对的格 DS 是 +0.027~+0.062。归 RAY-354。

   所以这里**只记录**逐格读数，外加一条**反向守卫**：≥ 5/12 趟的 DS ≥ −0.10。
   它挡的是「有人把切换退回去」—— 旧路径的 DS 全部落在 −0.945~−0.590，**一格都够
   不着 −0.10**；切换后实测有 7 趟够得着。门定在 5，留余量。

   达标门度量的是分段而不是本 Issue 改的东西，继续拿着它只会让一个不归本 scope 管
   的缺陷阻住一个已经证实的改进。
2. **支撑相占比**：12 趟 × 2 足全部落在 [35%, 60%]。

   **不设 60~75% 的生理门** —— 差距成因已定位（向外推的判据在两端不对称）但量未知，
   真机没有 IC/TO 真值可校。设一个够不着的门只会让脚本永远红。
3. **阳性对照**：同一次导航结果走**旧路径**（`segment_cycles` 不传 `stance_edges`），
   两条门都必须拦下它。
4. **完整链也要跑得完**（RAY-357）。本脚本对每趟同时跑 `run_basic_chain` 与
   `run_full_chain` —— 后者曾因 `FilterHistory` 不覆盖碎段而在真机上崩在 `rts.smooth`，
   而当时的判据只说了基础链，套件、单测、CI 全绿。完整链的读数**只记录不设门**
   （它多了 RTS + 锚定 + 双足约束三件事，达标门归各自的 Issue）。

   对照用的不是编出来的缺陷，**就是切换之前的产品链路本身** —— 一条真实存在过的
   更差路径。

判据 3（合成不许变坏）由 `tests/test_cloud_chain.py` 的合成回归守，不在这里 ——
本脚本读云端采集，合成的事不该等它。

用法见 `tools/run_acceptance.py`。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from acceptance._dataset import load_walks, parse_args, report
from gait.analysis import events
from gait.cloud.chain import run_basic_chain, run_full_chain
from gait.config import AlgoConfig
from gait.contracts import FootSeries, Quality

#: 「像样的双支撑期读数」的界。**它不是达标门**（见模块文档判据 1）—— 只用来数
#: 有几趟够得着，作为「切换没有被退回去」的反向证据。
DS_FLOOR = -0.10
#: 反向守卫：至少要有这么多趟够得着 `DS_FLOOR`。旧路径一趟都够不着（全部
#: −0.945~−0.590），切换后实测 7 趟够得着，门定在 5 留余量。
DS_REACHABLE_MIN = 5
#: 支撑相占周期的比例，%。文献 60~75，真机实测偏窄 —— 门按实测放，见模块文档。
STANCE_PCT_RANGE = (35.0, 60.0)

SYNC_QUALITY = {"acceptance": True, "determinate": True, "flagged": False}

#: 每趟起点往前延的秒数。产品链路要求序列开头有静止段（RAY-202 的初始对准），而逐趟
#: 切片是从走起来那一刻切的。实测前延 1 s 六趟全部找得到，取 2 s 留余量。
#:
#: **这不是为了让脚本通过而放宽什么** —— 真实会话本来就是"静立后开始"（PRD §7），
#: 有静止前导才是产品看到的样子；没有它反而是本数据集切片方式的产物。前延进来的
#: 静止段进不了指标：分段筛选只留直行段的中段步。
LEAD_S = 2.0


def _foot_series(label: str, foot) -> FootSeries:
    """把验收数据集的一只脚装成链路要的 `FootSeries`。

    `segments` 取完整性报告切出来的段 —— 与 `run_ins` 逐段滤波的口径一致，也是
    RAY-351 按段组装支撑相区间的前提。
    """
    n = len(foot.accel)
    bounds = list(foot.integrity.segments) if foot.integrity is not None else [(0, n)]
    return FootSeries(
        label=label,  # type: ignore[arg-type]
        t=np.arange(n) / foot.fs,
        acc=foot.accel,
        gyr=foot.gyro,
        quality=np.full(n, Quality.NONE, dtype=np.uint8),
        segments=bounds,
        fs=foot.fs,
    )


def _old_path(outcome, series: FootSeries, cfg: AlgoConfig) -> dict:
    """同一次导航结果走**切换之前**的那条路：`segment_cycles` 不传 `stance_edges`。"""
    navigation = outcome.navigation
    cycles, _ = events.segment_cycles(
        outcome.label, navigation.t, series.acc, series.gyr, navigation.stances,
        position=navigation.p, cfg=cfg,
    )
    summary = events.summarize(outcome.label, cycles) if cycles else None
    return {"cycles": cycles, "stance_pct": summary.stance_ratio if summary else None}


def analyse(trial_dir: Path, cfg: AlgoConfig) -> list[dict]:
    rows: list[dict] = []
    for walk in load_walks(trial_dir, cfg, lead_s=LEAD_S):
        series = {label: _foot_series(label, foot) for label, foot in walk.feet.items()}
        chain = run_basic_chain(series, cfg, sync_quality=SYNC_QUALITY)
        # **完整链也要跑。** RAY-352 的跳过让 `FilterHistory` 不再覆盖碎段，
        # `rts.smooth` 的覆盖检查因此把一个 8 采样的碎段误判成"来自不同调用"，
        # 整条完整链在真机上崩掉（RAY-357）——而当时的判据只说了基础链，套件、
        # 单测、CI 全绿。这一行就是把那半边补上：它抛，`analyse` 就抛，runner
        # 会如实报"崩溃"而不是"0 条不达标"。
        full = run_full_chain(series, cfg, sync_quality=SYNC_QUALITY)

        old = {label: _old_path(chain.feet[label], series[label], cfg) for label in series}
        old_ds = None
        if old["L"]["cycles"] and old["R"]["cycles"]:
            old_ds = float(
                events.double_support(
                    old["L"]["cycles"], old["R"]["cycles"], sync_quality=SYNC_QUALITY
                ).fraction
            )

        rows.append(
            {
                "trial": walk.trial,
                "walk": walk.walk,
                "ds_fraction": (
                    round(float(chain.double_support.fraction), 4)
                    if chain.double_support
                    else None
                ),
                # RAY-354 判据 2：占比被单个离群相位支配（实测
                # `最小相位 vs 占比 r = +0.931`），所以稳健量也要逐格记下来。
                "ds_median": (
                    round(float(chain.double_support.median), 4)
                    if chain.double_support
                    else None
                ),
                "ds_count": chain.double_support.count if chain.double_support else None,
                "ds_excluded": (
                    chain.double_support.excluded if chain.double_support else None
                ),
                "ds_coverage": (
                    round(float(chain.double_support.coverage), 3)
                    if chain.double_support
                    else None
                ),
                # RAY-354 判据 3：中段步保留率 = 中段步 ÷ 周期。
                # 上限由 `trim` 定死（trim=1 时 (n−2)/n），下限就是分段损害。
                "retention": {
                    label: (
                        round(len(outcome.selected) / len(outcome.cycles), 3)
                        if outcome.cycles
                        else None
                    )
                    for label, outcome in sorted(chain.feet.items())
                },
                "stance_pct": {
                    label: (
                        round(float(outcome.spatiotemporal.stance_ratio), 1)
                        if outcome.spatiotemporal
                        else None
                    )
                    for label, outcome in sorted(chain.feet.items())
                },
                "stride_length": {
                    label: (
                        round(float(outcome.spatiotemporal.stride_length), 3)
                        if outcome.spatiotemporal
                        else None
                    )
                    for label, outcome in sorted(chain.feet.items())
                },
                "gait_speed": {
                    label: (
                        round(float(outcome.spatiotemporal.gait_speed), 3)
                        if outcome.spatiotemporal
                        else None
                    )
                    for label, outcome in sorted(chain.feet.items())
                },
                "cycles": {
                    label: len(outcome.cycles) for label, outcome in sorted(chain.feet.items())
                },
                # 阳性对照：切换之前的那条路。
                "control_ds": None if old_ds is None else round(old_ds, 4),
                "control_stance_pct": {
                    label: (
                        round(float(old[label]["stance_pct"]), 1)
                        if old[label]["stance_pct"] is not None
                        else None
                    )
                    for label in sorted(old)
                },
                "control_cycles": {label: len(old[label]["cycles"]) for label in sorted(old)},
                # 完整链（前向 + RTS + 锚定 + 双足约束）的同一批读数，只记录不设门。
                "full_ds": (
                    round(float(full.double_support.fraction), 4)
                    if full.double_support
                    else None
                ),
                "full_stance_pct": {
                    label: (
                        round(float(o.spatiotemporal.stance_ratio), 1)
                        if o.spatiotemporal
                        else None
                    )
                    for label, o in sorted(full.feet.items())
                },
                "full_dualfoot_applied": bool(full.diagnostics.get("dualfoot_applied")),
            }
        )
    return rows


#: 达到中段步保留率理论上限（`(n−2)/n`，`trim=1`）的**足数**允许的带，共 24 只脚。
#: 实测 **22/24** —— 只有 `S1-sport/slow-a` 两只脚在下面（0.515 / 0.548），那是已知的
#: 前向解发散格。**双向**：涨到 24 说明有人把那格修好了，该来收紧这条。
AT_CAP_FEET_BAND = (18, 23)

#: 被判据 7 剔掉的跨步配对总数允许的带。实测 **2**，两个都在 `S1-sport/slow-a`。
#: **双向**：掉到 0 说明配对伪影没了（好事，该更新判据）；涨上去说明步序在退化。
EXCLUDED_PHASES_BAND = (1, 6)


def judge(rows: list[dict]) -> list[str]:
    failures: list[str] = []
    reachable = 0
    control_caught = 0

    for row in rows:
        cell = f"{row['trial']}/{row['walk']}"
        ds = row["ds_fraction"]
        if ds is None:
            failures.append(f"性质 1：{cell} 链路没有算出双支撑期")
        elif ds >= DS_FLOOR:
            reachable += 1

        # RAY-354 判据 1：配对达成率。低于门说明两足步序配不上，那时 DS 不可计算。
        coverage = row["ds_coverage"]
        if coverage is not None and coverage < events.MIN_PAIRING_COVERAGE:
            failures.append(
                f"性质 5：{cell} 的配对达成率 {coverage:.2f} < "
                f"{events.MIN_PAIRING_COVERAGE} —— 两足步序配不上，"
                f"链路本该把 DS 标为不可计算却给出了读数"
            )

        for label, value in row["stance_pct"].items():
            low, high = STANCE_PCT_RANGE
            if value is None:
                failures.append(f"性质 2：{cell}/{label} 没有时空参数")
            elif not low <= value <= high:
                failures.append(
                    f"性质 2：{cell}/{label} 支撑相占比 {value:.0f}% 不在 "
                    f"[{low:.0f}, {high:.0f}]% 内"
                )

        # ③ 阳性对照：旧路径必须被**支撑相占比**那道门拦下。
        #
        # 不拿 DS 当对照 —— 它已经不是达标门了（R3）。支撑相占比反而是更硬的对照：
        # 旧路径 1~16%，门是 [35%, 60%]，12 趟无一幸免。
        control_ds = row["control_ds"]
        control_bad = any(
            value is not None and not (STANCE_PCT_RANGE[0] <= value <= STANCE_PCT_RANGE[1])
            for value in row["control_stance_pct"].values()
        )
        if control_bad:
            control_caught += 1
        else:
            failures.append(
                f"性质 3：{cell} 的阳性对照没被抓出 —— 旧路径支撑相占比 "
                f"{row['control_stance_pct']} 落在门内（DS 当时是 {control_ds}），"
                f"这道门没有通电"
            )

    if rows and reachable < DS_REACHABLE_MIN:
        failures.append(
            f"性质 1（反向守卫）：只有 {reachable}/{len(rows)} 趟的 DS ≥ {DS_FLOOR}，"
            f"少于 {DS_REACHABLE_MIN}。旧的边缘细化路径一趟都够不着（全部 −0.945~−0.590）"
            f"—— 这个数掉下来，多半是支撑相区间那条路被退回去了"
        )
    if rows and control_caught == 0:
        failures.append("性质 3：一趟对照都没被抓出 —— 这道门在这份数据上无从证明有牙")

    # ── RAY-354 判据 3：中段步保留率的双向哨兵 ────────────────────────────────
    # 上限由 `trim` 定死（`(n−2)/n`），所以"有多少只脚顶到上限"才是能双向走的量。
    at_cap = sum(
        1
        for row in rows
        for label, value in row["retention"].items()
        if value is not None
        and row["cycles"].get(label)
        and abs(value - (row["cycles"][label] - 2) / row["cycles"][label]) < 1e-3
    )
    low, high = AT_CAP_FEET_BAND
    if rows and at_cap > high:
        failures.append(
            f"性质 6：{at_cap} 只脚的中段步保留率顶到理论上限 > {high} —— **变好了**，"
            f"分段损害比记录时更小，该来收紧这条判据"
        )
    elif rows and at_cap < low:
        failures.append(
            f"性质 6：只有 {at_cap} 只脚顶到保留率上限 < {low} —— 分段又在吃步了"
        )

    # ── RAY-354 判据 7：被剔掉的跨步配对，双向 ────────────────────────────────
    excluded = sum(row["ds_excluded"] or 0 for row in rows)
    low, high = EXCLUDED_PHASES_BAND
    if rows and excluded > high:
        failures.append(
            f"性质 7：剔掉了 {excluded} 个跨步配对 > {high} —— 两足步序在退化"
        )
    elif rows and excluded < low:
        failures.append(
            f"性质 7：一个跨步配对都没剔掉（{excluded} < {low}）—— **变好了**，"
            f"配对伪影消失了，该来更新这条判据；也可能是剔除逻辑被摘掉了"
        )
    return failures


def _span(values) -> str:
    clean = [v for v in values if v is not None]
    return f"{min(clean):.3f}~{max(clean):.3f}" if clean else "—"


def main() -> int:
    args = parse_args(__doc__ or "")
    cfg = AlgoConfig()
    rows = [row for trial in args.trials for row in analyse(trial, cfg)]
    if not rows:
        return report(rows, ["没有可用的趟次"], "产品链路的两个显示指标", args.out)

    print(f"{'趟':22s}{'DS链路':>9s}{'DS旧(对照)':>13s}{'支撑%链路':>12s}{'支撑%旧':>10s}{'周期数':>9s}")
    for row in rows:
        pct = "/".join(f"{v:.0f}" if v is not None else "—" for v in row["stance_pct"].values())
        old_pct = "/".join(
            f"{v:.0f}" if v is not None else "—" for v in row["control_stance_pct"].values()
        )
        cyc = "/".join(str(v) for v in row["cycles"].values())
        ds = row["ds_fraction"]
        old_ds = row["control_ds"]
        print(
            f"{row['trial'] + '/' + row['walk']:22s}"
            f"{('—' if ds is None else f'{ds:+.3f}'):>9s}"
            f"{('—' if old_ds is None else f'{old_ds:+.3f}'):>13s}"
            f"{pct:>12s}{old_pct:>10s}{cyc:>9s}"
        )

    stance = [v for row in rows for v in row["stance_pct"].values()]
    old_stance = [v for row in rows for v in row["control_stance_pct"].values()]
    print(
        f"\n链路：DS {_span(r['ds_fraction'] for r in rows)} | 支撑相占比 "
        f"{min(v for v in stance if v is not None):.0f}~{max(v for v in stance if v is not None):.0f}%"
        f"\n对照（旧路径）：DS {_span(r['control_ds'] for r in rows)} | 支撑相占比 "
        f"{min(v for v in old_stance if v is not None):.0f}~"
        f"{max(v for v in old_stance if v is not None):.0f}%"
    )
    # 判据 4 要求记录另外两个下游读数的变化 —— 不设门，只报出来。
    for name in ("stride_length", "gait_speed"):
        values = [v for row in rows for v in row[name].values() if v is not None]
        if values:
            print(f"{name}: {min(values):.3f}~{max(values):.3f}（判据 4：只记录，不设门）")

    return report(
        rows,
        judge(rows),
        "产品链路的两个显示指标",
        args.out,
        extra={"ds_floor": DS_FLOOR, "stance_pct_range": list(STANCE_PCT_RANGE)},
    )


if __name__ == "__main__":
    raise SystemExit(main())
