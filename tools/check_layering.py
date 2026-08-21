"""分层红线检查：`gait.core` 不得 import `gait.io`、`gait.device`、`gait.sync`。

来源见 `documents/Ref/模块划分与接口契约.md` §2 与 PRD v1.2 §14。红线的意义是让
`core/` 保持纯函数库 —— 能在 CLI、Windows 采集端、云端重算三个宿主里跑同一份代码，
也能把外部数据集直接喂进来。一旦 `core/` 认识了文件系统或 BLE，这个性质就没了，
而且是悄悄没的：代码照常跑，只是再也搬不动。

**禁止清单不写在这里**，读自 `gait.CORE_FORBIDDEN_IMPORTS`。`package-skeleton` 已经
把它作为分层事实的唯一声明；在这里再抄一份，两处迟早对不上，而对不上的那一天正是
检查该拦没拦住的那一天。

用 ast 解析而不是正则或子串匹配。同一个 Issue 里已经出现过一次子串检查的教训：
`"import gait" not in source` 对 `from gait import core` 返回真 —— 恰恰是要禁止的
那种写法能整个绕过。四种拼法都要覆盖：

    import gait.io                 -> Import,     alias.name = "gait.io"
    from gait.io import x          -> ImportFrom, module = "gait.io"
    from gait import io            -> ImportFrom, module = "gait", alias.name = "io"
    from ..io import x             -> ImportFrom, level = 2, module = "io"

扫描的根与层名都是参数而不是写死的常量，这样测试能在临时目录里造出真实的违规。
对着真实的 `core/` 跑只能证明"现在没有违规"，证明不了"有违规时会失败"—— 而后者
才是这个检查存在的理由。
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import gait

REPO_ROOT = Path(__file__).resolve().parent.parent
PACKAGE_ROOT = REPO_ROOT / "src" / "gait"
GUARDED_LAYER = "core"


def _imported_modules(tree: ast.AST, parts: tuple[str, ...]) -> list[tuple[int, str]]:
    """(行号, 被导入模块的绝对点分名)，相对导入已按所在包解析为绝对名。"""
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.append((node.lineno, alias.name))
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                # level 1 指本包，level 2 指上一层，依此类推。
                base = parts[: len(parts) - (node.level - 1)] if node.level > 1 else parts
                target = list(base) + (node.module.split(".") if node.module else [])
            else:
                target = node.module.split(".") if node.module else []
            if target:
                found.append((node.lineno, ".".join(target)))
            # `from gait import io`：被导入的名字本身就是层。
            for alias in node.names:
                found.append((node.lineno, ".".join(target + [alias.name])))
    return found


def scan(package_root: Path, layer: str, forbidden: set[str]) -> list[str]:
    """`package_root/layer` 下所有 .py 中触碰 `forbidden` 的位置。

    `forbidden` 是绝对点分名的集合，例如 {"gait.io", "gait.device"}。
    """
    reported: list[str] = []
    # 同一行的同一个违规只报一次。`from wt901 import ImuSample` 会同时产出
    # "wt901" 与 "wt901.ImuSample"，两者都命中单段的 "wt901" —— 报两遍不会让
    # 检查更严，只会让它看起来坏了。
    seen: set[tuple[str, int, str]] = set()
    for path in sorted((package_root / layer).rglob("*.py")):
        parts = path.relative_to(package_root.parent).parts[:-1]
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for lineno, name in _imported_modules(tree, parts):
            segments = name.split(".")
            # 同时比对首段与前两段。本包内的层要两段（gait.io），第三方包只有
            # 一段（bleak）；而 `from wt901.models import X` 的前两段是
            # wt901.models，只看前两段会整个漏掉它。
            candidates = {segments[0], ".".join(segments[:2])}
            hits = candidates & forbidden
            if hits:
                # 取最长的那个匹配：报 gait.io 比报 gait 有用。
                offender = max(hits, key=len)
                key = (str(path), lineno, offender)
                if key in seen:
                    continue
                seen.add(key)
                reported.append(
                    f"{path}:{lineno}: {package_root.name}.{layer} 不得 import {offender}"
                )
    return reported


def declared_forbidden() -> set[str]:
    """禁止清单，来自包自身的两处声明，并校验层清单没有和 LAYERS 漂移。

    两个来源分列是因为它们的失效方式不同：本包内的层写错会与 ``LAYERS`` 漂移，
    可以被校验出来；第三方包名写错只会让检查静默失效，没有可比对的第二处真相。
    """
    unknown = set(gait.CORE_FORBIDDEN_IMPORTS) - set(gait.LAYERS)
    if unknown:
        # 禁止清单里出现了不存在的层，说明两个声明已经漂移。此时"没有违规"
        # 是无意义的结论，必须当成失败而不是通过。
        raise ValueError(f"CORE_FORBIDDEN_IMPORTS 含有不在 LAYERS 中的层: {sorted(unknown)}")
    layers = {f"gait.{name}" for name in gait.CORE_FORBIDDEN_IMPORTS}
    return layers | set(gait.CORE_FORBIDDEN_PACKAGES)


def main() -> int:
    try:
        forbidden = declared_forbidden()
    except ValueError as error:
        print(f"分层声明自相矛盾：{error}", file=sys.stderr)
        return 1

    reported = scan(PACKAGE_ROOT, GUARDED_LAYER, forbidden)
    if reported:
        print("分层红线被破坏：", file=sys.stderr)
        for line in reported:
            try:
                shown = Path(line.split(":", 1)[0]).relative_to(REPO_ROOT)
                line = f"{shown}:{line.split(':', 1)[1]}"
            except ValueError:
                pass
            print(f"  {line}", file=sys.stderr)
        print(
            f"\n{GUARDED_LAYER}/ 必须保持纯函数库；禁止清单见 gait.CORE_FORBIDDEN_IMPORTS，"
            "依据见《模块划分与接口契约》§2。",
            file=sys.stderr,
        )
        return 1

    allowed = ", ".join(sorted(forbidden))
    print(f"分层红线检查通过：gait.{GUARDED_LAYER} 未 import {allowed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
