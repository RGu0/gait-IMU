"""RAY-395 `one-report-builder`：两条报告路径只剩一个装配层。

## 这里守的是什么

从前 `app/service.py` 的 `reportFor`（`build_report`）与 `cli/mvp.py`
（`assemble_report`）各自把结果摊成模板要的 dict —— 两份装配逻辑，一份模板（R-4）。
它们分别漂移：同一个指标键名不同（`ds` / `double-support`）、同一个量一边进核心区
一边进专业参数、一边有**步长**与 `qualityFooter` 另一边没有、另一边有站立相/摆动相
占比与转身次数这边没有。

所以本文件的第一条断言是**逐字段相等**，不是「两边都有这个键」。`honest-sync-quality`
的教训正是这个：那次有一条变异最初没被抓住，因为断言写的是「非空」，而一句写死的
中文同样非空。比对具体值，「有没有」抓不住换了个数的改动。

## 为什么双支撑期要单独钉两次

拉齐**不是**把两条路径的输入抹平。`build_report` 按手里有没有同步依据在两个估计量
之间选（相位重叠口径 / 站立相恒等式），因为 `events.double_support` 在没有同步质量时
是**拒绝计算**的（PRD §13：跨足指标离开同步质量没有意义）。所以这里两种证据各钉一次，
且钉的是**具体的数**：只断言「有个值」的话，把两个估计量互换、或者把其中一条悄悄
换成兜底，测试都不会红。
"""

from __future__ import annotations

import statistics

import pytest

from gait.analysis import events
from gait.cloud.chain import run_basic_chain
from gait.report.assemble import assemble_report
from gait.report.basic import build_report
from gait.validate.synthetic import WalkSpec, generate_dual_walk
from tests.test_report_assemble import _SYNC, make_meta

_DURATION_S = 20.0


@pytest.fixture(scope="module")
def chain():
    dual = generate_dual_walk(WalkSpec(duration_s=_DURATION_S))
    series = {label: dual[label][0] for label in dual}
    return run_basic_chain(series, sync_quality=_SYNC, protocol_seconds=int(_DURATION_S))


def _selected(chain) -> list:
    return [cycle for label in sorted(chain.feet) for cycle in chain.feet[label].selected]


def _via_cycles(chain, meta, **overrides):
    """把链侧那份入参原样喂给采集端的入口。

    参数与 `assemble_report` 内部的翻译一一对应 —— 它就是「同一份数据」的定义。
    """
    kwargs = {
        "report_id": f"R-{meta.session_id[:8]}-{meta.session_id[-4:]}",
        "organization": "",
        "subject_label": f"**{meta.subject_uuid[:4]}",
        "assessed_at": meta.created_at[:10],
        "duration_s": int(meta.protocol_config["duration_s"]),
        "algo_version": chain.algo_version,
        "protocol_version": f"T-01 v{meta.protocol_config['version']}",
        "valid_seconds": None,
        "turns": chain.variability.turns if chain.variability else None,
        "chain": chain.chain,
        "sync_quality": _SYNC,
        "zupt_quality": None,
    }
    kwargs.update(overrides)
    return build_report(_selected(chain), **kwargs)


# ── 判据 1：同一份数据，逐字段相同 ────────────────────────────────────────────


def test_both_entry_points_produce_the_same_payload_field_for_field(chain):
    """同一份数据走两个入口，payload 必须**相等**，不是「差不多」。

    dict 相等一路比到每个数值、每条理由、每个 `quality` 快照。任何一边多一个键、
    少一行、或者把某个数换了个算法，这条就红。
    """
    meta = make_meta()
    assert assemble_report(chain, meta) == _via_cycles(chain, meta)


def test_both_entry_points_agree_when_the_session_has_no_variability_report(chain):
    """单足会话：`variability.analyse` 拒绝，链上没有变异性报告，于是转身次数**没数过**。

    单独钉这一条，是因为上面那条用的是双足数据 —— 那里 `chain.variability` 一直在，
    「没有变异性报告时怎么翻译转身次数」那条分支从来没被走到。变异验证里把链侧的
    `else None` 改成 `else 0`，上面那条纹丝不动；有了这条才红。

    分支本身要紧：翻成 0 等于让报告断言「这次一次都没转身」，而实情是没人数过。
    """
    meta = make_meta()
    single = {"L": generate_dual_walk(WalkSpec(duration_s=_DURATION_S))["L"][0]}
    one_foot = run_basic_chain(single, sync_quality=_SYNC, protocol_seconds=int(_DURATION_S))
    assert one_foot.variability is None, "这条用例的前提是链上没有变异性报告"

    payload = assemble_report(one_foot, meta)
    assert payload == _via_cycles(one_foot, meta)

    turns = next(row for row in payload["parameters"] if row["label"] == "转身次数")
    assert turns["grade"] == "uncomputable"
    assert turns["value"] == "本次不适用"


def test_the_equality_is_not_vacuous(chain):
    """守卫上一条：payload 得真的装着东西，否则「两个空 dict 相等」也能过。"""
    payload = assemble_report(chain, make_meta())
    assert payload["metrics"] and payload["parameters"] and payload["conditions"]
    assert payload["timeline"]["left"] and payload["timeline"]["right"]


# ── 判据 2：拉齐后没有任何一条路径失去它原有的 PRD §12 核心指标 ───────────────


def test_the_core_metrics_are_exactly_the_prd_four(chain):
    """步速、步频、步长、双支撑期 —— 一个不少，键名按产品路径（`double-support`）。"""
    payload = assemble_report(chain, make_meta())
    assert [metric["key"] for metric in payload["metrics"]] == [
        "speed",
        "cadence",
        "stride",
        "double-support",
    ]


def test_stride_survives_on_the_chain_path_with_its_value(chain):
    """**步长**从前只有 `build_report` 有 —— 走链那条路的报告里根本没有这一项。"""
    payload = assemble_report(chain, make_meta())
    stride = next(m for m in payload["metrics"] if m["key"] == "stride")
    expected = statistics.fmean(
        [cycle.stride_length for cycle in _selected(chain) if cycle.valid]
    )
    assert stride["value"] == f"{expected:.2f}"


def test_the_quality_footer_survives_on_the_chain_path(chain):
    """`qualityFooter` 同上：从前只有 `build_report` 有。"""
    payload = assemble_report(chain, make_meta())
    footer = payload["qualityFooter"]
    assert footer["metrics"] == len(payload["metrics"]) + len(payload["parameters"])
    assert footer["rules_version"]


def test_the_cli_only_parameters_are_merged_not_dropped(chain):
    """站立相/摆动相占比与转身次数是 PRD §13 的 v1 输出指标，并入而不是删。"""
    labels = [row["label"] for row in assemble_report(chain, make_meta())["parameters"]]
    for merged in ("左站立相占比", "右站立相占比", "摆动相占比", "步周期时长", "转身次数"):
        assert merged in labels
    # 产品路径原有的三项一个都没掉。
    for kept in ("步长变异系数", "步周期变异系数", "疲劳衰减"):
        assert kept in labels


# ── 判据 3：拉齐的是装配逻辑，不是输入的真伪 ─────────────────────────────────


def test_with_sync_evidence_double_support_is_the_phase_overlap_reading(chain):
    """有同步依据 → 相位重叠口径（PRD §13 点名的那个），比的是**具体的数**。"""
    payload = assemble_report(chain, make_meta())
    reading = next(m for m in payload["metrics"] if m["key"] == "double-support")
    cycles = [cycle for cycle in _selected(chain) if cycle.valid]
    expected = events.double_support(
        [c for c in cycles if c.foot == "L"],
        [c for c in cycles if c.foot == "R"],
        sync_quality=_SYNC,
    ).fraction * 100.0
    assert reading["value"] == f"{expected:.1f}"


def test_without_sync_evidence_double_support_falls_to_the_stance_identity(chain):
    """没有同步依据 → 站立相恒等式（足内量，不碰跨足时序），也比具体的数。

    这一项**不会**因为缺同步依据就消失：`reportFor` 那条路从来不传 `sync_quality`，
    若拉齐时统一成相位重叠口径，采集端的报告会当场失去一个 PRD §12 的核心指标。
    """
    meta = make_meta()
    payload = _via_cycles(chain, meta, sync_quality=None)
    reading = next(m for m in payload["metrics"] if m["key"] == "double-support")
    cycles = [cycle for cycle in _selected(chain) if cycle.valid]
    expected = 2 * statistics.fmean([c.stance_ratio for c in cycles]) - 100.0
    assert reading["value"] == f"{expected:.1f}"
    # 缺同步依据是一个**质量问题**，要说出来（PRD §13 强制附同步质量标注）。
    assert "missing_sync_quality" in reading["quality"]["reasons"]


def test_the_two_estimators_do_not_agree(chain):
    """守卫上面两条：若两个估计量碰巧给出同一个数，那两条断言就都不说明问题了。"""
    meta = make_meta()
    with_sync = assemble_report(chain, meta)
    without = _via_cycles(chain, meta, sync_quality=None)

    def reading(payload):
        return next(m for m in payload["metrics"] if m["key"] == "double-support")["value"]

    assert reading(with_sync) != reading(without)


# ── 模板是唯一的形状来源（R-4） ──────────────────────────────────────────────


def test_an_uncomputable_metric_carries_the_reason_the_template_prints(chain):
    """模板的 `MetricValue` 不可算时印的是 `metric.reason`。

    `build_report` 从前只给 `note` —— 于是版面上是「本次不适用」加一个**空的原因**。
    断言比的是那句**具体的话**：只断言「非空」的话，把翻译换成任何一句中文都不会红
    （`honest-sync-quality` 有一条变异就是这么漏掉的）。

    这句话本身**不对劲**：这次不可算的来路是「只有一只脚」，不是「协议不产出」。
    那是 RAY-398（`not_computable` 一个 reason 盖了两种来路）的范围，不在本 scope 里
    修。钉住现状是为了 RAY-398 动翻译时这条会红，有人来看一眼。
    """
    meta = make_meta()
    single = {"L": generate_dual_walk(WalkSpec(duration_s=_DURATION_S))["L"][0]}
    one_foot = run_basic_chain(single, sync_quality=_SYNC, protocol_seconds=int(_DURATION_S))
    payload = assemble_report(one_foot, meta)
    ds = next(m for m in payload["metrics"] if m["key"] == "double-support")
    assert ds["grade"] == "uncomputable"
    assert ds["quality"]["reasons"] == ["not_computable"]
    assert ds["reason"] == "本次协议不产出该项。"
