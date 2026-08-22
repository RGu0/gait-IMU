"""质量标注。契约 §1 的 `quality/`（F4.4）—— **端云同构的唯一实现点**。

PRD §13 与原则 12：v1 **不做门控**，全量输出 + 质量标注。这句话的分量在于它把一个
产品决定变成了一条代码约束：任何"这个指标不够好所以不给"的逻辑都是错的，正确的做法
是给出来并说明它有多好。

## 一、为什么"唯一实现点"是这个模块的名字的一半

同一份质量逻辑会被三个宿主用到：Windows 采集端的基础链、云端重算的完整链、以及
CLI/回放。三处各写一遍的后果不是"不一致"这么轻 —— 是**同一次采集在采集端显示 normal、
在云端报告里显示 low**，而两个数字都出自"我们的系统"。用户没有任何办法判断该信哪个。

所以分级函数只有这一个。三个宿主调同一份代码。

RAY-218 的红线 R-3 说的是同一件事在前端的版本：**React 渲染进程不得复算质量分级**，
只渲染 sidecar 通过 IPC 给出的 `grade`。为"显示得快"在前端照阈值算一遍，质量逻辑
立刻有了第二实现。`tools/check_quality_single_source.py` 在 CI 里守这条。

## 二、门控矩阵是配置预留，默认全不启用（FR-510）

`GateMatrix` 存在，但默认每一项都是 `False`，且 `apply()` 在全不启用时是恒等的。

留着它而不是不写，是因为"以后要不要门控"是个产品问题，而产品问题变的时候，代码里
有没有一个**明确的地方**去改，决定了那次改动是一行配置还是一次翻修。

启用任何一项都会改变 `rules_version` —— 因为启用门控之后产出的报告与之前的不可直接
比较，而"不可比较"这件事必须在报告页脚看得见，不能靠人记得。

## 三、grade 的取值与 PRD 的三档

PRD §13 给的是 `normal` / `low` / `uncomputable`。注意它与 `contracts.Confidence`
（`normal` / `degraded` / `invalid`）**不是同一套** —— 后者说的是"这条步态周期本身
可不可信"，前者说的是"这个**指标**能支撑什么结论"。一条 degraded 的周期照样能进一个
normal 的指标（只要样本够多），反过来也成立（周期都很好，但只有 6 步）。

两套词混用是很容易发生的，所以这里的取值是独立的常量，且 `annotate()` 只接受本模块
自己的那套。

## 四、证据必须能追溯到具体的数

「任一指标的质量证据入库可查」是验收标准。所以 `QualityAnnotation` 存的不是一个等级，
而是**得出那个等级所依据的每一个量**：样本量、同步质量、零速质量、算的是哪条链、
用的是哪一版规则。

只存等级的后果很具体：三个月后有人问"这个 low 是因为步数少还是因为同步差"，而那份
报告已经答不上来了。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final

#: 分级规则的版本。**进报告页脚**（PRD §13）。
#:
#: 规则一变，此前产出的报告与此后的就不可直接比较 —— 而"不可比较"必须看得见。
RULES_VERSION: Final[str] = "1.0"

#: PRD §13 的三档。**与 `contracts.Confidence` 不是同一套**，见模块文档 §3。
GRADE_NORMAL: Final[str] = "normal"
GRADE_LOW: Final[str] = "low"
GRADE_UNCOMPUTABLE: Final[str] = "uncomputable"
GRADES: Final[tuple[str, ...]] = (GRADE_NORMAL, GRADE_LOW, GRADE_UNCOMPUTABLE)

#: 越大越差。汇总取最差的一项。
_SEVERITY: Final[dict[str, int]] = {
    GRADE_NORMAL: 0,
    GRADE_LOW: 1,
    GRADE_UNCOMPUTABLE: 2,
}

#: 计算链。基础链在采集端跑，完整链在云端跑 —— **两者共用本模块**。
CHAIN_BASIC: Final[str] = "basic"
CHAIN_FULL: Final[str] = "full"
CHAINS: Final[tuple[str, ...]] = (CHAIN_BASIC, CHAIN_FULL)

#: 埋点名。PRD §13 要求 `metric_low_grade`。
TELEMETRY_EVENT: Final[str] = "metric_low_grade"

#: 样本量低于此判 `low`。与 `analysis/variability.MIN_STEPS_FOR_CV` 是同一个数，
#: 但**故意不 import 它** —— 那个门槛问的是"CV 分得开一倍差异吗"，这个门槛问的是
#: "这个指标该不该标 low"。今天数值相同是巧合，把它们绑在一起会让改动其一时悄悄
#: 改动其二。
MIN_STEPS_FOR_NORMAL: Final[int] = 16


class QualityError(ValueError):
    """质量标注的输入非法。"""


@dataclass(frozen=True)
class GateMatrix:
    """门控矩阵。**默认全部不启用**（FR-510 语义），见模块文档 §2。

    每一项为真表示"该情形下拦截该指标"。v1 不用它，但它的位置是确定的 —— 产品要开
    门控时，改的是这里，不是散在各处的 if。
    """

    block_uncomputable: bool = False
    block_low: bool = False
    block_missing_sync: bool = False

    @property
    def any_enabled(self) -> bool:
        return self.block_uncomputable or self.block_low or self.block_missing_sync

    @property
    def rules_version(self) -> str:
        """启用任何一项都产生新的规则版本，理由见模块文档 §2。"""
        if not self.any_enabled:
            return RULES_VERSION
        flags = "".join(
            "1" if value else "0"
            for value in (self.block_uncomputable, self.block_low, self.block_missing_sync)
        )
        return f"{RULES_VERSION}+gate.{flags}"

    def snapshot(self) -> dict[str, Any]:
        return {
            "block_uncomputable": self.block_uncomputable,
            "block_low": self.block_low,
            "block_missing_sync": self.block_missing_sync,
            "any_enabled": self.any_enabled,
            "rules_version": self.rules_version,
        }


@dataclass(frozen=True)
class QualityAnnotation:
    """一项指标的质量证据与等级。

    存的是**得出等级所依据的每一个量**，不只是等级 —— 见模块文档 §4。
    """

    metric: str
    n_steps: int
    #: 同步质量快照。跨足指标必须有；足内指标可以是 `None`。
    sync_quality: dict[str, Any] | None
    #: 零速检测质量快照（RAY-203 的 `StanceDetection.confidence` 等）。
    zupt_quality: dict[str, Any] | None
    chain: str
    grade: str
    #: 得出这个等级的具体理由，按判定顺序。空表示 `normal`。
    reasons: list[str] = field(default_factory=list)
    rules_version: str = RULES_VERSION

    @property
    def blocked(self) -> bool:
        """是否被门控拦截。**v1 恒为假** —— 门控默认全不启用。"""
        return False

    @property
    def telemetry(self) -> dict[str, Any] | None:
        """`metric_low_grade` 的载荷；`normal` 时为 `None`。"""
        if self.grade == GRADE_NORMAL:
            return None
        return {
            "event": TELEMETRY_EVENT,
            "metric": self.metric,
            "grade": self.grade,
            "reasons": list(self.reasons),
            "n_steps": self.n_steps,
            "chain": self.chain,
            "rules_version": self.rules_version,
        }

    def snapshot(self) -> dict[str, Any]:
        return {
            "metric": self.metric,
            "n_steps": self.n_steps,
            "sync_quality": dict(self.sync_quality) if self.sync_quality else None,
            "zupt_quality": dict(self.zupt_quality) if self.zupt_quality else None,
            "chain": self.chain,
            "grade": self.grade,
            "reasons": list(self.reasons),
            "rules_version": self.rules_version,
        }


def annotate(
    metric: str,
    *,
    n_steps: int,
    chain: str,
    cross_foot: bool = False,
    sync_quality: Mapping[str, Any] | None = None,
    zupt_quality: Mapping[str, Any] | None = None,
    computable: bool = True,
) -> QualityAnnotation:
    """给一项指标定级。**这是端云共用的唯一实现点。**

    判定顺序是有意的，因为 `reasons` 按顺序记录：先问"算得出来吗"，再问"证据够吗"。
    一个算不出来的指标不必再谈它的样本量。

    `cross_foot` 为真而 `sync_quality` 缺失时判 `low` 而不是抛错 —— 缺同步标注是一个
    **质量问题**，不是调用错误。抛错会让整份报告失败，而 PRD §13 的原则正是"不做门控"。
    """
    if chain not in CHAINS:
        raise QualityError(f"chain 应为 {CHAINS} 之一，收到 {chain!r}")
    if n_steps < 0:
        raise QualityError(f"n_steps 不得为负，收到 {n_steps}")
    if not metric:
        raise QualityError("metric 名不能为空")

    reasons: list[str] = []
    grade = GRADE_NORMAL

    if not computable or n_steps == 0:
        grade = GRADE_UNCOMPUTABLE
        reasons.append("not_computable" if not computable else "no_steps")
    else:
        if n_steps < MIN_STEPS_FOR_NORMAL:
            grade = GRADE_LOW
            reasons.append(f"few_steps:{n_steps}<{MIN_STEPS_FOR_NORMAL}")
        if cross_foot and not sync_quality:
            grade = GRADE_LOW
            reasons.append("missing_sync_quality")
        elif cross_foot and sync_quality and not sync_quality.get("determinate", True):
            grade = GRADE_LOW
            reasons.append("sync_indeterminate")
        elif cross_foot and sync_quality and sync_quality.get("flagged"):
            grade = GRADE_LOW
            reasons.append("sync_flagged")
        if zupt_quality and zupt_quality.get("degraded_fraction", 0.0) > 0.25:
            grade = GRADE_LOW
            reasons.append("zupt_degraded")

    return QualityAnnotation(
        metric=metric,
        n_steps=n_steps,
        sync_quality=dict(sync_quality) if sync_quality else None,
        zupt_quality=dict(zupt_quality) if zupt_quality else None,
        chain=chain,
        grade=grade,
        reasons=reasons,
    )


def apply_gates(
    annotations: list[QualityAnnotation], gates: GateMatrix | None = None
) -> tuple[list[QualityAnnotation], list[str]]:
    """按门控矩阵筛选。返回 `(留下的, 被拦的指标名)`。

    **默认全不启用，此时它是恒等函数**（FR-510 语义）—— 传进去什么就返回什么，被拦
    列表为空。这一点有测试守着：门控的"默认关"必须是可验证的行为，不是一句注释。
    """
    gates = gates or GateMatrix()
    if not gates.any_enabled:
        return list(annotations), []

    kept: list[QualityAnnotation] = []
    blocked: list[str] = []
    for item in annotations:
        if gates.block_uncomputable and item.grade == GRADE_UNCOMPUTABLE:
            blocked.append(item.metric)
            continue
        if gates.block_low and item.grade == GRADE_LOW:
            blocked.append(item.metric)
            continue
        if gates.block_missing_sync and "missing_sync_quality" in item.reasons:
            blocked.append(item.metric)
            continue
        kept.append(item)
    return kept, blocked


@dataclass(frozen=True)
class QualityFooter:
    """报告页脚（PRD §13：`grade` 汇总规则版本化，进报告页脚）。"""

    rules_version: str
    chain: str
    metrics: int
    normal: int
    low: int
    uncomputable: int
    gates_enabled: bool

    @property
    def overall(self) -> str:
        """整体等级取最差的一项 —— 一个可用的指标救不了一个不可用的。"""
        if self.uncomputable:
            return GRADE_UNCOMPUTABLE
        if self.low:
            return GRADE_LOW
        return GRADE_NORMAL

    def snapshot(self) -> dict[str, Any]:
        return {
            "rules_version": self.rules_version,
            "chain": self.chain,
            "metrics": self.metrics,
            "normal": self.normal,
            "low": self.low,
            "uncomputable": self.uncomputable,
            "gates_enabled": self.gates_enabled,
            "overall": self.overall,
        }


def summarize(
    annotations: list[QualityAnnotation], *, gates: GateMatrix | None = None
) -> QualityFooter:
    """汇总成报告页脚。"""
    if not annotations:
        raise QualityError("没有任何指标标注，页脚无从谈起")
    gates = gates or GateMatrix()
    chains = {item.chain for item in annotations}
    if len(chains) != 1:
        raise QualityError(
            f"一份报告里出现了多条计算链 {sorted(chains)}。"
            "基础链与完整链的结果不可混在同一份页脚里 —— 页脚要回答的是"
            "'这份报告是怎么算出来的'，两条链就是两个答案。"
        )
    counts = {grade: 0 for grade in GRADES}
    for item in annotations:
        counts[item.grade] += 1
    return QualityFooter(
        rules_version=gates.rules_version,
        chain=next(iter(chains)),
        metrics=len(annotations),
        normal=counts[GRADE_NORMAL],
        low=counts[GRADE_LOW],
        uncomputable=counts[GRADE_UNCOMPUTABLE],
        gates_enabled=gates.any_enabled,
    )


def worst(grades: list[str]) -> str:
    """一组等级里最差的那个。"""
    if not grades:
        return GRADE_UNCOMPUTABLE
    unknown = set(grades) - set(GRADES)
    if unknown:
        raise QualityError(f"未知的等级 {sorted(unknown)}；可选 {GRADES}")
    return max(grades, key=lambda item: _SEVERITY[item])
