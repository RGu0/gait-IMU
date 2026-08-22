"""质量分级红线检查：渲染进程不得复算质量分级（RAY-218 的 R-3）。

来源见《UI布局设计 v0.2》§11.2 与 PRD v1.2 §13。红线的意义是让质量逻辑只有**一处**
实现：Windows 采集端的基础链、云端重算的完整链、CLI/回放三个宿主调同一份
`gait.quality`。

破坏它的后果不是"不一致"这么轻 —— 是**同一次采集在采集端显示 normal、在报告里显示
low**，而两个数字都出自我们的系统，用户没有任何办法判断该信哪个。

最容易的破坏方式很具体，也很有诱惑力：前端为了"显示得快"，照着阈值在本地算一遍
`low`。所以这个检查针对的正是那种写法。

## 这个检查能抓什么、抓不到什么

**能抓**：

1. 渲染进程里出现质量阈值常量的名字或取值（`MIN_STEPS_FOR_NORMAL` 及其数值）。
2. 在同一条语句里既有关系运算符、又赋出/返回一个等级字面量 —— 那是"由比较得出等级"
   的签名。

**抓不到**：把阈值拆成两半再拼、从后端拿一个数再自己比、或者干脆用别的词命名等级。
所以它是一道**防手滑**的闸，不是一个证明。这一点必须写明，否则它会被当成比实际更强
的保证 —— 而那种误解比没有检查更危险。

**它会误伤，而且方向是刻意的。** 像 `{items.length > 0 && <Badge grade="normal" />}`
这样的一行会被拦下来 —— 那里并没有复算分级，只是同一行里恰好既有比较又有等级字面量。
误伤会当场暴露并被讨论（改一行、拆成两行，或者在这里加一条豁免）；漏过则不会，而漏
过的那一天正是这个检查该拦没拦住的那一天。与 `io/session.py` 那道身份明文检查同一个
取舍。

## 为什么它在渲染进程还不存在的时候就要有

红线检查必须**早于**它守护的代码。等 `apps/terminal/` 写好再补，那时已经有一份实现
需要"顺手"迁走，而顺手的事往往就不做了。现在加，第一行渲染代码落地时它就在。

目录不存在时检查通过并明说"没有可扫描的渲染进程代码"—— 静默通过与"扫过了没问题"
看起来一样，而两者是不同的结论。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from gait.quality.annotate import GRADES, MIN_STEPS_FOR_NORMAL

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")

REPO_ROOT = Path(__file__).resolve().parent.parent

#: 渲染进程的代码在哪。`packages/` 是设计系统，`apps/` 是采集端 —— 两处都不该有
#: 质量逻辑。sidecar（Python）不在此列，它**就是**唯一实现点。
RENDERER_ROOTS: tuple[str, ...] = ("apps", "packages")
SOURCE_SUFFIXES: tuple[str, ...] = (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs")

#: 阈值常量的名字与取值。名字来自 `gait.quality`，不在这里抄第二份定义。
_FORBIDDEN_NAMES: tuple[str, ...] = ("MIN_STEPS_FOR_NORMAL",)

#: 「由比较得出等级」的签名：同一条语句里既有关系运算符、又有等级字面量。
_RELATIONAL = re.compile(r"[<>]=?|>=|<=")
_GRADE_LITERAL = re.compile(
    r"""['"`](?:""" + "|".join(re.escape(grade) for grade in GRADES) + r""")['"`]"""
)


def _sources(root: Path) -> list[Path]:
    found: list[Path] = []
    for name in RENDERER_ROOTS:
        directory = root / name
        if not directory.is_dir():
            continue
        for path in sorted(directory.rglob("*")):
            if path.suffix not in SOURCE_SUFFIXES:
                continue
            if "node_modules" in path.parts or path.name.endswith(".d.ts"):
                continue
            found.append(path)
    return found


def scan(root: Path) -> tuple[list[str], int]:
    """返回 `(违规行, 扫描的文件数)`。"""
    offences: list[str] = []
    files = _sources(root)
    for path in files:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith(("//", "*")):
                continue
            for name in _FORBIDDEN_NAMES:
                if name in line:
                    offences.append(f"{path}:{number}: 出现质量阈值常量 {name}")
            if re.search(rf"\b{MIN_STEPS_FOR_NORMAL}\b", line) and _GRADE_LITERAL.search(line):
                offences.append(
                    f"{path}:{number}: 同一行里既有阈值 {MIN_STEPS_FOR_NORMAL} 又有等级字面量"
                )
            if _RELATIONAL.search(line) and _GRADE_LITERAL.search(line):
                offences.append(f"{path}:{number}: 由比较得出等级（渲染进程不得复算分级）")
    return offences, len(files)


def main() -> int:
    offences, scanned = scan(REPO_ROOT)
    if offences:
        print("质量分级红线被破坏：", file=sys.stderr)
        for line in offences:
            try:
                head, rest = line.split(":", 1)
                line = f"{Path(head).relative_to(REPO_ROOT)}:{rest}"
            except ValueError:
                pass
            print(f"  {line}", file=sys.stderr)
        print(
            "\n渲染进程只渲染 sidecar 通过 IPC 给出的 grade，不得复算 —— "
            "复算会让质量逻辑有第二实现，端云同构失效。"
            "依据见《UI布局设计 v0.2》§11.2 与 PRD v1.2 §13。",
            file=sys.stderr,
        )
        return 1

    if scanned == 0:
        # 明说没扫到东西。静默通过与"扫过了没问题"看起来一样，而两者是不同的结论。
        print(
            "质量分级红线检查通过：暂无可扫描的渲染进程代码"
            f"（{', '.join(RENDERER_ROOTS)}/ 下没有 {'/'.join(SOURCE_SUFFIXES)} 源文件）"
        )
        return 0
    print(f"质量分级红线检查通过：{scanned} 个渲染进程源文件里未发现复算质量分级")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
