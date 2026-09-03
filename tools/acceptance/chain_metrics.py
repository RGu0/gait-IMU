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

1. **双支撑期占比**：12 趟全部 ≥ −0.10，其中 ≥ 10/12 非负。

   为什么不是"一趟不许为负"：`S1-sport` 快档两趟（厚软中底把撞击摊长）实测约 −0.068，
   **2026-09-02 用户已裁决接受**。
2. **支撑相占比**：12 趟 × 2 足全部落在 [35%, 60%]。

   **不设 60~75% 的生理门** —— 差距成因已定位（向外推的判据在两端不对称）但量未知，
   真机没有 IC/TO 真值可校。设一个够不着的门只会让脚本永远红。
3. **阳性对照**：同一次导航结果走**旧路径**（`segment_cycles` 不传 `stance_edges`），
   两条门都必须拦下它。

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
from gait.cloud.chain import run_basic_chain
from gait.config import AlgoConfig
from gait.contracts import FootSeries, Quality

#: 双支撑期占比的下限。实测最负约 −0.068（用户已裁决接受），留约 30% 余量。
DS_FLOOR = -0.10
#: 允许为负的趟数上限（12 趟中）。
DS_NEGATIVE_MAX = 2
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
            }
        )
    return rows


def judge(rows: list[dict]) -> list[str]:
    failures: list[str] = []
    negative = 0
    control_caught = 0

    for row in rows:
        cell = f"{row['trial']}/{row['walk']}"
        ds = row["ds_fraction"]
        if ds is None:
            failures.append(f"性质 1：{cell} 链路没有算出双支撑期")
        else:
            if ds < DS_FLOOR:
                failures.append(f"性质 1：{cell} 链路 DS {ds:+.3f} < {DS_FLOOR}")
            if ds < 0:
                negative += 1

        for label, value in row["stance_pct"].items():
            low, high = STANCE_PCT_RANGE
            if value is None:
                failures.append(f"性质 2：{cell}/{label} 没有时空参数")
            elif not low <= value <= high:
                failures.append(
                    f"性质 2：{cell}/{label} 支撑相占比 {value:.0f}% 不在 "
                    f"[{low:.0f}, {high:.0f}]% 内"
                )

        # ③ 阳性对照：旧路径必须被上面两道门之一拦下。
        control_ds = row["control_ds"]
        control_bad = control_ds is not None and control_ds < DS_FLOOR
        control_bad = control_bad or any(
            value is not None and not (STANCE_PCT_RANGE[0] <= value <= STANCE_PCT_RANGE[1])
            for value in row["control_stance_pct"].values()
        )
        if control_bad:
            control_caught += 1
        else:
            failures.append(
                f"性质 3：{cell} 的阳性对照没被抓出 —— 旧路径 DS {control_ds}、"
                f"支撑相占比 {row['control_stance_pct']} 都过了门，这两道门没有通电"
            )

    if negative > DS_NEGATIVE_MAX:
        failures.append(
            f"性质 1：{negative}/{len(rows)} 趟 DS 为负，超过允许的 {DS_NEGATIVE_MAX} 趟"
        )
    if rows and control_caught == 0:
        failures.append("性质 3：一趟对照都没被抓出 —— 两道门在这份数据上无从证明有牙")
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
