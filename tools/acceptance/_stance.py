"""支撑相相关的共用计算。

`stance_intervals.py` 与 `selfcheck_contrast.py` 都要「先 `detect_stance`，再按某条
路径算双支撑期」。抄两遍就是两份各自会漂的代码 —— 而「各自漂」正是 RAY-328 → 339 →
343 → 346 这一串 Issue 在治的病，在这套脚本自己身上重演一次未免难看。

`detect_stance` 的结果与走哪条路径无关，所以它算一次、两条路径共用：一趟两只脚的
检测占了这两个脚本的绝大部分耗时。
"""

from __future__ import annotations

from itertools import pairwise
from typing import Any

import numpy as np

from acceptance._dataset import Walk
from gait.analysis.events import (
    detect_stance_intervals,
    double_support,
    refine_stance_edges,
    segment_cycles,
)
from gait.config import AlgoConfig
from gait.core.zupt import StanceDetection, detect_stance
from gait.sync import selfcheck


def detections(walk: Walk, cfg: AlgoConfig) -> dict[str, StanceDetection]:
    """一趟两只脚的零速检测。两条路径共用，别各算各的。"""
    return {
        label: detect_stance(foot.accel, foot.gyro, foot.fs, cfg)
        for label, foot in walk.feet.items()
    }


def events_double_support(
    walk: Walk, found: dict[str, StanceDetection], cfg: AlgoConfig, path: str
) -> dict[str, Any]:
    """`analysis/events` 那条路径的双支撑期。

    `path` 取 `new`（`detect_stance_intervals`，支撑相**区间**）或 `old`
    （`refine_stance_edges`，零速区间的**边缘细化**）。两者产出的支撑相宽度差一个
    数量级，所以它是显式参数 —— 读报告的人得能从调用点看出拿的是哪一条。
    """
    cycles: dict[str, list] = {}
    stance_pct: list[float] = []
    intervals: list[int] = []

    for label, foot in walk.feet.items():
        detection = found[label]
        edges = (
            detect_stance_intervals(foot.accel, foot.fs, detection, cfg)
            if path == "new"
            else refine_stance_edges(foot.accel, foot.gyro, detection.stances, cfg)
        )
        # 趟内相对时刻。两只脚的切片由 `_dataset` 用**共同原点**取，所以两条
        # `arange/fs` 的零点对得齐 —— 各减各的 `arrival[0]` 会让它们各自归零。
        times = np.arange(len(foot.accel)) / foot.fs
        segmented, _ = segment_cycles(
            label,  # type: ignore[arg-type]  # FootLabel 是 Literal["L", "R"]
            times,
            foot.accel,
            foot.gyro,
            detection.stances,
            cfg=cfg,
            stance_edges=edges,
        )
        cycles[label] = segmented
        intervals.append(len(edges))
        if edges and detection.period is not None:
            stance_pct.append(
                100.0
                * float(np.median([edge.samples for edge in edges]))
                / float(detection.period.period_samples)
            )

    result: dict[str, Any] = {
        "path": path,
        "ds_fraction": None,
        # RAY-354 判据 2：`fraction` 建在均值上，被单个离群相位支配。实测这条路径上
        # `S1-sport/fast-a` 有一个 **+4.412 s**（4.5 个步态周期）的静止前导伪影，
        # 把均值从 −0.1026 顶到 −0.0331 —— 而中位 −0.1112 纹丝不动。
        # 判据读中位，`fraction` 只作记录。
        "ds_median": None,
        "ds_excluded": None,
        "same_foot": None,
        "stance_pct": [round(value, 1) for value in stance_pct],
        "intervals": intervals,
    }
    if not (cycles.get("L") and cycles.get("R")):
        return result

    support = double_support(cycles["L"], cycles["R"], sync_quality={"acceptance": True})
    order = sorted(
        [(cycle.t_ic, "L") for cycle in cycles["L"]]
        + [(cycle.t_ic, "R") for cycle in cycles["R"]]
    )
    result["ds_fraction"] = round(float(support.fraction), 4)
    result["ds_median"] = round(float(support.median), 4)
    result["ds_excluded"] = int(support.excluded)
    result["same_foot"] = sum(1 for a, b in pairwise(order) if a[1] == b[1])
    return result


def selfcheck_double_support(
    walk: Walk, found: dict[str, StanceDetection], cfg: AlgoConfig
) -> dict[str, Any]:
    """`sync/selfcheck` 那条**粗判**路径的双支撑期。

    它直接拿零速区间算重叠，不做 IC/TO 细化。模块自己的文档写明这是给「有没有严重
    异常」用的，不是指标 —— 本函数存在的理由就是把这个差别量出来，见
    `selfcheck_contrast.py`。
    """
    spans: dict[str, list] = {}
    step_times: list[float] = []
    for label, foot in walk.feet.items():
        detection = found[label]
        # 到达时刻本身就是共钟的绝对时刻，两只脚可比；这里比的全是差值，原点无关。
        spans[label] = selfcheck.drop_still_lead(
            selfcheck.stance_spans(foot.arrival, detection.stances), cfg
        )
        if detection.period is not None:
            step_times.append(float(detection.period.period_samples) / foot.fs / 2.0)

    if not step_times or not all(spans.get(label) for label in ("L", "R")):
        return {"path": "selfcheck", "ds_fraction": None, "same_foot": None}

    support = selfcheck.double_support(
        spans["L"], spans["R"], float(np.median(step_times))
    )
    return {
        "path": "selfcheck",
        "ds_fraction": round(float(support.fraction), 4),
        "same_foot": support.same_foot_adjacencies,
        "phases": support.phases,
    }
