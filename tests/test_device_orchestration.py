"""RAY-197 `session-orchestration`：开流前准入、断连语义与收尾遥测。

全部用合成 `Battery` 与结局对象，不需要蓝牙 —— 这正是把判断与 I/O 分开的目的。
"""

from __future__ import annotations

import pytest
from wt901 import Battery, ReconnectPolicy

from gait.device.orchestration import (
    MIN_BATTERY_PERCENT,
    LinkOutcome,
    OrchestrationError,
    PreflightVerdict,
    SessionOutcome,
    preflight_battery,
    reconnect_snapshot,
    summarize_session,
)

FULL = Battery(raw=396, percent=100)
JUST_ENOUGH = Battery(raw=373, percent=30)
LOW = Battery(raw=370, percent=20)
INVALID = Battery(raw=0, percent=None)


class TestBatteryGate:
    """验收 2：电量在开高速流之前读取，<30% 阻断。"""

    def test_both_feet_charged_is_admitted(self):
        verdict = preflight_battery({"L": FULL, "R": FULL})
        assert verdict.admitted
        assert verdict.problems == ()

    def test_exactly_the_threshold_is_admitted(self):
        # 判据是「低于 30% 阻断」，30% 本身放行。30 在阶梯表里是精确档位
        # （raw >= 373），所以这里没有边界抖动的余地。
        assert preflight_battery({"L": JUST_ENOUGH, "R": FULL}).admitted

    def test_one_foot_below_threshold_blocks_the_session(self):
        verdict = preflight_battery({"L": LOW, "R": FULL})
        assert not verdict.admitted
        assert any("低于" in p and "左脚" in p for p in verdict.problems)

    def test_both_feet_low_reports_both(self):
        assert len(preflight_battery({"L": LOW, "R": LOW}).problems) == 2

    def test_the_threshold_is_the_documented_one(self):
        assert MIN_BATTERY_PERCENT == 30


class TestUnknownIsNotLow:
    """电量有三种状态。把「未知」说成「不足」会让操作者去换电池，而问题在别处。"""

    def test_unreadable_battery_blocks_but_says_it_is_not_low(self):
        verdict = preflight_battery({"L": None, "R": FULL})
        assert not verdict.admitted
        assert any("读不到" in p for p in verdict.problems)
        assert any("换电池解决不了" in p for p in verdict.problems)

    def test_invalid_reading_blocks_but_says_it_is_not_low(self):
        # wt901 的 battery_percent 对 raw <= 0 返回 None：一台刚回答完寄存器读
        # 的设备不可能是 0 V，那是无效读数而不是没电。
        verdict = preflight_battery({"L": INVALID, "R": FULL})
        assert not verdict.admitted
        assert any("读数无效" in p for p in verdict.problems)
        assert any("换电池解决不了" in p for p in verdict.problems)

    def test_low_and_unknown_give_different_reasons(self):
        low = preflight_battery({"L": LOW, "R": FULL}).problems
        unknown = preflight_battery({"L": None, "R": FULL}).problems
        assert low != unknown

    def test_unknown_is_blocked_not_waved_through(self):
        # PRD 要求电量在开流前被「记录」；记不到等于前置条件没成立。
        assert not preflight_battery({"L": None, "R": None}).admitted

    def test_readings_are_recorded_even_when_blocked(self):
        verdict = preflight_battery({"L": None, "R": LOW})
        assert verdict.readings["L"] == {"percent": None, "raw": None, "read": False}
        assert verdict.readings["R"]["raw"] == 370


class TestPreflightInputGuards:
    def test_a_missing_foot_is_refused_not_assumed_fine(self):
        with pytest.raises(OrchestrationError, match="缺少这些脚"):
            preflight_battery({"L": FULL})

    def test_an_unknown_foot_label_is_refused(self):
        with pytest.raises(OrchestrationError, match="未知脚标"):
            preflight_battery({"L": FULL, "R": FULL, "X": FULL})

    def test_admitted_with_problems_is_refused_at_construction(self):
        with pytest.raises(OrchestrationError, match="两头猜"):
            PreflightVerdict(admitted=True, problems=("x",))


class TestSessionCompleteness:
    """验收 4：单路断连时另一路数据完整，会话正确标记不完整。"""

    def _links(self, **overrides) -> tuple[LinkOutcome, ...]:
        left = LinkOutcome(foot="L", **overrides)
        return (left, LinkOutcome(foot="R"))

    def test_two_clean_links_make_a_complete_session(self):
        outcome = summarize_session(self._links())
        assert outcome.complete
        assert outcome.problems == ()

    def test_one_disconnect_makes_the_session_incomplete(self):
        outcome = summarize_session(self._links(disconnected_at=123.4))
        assert not outcome.complete
        assert any("断连" in p and "左脚" in p for p in outcome.problems)

    def test_the_other_link_is_still_reported_clean(self):
        # 「另一路数据完整」与「会话完整」是两回事 —— 前者成立不代表后者成立。
        outcome = summarize_session(self._links(disconnected_at=1.0))
        by_foot = {link.foot: link for link in outcome.links}
        assert by_foot["R"].clean
        assert not by_foot["L"].clean
        assert not outcome.complete

    def test_a_reconnect_without_a_final_disconnect_still_breaks_continuity(self):
        # 重连成功不等于序列连续：wt901 的 seq 每次重连后归零。
        outcome = summarize_session(self._links(reconnects=2))
        assert not outcome.complete
        assert any("重连过 2 次" in p for p in outcome.problems)

    def test_a_recording_error_makes_the_session_incomplete(self):
        outcome = summarize_session(self._links(recording_error="disk full"))
        assert not outcome.complete
        assert any("落盘失败" in p for p in outcome.problems)

    def test_a_session_needs_exactly_two_feet(self):
        with pytest.raises(OrchestrationError, match="恰好两条链路"):
            SessionOutcome(complete=True, links=(LinkOutcome(foot="L"),))

    def test_duplicate_feet_are_refused(self):
        with pytest.raises(OrchestrationError, match="恰好两条链路"):
            summarize_session((LinkOutcome(foot="L"), LinkOutcome(foot="L")))

    def test_a_bad_foot_label_is_refused(self):
        with pytest.raises(OrchestrationError, match="脚标"):
            LinkOutcome(foot="left")

    def test_negative_reconnects_are_refused(self):
        with pytest.raises(OrchestrationError, match="不能为负"):
            LinkOutcome(foot="L", reconnects=-1)


class TestClosingTelemetry:
    """验收 3：结束后读电量 + 温度并记录。"""

    def test_temperature_is_recorded_not_judged(self):
        # PRD 要「记录温升」，没给阈值 —— 本模块不发明一个。
        link = LinkOutcome(foot="L", temperature_after_c=38.25)
        assert "38.2" in link.temperature_rise_note
        assert link.snapshot()["temperature_after_c"] == 38.25

    def test_a_missing_temperature_is_none_not_zero(self):
        assert LinkOutcome(foot="L").temperature_rise_note is None

    def test_before_and_after_battery_both_reach_the_snapshot(self):
        link = LinkOutcome(foot="R", battery_before=FULL, battery_after=LOW)
        snap = link.snapshot()
        assert snap["battery_before"]["percent"] == 100
        assert snap["battery_after"]["percent"] == 20

    def test_an_unread_closing_battery_is_marked_unread(self):
        snap = LinkOutcome(foot="L", battery_before=FULL).snapshot()
        assert snap["battery_after"] == {"percent": None, "raw": None, "read": False}


class TestReconnectPolicyIsRecorded:
    """验收 5：重连策略显式设定并进会话元数据。"""

    def test_the_policy_reaches_the_snapshot(self):
        snap = reconnect_snapshot(ReconnectPolicy(), enabled=False)
        assert snap["auto_reconnect"] is False
        assert snap["initial_delay_s"] == 0.5
        assert snap["max_delay_s"] == 30.0

    def test_a_custom_policy_is_recorded_as_given(self):
        policy = ReconnectPolicy(initial_delay=1.0, max_delay=8.0, max_attempts=3)
        snap = reconnect_snapshot(policy, enabled=True)
        assert snap["auto_reconnect"] is True
        assert snap["max_attempts"] == 3

    def test_unlimited_retries_are_recorded_as_none_not_zero(self):
        # max_attempts=None 表示一直重试；记成 0 会读成「不重试」，正好相反。
        assert reconnect_snapshot(ReconnectPolicy(), enabled=True)["max_attempts"] is None
