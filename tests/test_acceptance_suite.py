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
    assert len(MODULES) >= 13


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
    assert outcome.failures == []  # 一条判据都没跑到
    assert outcome.passed is False  # 但绝不算通过


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
        "trial": "T",
        "walk": f"{speed}-a",
        "foot": "L",
        "speed": speed,
        "cycles": cycles,
        "error_pct": (cycles - 38) / 38 * 100.0,
        "control_cycles": control,
    }
    row.update(extra)
    return row


def test_period_cycles_accepts_the_measured_shape():
    """实测形状（周期数 36~39、对照掉到 16~19）必须一条不报。"""
    from acceptance import period_cycles

    rows = [
        _period_row(36, 17, "slow"),
        _period_row(37, 18, "slow"),
        _period_row(38, 18, "mid"),
        _period_row(39, 18, "fast"),
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
        _period_row(41, 18, "slow"),
        _period_row(42, 18, "slow"),
        _period_row(35, 17, "fast"),
        _period_row(36, 17, "fast"),
    ]
    assert any("两端反号" in line for line in period_cycles.judge(rows))


def test_period_cycles_does_not_call_a_band_touching_zero_a_reversal():
    """快档最好的一格恰好落在真值上（误差 +0%）不算反号 —— 那是噪声不是失效。

    实测就是这个形状：slow −5%~−3%，fast −5%~+3%。写成「任一格越零就报」会让这套
    判据在自己的正样本上红。
    """
    from acceptance import period_cycles

    rows = [
        _period_row(36, 17, "slow"),
        _period_row(37, 18, "slow"),
        _period_row(36, 17, "fast"),
        _period_row(39, 18, "fast"),
    ]
    assert period_cycles.judge(rows) == []


def _interval_row(
    new_ds: float, old_ds: float, same_foot: int = 0, stance_pct=(53.0, 54.0)
) -> dict:
    return {
        "trial": "T",
        "walk": "mid-a",
        "new": {
            "path": "new",
            # 判据读的是**中位**（RAY-354 判据 2）：`fraction` 建在均值上，被单个
            # 离群相位支配 —— 实测一个 +4.412 s 的静止前导伪影就能把它顶起 0.07，
            # 而中位纹丝不动。两个都给，但只有中位进判据。
            "ds_fraction": new_ds,
            "ds_median": new_ds,
            "ds_excluded": 0,
            "same_foot": same_foot,
            "stance_pct": list(stance_pct),
            "intervals": [36, 36],
        },
        "old": {
            "path": "old",
            "ds_fraction": old_ds,
            "ds_median": old_ds,
            "ds_excluded": 0,
            "same_foot": 3,
            "stance_pct": [9.0, 9.0],
            "intervals": [37, 36],
        },
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
        "trial": "T",
        "walk": "mid-a",
        "selfcheck": {"path": "selfcheck", "ds_fraction": coarse, "same_foot": 1},
        "refined": {
            "path": "new",
            "ds_fraction": refined,
            "same_foot": 0,
            "stance_pct": [53.0],
            "intervals": [36],
        },
        "control": {
            "path": "old",
            "ds_fraction": control,
            "same_foot": 3,
            "stance_pct": [9.0],
            "intervals": [37],
        },
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


# ─────────────────────────────────────────────────────────────────────────────
# RAY-356：航向漂移哨兵。**四条判据全是双向门**，所以每条都要两个方向各测一次 ——
# 只测"变坏会红"会漏掉这套判据真正的新意：它在缺陷被修好时也响。
# ─────────────────────────────────────────────────────────────────────────────


def _heading_row(heading, turns=0, speed="mid", zupt=0.09, free=1.6, **extra):
    row = {
        "kind": "cell",
        "trial": "T",
        "walk": f"{speed}-a",
        "foot": "L",
        "speed": speed,
        "heading_p50": heading,
        "turns": turns,
        "cycles": 38,
        "zupt_fraction": zupt,
        "free_run_p50": free,
    }
    row.update(extra)
    return row


def _control_row(heading, duration=20.0, foot="L"):
    return {
        "kind": "control",
        "duration_s": duration,
        "foot": foot,
        "heading_p50": heading,
        "turns": 0,
        "cycles": 16,
        "zupt_fraction": 0.20,
        "free_run_p50": 0.30,
    }


def _measured_headings() -> list[dict]:
    """实测的那个形状（`S1-sport` 十二格 + 四格合成对照）。

    这里**必须**用真实读数而不是随手编的数：判据 3、4 钉的是速度依赖与机制相关，
    编出来的数据很容易碰巧满足它们，那样这套测试就只在测自己。
    """
    cells = [
        _heading_row(33.5, 4, "slow", 0.028, 2.654),
        _heading_row(11.9, 3, "slow", 0.029, 2.640),
        _heading_row(15.4, 0, "slow", 0.046, 2.896),
        _heading_row(12.9, 0, "slow", 0.043, 2.999),
        _heading_row(11.4, 0, "mid", 0.089, 1.593),
        _heading_row(9.0, 0, "mid", 0.089, 1.581),
        _heading_row(11.6, 0, "mid", 0.084, 1.536),
        _heading_row(10.0, 0, "mid", 0.086, 1.541),
        _heading_row(9.4, 0, "fast", 0.157, 0.990),
        _heading_row(4.2, 0, "fast", 0.153, 0.985),
        _heading_row(8.8, 0, "fast", 0.164, 0.992),
        _heading_row(3.2, 0, "fast", 0.153, 0.987),
    ]
    controls = [
        _control_row(0.21),
        _control_row(0.19, foot="R"),
        _control_row(0.09, 40.0),
        _control_row(0.07, 40.0, foot="R"),
    ]
    return cells + controls


def test_heading_drift_accepts_the_measured_shape():
    """实测形状一条不报 —— 否则这套哨兵在自己的正样本上就是红的。

    `_healthy_gate()` 是 RAY-362 之后"完整的一次运行"必须带上的两道对照。
    少了它们这一组就不是正样本，而是"上限门那一格没测"。
    """
    from acceptance import heading_drift

    assert heading_drift.judge(_measured_headings() + _healthy_gate()) == []


def test_heading_drift_catches_a_cell_that_got_worse():
    from acceptance import heading_drift

    rows = _measured_headings() + [_heading_row(55.0, 1, "slow", 0.02, 3.5)]
    assert any("变坏了" in line for line in heading_drift.judge(rows))


def test_heading_drift_catches_a_cell_that_got_better():
    """**这一条是本 Issue 的要点**：漂移被修好，判据也必须红。

    钉"不得更坏"会把 33.5° 当成基线供起来 —— 下一个人看到绿灯，以为这里没问题。
    """
    from acceptance import heading_drift

    rows = _measured_headings() + [_heading_row(0.8, 0, "fast", 0.30, 0.4)]
    assert any("变好了" in line for line in heading_drift.judge(rows))


def test_heading_drift_catches_the_turn_count_in_both_directions():
    """转身误报真值是 0，所以判出的全是误报；总数两头都是门。"""
    from acceptance import heading_drift

    worse = [_heading_row(12.0, 10, "slow"), _heading_row(12.0, 9, "fast")]
    assert any("变坏了（真值是 0）" in line for line in heading_drift.judge(worse))

    better = [_heading_row(12.0, 1, "slow"), _heading_row(12.0, 1, "fast")]
    assert any("变好了" in line for line in heading_drift.judge(better))


def test_heading_drift_catches_a_speed_dependence_that_flipped():
    """慢档反而比快档好 = 机制变了，RAY-356 的结论要重新审视，不该默默继续用。"""
    from acceptance import heading_drift

    rows = [
        _heading_row(5.0, 1, "slow", 0.028, 2.7),
        _heading_row(5.0, 1, "slow", 0.029, 2.6),
        _heading_row(20.0, 1, "fast", 0.157, 1.0),
        _heading_row(20.0, 1, "fast", 0.153, 1.0),
    ]
    assert any("速度依赖翻转或消失了" in line for line in heading_drift.judge(rows))


def test_heading_drift_catches_a_collapsed_mechanism():
    """观测密度不再解释漂移 —— 守的是**解释**，不是病本身。

    这里让两个机制量**有变化但不相关**，而不是把它们钉成常数：常数会让
    `corrcoef` 除以零方差、判据靠 `nan` 走到同一条失败上 —— 那测的是退化路径，
    不是"相关塌了"这个真实失效。
    """
    from acceptance import heading_drift

    rows = [
        _heading_row(12.0, 1, "slow", 0.10, 1.5),
        _heading_row(3.0, 1, "slow", 0.14, 1.6),
        _heading_row(12.0, 1, "fast", 0.13, 1.6),
        _heading_row(3.0, 1, "fast", 0.11, 1.5),
    ]
    assert any("机制变了" in line for line in heading_drift.judge(rows))


def test_heading_drift_catches_a_dead_positive_control():
    """合成步态航向近乎无漂；它都触不红下限门，那道门就没有通电。"""
    from acceptance import heading_drift

    rows = [row for row in _measured_headings() if row["kind"] == "cell"]
    rows.append(_control_row(5.0))
    assert any("没有通电" in line for line in heading_drift.judge(rows))


def test_heading_drift_notices_a_missing_positive_control():
    """对照一格都没跑出来 = 静默失效，比红灯更危险，必须报。"""
    from acceptance import heading_drift

    rows = [row for row in _measured_headings() if row["kind"] == "cell"]
    assert any("对照没跑出任何一格" in line for line in heading_drift.judge(rows))



# ─────────────────────────────────────────────────────────────────────────────
# RAY-362：上限门的阳性对照 + 阴性对照。**三种结局要分开**：正常、崩溃、静默失效。
# 只测"没顶过门会红"是不够的 —— RAY-356 正是因为把崩溃读成"这条路走不通"，
# 才写下了"上限门够不着"这个错结论。
# ─────────────────────────────────────────────────────────────────────────────


def _upper_row(heading=61.2, foot="L", **extra):
    from acceptance import heading_drift

    row = {
        "kind": "upper_control",
        "trial": heading_drift.UPPER_CELL[0],
        "walk": heading_drift.UPPER_CELL[1],
        "peak_dps": heading_drift.UPPER_PEAK_DPS,
        "onset_s": heading_drift.UPPER_ONSET_S,
        "foot": foot,
        "outcome": "ran",
        "heading_p50": heading,
        "turns": 3,
        "cycles": 33,
        "zupt_fraction": 0.04,
        "free_run_p50": 2.9,
    }
    row.update(extra)
    return row


def _null_row(heading=24.0, foot="L", **extra):
    from acceptance import heading_drift

    row = {**_upper_row(heading, foot), "kind": "null_control"}
    row["peak_dps"] = heading_drift.NULL_PEAK_DPS
    row.update(extra)
    return row


def _healthy_gate() -> list[dict]:
    """两道对照都正常的那一组：阳性两足越线、阴性两足留在带内。"""
    return [
        _upper_row(61.2, "L"),
        _upper_row(61.4, "R"),
        _null_row(24.0, "L"),
        _null_row(17.5, "R"),
    ]


def test_heading_drift_accepts_a_healthy_pair_of_gate_controls():
    from acceptance import heading_drift

    assert heading_drift.judge(_measured_headings() + _healthy_gate()) == []


def test_heading_drift_reports_a_missing_upper_control_as_not_run():
    """对照钉在某一格上，那一格没供上就是**没测**，不是"门没通电"。

    这一条守的是 RAY-362 的成因：把"这次没测出来"读成"这里测不出来"。
    """
    from acceptance import heading_drift

    failures = heading_drift.judge(_measured_headings())
    assert any("上限门的阳性对照**没跑**" in line for line in failures)
    assert any("这不是「门没通电」，是这次没测" in line for line in failures)


def test_heading_drift_separates_a_crashed_upper_control_from_a_failed_one():
    """崩溃与"没顶过门"分开报 —— 混报会让下一个人去查一个没坏的门。"""
    from acceptance import heading_drift

    crashed = [
        {**_upper_row(), "outcome": "crashed", "error": "SegmentationError"},
        _null_row(24.0, "L"),
    ]
    failures = heading_drift.judge(_measured_headings() + crashed)
    assert any("SegmentationError" in line and "**崩了，不是不达标**" in line for line in failures)
    # 崩了就不该再报"没越过上限"——那是另一种结局。
    assert not any("没有越过上限" in line for line in failures)


def test_heading_drift_calls_a_silent_upper_control_an_injection_failure():
    """链子跑完但没顶过门 —— 是**注入在这一格失效**，不是门坏了。"""
    from acceptance import heading_drift

    weak = [_upper_row(31.0, "L"), _upper_row(30.0, "R"), _null_row(24.0, "L")]
    failures = heading_drift.judge(_measured_headings() + weak)
    assert any("**这是注入在这一格失效，不是上限门坏了**" in line for line in failures)


def test_heading_drift_judges_the_upper_control_on_the_pinned_foot_only():
    """右脚在 onset 2.9 有一道 0.1 s 宽的悬崖，所以判据只钉左脚。

    右脚掉回带内**不该**让对照失败 —— 否则这个哨兵会在一个已知的、
    与算法无关的敏感点上变红。
    """
    from acceptance import heading_drift

    rows = [_upper_row(61.2, "L"), _upper_row(29.6, "R"), _null_row(24.0, "L")]
    assert heading_drift.judge(_measured_headings() + rows) == []


def test_heading_drift_catches_a_null_control_that_went_red():
    """阴性对照变红 = 注入装置本身有问题。

    一个把什么都染红的注入，证不了上限门通电 —— 只测阳性对照就会漏掉这一类。
    """
    from acceptance import heading_drift

    rows = [_upper_row(61.2, "L"), _null_row(55.0, "L")]
    failures = heading_drift.judge(_measured_headings() + rows)
    assert any("**注入装置本身有问题**" in line for line in failures)


def test_heading_drift_notices_a_missing_null_control():
    """只有阳性对照的一组是不完整的，要报出来而不是默默通过。"""
    from acceptance import heading_drift

    rows = [_upper_row(61.2, "L"), _upper_row(61.4, "R")]
    failures = heading_drift.judge(_measured_headings() + rows)
    assert any("阴性对照没跑" in line for line in failures)


# ─────────────────────────────────────────────────────────────────────────────
# RAY-350：覆盖图与目录的一致性
#
# `COVERAGE.md` 大部分内容是判断（哪条判据由什么守着），机器核不了。能核的只有一件：
# 它标成"真机"的那些，指的脚本必须真的存在。这一条挡住的是"图上写着有人守、而那个
# 脚本已经被删或改名"——那正是 RAY-346 里 `period_stance_field.py` 的死法。
# ─────────────────────────────────────────────────────────────────────────────

COVERAGE = Path(acceptance.__path__[0]) / "COVERAGE.md"


def test_every_script_the_coverage_map_calls_real_machine_exists():
    """覆盖图里标成"真机"的行，提到的脚本必须在目录里。"""
    named: set[str] = set()
    for line in COVERAGE.read_text(encoding="utf-8").splitlines():
        if not line.startswith("|") or "真机" not in line:
            continue
        for chunk in line.split("`")[1::2]:
            if chunk.endswith(".py"):
                named.add(chunk[: -len(".py")])
    assert named, "覆盖图里一个真机脚本都没提到 —— 解析多半坏了"
    missing = sorted(named - set(MODULES))
    assert not missing, f"覆盖图说这些脚本在守，但目录里没有：{missing}"


def test_the_coverage_map_states_its_own_boundary():
    """**判据 2 是硬要求**：图必须写出查了哪些、跳过哪些、为什么。

    一张不说边界的覆盖图会被当成"全都查过了"，而那种错觉比没有图更危险 —— 它正是
    RAY-350 要消灭的东西。这条测试钉住的是那一节不许被悄悄删掉。
    """
    text = COVERAGE.read_text(encoding="utf-8")
    assert "## 审计边界" in text
    assert "没查" in text and "为什么不查" in text
    # 边界必须点名它查过的 Issue，而不是只说"共享算法路径"这种没法核对的话。
    assert text.count("RAY-") > 40
