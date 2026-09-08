"""报告装配的**链侧入口**：`ChainResult` + `SessionMeta` → `build_report` 的入参。

## 这里从前有第二个装配层，现在没有了（RAY-395）

本模块原本自己把一条链的结果摊成模板要的那个 dict —— 与 `report/basic.py` 的
`build_report` 并列，两份装配逻辑，同一份模板。RAY-395 的实测把代价摆了出来：同一个
指标在两边键名不同（`ds` / `double-support`）、同一个量一边进核心区一边进专业参数、
`build_report` 有的**步长**与 `qualityFooter` 这边没有、这边有的站立相/摆动相占比与
转身次数那边没有。没有哪一份是「对的」，它们只是**分别漂移**了。

所以现在只有一个装配层，在 `report/basic.py::build_report`；本模块只做**翻译**：
把 `ChainResult` 与 `SessionMeta` 拆成那个函数的入参。判断口径的地方一处也不留在
这里 —— 留一处，它就会再漂一次。

## 为什么宿主是 `build_report` 而不是本模块

`ChainResult` 装不下采集端手里的东西：它的 `FootOutcome` 要 `NavResult` 与
`SegmentationReport`，而 sidecar 的 `reportFor` 手里只有一串 `GaitCycle`。把
`ChainResult` 定成规范输入，等于让采集端**造一个 `NavResult`** 才能出报告 ——
而 `_zupt_quality()` 会去读它，于是采集端就替自己编了一份从没算过的零速质量证据。
那正是 `honest-sync-quality`（RAY-395 第二条指控）刚修掉的那类事，只是换了一层。

反过来，`list[GaitCycle]` 是两条路都真拿得到的东西：链侧从 `feet[*].selected` 里
取，采集端本来就是它。所以规范输入是周期列表，本模块负责把链拆成它。

## 拆下来会丢什么，以及为什么只丢这一样

* **双支撑期不丢。** `build_report` 按**手里有没有同步依据**在两个估计量之间选，
  而不是按调用方选（见那边的 `_double_support`）。链侧带着 `sync_quality` 过去，
  于是照样走 `events.double_support` 的相位重叠口径 —— 与 `chain.double_support`
  同一个函数、同一批周期、同一份同步证据，得同一个数。
* **逐足 ZUPT 证据丢。** `ChainResult` 的零速质量是**按脚**的，而拉齐后的核心指标
  是双足合并的一项（`speed` 一项，不是 `gait_speed.L` 与 `gait_speed.R` 两项），
  一项指标只能挂一份 `zupt_quality`。把两只脚的合成一份需要一条「取哪只」的规则，
  而那是一条**质量规则**，按 FR-08 只能实现在 `gait/quality/`，不能写在报告层。
  所以这里传 `None`：报告如实说「这一项没有零速质量依据」，而不是挂一份来路不明的。
  逐足证据仍然在 `ChainResult.annotations` 里，没有消失，只是不进这份 payload。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from gait.cloud.chain import ChainResult
from gait.config import DEFAULT_DURATION_S
from gait.contracts import SessionMeta
from gait.report.basic import build_report


class AssembleError(ValueError):
    """报告装配的输入不完整。

    保留是为了兼容既有的 `except` 子句；周期为空时实际抛的是
    `report.basic.ReportError`（「会话级无效不生成报告」，PRD §13）。
    """


#: 跨足指标的名字。同步质量是随它一起被记下来的，所以要取回同步依据就问它。
_CROSS_FOOT_METRIC = "double_support_ratio"


def _sync_quality(chain: ChainResult) -> dict[str, Any] | None:
    """这条链当初拿到的同步质量。

    `ChainResult` 没有单独存它，但 `_annotate_all` 一定会为 `double_support_ratio`
    留一条标注，而那条标注里存着当时用的那份 `sync_quality`（`QualityAnnotation`
    存的是「得出等级所依据的每一个量」）。从那里取回来，而不是让调用方再传一次 ——
    再传一次就有传成另一份的可能，而那份会与链里已经定好的等级对不上。
    """
    for annotation in chain.annotations:
        if annotation.metric == _CROSS_FOOT_METRIC:
            return dict(annotation.sync_quality) if annotation.sync_quality else None
    return None


def assemble_report(
    chain: ChainResult,
    meta: SessionMeta,
    *,
    subject_label: str | None = None,
    organization: str = "",
    protocol_name: str = "定时步行测试",
    report_id: str | None = None,
) -> dict[str, Any]:
    """把一条链的结果与一份会话元数据交给唯一的装配层。

    入参不要求完整：`meta` 只用到 `subject_uuid` / `created_at` / `session_id` /
    `protocol_config` / `contract_version`。
    """
    cycles = [cycle for label in sorted(chain.feet) for cycle in chain.feet[label].selected]
    seconds = int(meta.protocol_config.get("duration_s", DEFAULT_DURATION_S))
    protocol_version = meta.protocol_config.get("version", meta.contract_version)

    return build_report(
        cycles,
        report_id=report_id or f"R-{meta.session_id[:8]}-{meta.session_id[-4:]}",
        organization=organization,
        subject_label=subject_label or f"**{meta.subject_uuid[:4]}",
        assessed_at=(meta.created_at or "")[:10] or datetime.now(UTC).strftime("%Y-%m-%d"),
        duration_s=seconds,
        algo_version=chain.algo_version,
        protocol_version=f"T-01 v{protocol_version}",
        # 链手里没有有效时长（那是采集端的账），传 None 让「测试条件」写「未记录」。
        valid_seconds=None,
        # 没有变异性报告时转身次数是**没数过**，不是 0 —— 见 `build_parameters`。
        turns=chain.variability.turns if chain.variability else None,
        protocol_name=protocol_name,
        chain=chain.chain,
        sync_quality=_sync_quality(chain),
        # 逐足零速证据合不成一份，见模块文档。
        zupt_quality=None,
    )
