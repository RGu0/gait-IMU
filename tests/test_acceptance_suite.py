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

from gait.config import AlgoConfig

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


@pytest.mark.parametrize("name", MODULES)
def test_every_analyse_takes_the_same_two_arguments(name):
    """`analyse(trial_dir, cfg)` —— 签名统一，`tools/run_acceptance.py` 按它统一调用。

    这条是被 runner 逼出来的：`event_interval` 原本只收 `trial_dir`（它自己造两套
    配置），于是 runner 要么按名字特判，要么用 `inspect` 猜。两者都是把"接口不一致"
    从一次性的修改成本变成永久的调用成本。
    """
    import inspect

    module = importlib.import_module(f"acceptance.{name}")
    parameters = list(inspect.signature(module.analyse).parameters)
    assert parameters == ["trial_dir", "cfg"], f"{name}.analyse 的签名是 {parameters}"


def test_the_runner_finds_every_script_and_skips_the_private_ones():
    """runner 的发现逻辑：找齐全部脚本，且不把 `_dataset` 当成一个验收脚本。"""
    import run_acceptance

    found = run_acceptance.scripts()
    assert set(found) == set(MODULES)
    assert not any(name.startswith("_") for name in found)


def test_a_crashed_script_is_reported_as_crashed_not_as_passing(monkeypatch):
    """**崩溃与"零条不达标"必须分开报。**

    RAY-328 的 `xcorr_prior_acceptance.py` 在上游删掉一个字段之后是 `KeyError` 直接
    崩的 —— 那时它一行判据都没跑到。若 runner 把它算成"0 条不达标"，汇总表会显示
    全绿，而实际上那个脚本什么也没验。这正是本 Issue 要治的那种"看起来在守"。
    """
    import run_acceptance

    class Exploding:
        @staticmethod
        def analyse(trial_dir, cfg):
            raise KeyError("seeded")

        @staticmethod
        def judge(rows):
            return []

    monkeypatch.setattr(run_acceptance.importlib, "import_module", lambda _: Exploding)
    outcome = run_acceptance.run("whatever", [Path(".")], AlgoConfig(), None)

    assert outcome.crashed is not None
    assert "KeyError" in outcome.crashed
    assert outcome.failures == []          # 一条判据都没跑到
    assert outcome.passed is False         # 但绝不算通过
