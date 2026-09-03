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
from types import SimpleNamespace

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
    assert len(MODULES) >= 10


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


# ─────────────────────────────────────────────────────────────────────────────
# RAY-346：登记表，与三个新脚本的判据形状
#
# 上面那些守的是"脚本还是不是一个验收脚本"。下面这些守的是"这些门还通不通电" ——
# RAY-343 定下的规矩是每个脚本带阳性对照，而对照本身也可能被写死成永远通过。
# 数据驱动的对照在 `tools/acceptance/` 里跑真机数据；这里用构造出来的行，验的是
# judge 在收到"本该被抓"的输入时确实会抓。
# ─────────────────────────────────────────────────────────────────────────────

REGISTRY = Path(acceptance.__path__[0]) / "REGISTRY.md"


def _registered() -> set[str]:
    """登记表里 `| \\`名字\\` |` 那一列。"""
    names: set[str] = set()
    for line in REGISTRY.read_text(encoding="utf-8").splitlines():
        if not line.startswith("| `"):
            continue
        names.add(line.split("`")[1])
    return names


def test_the_registry_and_the_directory_agree():
    """**表与目录一一对应。**

    RAY-346 的成因是 RAY-325 判据 2 有脚本却不在这个目录里，于是 runner 报"7/7 全部
    达标"时并不知道有第八条。登记表挡不住"谁都没登记的判据"（那要靠 AGENTS.md 里的
    约定），但它至少保证眼前这些是对得上的：没有未登记的脚本，也没有指向不存在脚本
    的登记。
    """
    assert _registered() == set(MODULES), (
        f"登记表与目录对不上：只在表里 {_registered() - set(MODULES)}，"
        f"只在目录里 {set(MODULES) - _registered()}"
    )


def test_the_registry_does_not_list_the_private_helpers():
    """`_dataset` / `_stance` 是共用计算，不是脚本 —— runner 不收，表也不登记。"""
    assert not any(name.startswith("_") for name in _registered())


def _period_row(cycles: int, control: int, speed: str = "mid", **extra) -> dict:
    row = {
        "trial": "T", "walk": f"{speed}-a", "foot": "L", "speed": speed,
        "cycles": cycles, "error_pct": (cycles - 38) / 38 * 100.0,
        "control_cycles": control,
    }
    row.update(extra)
    return row


def test_period_cycles_accepts_the_measured_shape():
    """实测形状（周期数 36~39、对照掉到 16~19）必须一条不报。"""
    from acceptance import period_cycles

    rows = [
        _period_row(36, 17, "slow"), _period_row(37, 18, "slow"),
        _period_row(38, 18, "mid"), _period_row(39, 18, "fast"),
        _period_row(36, 16, "fast"),
    ]
    assert period_cycles.judge(rows) == []


def test_period_cycles_catches_a_dead_positive_control():
    """对照没掉出区间 = 这个区间检查没有通电，必须报。

    只喂前一半数据却仍数出 37 个周期，说明周期数根本不跟着输入走。
    """
    from acceptance import period_cycles

    failures = period_cycles.judge([_period_row(38, 37)])
    assert any("阳性对照没被抓出" in line for line in failures)


def test_period_cycles_catches_a_sign_reversal_across_speed_bands():
    """慢档整档为正、快档整档为负 —— 那正是阈值法「慢速过检、快速漏检」的失效。"""
    from acceptance import period_cycles

    rows = [
        _period_row(41, 18, "slow"), _period_row(42, 18, "slow"),
        _period_row(35, 17, "fast"), _period_row(36, 17, "fast"),
    ]
    assert any("两端反号" in line for line in period_cycles.judge(rows))


def test_period_cycles_does_not_call_a_band_touching_zero_a_reversal():
    """快档最好的一格恰好落在真值上（误差 +0%）不算反号 —— 那是噪声不是失效。

    实测就是这个形状：slow −5%~−3%，fast −5%~+3%。写成「任一格越零就报」会让这套
    判据在自己的正样本上红。
    """
    from acceptance import period_cycles

    rows = [
        _period_row(36, 17, "slow"), _period_row(37, 18, "slow"),
        _period_row(36, 17, "fast"), _period_row(39, 18, "fast"),
    ]
    assert period_cycles.judge(rows) == []


def _interval_row(new_ds: float, old_ds: float, same_foot: int = 0,
                  stance_pct=(53.0, 54.0)) -> dict:
    return {
        "trial": "T", "walk": "mid-a",
        "new": {"path": "new", "ds_fraction": new_ds, "same_foot": same_foot,
                "stance_pct": list(stance_pct), "intervals": [36, 36]},
        "old": {"path": "old", "ds_fraction": old_ds, "same_foot": 3,
                "stance_pct": [9.0, 9.0], "intervals": [37, 36]},
    }


def test_stance_intervals_accepts_the_two_negative_cells_the_user_ruled_on():
    """`S1-sport` 快档两格实测 −0.068，**2026-09-02 用户裁决接受**。

    判据钉的是「负得有限」不是「不许负」—— 写成后者就只能靠改判据来达成。
    """
    from acceptance import stance_intervals

    rows = [_interval_row(+0.123, -0.799)] * 10 + [_interval_row(-0.068, -0.925)] * 2
    assert stance_intervals.judge(rows) == []


def test_stance_intervals_catches_a_dead_positive_control():
    """旧路径（`refine_stance_edges`）若也过了那道门，门就没有通电。"""
    from acceptance import stance_intervals

    failures = stance_intervals.judge([_interval_row(+0.100, -0.05)])
    assert any("阳性对照没被抓出" in line for line in failures)


def test_stance_intervals_catches_a_collapse_back_to_zero_width_intervals():
    """支撑相占比掉回个位数 = 退回零速时刻，`detect_stance_intervals` 白做了。"""
    from acceptance import stance_intervals

    failures = stance_intervals.judge(
        [_interval_row(-0.9, -0.95, stance_pct=(9.0, 9.0))]
    )
    assert any("支撑相占比" in line for line in failures)


def _contrast_row(coarse: float, refined: float, control: float) -> dict:
    return {
        "trial": "T", "walk": "mid-a",
        "selfcheck": {"path": "selfcheck", "ds_fraction": coarse, "same_foot": 1},
        "refined": {"path": "new", "ds_fraction": refined, "same_foot": 0,
                    "stance_pct": [53.0], "intervals": [36]},
        "control": {"path": "old", "ds_fraction": control, "same_foot": 3,
                    "stance_pct": [9.0], "intervals": [37]},
    }


def test_selfcheck_contrast_accepts_the_measured_gap():
    """实测：粗判 −1.003、现行 −0.068 → 差距 0.935；对照 −0.925 → 0.078。"""
    from acceptance import selfcheck_contrast

    assert selfcheck_contrast.judge([_contrast_row(-1.003, -0.068, -0.925)]) == []


def test_selfcheck_contrast_catches_a_collapsed_gap():
    """现行路径退回粗判水平 —— 差距塌掉必须报。"""
    from acceptance import selfcheck_contrast

    failures = selfcheck_contrast.judge([_contrast_row(-1.003, -0.95, -0.925)])
    assert any("差距塌了" in line for line in failures)


def test_selfcheck_contrast_catches_a_dead_positive_control():
    """旧细化路径若也够得着那道门，门就没有通电。"""
    from acceptance import selfcheck_contrast

    failures = selfcheck_contrast.judge([_contrast_row(-1.003, -0.068, -0.05)])
    assert any("阳性对照没被抓出" in line for line in failures)


def test_selfcheck_contrast_trips_when_the_coarse_path_stops_being_near_minus_one():
    """粗判路径不再 ≈ −1 是**绊线不是质量线**。

    它变了不代表变差 —— 也可能是有人把 `sync/selfcheck` 改成用区间了，那是好事。
    脚本分不出好坏，它只负责让这件事被看见，所以报出来的话里要说清这一点。
    """
    from acceptance import selfcheck_contrast

    failures = selfcheck_contrast.judge([_contrast_row(-0.2, -0.068, -0.925)])
    assert any("不一定是缺陷" in line for line in failures)


@pytest.mark.parametrize(
    "name,row",
    [
        ("stance_intervals", _interval_row(0.0, -0.9)),
        ("selfcheck_contrast", _contrast_row(-1.0, -0.068, -0.925)),
    ],
)
def test_a_degenerate_cell_does_not_crash_the_report(name, row, capsys):
    """某一格算不出来时，表格要打成破折号，**不能崩在格式化上**。

    judge 早就把 `None` 记成不达标了；崩在 `format` 上的后果是那条不达标根本印不
    出来 —— 「崩了没人知道」正是本 Issue 在治的病，不该由这套脚本自己再犯一次。
    """
    module = importlib.import_module(f"acceptance.{name}")
    empty = dict.fromkeys(("ds_fraction", "same_foot"))
    for key in ("new", "old", "selfcheck", "refined", "control"):
        if key in row:
            row[key] = {**row[key], **empty}

    args = SimpleNamespace(trials=[], out=None)
    monkey = pytest.MonkeyPatch()
    monkey.setattr(module, "parse_args", lambda _: args)
    monkey.setattr(module, "analyse", lambda *_: [row])
    try:
        module.main()
    finally:
        monkey.undo()
    assert "—" in capsys.readouterr().out
