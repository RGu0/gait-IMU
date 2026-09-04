"""`report/wording` 的翻译表必须覆盖 `quality/annotate` 真正产出的每一个 reason。

RAY-395：第一版这张表里写的是 `"missing_sync"`，而 `annotate` 产出的是
`"missing_sync_quality"` —— **那个键从来没有匹配过任何东西**。它看着像在覆盖同步
那一类，实际一直落到兜底句上；另外三个 sync/zupt 的 reason 则根本没有条目。

这件事**不会报错**：兜底句是安全的（不编原因），所以没有任何测试会红，而表看起来
是穷举的。抄错一个字母就让一整类原因说不出话，且没人知道 —— 正是这条测试要拦的。

判据直接从 `annotate.py` 的源码里抓 `reasons.append(...)` 的字面量，**不抄一份清单**：
抄的那份会在 `annotate` 加了新 reason 之后继续通过，而那正是漏译的来源。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from gait.report import wording

ANNOTATE = Path(wording.__file__).resolve().parents[1] / "quality" / "annotate.py"


def _emitted_reasons() -> set[str]:
    """`annotate.py` 里 `reasons.append(...)` 能产出的 reason 头部。

    两种字面量都要认：直接的字符串常量，以及 f-string（`few_steps:{n}<{m}`）——
    后者取 `:` 之前那一段，与 `reason_text` 的切法一致。
    """
    tree = ast.parse(ANNOTATE.read_text(encoding="utf-8"))
    heads: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "append"):
            continue
        if not (isinstance(func.value, ast.Name) and func.value.id == "reasons"):
            continue
        for argument in node.args:
            heads |= _heads_of(argument)
    return heads


def _heads_of(node: ast.AST) -> set[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return {node.value.split(":", 1)[0]}
    if isinstance(node, ast.IfExp):  # `"a" if cond else "b"`
        return _heads_of(node.body) | _heads_of(node.orelse)
    if isinstance(node, ast.JoinedStr):  # f-string：取第一段常量的 `:` 之前
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                return {value.value.split(":", 1)[0]}
    return set()


def test_the_scraper_actually_finds_something():
    """先钉住取证手段本身 —— 一个永远返回空集的扫描器也能让下面那条通过。"""
    reasons = _emitted_reasons()
    assert len(reasons) >= 5, f"只从 annotate.py 抓到 {reasons}，正则/AST 该更新了"
    assert "not_computable" in reasons
    assert "missing_sync_quality" in reasons


@pytest.mark.parametrize("reason", sorted(_emitted_reasons()))
def test_every_reason_annotate_can_emit_has_its_own_text(reason: str) -> None:
    """每一个 reason 都要有自己的译文，不能落到兜底句上。

    兜底句（「本次无法给出这一项。」）是**安全**的 —— 它不编原因 —— 但它也什么都
    没说。一个已知的 reason 落到它上面，等于报告放弃解释一件它其实解释得了的事。
    """
    text = wording.reason_text([reason])
    assert text != wording.reason_text(["某个不存在的_reason"]), (
        f"reason {reason!r} 没有译文，落到了兜底句上 —— "
        "`_REASON_TEXT` 的键要与 annotate 产出的字符串逐字对上"
    )
