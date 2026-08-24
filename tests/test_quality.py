"""`gait.quality.annotate` 与质量分级红线检查。

验收标准两条：**任一指标的质量证据入库可查**；**`low` 只影响报告呈现样式，不拦截**。

第二条是 PRD §13「不做门控、全量输出 + 质量标注」的直接体现，也是最容易被"顺手"破坏
的一条 —— 所以这里有一组测试专门守「默认不拦截」这个行为，而不是只在注释里写。

另有一组守红线检查本身：对着现有代码跑只能证明"现在没有违规"，证明不了"有违规时会
失败"—— 而后者才是这个检查存在的理由。
"""

import subprocess
import sys
from pathlib import Path

import pytest

from gait.quality.annotate import (
    CHAIN_BASIC,
    CHAIN_FULL,
    GRADE_LOW,
    GRADE_NORMAL,
    GRADE_UNCOMPUTABLE,
    RULES_VERSION,
    TELEMETRY_EVENT,
    GateMatrix,
    QualityError,
    annotate,
    apply_gates,
    summarize,
    worst,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
CHECK = REPO_ROOT / "tools" / "check_quality_single_source.py"

GOOD_SYNC = {"determinate": True, "flagged": False, "offset_estimate": 0.001}


def many(**kwargs):
    """一个样本量充足的标注。"""
    defaults = {"n_steps": 40, "chain": CHAIN_BASIC}
    defaults.update(kwargs)
    return annotate("stride_length", **defaults)


# ── 验收标准：low 不拦截 ──────────────────────────────────────────────────────


def test_a_low_metric_is_still_returned():
    """**PRD §13：不做门控。** `low` 只影响呈现样式。

    不输出等于替读者做了一个他没同意的决定 —— 而他连"有这么一项被拿掉了"都不知道。
    """
    item = many(n_steps=5)

    assert item.grade == GRADE_LOW
    assert not item.blocked
    kept, blocked = apply_gates([item])
    assert kept == [item]
    assert blocked == []


def test_an_uncomputable_metric_is_also_still_returned():
    item = many(computable=False)

    assert item.grade == GRADE_UNCOMPUTABLE
    kept, blocked = apply_gates([item])
    assert kept == [item] and blocked == []


def test_the_gate_matrix_is_off_by_default_and_is_then_the_identity():
    """**FR-510：门控矩阵仅作为配置预留，默认全部不启用。**

    「默认关」必须是可验证的行为，不是一句注释 —— 所以这里断言它是恒等函数。
    """
    gates = GateMatrix()

    assert not gates.any_enabled
    items = [many(n_steps=5), many(computable=False), many()]
    kept, blocked = apply_gates(items, gates)
    assert kept == items
    assert blocked == []


def test_enabling_a_gate_actually_blocks_and_changes_the_rules_version():
    """预留不等于摆设：启用要真的生效，且**产生新的配置版本**。

    版本必须变，因为启用门控之后产出的报告与之前的不可直接比较，而"不可比较"这件事
    必须在报告页脚看得见，不能靠人记得。
    """
    gates = GateMatrix(block_low=True)
    items = [many(n_steps=5), many()]
    kept, blocked = apply_gates(items, gates)

    assert len(kept) == 1 and kept[0].grade == GRADE_NORMAL
    assert blocked == ["stride_length"]
    assert gates.rules_version != RULES_VERSION
    assert summarize(items, gates=gates).gates_enabled


def test_the_default_rules_version_is_the_plain_one():
    assert GateMatrix().rules_version == RULES_VERSION


# ── 验收标准：质量证据入库可查 ────────────────────────────────────────────────


def test_the_annotation_records_every_quantity_the_grade_rests_on():
    """**只存等级答不了"这个 low 是因为步数少还是同步差"。**

    三个月后有人问起时，那份报告必须还能回答。
    """
    item = annotate(
        "double_support",
        n_steps=8,
        chain=CHAIN_FULL,
        cross_foot=True,
        sync_quality=GOOD_SYNC,
        zupt_quality={"degraded_fraction": 0.05},
    )
    snapshot = item.snapshot()

    assert snapshot["n_steps"] == 8
    assert snapshot["sync_quality"] == GOOD_SYNC
    assert snapshot["zupt_quality"] == {"degraded_fraction": 0.05}
    assert snapshot["chain"] == CHAIN_FULL
    assert snapshot["rules_version"] == RULES_VERSION
    assert any("few_steps" in reason for reason in snapshot["reasons"])


def test_the_reasons_distinguish_the_two_ways_to_be_low():
    """步数少与同步差是两件事，`reasons` 必须分得开。"""
    few = many(n_steps=5)
    bad_sync = annotate(
        "step_time",
        n_steps=40,
        chain=CHAIN_BASIC,
        cross_foot=True,
        sync_quality={"determinate": True, "flagged": True},
    )

    assert any("few_steps" in reason for reason in few.reasons)
    assert "sync_flagged" in bad_sync.reasons
    assert few.reasons != bad_sync.reasons


def test_a_normal_metric_has_no_reasons():
    assert many().reasons == []


def test_the_snapshot_is_plain_json_types():
    import json

    snapshot = many(n_steps=5).snapshot()

    assert json.loads(json.dumps(snapshot, ensure_ascii=False))["grade"] == GRADE_LOW


# ── 分级规则 ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("n_steps", "expected"),
    [(0, GRADE_UNCOMPUTABLE), (1, GRADE_LOW), (15, GRADE_LOW), (16, GRADE_NORMAL), (40, GRADE_NORMAL)],
)
def test_the_grade_follows_the_sample_size(n_steps, expected):
    assert many(n_steps=n_steps).grade == expected


def test_a_cross_foot_metric_without_sync_quality_is_low_not_an_error():
    """**缺同步标注是一个质量问题，不是调用错误。**

    抛错会让整份报告失败，而 PRD §13 的原则正是"不做门控"—— 一项证据不全的指标该被
    标出来，不该把别的指标一起带走。
    """
    item = annotate("step_time", n_steps=40, chain=CHAIN_BASIC, cross_foot=True)

    assert item.grade == GRADE_LOW
    assert "missing_sync_quality" in item.reasons


def test_an_indeterminate_sync_makes_a_cross_foot_metric_low():
    """RAY-211 的 `determinate=False` 表示不存在恒定 offset —— 跨足指标不可解读。"""
    item = annotate(
        "double_support",
        n_steps=40,
        chain=CHAIN_BASIC,
        cross_foot=True,
        sync_quality={"determinate": False},
    )

    assert item.grade == GRADE_LOW
    assert "sync_indeterminate" in item.reasons


def test_a_within_foot_metric_ignores_sync_quality():
    """足内量不受跨足同步影响 —— 给它标注也不该改变它的等级。"""
    with_sync = many(sync_quality={"determinate": False, "flagged": True})
    without = many()

    assert with_sync.grade == without.grade == GRADE_NORMAL


def test_heavily_degraded_zupt_makes_a_metric_low():
    item = many(zupt_quality={"degraded_fraction": 0.5})

    assert item.grade == GRADE_LOW
    assert "zupt_degraded" in item.reasons


def test_uncomputable_short_circuits_the_other_checks():
    """算不出来的指标不必再谈它的样本量 —— 判定顺序体现在 `reasons` 里。"""
    item = annotate("stride_length", n_steps=0, chain=CHAIN_BASIC, cross_foot=True)

    assert item.grade == GRADE_UNCOMPUTABLE
    assert item.reasons == ["no_steps"]


def test_the_grade_vocabulary_is_not_the_confidence_vocabulary():
    """**PRD §13 的三档与 `contracts.Confidence` 不是同一套。**

    后者说的是"这条步态周期本身可不可信"，前者说的是"这个**指标**能支撑什么结论"。
    一条 degraded 的周期照样能进一个 normal 的指标（只要样本够多），反过来也成立。

    混用是很容易发生的，所以这里断言两套词确实不同。
    """
    from gait.contracts import _CONFIDENCE_VALUES
    from gait.quality.annotate import GRADES

    assert set(GRADES) != set(_CONFIDENCE_VALUES)
    assert GRADE_LOW not in _CONFIDENCE_VALUES
    assert GRADE_UNCOMPUTABLE not in _CONFIDENCE_VALUES


# ── 端云同构 ──────────────────────────────────────────────────────────────────


def test_both_chains_use_the_same_rules():
    """**端云同构。** 同一份证据，两条链必须给出同一个等级。

    不成立的后果不是"不一致"这么轻 —— 是同一次采集在采集端显示 normal、在报告里显示
    low，而两个数字都出自我们的系统。
    """
    evidence = {"n_steps": 9, "cross_foot": True, "sync_quality": GOOD_SYNC}
    basic = annotate("double_support", chain=CHAIN_BASIC, **evidence)
    full = annotate("double_support", chain=CHAIN_FULL, **evidence)

    assert basic.grade == full.grade
    assert basic.reasons == full.reasons
    assert basic.rules_version == full.rules_version


def test_a_footer_refuses_to_mix_two_chains():
    """页脚要回答的是"这份报告是怎么算出来的"，两条链就是两个答案。"""
    items = [many(chain=CHAIN_BASIC), many(chain=CHAIN_FULL)]

    with pytest.raises(QualityError, match="多条计算链"):
        summarize(items)


def test_the_footer_carries_the_rules_version():
    """PRD §13：`grade` 汇总规则版本化，**进报告页脚**。"""
    footer = summarize([many(), many(n_steps=5)])

    assert footer.rules_version == RULES_VERSION
    assert footer.snapshot()["rules_version"] == RULES_VERSION


def test_the_footer_overall_takes_the_worst():
    """一个可用的指标救不了一个不可用的。"""
    assert summarize([many(), many()]).overall == GRADE_NORMAL
    assert summarize([many(), many(n_steps=5)]).overall == GRADE_LOW
    assert summarize([many(), many(computable=False)]).overall == GRADE_UNCOMPUTABLE


def test_the_footer_counts_each_grade():
    footer = summarize([many(), many(n_steps=5), many(computable=False)])

    assert (footer.normal, footer.low, footer.uncomputable) == (1, 1, 1)
    assert footer.metrics == 3


def test_worst_of_nothing_is_uncomputable():
    assert worst([]) == GRADE_UNCOMPUTABLE


def test_worst_rejects_an_unknown_grade():
    with pytest.raises(QualityError, match="未知的等级"):
        worst([GRADE_NORMAL, "excellent"])


# ── 埋点 ──────────────────────────────────────────────────────────────────────


def test_the_telemetry_fires_only_below_normal():
    assert many().telemetry is None
    assert many(n_steps=5).telemetry is not None
    assert many(computable=False).telemetry is not None


def test_the_telemetry_carries_the_reasons_not_just_the_grade():
    """只报等级的话，后台分不出"步数少"与"同步差"这两类完全不同的问题。"""
    payload = many(n_steps=5).telemetry

    assert payload["event"] == TELEMETRY_EVENT
    assert payload["reasons"]
    assert payload["n_steps"] == 5
    assert payload["rules_version"] == RULES_VERSION


# ── 输入校验 ──────────────────────────────────────────────────────────────────


def test_an_unknown_chain_is_rejected():
    with pytest.raises(QualityError, match="chain"):
        annotate("x", n_steps=10, chain="halfway")


def test_a_negative_step_count_is_rejected():
    with pytest.raises(QualityError, match="n_steps"):
        annotate("x", n_steps=-1, chain=CHAIN_BASIC)


def test_an_empty_metric_name_is_rejected():
    with pytest.raises(QualityError, match="metric"):
        annotate("", n_steps=10, chain=CHAIN_BASIC)


def test_an_empty_footer_is_rejected():
    with pytest.raises(QualityError, match="没有任何指标"):
        summarize([])


# ── 红线检查本身 ──────────────────────────────────────────────────────────────


def run_check(cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(CHECK)],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


def test_the_repository_passes_the_red_line_check():
    result = run_check(REPO_ROOT)

    assert result.returncode == 0, result.stderr


def make_fake_repo(tmp_path: Path, source: str) -> Path:
    """造一个只有渲染进程代码的假仓库，好让检查有东西可扫。"""
    renderer = tmp_path / "apps" / "terminal" / "src"
    renderer.mkdir(parents=True)
    (renderer / "QualityBadge.jsx").write_text(source, encoding="utf-8")
    return tmp_path


def run_check_against(root: Path) -> tuple[int, str]:
    """对着一个任意的根跑检查。"""
    from tools.check_quality_single_source import scan

    offences, scanned = scan(root)
    return len(offences), f"scanned={scanned}"


def test_the_check_catches_a_grade_derived_from_a_comparison(tmp_path):
    """**对着现有代码跑只能证明"现在没有违规"。**

    证明不了"有违规时会失败" —— 而后者才是这个检查存在的理由。这里造出真实的违规。
    """
    root = make_fake_repo(
        tmp_path,
        "export function badge(nSteps) {\n"
        "  const grade = nSteps < 16 ? 'low' : 'normal';\n"
        "  return grade;\n"
        "}\n",
    )
    offences, _ = run_check_against(root)

    assert offences > 0


def test_the_check_catches_the_threshold_constant_by_name(tmp_path):
    root = make_fake_repo(
        tmp_path, "export const MIN_STEPS_FOR_NORMAL = 16;\n"
    )
    offences, _ = run_check_against(root)

    assert offences > 0


def test_the_check_allows_merely_rendering_a_grade(tmp_path):
    """**渲染等级是允许的** —— 禁止的是**算出**等级。

    分不清这两者的检查会逼着前端绕路，而绕路的写法比原来更难看懂。
    """
    root = make_fake_repo(
        tmp_path,
        "export function Badge({ grade }) {\n"
        "  return <span className={`badge badge--${grade}`}>{grade}</span>;\n"
        "}\n",
    )
    offences, _ = run_check_against(root)

    assert offences == 0


@pytest.mark.parametrize(
    ("label", "source"),
    [
        ("自闭合标签传 grade", 'export const A = () => <MetricTile grade="low" />;'),
        (
            "开标签里按 grade 分支",
            'export function B({g}) { return <p className={g === "low" ? "a" : "b"}>x</p>; }',
        ),
        (
            "标签内读 grade 决定文案",
            'export const C = ({r}) => <td>{r.grade === "uncomputable" ? "本次不适用" : r.value}</td>;',
        ),
        ("箭头函数按 grade 分支", 'export const toneOf = (g) => g === "low" ? 1 : 0;'),
    ],
)
def test_the_check_does_not_mistake_punctuation_for_a_comparison(tmp_path, label, source):
    """`<`、`>` 在 JSX 与箭头里是**标点**，不是运算符。

    原先这些全都被拦下来，而它们一个比较都没有。判别的成了"是不是 JSX"，不是
    "是不是在比较" —— 同一个 `g === "low"`，写在 JSX 里被拦、写在赋值里放行。
    见 RAY-265。
    """
    offences, _ = run_check_against(make_fake_repo(tmp_path, source + "\n"))

    assert offences == 0, label


@pytest.mark.parametrize(
    ("label", "source"),
    [
        (
            "标签里藏着真比较",
            'export const F = ({n}) => <p className={n < 16 ? "low" : "normal"}>x</p>;',
        ),
        ("比较与等级同一行", 'export function D(x, g) { return x < 16 && g === "low"; }'),
        (
            "标签外有真比较",
            'export const G = ({i}) => <div>{i.length > 0 && <Badge grade="normal" />}</div>;',
        ),
    ],
)
def test_剔掉标点不会放过真正的比较(tmp_path, label, source):
    """**只测"不再误伤"会让一个什么都不拦的实现也全绿。**

    所以这一组反过来测：剔标点之后，真正的比较必须仍然被抓住。

    第一条是关键 —— 剔的是**完整标签**，标签内部若含真正的 `<`，那个标签就剔不
    干净，残留的运算符照样触发。把 `<` 单独排除掉就做不到这个区分。

    第三条是**刻意保留**的误伤：那一行确实有一个真比较，与"纯标点"不是一回事。
    """
    offences, _ = run_check_against(make_fake_repo(tmp_path, source + "\n"))

    assert offences > 0, label


def test_the_check_ignores_comments(tmp_path):
    root = make_fake_repo(
        tmp_path,
        "// grade 由 sidecar 给出；前端不得按 n < 16 判 'low'\n"
        "export const Badge = ({ grade }) => grade;\n",
    )
    offences, _ = run_check_against(root)

    assert offences == 0


def test_the_check_says_so_when_there_is_nothing_to_scan(tmp_path):
    """静默通过与"扫过了没问题"看起来一样，而两者是不同的结论。"""
    from tools.check_quality_single_source import scan

    offences, scanned = scan(tmp_path)

    assert offences == [] and scanned == 0


def test_the_check_skips_type_declarations(tmp_path):
    """`.d.ts` 里只有类型，没有实现 —— 扫它只会制造噪声。"""
    renderer = tmp_path / "packages" / "design-system"
    renderer.mkdir(parents=True)
    (renderer / "Badge.d.ts").write_text(
        "export declare const g: 'low' | 'normal';\n", encoding="utf-8"
    )
    from tools.check_quality_single_source import scan

    _, scanned = scan(tmp_path)
    assert scanned == 0


def test_the_check_skips_generated_renderer_assets(tmp_path):
    root = make_fake_repo(tmp_path, "export const Badge = ({ grade }) => grade;\n")
    generated = root / "apps" / "terminal" / "renderer" / "dist" / "assets"
    generated.mkdir(parents=True)
    (generated / "index.js").write_text(
        "const grade = nSteps < 16 ? 'low' : 'normal';\n", encoding="utf-8"
    )

    from tools.check_quality_single_source import scan

    offences, scanned = scan(root)

    assert offences == []
    assert scanned == 1
