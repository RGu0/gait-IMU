"""粗判路径与细化路径的差距 —— 取代 RAY-325 的 `gait_metrics_field.py`。

## 它守的**不是**「粗判路径达标」

原件量的是 `sync/selfcheck` 那条粗判路径的 `double_support_fraction`，当前实测
**−1.003 ~ −0.966**，同足相邻 0~8。原件自己的结论是「两项都改善但都未达标，
**成因是结构性的**」。

那个结论今天依然成立，而且**永远成立**：`core/zupt` 产出的是零速**时刻**（跨度占
周期 0.7%~2.1%），两个近似零宽的区间算重叠，`fraction` 必然趋近 −1 个 step 时长。
**这不是缺陷。**

所以把它的数钉成达标线就是钉住一个永久的红 —— 那正是 RAY-328 判据 4 那张
`CYCLES_AFTER_L1` 对照表的另一种死法：脚本天天报错，而代码没有任何问题。

## 它守的是**差距**

粗判路径的价值在于它是一条**反证**：它证明 `analysis/events` 那条细化路径不可省。
所以本模块钉的是两条路径的距离，不是任何一条的绝对值：

1. **差距** = 细化路径最差的 DS − 粗判路径最差的 DS **≥ 0.80**。
   实测 −0.069 −(−1.003) = **0.934**。

   差距塌掉说明两条路径之一的语义被人改了。那时该有人来看一眼 —— 可能是好事
   （有人把 `selfcheck` 改成用区间了），也可能是坏事（有人把细化路径改回零宽）。
   脚本分不出好坏，**它只负责让这件事被看见**。
2. **粗判路径仍然 ≈ −1**（逐格 ≤ −0.90）。这是上面那条结构性论断的直接读数。
   它是**绊线不是质量线**：它变了不等于变差，等于「这里的前提变了，来读一遍」。
3. **阳性对照**：把细化路径换成**旧的边缘细化**（`refine_stance_edges`，实测
   −0.923 ~ −0.624），差距掉到 0.08 —— 必须被第 1 条抓出。

   这条对照用的是真实存在过的代码路径：它当年就是「细化了但仍然太窄」的那一版。
   有它，第 1 条才不能靠把 0.80 调低来满足。

## 三条路径，一份数据（`fe25aa7`）

| 路径 | DS 占比 | 同足相邻 |
| --- | --- | --- |
| `sync/selfcheck`（粗判，零速区间） | −1.003 ~ −0.966 | 0~8 |
| `analysis/events` + `refine_stance_edges`（旧细化） | −0.923 ~ −0.624 | 0~6 |
| **`analysis/events` + `detect_stance_intervals`（现行）** | **−0.069 ~ +0.123** | **0~1** |

用法见 `tools/run_acceptance.py`。
"""

from __future__ import annotations

from pathlib import Path

from acceptance import _stance
from acceptance._dataset import load_walks, parse_args, report
from gait.config import AlgoConfig

#: 细化路径与粗判路径最差 DS 之间的最小差距。实测 0.934，留约 15% 余量。
MIN_GAP = 0.80
#: 粗判路径的绊线：它是零宽区间的算术产物，逐格该在 −1 附近。
SELFCHECK_CEILING = -0.90


def analyse(trial_dir: Path, cfg: AlgoConfig) -> list[dict]:
    rows: list[dict] = []
    for walk in load_walks(trial_dir, cfg):
        found = _stance.detections(walk, cfg)
        rows.append(
            {
                "trial": walk.trial,
                "walk": walk.walk,
                "selfcheck": _stance.selfcheck_double_support(walk, found, cfg),
                "refined": _stance.events_double_support(walk, found, cfg, "new"),
                # 阳性对照：真实存在过的、只细化了一半的那条路径。
                "control": _stance.events_double_support(walk, found, cfg, "old"),
            }
        )
    return rows


def _number(value, spec: str = "{:+.3f}") -> str:
    """`None` 打成破折号而不是让 `format` 抛异常。"""
    return "—" if value is None else spec.format(value)


def _gap(value: float | None, baseline: float) -> float | None:
    return None if value is None else value - baseline


def _worst(rows: list[dict], key: str) -> float | None:
    values = [row[key]["ds_fraction"] for row in rows if row[key]["ds_fraction"] is not None]
    return min(values) if values else None


def judge(rows: list[dict]) -> list[str]:
    failures: list[str] = []

    # ② 绊线：粗判路径逐格仍在 −1 附近。
    for row in rows:
        cell = f"{row['trial']}/{row['walk']}"
        coarse = row["selfcheck"]["ds_fraction"]
        if coarse is None:
            failures.append(f"性质 2：{cell} 粗判路径没有算出双支撑期")
        elif coarse > SELFCHECK_CEILING:
            failures.append(
                f"性质 2：{cell} 粗判路径 DS {coarse:+.3f} > {SELFCHECK_CEILING} —— "
                f"它本该是零宽区间的算术产物（≈ −1）。**这不一定是缺陷**，"
                f"但它的前提变了，得有人来读一遍"
            )

    coarse_worst = _worst(rows, "selfcheck")
    refined_worst = _worst(rows, "refined")
    control_worst = _worst(rows, "control")
    if coarse_worst is None or refined_worst is None:
        failures.append("性质 1：两条路径里有一条一格都没算出来，无法比差距")
        return failures

    # ① 差距。
    gap = refined_worst - coarse_worst
    if gap < MIN_GAP:
        failures.append(
            f"性质 1：细化路径最差 {refined_worst:+.3f} − 粗判路径最差 "
            f"{coarse_worst:+.3f} = {gap:.3f} < {MIN_GAP} —— 两条路径的差距塌了，"
            f"其中一条的语义变了"
        )
    # ③ 阳性对照：旧细化路径的差距必须够不着这道门。
    if control_worst is not None:
        control_gap = control_worst - coarse_worst
        if control_gap >= MIN_GAP:
            failures.append(
                f"性质 3：阳性对照没被抓出 —— 旧的边缘细化路径差距 "
                f"{control_gap:.3f} 仍 ≥ {MIN_GAP}，这道门没有通电"
            )
    return failures


def main() -> int:
    args = parse_args(__doc__ or "")
    cfg = AlgoConfig()
    rows = [row for trial in args.trials for row in analyse(trial, cfg)]

    print(f"{'趟':22s}{'粗判':>9s}{'旧细化(对照)':>15s}{'现行':>9s}{'粗判同足':>10s}")
    for row in rows:
        # 任何一条路径都可能在某一格算不出来（有一只脚没有周期）。judge 会把它记成
        # 不达标，**表格不能因此崩掉**。
        print(
            f"{row['trial'] + '/' + row['walk']:22s}"
            f"{_number(row['selfcheck']['ds_fraction']):>9s}"
            f"{_number(row['control']['ds_fraction']):>15s}"
            f"{_number(row['refined']['ds_fraction']):>9s}"
            f"{_number(row['selfcheck']['same_foot'], '{:d}'):>10s}"
        )

    coarse, refined, control = (
        _worst(rows, "selfcheck"),
        _worst(rows, "refined"),
        _worst(rows, "control"),
    )
    print(
        f"\n最差 DS：粗判 {_number(coarse)} | 旧细化 {_number(control)} "
        f"| 现行 {_number(refined)}"
    )
    if coarse is not None:
        print(
            f"差距：现行 {_number(_gap(refined, coarse), '{:.3f}')}（门 {MIN_GAP}）；"
            f"对照 {_number(_gap(control, coarse), '{:.3f}')}（必须够不着这道门）"
        )

    return report(
        rows,
        judge(rows),
        "粗判路径与细化路径的差距",
        args.out,
        extra={"min_gap": MIN_GAP, "selfcheck_ceiling": SELFCHECK_CEILING},
    )


if __name__ == "__main__":
    raise SystemExit(main())
