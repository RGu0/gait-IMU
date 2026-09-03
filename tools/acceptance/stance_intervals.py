"""支撑相区间与双支撑期 —— 取代 RAY-325 的 `stance_interval_field.py`。

守 RAY-325 判据 2：`double_support_fraction` 不再出现负值，`same_foot_adjacencies`
显著下降。

## 原件为什么被取代

原件跑得动，读数也没漂 —— 但它住在云端证据库里，与那个**已经崩掉**的
`period_stance_field.py` 是同一个位置、同一份风险（见 `period_cycles.py` 的模块
文档）。RAY-343 判定验收脚本是代码，本模块是那条判定的外推。

它还有两处必须改：

* **原件不判**，只打印。达标与否停留在 Linear 的判据正文里，靠人读表比对。
* **原件用 **`_cycles_from_edges`（私有），本版走公开的 `segment_cycles(...,
  stance_edges=...)` —— 那个关键字参数正是为这条路存在的。依赖私有名字正是
  `_runs` 那次崩溃的成因。

## 钉什么

1. **DS 占比**：全部 ≥ −0.10，其中 ≥ 10/12 格非负。

   为什么不是「一格不许为负」：ZUPT 边界比生理边界系统性内缩，`S1-sport` 快档两格
   （HOKA 厚软中底把撞击摊长到 85~95 ms）实测 −0.069/−0.068，**2026-09-02 用户裁决
   接受**。判据钉的是「负得有限」，不是「不许负」——把它写成后者就得靠改判据来达成。
2. **同足相邻**：全部 ≤ 1，其中 ≥ 10/12 格为 0。步态必须严格左右交替，这个数是那个
   前提被打破的最直接读数。
3. **支撑相占比**落在 [35%, 60%]。文献 60%~75%，真机实测 38~56% —— **仍然偏窄，
   方向已知、量未知**（真机没有 IC/TO 真值：无测力台、无压力垫、无录像）。所以区间
   下沿贴着实测放，它守的是「没有退回零宽」，不是「达到了生理值」。
4. **阳性对照**：同一批真机数据走**旧路径**（`refine_stance_edges`，零速区间的边缘
   细化），DS 必须**掉出** ≥ −0.10 那道门。

   这是一个真实的、更差的代码路径，不是编出来的缺陷 —— 它当年就是这条判据要替换的
   东西。实测旧路径 −0.923~−0.624，全部 12 格都该被抓出。

## 24 格实测（`fe25aa7`）

| | DS 占比 | 同足相邻 | 支撑占比 | 区间数 |
| --- | --- | --- | --- | --- |
| 旧（`refine_stance_edges`） | −0.923 ~ −0.624 | 0~6 | 1~16% | 35~42 |
| **新（**`detect_stance_intervals`**）** | **−0.069 ~ +0.123** | **0~1** | **38~56%** | **34~37** |

用法见 `tools/run_acceptance.py`。
"""

from __future__ import annotations

from pathlib import Path

from acceptance import _stance
from acceptance._dataset import load_walks, parse_args, report
from gait.config import AlgoConfig

#: DS 占比的下限。实测最负 −0.069（`S1-sport` 快档，用户已裁决接受），留约 30% 余量。
DS_FLOOR = -0.10
#: 允许为负的格数上限（12 格中）。实测正好 2 格。
DS_NEGATIVE_MAX = 2
#: 同足相邻的逐格上限，与「非零格数」的上限。步态严格交替，正常值恒为 0。
SAME_FOOT_MAX = 1
SAME_FOOT_NONZERO_MAX = 2
#: 支撑相占周期的比例，%。实测 38~56；文献 60~75，**这里仍偏窄**。
STANCE_PCT_RANGE = (35.0, 60.0)


def analyse(trial_dir: Path, cfg: AlgoConfig) -> list[dict]:
    rows: list[dict] = []
    for walk in load_walks(trial_dir, cfg):
        # 零速检测与走哪条路径无关，算一次两条路径共用。
        found = _stance.detections(walk, cfg)
        rows.append(
            {
                "trial": walk.trial,
                "walk": walk.walk,
                "new": _stance.events_double_support(walk, found, cfg, "new"),
                # 阳性对照：真实存在的、更差的那条路径。
                "old": _stance.events_double_support(walk, found, cfg, "old"),
            }
        )
    return rows


def judge(rows: list[dict]) -> list[str]:
    failures: list[str] = []
    negative = 0
    same_foot_nonzero = 0

    for row in rows:
        cell = f"{row['trial']}/{row['walk']}"
        new, old = row["new"], row["old"]

        if new["ds_fraction"] is None:
            failures.append(f"性质 1：{cell} 没有算出双支撑期 —— 有一只脚没有周期")
            continue

        # ① DS 占比：负得有限。
        if new["ds_fraction"] < DS_FLOOR:
            failures.append(
                f"性质 1：{cell} DS 占比 {new['ds_fraction']:+.3f} < {DS_FLOOR}"
            )
        if new["ds_fraction"] < 0:
            negative += 1
        # ② 同足相邻。
        if new["same_foot"] > SAME_FOOT_MAX:
            failures.append(
                f"性质 2：{cell} 同足相邻 {new['same_foot']} > {SAME_FOOT_MAX}"
                f" —— 左右交替这个前提被打破了"
            )
        if new["same_foot"] > 0:
            same_foot_nonzero += 1
        # ③ 支撑相占比。
        low, high = STANCE_PCT_RANGE
        for value in new["stance_pct"]:
            if not low <= value <= high:
                failures.append(
                    f"性质 3：{cell} 支撑相占比 {value:.0f}% 不在 [{low:.0f}, {high:.0f}]% 内"
                )
        # ④ 阳性对照：旧路径必须被这道门拦下。
        if old["ds_fraction"] is not None and old["ds_fraction"] >= DS_FLOOR:
            failures.append(
                f"性质 4：{cell} 的阳性对照没被抓出 —— 旧路径"
                f"（refine_stance_edges）DS {old['ds_fraction']:+.3f} 仍 ≥ {DS_FLOOR}，"
                f"这道门没有通电"
            )

    if negative > DS_NEGATIVE_MAX:
        failures.append(
            f"性质 1：{negative}/{len(rows)} 格 DS 为负，超过允许的 {DS_NEGATIVE_MAX} 格"
        )
    if same_foot_nonzero > SAME_FOOT_NONZERO_MAX:
        failures.append(
            f"性质 2：{same_foot_nonzero}/{len(rows)} 格同足相邻非零，"
            f"超过允许的 {SAME_FOOT_NONZERO_MAX} 格"
        )
    return failures


def _number(value, spec: str = "{:+.3f}") -> str:
    """`None` 打成破折号而不是让 `format` 抛异常。"""
    return "—" if value is None else spec.format(value)


def _span(rows: list[dict], path: str, key: str) -> str:
    values = [row[path][key] for row in rows if row[path][key] is not None]
    if not values:
        return "—"
    if key in ("stance_pct", "intervals"):
        flat = [item for value in values for item in value]
        return f"{min(flat):.0f}~{max(flat):.0f}" if flat else "—"
    if key == "same_foot":
        return f"{min(values)}~{max(values)}"
    return f"{min(values):+.3f}~{max(values):+.3f}"


def main() -> int:
    args = parse_args(__doc__ or "")
    cfg = AlgoConfig()
    rows = [row for trial in args.trials for row in analyse(trial, cfg)]

    print(f"{'趟':22s}{'DS新':>9s}{'DS旧(对照)':>13s}{'同足':>6s}{'支撑%':>10s}{'区间数':>9s}")
    for row in rows:
        new, old = row["new"], row["old"]
        # 有一只脚没算出周期时 `ds_fraction` 是 `None`。judge 会把它记成不达标，
        # **表格不能因此崩掉** —— 崩在格式化上就看不到是哪一格出的事了。
        print(
            f"{row['trial'] + '/' + row['walk']:22s}"
            f"{_number(new['ds_fraction']):>9s}{_number(old['ds_fraction']):>13s}"
            f"{_number(new['same_foot'], '{:d}'):>6s}"
            f"{'/'.join(f'{v:.0f}' for v in new['stance_pct']) or '—':>10s}"
            f"{'/'.join(str(v) for v in new['intervals']) or '—':>9s}"
        )

    print(
        f"\n[新] DS {_span(rows, 'new', 'ds_fraction')} | 同足相邻 "
        f"{_span(rows, 'new', 'same_foot')} | 支撑占比 {_span(rows, 'new', 'stance_pct')}% "
        f"| 区间数 {_span(rows, 'new', 'intervals')}"
    )
    print(
        f"[旧] DS {_span(rows, 'old', 'ds_fraction')} | 同足相邻 "
        f"{_span(rows, 'old', 'same_foot')} | 支撑占比 {_span(rows, 'old', 'stance_pct')}% "
        f"| 区间数 {_span(rows, 'old', 'intervals')}   ← 阳性对照，必须被拦下"
    )

    return report(
        rows,
        judge(rows),
        "支撑相区间与双支撑期",
        args.out,
        extra={"ds_floor": DS_FLOOR, "stance_pct_range": list(STANCE_PCT_RANGE)},
    )


if __name__ == "__main__":
    raise SystemExit(main())
