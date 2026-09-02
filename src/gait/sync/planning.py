"""周期规划的净窗与宽闸。`sync/planning.py`（RAY-328 `dual-foot-qc-windowing`）。

## 为什么要第二道闸，而不是把第一道放松

仓库里已经有一道时基信任闸：`SyncReport.stable`（分窗采样率相对离散 < 0.1%，RAY-209）
加 `IntegrityReport.grade != "unusable"`（RAY-210）。`cli/v3prime.py` 的
`timebase_trustworthy` 就是它，而它是**为步态参数的精度设计的** —— 步长、步速要在
时间轴上做微分，fs 差百分之几，几万个样本累积下来就是秒级的时刻误差。

周期规划要的精度差一个数量级。它只做两件事：这一段有几个周期、每个周期从哪到哪。
周期本身是 1~3.8 s 的量，而 BLE 到达时刻的时基残差实测 p95 是 27~40 ms —— 占周期的
1%~4%。**用步态参数的尺子量周期规划，量出来的结论是"不可用"，而它明明可用**：实测
整个 S1-sport 被 `timebase_trustworthy` 判为不可信，但它的周期估计好得很，24 格里
它贡献了 5 个覆盖率 ≥99% 的格。

所以这里立一道**宽闸**，与严闸并存、互不影响：

| | 严闸（步态参数） | 宽闸（周期规划） |
| -- | -- | -- |
| `SyncReport.stable` | 要求 | **不看** |
| `IntegrityReport.grade` | 拒 `unusable` | 拒 `unusable`（同） |
| 空洞 | 切段 | 挖净窗 + 保护带 |
| 双脚 | 各判各的 | 取**交集**，报覆盖率 |

放松的只有 `stable` 那一条，因为它量的正是"微分够不够准"。`unusable` 那一条一个字
不改 —— 它是 `assess` 自己定义的"不可用"，与用途无关。实测 24 格里唯一残差超预算的
S1-sport/slow-a（右脚 RMS 148 ms、p95 286 ms）正是被这一条挑出来的，宽闸并不需要为
它另发明判据。

**严闸的代码本模块一行都不碰。** 两条路径共用一道闸必然有一边错，而"错"的方向相反：
严闸给周期规划用就过严，宽闸给步态参数用就过松。

## 净窗：为什么按到达时刻挖，为什么要保护带

净窗是"这段时间里两只脚的数据都完整"。空洞的位置用**实际到达时刻**取，不用拟合时基
的时刻：时基按样本序号回归，空洞之后的样本序号整体前移了（丢掉的样本不在数组里），
拿回归时刻去标空洞位置会把窗口标偏。

两侧各留 `AlgoConfig.planning_gap_guard_s` 的保护带，因为 BLE 按连接事件**成簇**送达：
空洞前后那几个样本是迟到扎堆的一簇，到达时刻挤在一起，用它们做任何时间推断都偏。
这也是可行性实测里踩过的坑的另一面 —— 那次是拿簇内 dt 当采样周期，把每个簇间隔都
判成了空洞（见 `evidence/ray-328/feasibility/README.md`）。

## 无静默截断

覆盖率与拒绝理由**一起**返回，而且拒绝了也照样返回净窗本身。一个"这一趟不能规划"
的布尔值配不上这件事：调用方要能看出是覆盖率不够、是某只脚 unusable、还是两脚的
采集时间根本不重叠 —— 三者的补救完全不同。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from gait.config import AlgoConfig
from gait.sync.integrity import IntegrityReport, assess

__all__ = [
    "DualNetWindow",
    "FootNetWindow",
    "PlanningError",
    "cycle_is_net",
    "net_window",
    "plan_dual_net_window",
]

Span = tuple[float, float]


class PlanningError(ValueError):
    """周期规划的输入非法。"""


@dataclass(frozen=True)
class FootNetWindow:
    """一只脚的净窗。"""

    #: 这只脚的采集跨度，`(第一个到达时刻, 最后一个到达时刻)`。
    span: Span
    #: 挖掉空洞与保护带之后剩下的区间，升序、不重叠、半开语义无关（都是闭区间的
    #: 时刻对，长度才有意义）。
    net: tuple[Span, ...]
    #: `net` 的总时长占 `span` 的比例。
    coverage: float
    #: `assess` 的分级，原样带出。
    grade: str
    gaps: int
    lost_samples: int


@dataclass(frozen=True)
class DualNetWindow:
    """两只脚净窗的交集，以及它够不够做周期规划。"""

    #: 两脚共同的采集跨度。两脚没有重叠时长度为零。
    span: Span
    #: 交集后的净区间。
    net: tuple[Span, ...]
    #: `net` 总时长 / `span` 时长。`span` 为零时取 0.0。
    coverage: float
    #: 最长的一段连续净区间，s。它比覆盖率更能说明"还剩多少能连着走的周期"——
    #: 同样 95% 的覆盖率，碎成 20 段和只缺一处，可用性完全不同。
    longest_run_s: float
    left: FootNetWindow
    right: FootNetWindow
    guard_s: float
    minimum_coverage: float
    #: 拒绝理由，空元组表示可规划。**拒绝了也照样有上面的净窗与覆盖率**。
    refusals: tuple[str, ...]

    @property
    def plannable(self) -> bool:
        return not self.refusals

    def snapshot(self) -> dict[str, Any]:
        """可落盘的读数。覆盖率是 PRD §6.1 要求上报的量，不是调试信息。"""
        return {
            "span": list(self.span),
            "net": [list(item) for item in self.net],
            "coverage": self.coverage,
            "longest_run_s": self.longest_run_s,
            "guard_s": self.guard_s,
            "minimum_coverage": self.minimum_coverage,
            "refusals": list(self.refusals),
            "plannable": self.plannable,
            "left": {
                "span": list(self.left.span),
                "coverage": self.left.coverage,
                "grade": self.left.grade,
                "gaps": self.left.gaps,
                "lost_samples": self.left.lost_samples,
            },
            "right": {
                "span": list(self.right.span),
                "coverage": self.right.coverage,
                "grade": self.right.grade,
                "gaps": self.right.gaps,
                "lost_samples": self.right.lost_samples,
            },
        }


def _subtract(span: Span, holes: list[Span]) -> tuple[Span, ...]:
    """从一个区间里挖掉一组洞。洞可以重叠、可以越界。"""
    start, stop = span
    if stop <= start:
        return ()
    pieces: list[Span] = []
    cursor = start
    for lo, hi in sorted(holes):
        lo, hi = max(lo, start), min(hi, stop)
        if hi <= cursor:
            continue
        if lo > cursor:
            pieces.append((cursor, min(lo, stop)))
        cursor = max(cursor, hi)
        if cursor >= stop:
            break
    if cursor < stop:
        pieces.append((cursor, stop))
    return tuple(piece for piece in pieces if piece[1] > piece[0])


def _intersect(left: tuple[Span, ...], right: tuple[Span, ...]) -> tuple[Span, ...]:
    """两组升序不重叠区间的交集。双指针一次扫过，不建网格。

    不建网格是有意的：可行性脚本按 200 Hz 采样点算覆盖率，那让结果带上一个 5 ms 的
    量化误差，而 0.95 这个下限就在实测值 0.9474 的隔壁 —— 量化噪声正好在会改变结论
    的量级上。区间运算没有这个问题。
    """
    result: list[Span] = []
    i = j = 0
    while i < len(left) and j < len(right):
        lo = max(left[i][0], right[j][0])
        hi = min(left[i][1], right[j][1])
        if hi > lo:
            result.append((lo, hi))
        if left[i][1] < right[j][1]:
            i += 1
        else:
            j += 1
    return tuple(result)


def _total(spans: tuple[Span, ...]) -> float:
    return float(sum(hi - lo for lo, hi in spans))


def net_window(
    arrival: np.ndarray,
    nominal_fs: float,
    cfg: AlgoConfig | None = None,
    *,
    report: IntegrityReport | None = None,
) -> FootNetWindow:
    """一只脚的净窗。`arrival` 是主机侧到达时刻，s，升序。

    `report` 可以由调用方预先算好传入 —— `assess` 在长采集上不便宜，而调用方往往
    已经为别的用途算过一次。不传就自己算。
    """
    cfg = cfg or AlgoConfig()
    times = np.asarray(arrival, dtype=np.float64)
    if times.ndim != 1:
        raise PlanningError(f"arrival 应为一维数组，收到 shape={times.shape}")
    if times.size < 2:
        raise PlanningError(
            f"arrival 只有 {times.size} 个到达时刻，构不成一个跨度。"
            "空洞切分可能切出这样的碎段；调用方应当整段跳过。"
        )
    integrity = report if report is not None else assess(times, nominal_fs, cfg)
    guard = float(cfg.planning_gap_guard_s)
    span: Span = (float(times[0]), float(times[-1]))
    holes = [
        (float(times[gap.before]) - guard, float(times[gap.after]) + guard)
        for gap in integrity.gaps
    ]
    net = _subtract(span, holes)
    width = span[1] - span[0]
    return FootNetWindow(
        span=span,
        net=net,
        coverage=_total(net) / width if width > 0.0 else 0.0,
        grade=integrity.grade,
        gaps=len(integrity.gaps),
        lost_samples=integrity.lost_samples,
    )


def plan_dual_net_window(
    left_arrival: np.ndarray,
    right_arrival: np.ndarray,
    nominal_fs: float,
    cfg: AlgoConfig | None = None,
    *,
    left_report: IntegrityReport | None = None,
    right_report: IntegrityReport | None = None,
) -> DualNetWindow:
    """双脚净窗交集 + 宽闸判定。

    **两个 `arrival` 必须共钟。** 采集端的两台设备跑在同一个进程的同一个事件循环上，
    `t_host` 取自同一个 `time.monotonic()`（见 `cli/v3prime.py` 的模块文档），所以
    这个前提在本仓库的采集路径上天然成立。拿两份各自归零的录制文件调用本函数，交集
    会算在两个不相干的时间轴上，而**这里检查不出来** —— 它只能看见两个跨度不重叠，
    看不见"重叠了但对错了"。
    """
    cfg = cfg or AlgoConfig()
    left = net_window(left_arrival, nominal_fs, cfg, report=left_report)
    right = net_window(right_arrival, nominal_fs, cfg, report=right_report)

    common: Span = (
        max(left.span[0], right.span[0]),
        min(left.span[1], right.span[1]),
    )
    refusals: list[str] = []
    if left.grade == "unusable":
        refusals.append("left_unusable")
    if right.grade == "unusable":
        refusals.append("right_unusable")

    if common[1] <= common[0]:
        # 两脚的采集时间不重叠。这不是"覆盖率低"，是根本没有共同的窗口 ——
        # 分开一个理由，因为补救完全不同（重采 vs 换判据）。
        refusals.append("no_common_span")
        return DualNetWindow(
            span=common,
            net=(),
            coverage=0.0,
            longest_run_s=0.0,
            left=left,
            right=right,
            guard_s=float(cfg.planning_gap_guard_s),
            minimum_coverage=float(cfg.planning_min_coverage),
            refusals=tuple(refusals),
        )

    # 两脚的净窗各自已经落在自己的跨度内，所以它们的交集自动落在共同跨度内 ——
    # 不必再与 `common` 求一次交。
    net = _intersect(left.net, right.net)
    width = common[1] - common[0]
    coverage = _total(net) / width
    if coverage < cfg.planning_min_coverage:
        refusals.append("coverage_below_minimum")
    return DualNetWindow(
        span=common,
        net=net,
        coverage=coverage,
        longest_run_s=max((hi - lo for lo, hi in net), default=0.0),
        left=left,
        right=right,
        guard_s=float(cfg.planning_gap_guard_s),
        minimum_coverage=float(cfg.planning_min_coverage),
        refusals=tuple(refusals),
    )


def cycle_is_net(start: float, stop: float, window: DualNetWindow) -> bool:
    """这一个周期是否整个落在双脚净窗里。时刻与 `window.span` 同一条轴。

    这是 PRD §6.1「空洞跨越的步态周期标记 invalid」在**时间域**的对应件 ——
    `integrity.spans_gap` 做的是同一件事，但它在**样本序号**域，且只看一只脚。周期
    规划要的是"两只脚在这段时间里都完整"，而两只脚的样本序号互不通约，所以判断必须
    回到共钟的时刻上。

    整个落入才算数，不做"大部分落入"的宽容：一个周期跨过空洞，它的边界与内部极值就
    分别落在空洞两侧，而两侧之间隔着未知长的时间 —— 这样的周期不是"精度差一点"，是
    它测的那个量根本不存在。
    """
    if stop <= start:
        return False
    return any(lo <= start and stop <= hi for lo, hi in window.net)
