"""六域错误码，以及「错误文案只有一个来源」这条约束的落点。

## 为什么文案住在 Python 侧

RAY-248 的验收写明：**渲染进程不得自造错误文案** —— 文案与错误码同源于 sidecar。
理由不是洁癖。同一个现象在两处各写一遍文案，两份会分头漂移，而漂移的那一天正是
操作员照着界面上的话去做、却做了错事的那一天。更具体地说：`E-BLE-1004`（电量读
不到）与 `E-BLE-1002`（电量不足）在界面上长得像，**要做的事完全不同** —— 一个去
查连接，一个去换电池。`device/orchestration.py` 已经把这个区分写进了 `problems`
的措辞里；渲染端若另写一句「电量异常」，那个区分就没了。

所以本模块**不导出任何供前端拼装的模板**，只导出成品句子。前端拿到的是可以直接
显示的三段：现象、动作、码。

## 为什么是「现象 + 动作 + 码」三个字段而不是一整句

UI 设计 §7 定的统一格式是「一个现象 + 一个动作 + 一个错误码」。拆成三个字段，是
因为三处的排版不同：P-05 的 `ChecklistItem` 把动作放在第二行，P-08 的整页接管把
现象放大、动作做成按钮旁的说明。**拆分让排版自由，拼接让排版写死**；而拆分并不给
前端自造文案的空间 —— 三段都是这里给的，前端只决定它们出现在哪。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Final

CONTRACT_PATH: Final[Path] = Path(__file__).with_name("contract.json")


class ContractError(ValueError):
    """契约本身被违反 —— 未知的码、越界的编号、不认识的域。"""


@lru_cache(maxsize=1)
def contract() -> dict[str, Any]:
    """读那一份契约事实。

    JS 侧 import 的是**同一个文件**（`packages/terminal-contract/`），不是它的副本。
    抄一份过去就等于承认两边会不一样，而两边不一样的第一个征兆通常是线上某个码
    在前端显示成空白。
    """
    return json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))


def domains() -> dict[str, dict[str, Any]]:
    return dict(contract()["error_domains"])


def check_code(code: str) -> str:
    """确认 `code` 是一个本契约认得的码，并且编号落在它自己域的区间里。

    区间检查不是多余的：域前缀写对、编号却借用了别的域的号段，是一个不会报错也不会
    被人看出来的错误 —— 直到有人按号段去查日志。
    """
    if not isinstance(code, str):
        raise ContractError(f"错误码必须是字符串，收到 {type(code).__name__}")
    parts = code.split("-")
    if len(parts) != 3 or parts[0] != "E":
        raise ContractError(f"错误码格式应为 E-<域>-<编号>，收到 {code!r}")
    domain = f"E-{parts[1]}"
    known = domains()
    if domain not in known:
        raise ContractError(f"未知错误域 {domain!r}；六域为 {sorted(known)}")
    try:
        number = int(parts[2])
    except ValueError as exc:
        raise ContractError(f"错误码编号不是整数：{code!r}") from exc
    low, high = known[domain]["range"]
    if not low <= number <= high:
        raise ContractError(
            f"{code} 的编号 {number} 不在 {domain} 的号段 {low}–{high} 内"
        )
    if code not in contract()["codes"]:
        raise ContractError(
            f"{code} 未登记在 contract.json 的 codes 里。"
            "新码要先登记再使用 —— 未登记的码在前端只能显示成一个数字。"
        )
    return code


@dataclass(frozen=True, slots=True)
class TerminalError:
    """一个跨 IPC 的错误。三个字段都是成品文案，前端只排版不改写。"""

    code: str
    message: str
    action: str
    blocking: bool = True

    def __post_init__(self) -> None:
        check_code(self.code)
        if not self.message.strip():
            raise ContractError(f"{self.code} 缺少现象描述")
        if not self.action.strip():
            raise ContractError(
                f"{self.code} 缺少可执行动作。"
                "只说出了什么事、不说该做什么的错误，对操作员等于没说。"
            )

    @property
    def domain(self) -> str:
        return "E-" + self.code.split("-")[1]

    def snapshot(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "domain": self.domain,
            "message": self.message,
            "action": self.action,
            "blocking": self.blocking,
        }
