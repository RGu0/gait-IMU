"""红线检查：本仓库任何路径都不得触及 wt901 的**标定通道**。

依据 PRD **FR-03**：标定参数不写回模块寄存器，全部补偿在上位机完成，模块保持出厂
原始态。而 wt901 的标定通道（`device.calibration` / `Calibration` / `CalibrationMode`
/ 任何写 `Register.CALSW` 的路径）做的正是相反的事 —— `calibrate_acceleration()` 的
实现就是 `registers.write(Register.CALSW, CalibrationMode.ACCELERATION)`，它改的是
**模块内部状态**。

## 为什么这条要做成检查，而不是写进文档就够

三条叠在一起，缺一条都不至于要一道闸：

1. **调用它的人多半是想做正事。** 那个 API 就叫「加计校准」，而 `calib/accel.py`
   做的就是加计标定 —— 下一个实现者看到它会觉得正好。
2. **用错了不报错。** 模块把当时的姿态固化成「水平」，之后所有角度一直偏，数据里
   看不出异常。
3. **事后查不出来。** wt901 自己的模块文档写明 `0x01` 是**只写**寄存器，重连后设备
   的校准状态「未知，也无从查询」。一旦调了，「模块是否仍为出厂原始态」这件事就
   **永久失去了根据** —— 而此前所有会话的标定快照都建立在这个前提上。

所以这不是「wt901 这个 API 不好用」。它本身没有问题，是**本项目的补偿在上位机**，
用它就等于换了一套与 FR-03 冲突的方案，且换得悄无声息。

## 范围取整条通道，不是单个函数

磁场校准（`field_calibration()` 等）同样写 `CALSW`，同样改模块内部状态。只禁
`calibrate_acceleration()` 的闸挡不住从旁边绕进来的调用。

## 与 `check_layering.py` 的分工

那条守的是 **import 边界**（`core/` 不得 import `gait.io` 等）。本条守的是**调用
边界**：`wt901` 在 `gait.device` 里是允许 import 的，所以只看 import 拦不住
`device.calibration.calibrate_acceleration()` —— 那个调用的 import 完全合法。

扫描根是参数而不是写死的常量，这样测试能在临时目录里造出真实的违规。对着真实的
`src/` 跑只能证明「现在没有违规」，证明不了「有违规时会失败」—— 而后者才是这个
检查存在的理由。
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

# 入口（dev / dev.ps1）已设 PYTHONUTF8，但直接 `python tools/check_calibration_channel.py`
# 调试时不经入口。本脚本的输出是中文，因此自己也保证一次。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")

REPO_ROOT = Path(__file__).resolve().parent.parent

#: 被扫描的目录，相对仓库根。**包含 **`tests/`：一条「测试里可以随便调」的例外等于
#: 没有这条红线 —— 测试同样会连上真机，写进去的寄存器一样固化，而且没有豁免的
#: 必要（见下方关于豁免的说明）。
SCANNED_ROOTS: tuple[str, ...] = ("src", "tools", "tests")

#: wt901 里承载标定通道的模块。import 它本身就是信号，哪怕之后用别名调用。
FORBIDDEN_MODULES: frozenset[str] = frozenset({"wt901.calibration"})

#: 标定通道的具名入口。**精确匹配**，不做前缀或子串比对 —— 本仓库自己的
#: `CalibrationError` / `CalibrationVerdict` / `AccelCalibration` / `MountingCalibration`
#: 都以它们为名的一部分，子串匹配会把整个 `gait.calib` 判成违规。
FORBIDDEN_NAMES: frozenset[str] = frozenset(
    {
        "Calibration",  # device.calibration 的类
        "CalibrationMode",  # 写进 CALSW 的取值
        "CALSW",  # 寄存器本身
        "calibrate_acceleration",
        "field_calibration",
        "start_field_calibration",
        "end_field_calibration",
        "is_field_calibrating",
    }
)

#: 属性访问形式的通道入口。`calibration` 单列：`device.calibration` 是通道的门，
#: 而本仓库自己一律用 `calib` / `calib_snapshot` / `calibration_id` 命名，不会撞上
#: 光秃秃的 `.calibration`（`calibration_id` 是另一个属性名，精确匹配不命中）。
FORBIDDEN_ATTRIBUTES: frozenset[str] = frozenset({"calibration"}) | FORBIDDEN_NAMES

# 本检查**没有豁免名单**，这不是疏漏。
#
# 第一版给红线自己的测试留了一条豁免，理由听起来很正当：它必须能把违规写法摆出来。
# 写完去验证豁免有没有用，才发现它是**空操作** —— 那些违规写法在测试里是**字符串
# 字面量**（喂给 `ast.parse` 的数据），而本检查读的是 AST。字符串里的
# `device.calibration` 不是属性访问节点，它只是一个 `str`。
#
# 于是豁免名单从一开始就没有豁免掉任何东西，却会让人以为「tests/ 里有例外」，
# 并给后来者一个现成的口子往里加第二条。已删除。


def _violations(tree: ast.AST) -> list[tuple[int, str]]:
    """(行号, 说明)。四种触碰方式都要覆盖，因为它们能互相替代：

        import wt901.calibration            -> Import,    alias.name
        from wt901.calibration import X     -> ImportFrom, module
        from wt901 import calibration       -> ImportFrom, module + alias.name
        device.calibration.calibrate_...()  -> Attribute（import 完全合法，只能靠这条）
    """
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in FORBIDDEN_MODULES:
                    found.append((node.lineno, f"import {alias.name}"))
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module in FORBIDDEN_MODULES:
                found.append((node.lineno, f"from {module} import ..."))
                continue
            for alias in node.names:
                # `from wt901 import calibration` 拼出来的还是那个被禁模块。
                if f"{module}.{alias.name}" in FORBIDDEN_MODULES or module.split(".")[0] == "wt901" and alias.name in FORBIDDEN_NAMES:
                    found.append((node.lineno, f"from {module} import {alias.name}"))
        elif isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_ATTRIBUTES:
            found.append((node.lineno, f"属性访问 .{node.attr}"))
        elif isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
            found.append((node.lineno, f"名字 {node.id}"))
    return found


def scan(root: Path) -> list[str]:
    """`root` 下所有 .py 中触碰 wt901 标定通道的位置。"""
    reported: list[str] = []
    # 同一行的同一种触碰只报一次：`from wt901.calibration import Calibration` 会同时
    # 命中 ImportFrom 与随后的 Name，报两遍不会让检查更严，只会让它看起来坏了。
    seen: set[tuple[str, int, str]] = set()
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for lineno, reason in _violations(tree):
            key = (str(path), lineno, reason)
            if key in seen:
                continue
            seen.add(key)
            reported.append(f"{path}:{lineno}: 触碰 wt901 标定通道（{reason}）")
    return reported


def main() -> int:
    reported: list[str] = []
    for name in SCANNED_ROOTS:
        root = REPO_ROOT / name
        if root.is_dir():
            reported.extend(scan(root))

    if reported:
        print("wt901 标定通道红线被破坏：", file=sys.stderr)
        for line in reported:
            head, rest = line.split(":", 1)
            try:
                head = str(Path(head).relative_to(REPO_ROOT))
            except ValueError:
                pass
            print(f"  {head}:{rest}", file=sys.stderr)
        print(
            "\nPRD FR-03：标定参数不写回模块寄存器，全部补偿在上位机完成，模块保持"
            "出厂原始态。wt901 的标定通道写 Register.CALSW，与它直接冲突，且该寄存器"
            "只写不可读 —— 一旦调用，「模块是否仍为出厂原始态」永久失去根据。\n"
            "本项目的加计补偿见 gait/calib/accel.py（六面法，上位机侧）。",
            file=sys.stderr,
        )
        return 1

    scanned = ", ".join(SCANNED_ROOTS)
    print(f"wt901 标定通道红线检查通过：{scanned}/ 未触碰 device.calibration / CALSW")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
