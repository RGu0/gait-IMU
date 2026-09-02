"""双脚协同的周期规划入口。`analysis/planning.py`（RAY-328 `dual-foot-qc-windowing`）。

它把两件独立的事合成一份可落盘的读数：

* **宽闸与净窗**（`sync/planning.py`）—— 这一趟的哪些时间段两只脚都完整，覆盖率多少；
* **跨脚周期校验**（`core/dualfoot.check_cross_foot_period`）—— 两只脚的周期对得上吗。

合成放在 `analysis` 层而不是任一边，是分层红线逼出来的，而红线本身是对的：
`gait.core` **不得 import** `gait.sync`（`gait.CORE_FORBIDDEN_IMPORTS`，lint 强制），
因为 core 要能在 CLI、Windows 采集端、云端重算三个宿主里跑同一份代码，也要能把外部
数据集直接喂进来 —— 一旦它认识了同步层，这个性质就悄悄没了。所以跨脚信息**全部经
参数传入 core**：`check_cross_foot_period` 只收两个已经算好的 `PeriodReport`，它不知道
也不需要知道那两个周期是从哪条时间轴上来的。

## 降级是一个戳，不是一道闸

`PeriodPlan.degraded` 为真时，`window` 与两脚的周期估计**一个字都不变**。跨脚校验
只加票、不否决，理由见 `CrossFootPeriod` 的文档：同一个超阈的比值，既可能是估计跑
掉了，也可能是这个人两脚周期真的不同，而没有数据能分开这两者 —— 目标人群恰恰是后
一种人。

`plannable` 与 `degraded` 因此是两个正交的量：前者说"这份数据够不够做规划"（宽闸的
结论），后者说"规划出来的结果要不要打折看"（跨脚的一票）。一份 `plannable=True,
degraded=True` 的报告是完全正常的输出，不是矛盾。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from gait.config import AlgoConfig
from gait.core.dualfoot import CrossFootPeriod, check_cross_foot_period
from gait.core.zupt import PeriodReport
from gait.sync.integrity import IntegrityReport
from gait.sync.planning import DualNetWindow, plan_dual_net_window

__all__ = ["FootPlanInput", "PeriodPlan", "plan_periods"]


@dataclass(frozen=True)
class FootPlanInput:
    """一只脚送进规划的东西。

    `fs` 是这只脚的**实测**采样率（`SyncReport.fs`），不是标称值：`PeriodReport` 以
    样本计周期，而两脚的实测 fs 实测最大差 1.1%，用标称值换算等于把这个差记到跨脚
    比值上去。
    """

    #: 主机侧到达时刻，s，升序。与另一只脚**共钟**。
    arrival: np.ndarray
    #: 这只脚的周期估计。`None` 表示这一段没有可辨认的步态。
    period: PeriodReport | None
    #: 实测采样率，Hz。
    fs: float
    #: 已经算好的完整性报告。不传就由 `sync.planning` 自己算。
    integrity: IntegrityReport | None = None


@dataclass(frozen=True)
class PeriodPlan:
    """一趟的周期规划前提。"""

    window: DualNetWindow
    #: 跨脚校验。`None` 表示至少一只脚没有周期 —— 这一票**弃权**，不是赞成。
    cross_foot: CrossFootPeriod | None

    @property
    def plannable(self) -> bool:
        """宽闸的结论：这份数据够不够做周期规划。"""
        return self.window.plannable

    @property
    def degraded(self) -> bool:
        """跨脚的一票：两脚周期对不上。**不影响 `plannable`。**"""
        return self.cross_foot is not None and not self.cross_foot.agrees

    def snapshot(self) -> dict[str, Any]:
        return {
            "window": self.window.snapshot(),
            "cross_foot": self.cross_foot.snapshot() if self.cross_foot else None,
            "plannable": self.plannable,
            "degraded": self.degraded,
            "coverage": self.window.coverage,
        }


def plan_periods(
    left: FootPlanInput,
    right: FootPlanInput,
    nominal_fs: float,
    cfg: AlgoConfig | None = None,
) -> PeriodPlan:
    """算出这一趟的净窗、覆盖率与跨脚校验结论。

    两问分开算、都算完：宽闸拒了也照样出跨脚结论，跨脚弃权也照样出净窗。把其中一个
    的失败变成另一个的短路，会让报告里少掉的那一半看起来像"没这个问题"。
    """
    cfg = cfg or AlgoConfig()
    window = plan_dual_net_window(
        left.arrival,
        right.arrival,
        nominal_fs,
        cfg,
        left_report=left.integrity,
        right_report=right.integrity,
    )
    cross_foot = check_cross_foot_period(left.period, left.fs, right.period, right.fs, cfg)
    return PeriodPlan(window=window, cross_foot=cross_foot)
