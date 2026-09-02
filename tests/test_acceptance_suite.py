"""`tools/acceptance/` 下验收脚本的结构守卫。

这些脚本要跑起来需要 RAY-230 的现场采集（云端共享库，仓库里没有），所以**这里不跑
它们**，只守它们的形状。真正的运行由 `tools/run_acceptance.py` 在需要时做。

守两件事，两件都来自 RAY-343 查出来的那次腐烂：

1. **接口一致**。RAY-328 的 `xcorr_prior_acceptance.py` 在上游删掉一个字段之后
   `KeyError` 直接崩 —— 而那是**运行时**才发现的，隔了两个 Issue。统一接口挡不住
   全部这类漂移，但它挡住了"某个脚本悄悄变成另一种东西"这一类。

2. **不许钉逐格基线**。RAY-328 的 `alternation_acceptance.py` 里硬编码了一张
   `CYCLES_AFTER_L1`（逐格周期数）。RAY-339 一改周期估计，那张表整个过时，**12 条
   失败全部出自它**；而同一个文件里的账目恒等式（钉性质的那条）至今一字不差地成立。

   所以这里禁掉那张表的**形状**：模块级的、以 `(趟, 速度档)` 元组为键的字面量映射。
   要基线就当场重算，要锚就用受控真值 `TRUTH_CYCLES` —— 它是现场数出来的，不随代码变。
"""

import ast
import importlib
import pkgutil
from pathlib import Path

import acceptance
import pytest

MODULES = sorted(
    info.name
    for info in pkgutil.iter_modules(acceptance.__path__)
    if not info.name.startswith("_")
)


def test_the_suite_is_not_empty():
    """空的套件会让下面每一条参数化测试都变成零次调用 —— 全绿，而且什么也没测。"""
    assert len(MODULES) >= 7


@pytest.mark.parametrize("name", MODULES)
def test_every_script_imports_and_exposes_the_same_interface(name):
    """能 import，且 `analyse` / `judge` / `main` 三件齐全。

    `judge` 是判据所在，`main` 是入口，`analyse` 是数据到行的那一步 —— runner 与
    将来的批量分析都按这三个名字来。
    """
    module = importlib.import_module(f"acceptance.{name}")
    for attribute in ("analyse", "judge", "main"):
        assert callable(getattr(module, attribute, None)), f"{name} 缺少 {attribute}"


@pytest.mark.parametrize("name", MODULES)
def test_no_script_pins_a_per_cell_baseline(name):
    """模块级不许出现以 `(趟, 速度档)` 为键的字面量映射。

    那正是 `CYCLES_AFTER_L1` 的形状，也是本套件上一次烂掉的直接原因：它把"上一版代码
    在这些格上给出的数"钉成了判据，于是代码一改进，脚本就红，而红的是脚本不是代码。

    用 ast 解析而不是找字符串：变量叫什么名字不重要，**是不是一张逐格对照表**才重要。
    """
    source = Path(acceptance.__path__[0], f"{name}.py").read_text(encoding="utf-8")
    for node in ast.parse(source).body:
        if not isinstance(node, ast.Assign):
            continue
        value = node.value
        if not isinstance(value, ast.Dict):
            continue
        tuple_keys = [key for key in value.keys if isinstance(key, ast.Tuple)]
        assert not tuple_keys, (
            f"{name} 里有一张逐格对照表（模块级、元组为键的字面量映射）。"
            "基线要当场重算，锚要用受控真值 —— 见本文件文档。"
        )


@pytest.mark.parametrize("name", MODULES)
def test_every_script_anchors_on_the_controlled_truth_or_nothing(name):
    """凡是用到真值的脚本，都必须用共享的 `TRUTH_CYCLES`，不许自己写一个 38。

    自己写一个数就等于又开了一处会漂的地方 —— 真值将来若因新的采集协议而变，
    共享常量改一处，而散落的字面量改不干净。
    """
    source = Path(acceptance.__path__[0], f"{name}.py").read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id in {"TRUTH", "TRUTH_CYCLES"}
            for target in node.targets
        ):
            assert isinstance(node.value, ast.Name), (
                f"{name} 自己写了一个真值常量；应当引用 `_dataset.TRUTH_CYCLES`"
            )
