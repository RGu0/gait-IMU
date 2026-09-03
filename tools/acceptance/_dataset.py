"""验收脚本共用的数据装载。

## 为什么验收脚本住在 `tools/` 而不是证据库

**它们是代码，不是证据。** RAY-328 的三个验收脚本住在云端证据库里 —— 不进版本、
不过审查、不过 lint —— 于是没有任何东西逼它们保持能跑。RAY-339 改了周期估计之后，
其中一个的判据锚点整个失效、另一个直接 `KeyError` 崩掉，而这件事**过了两个 Issue
才被发现**（RAY-343）。

跑出来的数仍然归证据库。脚本是代码，结果才是证据。

## 为什么装载要抽出来

七个脚本原本各自抄了一遍"打开 `arrivals.npz`、按 `walk_segments.json` 切趟、建
`FootSeriesInput`"。抄七遍意味着七份各自会漂的代码 —— 而"各自漂"正是这一系列 Issue
在治的病。

## 数据不在仓库里

本模块读的是 RAY-230 的现场采集（云端共享库），仓库里没有也不该有。所以这些脚本
**不进 `./dev` 的本机门控** —— 那会把一个离线可跑的门变成要联云的。它们由
`tools/run_acceptance.py` 在需要时调用。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from gait.analysis.planning import FootSeriesInput
from gait.config import AlgoConfig
from gait.sync.integrity import assess
from gait.sync.timebase import build_timebase

#: T-230-03 一次采集里的八段，按顺序。首尾两段是对碰锚点，不是步行。
NAMES = ["tap", "slow-a", "slow-b", "mid-a", "mid-b", "fast-a", "fast-b", "endtap"]

#: 受控真值：每趟每脚 38 个步态周期。
#:
#: **采集时现场一步一步数出来并严格控制**，两种鞋型六个速度档全部 12 趟一致 ——
#: 不是从步长推的。几何自洽：走廊 45.148 m ÷ 1.2 m/整步 = 37.6，起步左脚与收尾右脚
#: 各半步凑成一整步。来源见 `evidence/ray-337/protocol-truth/README.md`。
#:
#: 它是这套验收里**唯一不随代码演进的锚**，所以凡是能靠它的判据都该靠它，而不是
#: 靠上一版代码的读数（RAY-328 的 `CYCLES_AFTER_L1` 就是靠后者的反面教材）。
TRUTH_CYCLES = 38.0


@dataclass(frozen=True)
class Walk:
    """一趟里的两只脚，外加算真周期需要的时长。"""

    trial: str
    walk: str
    feet: dict[str, FootSeriesInput]
    duration_s: float
    nominal_fs: float

    @property
    def cell(self) -> str:
        return f"{self.trial}/{self.walk}"

    @property
    def true_period_s(self) -> float:
        """受控真周期 = 趟时长 / 38。

        步数受控 + 走廊定长 ⟹ 速度档之间只差步频、步长是常量，所以这个除法成立。
        """
        return self.duration_s / TRUTH_CYCLES


def load_walks(trial_dir: Path, cfg: AlgoConfig, *, lead_s: float = 0.0) -> list[Walk]:
    """一次采集里的六趟步行段。对碰段不返回 —— 它们不是步态。

    `lead_s` 把每趟的起点**往前延**若干秒，默认 0（其余脚本的基线都建在 0 上，
    不要改这个默认值）。跑完整产品链路的脚本需要它：`core/alignment.py` 要求序列
    开头有 ≥ 0.5 s 的静止段（RAY-202 的初始对准），而逐趟切片是从走起来那一刻切的，
    静止前导落在切片之外。实测前延 1 s 就够六趟全部找得到，取 2 s 留余量。

    前延进来的静止段会被检成一个很长的支撑相，但它进不了指标：分段筛选
    （`analysis/segments.py`）只留直行段的中段步，静立既不是直行段也不在中段。
    """
    bundle = np.load(trial_dir / "arrivals.npz", allow_pickle=False)
    walks = json.loads((trial_dir / "walk_segments.json").read_text(encoding="utf-8"))
    label = str(bundle["label"])
    nominal_fs = float(bundle["nominal_fs"])
    # 两只脚共钟，但各自的第一个到达时刻不同；取较早的那个当原点，好让
    # `walk_segments.json` 里的相对时刻对两只脚都成立。
    origin = min(float(bundle["left_arrival"][0]), float(bundle["right_arrival"][0]))

    result: list[Walk] = []
    for index, name in enumerate(NAMES):
        if name in ("tap", "endtap"):
            continue
        feet: dict[str, FootSeriesInput] = {}
        duration = 0.0
        for prefix, foot in (("left", "L"), ("right", "R")):
            arrival = np.asarray(bundle[f"{prefix}_arrival"], dtype=np.float64)
            walk = walks["feet"][foot]["walks"][index]
            duration = float(walk["duration_s"])
            start = int(np.searchsorted(arrival - origin, max(0.0, walk["start_s"] - lead_s)))
            stop = int(np.searchsorted(arrival - origin, walk["end_s"]))
            sliced = arrival[start:stop]
            feet[foot] = FootSeriesInput(
                arrival=sliced,
                accel=np.asarray(bundle[f"{prefix}_accel"], dtype=np.float64)[start:stop],
                gyro=np.asarray(bundle[f"{prefix}_gyro"], dtype=np.float64)[start:stop],
                # 实测采样率，不是标称值：两脚的 fs 实测差到 1.1%，用标称值会把这个差
                # 记到跨脚比值上去。
                fs=build_timebase(sliced, nominal_fs, cfg).report.fs,
                integrity=assess(sliced, nominal_fs, cfg),
            )
        result.append(
            Walk(
                trial=label,
                walk=name,
                feet=feet,
                duration_s=duration,
                nominal_fs=nominal_fs,
            )
        )
    return result


def parse_args(description: str):
    """所有验收脚本共用的命令行：若干趟次目录 + 可选的 `--out`。"""
    import argparse

    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("trials", nargs="+", type=Path)
    parser.add_argument("--out", type=Path)
    return parser.parse_args()


def report(rows: list[dict], failures: list[str], title: str, out: Path | None, extra: dict | None = None) -> int:
    """打印失败清单、可选落盘、返回退出码。**格式统一，好让 runner 能汇总。**"""
    print()
    for line in failures:
        print(f"不达标：{line}")
    print(f"{title}：达标" if not failures else f"{len(failures)} 条不达标")

    if out is not None:
        out.write_text(
            json.dumps(
                {
                    "config_version": AlgoConfig().version,
                    "truth_cycles": TRUTH_CYCLES,
                    **(extra or {}),
                    "rows": rows,
                    "failures": failures,
                },
                ensure_ascii=False,
                indent=1,
            ),
            encoding="utf-8",
        )
    return 0 if not failures else 1
