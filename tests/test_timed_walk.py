"""`gait.protocolflow.timed_walk` 的 T-01 定时步行状态机。

验收标准两条：**状态机单测覆盖停顿/中断/提前停止路径**；**配置进 `protocol_config`
元数据**。

这个文件里最重要的一组守的是三条底线里那条**现在评不了**的：RAY-260 证明左右戴反在
位置法下数学上不可判定，而它是佩戴底线的一部分。所以佩戴的取值是三态而不是布尔，
且「评不了」不等于「通过」—— 后者正是 PRD §13 唯一硬拦截被悄悄架空的方式。
"""

import pytest

from gait.config import ProtocolConfig
from gait.protocolflow.timed_walk import (
    CHECK_FAIL,
    CHECK_PASS,
    CHECK_UNKNOWN,
    FLOW_VERSION,
    SEGMENT_BASELINE,
    SEGMENT_CALIBRATION,
    SEGMENT_WALKING,
    STATE_ABORTED,
    STATE_BASELINE,
    STATE_CALIBRATION,
    STATE_FINISHED,
    STATE_IDLE,
    STATE_PAUSED,
    STATE_WALKING,
    VERDICT_INDETERMINATE,
    VERDICT_INVALID,
    VERDICT_VALID,
    ProtocolError,
    TimedWalk,
)


def walked(*, duration_s=60, pauses=(), stop_at=None) -> TimedWalk:
    """跑完一次测试。`pauses` 是 `(开始, 结束)` 的列表。"""
    flow = TimedWalk(ProtocolConfig(duration_s=duration_s))
    flow.start_baseline(0.0)
    flow.start_calibration(10.0)
    flow.start_walking(25.0)
    for start, stop in pauses:
        flow.pause(start)
        flow.resume(stop)
    flow.stop(stop_at if stop_at is not None else 25.0 + duration_s)
    return flow


# ── 验收：停顿路径 ────────────────────────────────────────────────────────────


def test_a_short_pause_still_counts_toward_the_valid_duration():
    """短停顿是正常的犹豫、避让、转身减速 —— 剔掉它们会让每次真实行走都损失几秒。"""
    flow = walked(pauses=[(40.0, 43.0)])

    assert flow.pauses[0].duration == 3.0
    assert not flow.pauses[0].skipped
    assert flow.skipped_seconds == 0.0
    assert flow.valid_seconds == flow.elapsed_seconds


def test_a_long_pause_is_marked_and_skipped_but_does_not_void_the_test():
    """PRD §7：超过阈值即标记该时段并跳过，**不作废测试**。"""
    flow = walked(pauses=[(40.0, 55.0)])

    assert flow.pauses[0].skipped
    assert flow.skipped_seconds == 15.0
    assert flow.valid_seconds == flow.elapsed_seconds - 15.0
    assert flow.state == STATE_FINISHED  # 没有变成 aborted


def test_the_pause_threshold_comes_from_the_configuration():
    strict = TimedWalk(ProtocolConfig(duration_s=60, pause_threshold_s=2.0))
    strict.start_baseline(0.0)
    strict.start_walking(10.0)
    strict.pause(20.0)
    strict.resume(23.0)
    strict.stop(70.0)

    assert strict.pauses[0].skipped  # 3 s > 2 s 阈值


def test_several_pauses_accumulate():
    flow = walked(pauses=[(35.0, 38.0), (45.0, 60.0), (65.0, 72.0)])

    assert [item.skipped for item in flow.pauses] == [False, True, True]
    assert flow.skipped_seconds == 15.0 + 7.0


def test_the_walking_intervals_exclude_the_skipped_pauses():
    """**返回区间而不是"总时长"。**

    跨停顿的步态周期本身也该被排除，而那需要知道停顿在**哪里**，不只是停了多久。
    """
    flow = walked(pauses=[(40.0, 43.0), (55.0, 70.0)], stop_at=95.0)

    assert list(flow.walking_intervals()) == [(25.0, 55.0), (70.0, 95.0)]


def test_a_short_pause_does_not_split_the_walking_intervals():
    flow = walked(pauses=[(40.0, 43.0)], stop_at=85.0)

    assert list(flow.walking_intervals()) == [(25.0, 85.0)]


# ── 验收：中断路径 ────────────────────────────────────────────────────────────


def test_aborting_keeps_the_data_and_closes_the_segment():
    """**中断的是流程，不是数据。**"""
    flow = TimedWalk(ProtocolConfig(duration_s=60))
    flow.start_baseline(0.0)
    flow.start_walking(10.0)
    flow.abort(40.0, reason="受试者不适")

    assert flow.state == STATE_ABORTED
    assert flow.walking_segment().stop == 40.0
    assert flow.elapsed_seconds == 30.0
    assert flow.protocol_snapshot()["abort_reason"] == "受试者不适"


def test_an_abort_without_a_reason_is_refused():
    """没有理由的中断在事后与"程序崩了"无法区分。"""
    flow = TimedWalk()
    flow.start_baseline(0.0)

    with pytest.raises(ProtocolError, match="必须给出理由"):
        flow.abort(10.0, reason="")


def test_aborting_during_a_pause_closes_that_pause_too():
    """否则那次停顿的时长会消失，有效时长随之算多。"""
    flow = TimedWalk(ProtocolConfig(duration_s=60))
    flow.start_baseline(0.0)
    flow.start_walking(10.0)
    flow.pause(30.0)
    flow.abort(50.0, reason="链路断开")

    assert len(flow.pauses) == 1
    assert flow.pauses[0].duration == 20.0
    assert flow.skipped_seconds == 20.0


def test_aborting_before_walking_still_records_what_happened():
    flow = TimedWalk()
    flow.start_baseline(0.0)
    flow.start_calibration(10.0)
    flow.abort(20.0, reason="标定失败")

    kinds = [item.kind for item in flow.segments]
    assert SEGMENT_BASELINE in kinds
    assert flow.elapsed_seconds == 0.0


def test_a_terminal_state_cannot_be_aborted_again():
    flow = walked()

    with pytest.raises(ProtocolError, match="终态"):
        flow.abort(200.0, reason="再来一次")


# ── 验收：提前停止路径 ────────────────────────────────────────────────────────


def test_stopping_early_yields_a_short_valid_duration():
    """提前停止不是错误 —— 它只是让有效时长不够，由判定去说。"""
    flow = walked(duration_s=60, stop_at=55.0)

    assert flow.state == STATE_FINISHED
    assert flow.elapsed_seconds == 30.0
    assert flow.measured_valid_fraction == pytest.approx(0.5)


def test_stopping_early_below_seventy_percent_fails_the_duration_line():
    """PRD §7 的 70%。"""
    flow = walked(duration_s=60, stop_at=55.0)
    verdict = flow.verdict(wearing=CHECK_PASS, link=CHECK_PASS)

    assert verdict.duration == CHECK_FAIL
    assert verdict.overall == VERDICT_INVALID
    assert any("valid_seconds" in reason for reason in verdict.reasons)


def test_stopping_while_paused_closes_the_pause():
    flow = TimedWalk(ProtocolConfig(duration_s=60))
    flow.start_baseline(0.0)
    flow.start_walking(10.0)
    flow.pause(50.0)
    flow.stop(80.0)

    assert flow.state == STATE_FINISHED
    assert flow.pauses[0].duration == 30.0


def test_just_above_seventy_percent_passes():
    """边界要落在正确的一侧。"""
    flow = walked(duration_s=60, stop_at=25.0 + 42.1)

    assert flow.verdict(wearing=CHECK_PASS, link=CHECK_PASS).overall == VERDICT_VALID


# ── 三条底线里那条现在评不了 ──────────────────────────────────────────────────


def test_wearing_defaults_to_unknown_not_pass():
    """**RAY-260：左右戴反在位置法下数学上不可判定。**

    在有一个可用的判据之前，这条底线的诚实答案是"评不了"。把"评不了"默认成"通过"
    正是 PRD §13 唯一硬拦截被悄悄架空的方式。
    """
    verdict = walked().verdict(link=CHECK_PASS)

    assert verdict.wearing == CHECK_UNKNOWN
    assert verdict.overall == VERDICT_INDETERMINATE
    assert "wearing_unknown" in verdict.reasons


def test_unknown_is_not_pass():
    """三态不是装饰：`unknown` 与 `pass` 必须给出不同的整体结论。"""
    flow = walked()

    unknown = flow.verdict(wearing=CHECK_UNKNOWN, link=CHECK_PASS)
    passed = flow.verdict(wearing=CHECK_PASS, link=CHECK_PASS)

    assert unknown.overall != passed.overall
    assert passed.overall == VERDICT_VALID


def test_a_failed_line_beats_an_unknown_one():
    """明确的失败比"评不了"更确定 —— 整体结论取更强的那个。"""
    verdict = walked().verdict(wearing=CHECK_UNKNOWN, link=CHECK_FAIL)

    assert verdict.overall == VERDICT_INVALID


def test_all_three_lines_are_reported_separately():
    """报告要能回答"哪一条没过"，不只是"没过"。"""
    verdict = walked(duration_s=60, stop_at=50.0).verdict(
        wearing=CHECK_PASS, link=CHECK_FAIL
    )
    snapshot = verdict.snapshot()

    assert snapshot["wearing"] == CHECK_PASS
    assert snapshot["link"] == CHECK_FAIL
    assert snapshot["duration"] == CHECK_FAIL
    assert "link_failed" in snapshot["reasons"]


def test_an_unknown_check_value_is_refused():
    with pytest.raises(ProtocolError, match="pass/fail/unknown"):
        walked().verdict(wearing="maybe")


# ── 会话组装 ──────────────────────────────────────────────────────────────────


def test_the_three_segments_are_recorded_in_order():
    """自检基线段 → 标定段 → 测试段。"""
    flow = walked()
    kinds = [item.kind for item in flow.segments]

    assert kinds == [SEGMENT_BASELINE, SEGMENT_CALIBRATION, SEGMENT_WALKING]


def test_segments_are_contiguous():
    """段之间不该有缝 —— 缝里的数据不属于任何一段，也就不会被任何一步处理。"""
    flow = walked()

    for current, following in zip(flow.segments[:-1], flow.segments[1:], strict=True):
        assert current.stop == following.start


def test_skipping_calibration_records_a_zero_length_segment_not_a_missing_one():
    """**段的存在与否比它的时长更重要。**

    下游按段名取数据，缺一个段会变成 `KeyError`；一个零长度的段是明确的"这次没做"。
    """
    flow = TimedWalk(ProtocolConfig(duration_s=60))
    flow.start_baseline(0.0)
    flow.start_walking(10.0)
    flow.stop(70.0)

    calibration = next(item for item in flow.segments if item.kind == SEGMENT_CALIBRATION)
    assert calibration.duration == 0.0


def test_the_walking_segment_is_the_one_downstream_should_use():
    """基线段是静止的、标定段里在做规定动作 —— 混进步态统计都会污染结果。"""
    flow = walked()

    assert flow.walking_segment().kind == SEGMENT_WALKING
    assert flow.walking_segment().start == 25.0


def test_asking_for_the_walking_segment_too_early_is_an_error():
    flow = TimedWalk()
    flow.start_baseline(0.0)

    with pytest.raises(ProtocolError, match="还没有测试段"):
        flow.walking_segment()


# ── 验收：配置进元数据 ────────────────────────────────────────────────────────


def test_the_snapshot_carries_the_protocol_configuration():
    snapshot = walked(duration_s=180).protocol_snapshot()

    assert snapshot["duration_s"] == 180
    assert snapshot["valid_fraction"] == 0.70
    assert snapshot["pause_threshold_s"] == 5.0
    assert snapshot["flow_version"] == FLOW_VERSION


def test_the_measured_fraction_does_not_overwrite_the_configured_threshold():
    """**这条测试记录的是一次真实的撞车。**

    `ProtocolConfig.snapshot()` 用 `valid_fraction` 存**判定阈值**（0.70）。实测比例
    起初也叫这个名字，于是它把阈值覆盖掉了 —— 读回来的元数据看起来就像"这次的阈值是
    100%"，而没有任何一步会报错。

    所以实测值叫 `measured_valid_fraction`，两个键并存。
    """
    flow = walked(duration_s=60, stop_at=55.0)  # 只走了一半
    snapshot = flow.protocol_snapshot()

    assert snapshot["valid_fraction"] == 0.70  # 阈值
    assert snapshot["measured_valid_fraction"] == pytest.approx(0.5)  # 实测


def test_the_snapshot_carries_the_segment_boundaries_and_pauses():
    """验收标准要求边界入元数据 —— 下游据此只取测试段。"""
    snapshot = walked(pauses=[(40.0, 55.0)]).protocol_snapshot()

    assert len(snapshot["segments"]) == 3
    assert snapshot["pauses"][0]["skipped"] is True
    assert snapshot["skipped_seconds"] == 15.0
    assert snapshot["valid_seconds"] == snapshot["elapsed_seconds"] - 15.0


def test_the_snapshot_is_plain_json_types():
    import json

    snapshot = walked(pauses=[(40.0, 55.0)]).protocol_snapshot()

    assert json.loads(json.dumps(snapshot, ensure_ascii=False))["state"] == STATE_FINISHED


def test_the_fatigue_availability_comes_from_the_configuration():
    """那条规则只该有一处实现 —— 状态机不重复判断，直接用配置的属性。"""
    assert TimedWalk(ProtocolConfig(duration_s=180)).config.fatigue_decay_available
    assert not TimedWalk(ProtocolConfig(duration_s=60)).config.fatigue_decay_available


# ── 非法转移 ──────────────────────────────────────────────────────────────────


def test_the_state_machine_refuses_illegal_transitions_rather_than_ignoring_them():
    """静默忽略会让 UI 与实际流程悄悄分叉，而分叉之后没有任何一方是权威。"""
    flow = TimedWalk()

    with pytest.raises(ProtocolError, match="当前状态是"):
        flow.pause(1.0)
    with pytest.raises(ProtocolError):
        flow.stop(1.0)
    with pytest.raises(ProtocolError):
        flow.resume(1.0)


def test_walking_cannot_start_from_idle():
    flow = TimedWalk()

    with pytest.raises(ProtocolError):
        flow.start_walking(0.0)


def test_a_finished_flow_cannot_be_resumed():
    flow = walked()

    with pytest.raises(ProtocolError):
        flow.pause(200.0)


def test_the_state_sequence_is_what_the_ui_consumes():
    """UI 只消费状态 —— 这条测试就是那个契约。"""
    flow = TimedWalk(ProtocolConfig(duration_s=60))
    observed = [flow.state]
    observed.append(flow.start_baseline(0.0))
    observed.append(flow.start_calibration(10.0))
    observed.append(flow.start_walking(25.0))
    observed.append(flow.pause(40.0))
    observed.append(flow.resume(43.0))
    observed.append(flow.stop(85.0))

    assert observed == [
        STATE_IDLE,
        STATE_BASELINE,
        STATE_CALIBRATION,
        STATE_WALKING,
        STATE_PAUSED,
        STATE_WALKING,
        STATE_FINISHED,
    ]


def test_time_going_backwards_is_an_explicit_failure():
    """状态机不持有时钟，所以单调性只能由调用方保证 —— 这里把它变成显式失败。"""
    flow = TimedWalk()
    flow.start_baseline(100.0)

    with pytest.raises(ProtocolError, match="时间倒流"):
        flow.start_calibration(50.0)


def test_a_backwards_resume_is_refused():
    flow = TimedWalk(ProtocolConfig(duration_s=60))
    flow.start_baseline(0.0)
    flow.start_walking(10.0)
    flow.pause(40.0)

    with pytest.raises(ProtocolError, match="时间倒流"):
        flow.resume(30.0)


def test_an_unfinished_walk_reports_zero_elapsed():
    """还没停的测试没有"墙上时长" —— 返回 0 而不是"到现在为止"。

    "到现在为止"需要一个时钟，而本模块刻意没有。让调用方在 `stop()` 时给出时间，
    比让本模块去猜"现在"要诚实。
    """
    flow = TimedWalk()
    flow.start_baseline(0.0)
    flow.start_walking(10.0)

    assert flow.elapsed_seconds == 0.0
