"""报告里所有面向客户的措辞。集中在一处，因为它们受同一批规则约束。

## 为什么措辞值得单独一个模块

PRD §12 对措辞立了几条硬规矩，而它们**不是风格偏好**：

* 不用「确诊、患有、治疗方案」—— 这是一台筛查设备，不是诊断设备。用了诊断措辞，
  它在监管上的定性就变了，而那不是一句文案能承担的后果。
* `low` 的指标要附一句通俗说明（「本次有效步数较少，此项仅供参考」）；
* 算不出来的指标显示「本次不适用」+ 通俗原因，**不显示空白、0 或 N/A**。

散落在拼字符串的地方，这三条就只能靠人记得。放在一处，它们可以被一条测试逐字检查
（见 `tests/test_basic_report.py` 的措辞断言）。

## 为什么「不适用」的原因要从 reasons 翻译

`quality.annotate` 给的 `reasons` 是机器可读的（`few_steps:12<16`、`not_computable`），
面向的是排障与遥测。直接印到客户报告上，读者会看到一串代码。所以这里做一次翻译，
而**翻译表是穷举的**：遇到没见过的 reason 就说「本次不适用」而不编一个原因 ——
编出来的原因比没有原因更糟，它会让人以为自己知道发生了什么。
"""

from __future__ import annotations

from typing import Final

from gait.quality.annotate import GRADE_LOW, GRADE_NORMAL, GRADE_UNCOMPUTABLE

#: 指标算不出来时的占位。PRD §12：不显示空白、0 或 N/A。
NOT_APPLICABLE: Final[str] = "本次不适用"

#: `low` 的通用说明。PRD §12 给了原话。
LOW_NOTE: Final[str] = "本次有效步数较少，此项仅供参考。"

#: 诊断措辞黑名单。测试逐条扫整份报告 —— 这不是提醒，是拦截。
FORBIDDEN_WORDS: Final[tuple[str, ...]] = (
    "确诊",
    "患有",
    "治疗方案",
    "疾病",
    "诊断为",
)

#: 机器可读的 reason → 客户能看懂的原因。**穷举**，见模块文档。
_REASON_TEXT: Final[dict[str, str]] = {
    "not_computable": "本次协议不产出该项。",
    "no_steps": "本次没有采到有效步。",
    "missing_sync": "本次两侧同步证据不足。",
}


def reason_text(reasons: list[str]) -> str:
    """把一组机器 reason 翻成一句给客户看的原因。

    只翻第一条：`reasons` 按判定顺序记录，第一条就是最先把它判下来的那个。把三条
    理由并排印给客户，读者要自己判断哪条要紧 —— 而那正是这句话该替他做的事。
    """
    for reason in reasons:
        head = reason.split(":", 1)[0]
        if head in _REASON_TEXT:
            return _REASON_TEXT[head]
        if head == "few_steps":
            return "本次有效步数不足以给出这一项。"
    # 没见过的 reason 不编原因 —— 编出来的比没有更糟。
    return "本次无法给出这一项。"


def quality_label(grade: str) -> str:
    """质量三级在报告上的中文标。"""
    return {
        GRADE_NORMAL: "良好",
        GRADE_LOW: "参考",
        GRADE_UNCOMPUTABLE: NOT_APPLICABLE,
    }[grade]


def metric_note(grade: str, reasons: list[str]) -> str | None:
    """一项指标要不要附说明，附什么。`normal` 不附 —— 没话说就别说话。"""
    if grade == GRADE_NORMAL:
        return None
    if grade == GRADE_LOW:
        return LOW_NOTE
    return reason_text(reasons)
