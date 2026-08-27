"""RAY-196 `foot-binding`：左右绑定的持久化、会话准入与重新配对。

验收对应关系写在各测试类的 docstring 里。真实 MAC 读取属 WT901 RAY-279，
这里一律用合成身份。
"""

from __future__ import annotations

import json
from dataclasses import fields
from pathlib import Path

import pytest

from gait.device.binding import (
    BINDING_FORMAT_VERSION,
    AdmissionVerdict,
    BindingError,
    DeviceIdentity,
    FootBinding,
    admit_for_session,
    binding_path,
    read_binding,
    stale_identity_kinds,
    write_binding,
)

LEFT_MAC = DeviceIdentity(kind="mac", value="AA:BB:CC:DD:EE:01")
RIGHT_MAC = DeviceIdentity(kind="mac", value="AA:BB:CC:DD:EE:02")
BOUND = FootBinding(left=LEFT_MAC, right=RIGHT_MAC)


class TestIdentity:
    def test_mac_is_normalized_so_the_same_device_compares_equal(self):
        # 大小写与分隔符不同的同一个 MAC 必须相等 —— 否则「认不出」看起来会
        # 和「换了一台设备」一模一样。
        assert DeviceIdentity(kind="mac", value="aa-bb-cc-dd-ee-01") == LEFT_MAC

    def test_serial_is_not_uppercased(self):
        # 序列号是 ASCII 字符串，大小写有意义，不能照 MAC 那样规范化。
        assert DeviceIdentity(kind="serial", value="wt901abc").value == "wt901abc"

    @pytest.mark.parametrize("kind", ["mac", "serial"])
    def test_device_reported_identities_are_portable(self, kind):
        assert DeviceIdentity(kind=kind, value="x").portable

    def test_platform_address_is_not_portable(self):
        # macOS 上它是 CoreBluetooth 会话内标识，换机即失效。
        assert not DeviceIdentity(kind="platform-address", value="x").portable

    def test_unknown_kind_is_refused(self):
        with pytest.raises(BindingError, match="未知的身份种类"):
            DeviceIdentity(kind="uuid", value="x")

    @pytest.mark.parametrize("value", ["", "   "])
    def test_empty_value_is_refused(self, value):
        with pytest.raises(BindingError, match="不能为空"):
            DeviceIdentity(kind="mac", value=value)


class TestOneDeviceOneFoot:
    """一台设备不能同时是两只脚 —— 否则 `label_for` 没有正确答案。"""

    def test_constructing_both_feet_from_one_device_is_refused(self):
        with pytest.raises(BindingError, match="不能同时绑成左右脚"):
            FootBinding(left=LEFT_MAC, right=LEFT_MAC)

    def test_binding_a_device_removes_it_from_the_other_foot(self):
        moved = BOUND.bind("L", RIGHT_MAC)
        assert moved.left == RIGHT_MAC
        assert moved.right is None

    def test_label_for_needs_both_kind_and_value(self):
        same_value_other_kind = DeviceIdentity(
            kind="serial", value="AA:BB:CC:DD:EE:01"
        )
        assert BOUND.label_for(LEFT_MAC) == "L"
        assert BOUND.label_for(same_value_other_kind) is None


class TestRepairPaths:
    """验收 4：绑定错误可通过「重新配对」修正。"""

    def test_swap_fixes_reversed_feet_in_one_step(self):
        swapped = BOUND.swap()
        assert swapped.left == RIGHT_MAC
        assert swapped.right == LEFT_MAC

    def test_swap_is_its_own_inverse(self):
        assert BOUND.swap().swap() == BOUND

    def test_unbind_then_bind_is_available_for_a_single_foot(self):
        spare = DeviceIdentity(kind="mac", value="AA:BB:CC:DD:EE:03")
        repaired = BOUND.unbind("R").bind("R", spare)
        assert repaired.left == LEFT_MAC
        assert repaired.right == spare

    def test_unbind_leaves_the_other_foot_alone(self):
        assert BOUND.unbind("L") == FootBinding(left=None, right=RIGHT_MAC)

    @pytest.mark.parametrize("bad", ["l", "left", "X", ""])
    def test_a_bad_foot_label_is_refused(self, bad):
        with pytest.raises(BindingError, match="脚标"):
            BOUND.get(bad)

    def test_binding_is_immutable_so_the_original_survives(self):
        BOUND.swap()
        BOUND.unbind("L")
        assert BOUND.left == LEFT_MAC and BOUND.right == RIGHT_MAC


class TestPersistenceAcrossMachines:
    """验收 2：换机重启后绑定关系保持。"""

    def test_round_trip_through_disk_preserves_both_feet(self, tmp_path: Path):
        write_binding(tmp_path, BOUND)
        assert read_binding(tmp_path) == BOUND

    def test_a_fresh_process_reading_the_same_file_still_knows_left_from_right(
        self, tmp_path: Path
    ):
        # 「换机」在本仓库能验的部分：同一份持久化字节、一个全新的对象图，
        # 不依赖任何进程内状态。真机换机验证属 RAY-279。
        write_binding(tmp_path, BOUND)
        payload = json.loads(binding_path(tmp_path).read_text(encoding="utf-8"))
        restored = FootBinding.from_snapshot(payload)
        assert restored.label_for(LEFT_MAC) == "L"
        assert restored.label_for(RIGHT_MAC) == "R"

    def test_missing_file_reads_as_empty_not_as_an_error(self, tmp_path: Path):
        # 「还没绑过」是首次使用的正常状态。
        assert read_binding(tmp_path) == FootBinding()

    def test_a_corrupt_file_raises_instead_of_looking_unbound(self, tmp_path: Path):
        # 关键区别：坏文件绝不能读成「还没绑过」—— 那会让操作者重新配对一遍，
        # 而真正的问题（文件坏了）没人知道。
        binding_path(tmp_path).write_text("{not json", encoding="utf-8")
        with pytest.raises(BindingError, match="不是合法 JSON"):
            read_binding(tmp_path)

    def test_an_unknown_version_is_refused_not_reinterpreted(self, tmp_path: Path):
        payload = BOUND.snapshot() | {"version": "0.9"}
        binding_path(tmp_path).write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(BindingError, match="读得了"):
            read_binding(tmp_path)

    def test_the_written_file_records_the_kind_not_just_the_value(
        self, tmp_path: Path
    ):
        write_binding(tmp_path, BOUND)
        payload = json.loads(binding_path(tmp_path).read_text(encoding="utf-8"))
        assert payload["left"]["kind"] == "mac"
        assert payload["version"] == BINDING_FORMAT_VERSION

    def test_no_partial_file_is_left_behind(self, tmp_path: Path):
        write_binding(tmp_path, BOUND)
        assert [p.name for p in tmp_path.iterdir()] == [binding_path(tmp_path).name]

    def test_snapshot_covers_every_field(self):
        # 加了字段却忘了同步 snapshot，历史绑定会被静默读成缺字段。
        assert {f.name for f in fields(FootBinding)} <= set(BOUND.snapshot())


class TestSessionAdmission:
    """验收 3：未绑定模块可见但不可用于正式会话。"""

    def test_both_feet_bound_and_present_is_admitted(self):
        verdict = admit_for_session(BOUND, [LEFT_MAC, RIGHT_MAC])
        assert verdict.admitted
        assert verdict.problems == ()

    def test_an_unbound_extra_device_does_not_block_the_session(self):
        # 未绑定模块「可见」—— 它出现在扫描结果里不是拒绝的理由。
        stranger = DeviceIdentity(kind="mac", value="AA:BB:CC:DD:EE:09")
        assert admit_for_session(BOUND, [LEFT_MAC, RIGHT_MAC, stranger]).admitted

    def test_a_missing_binding_is_refused_with_an_actionable_reason(self):
        verdict = admit_for_session(FootBinding(left=LEFT_MAC), [LEFT_MAC])
        assert not verdict.admitted
        assert any("右脚尚未绑定" in p for p in verdict.problems)

    def test_a_bound_but_absent_device_is_refused(self):
        verdict = admit_for_session(BOUND, [LEFT_MAC])
        assert not verdict.admitted
        assert any("不在场" in p for p in verdict.problems)

    def test_absent_and_unbound_give_different_reasons(self):
        # 这两种情况需要的操作完全不同：一个去配对，一个去开机。
        unbound = admit_for_session(FootBinding(left=LEFT_MAC), [LEFT_MAC]).problems
        absent = admit_for_session(BOUND, [LEFT_MAC]).problems
        assert unbound != absent

    def test_nothing_bound_reports_both_feet(self):
        verdict = admit_for_session(FootBinding(), [])
        assert not verdict.admitted
        assert len(verdict.problems) == 2

    def test_a_non_portable_binding_is_refused_even_when_present(self):
        # platform-address 换机即静默失效，正是本 Issue 验收要排除的。
        local = DeviceIdentity(kind="platform-address", value="uuid-1")
        verdict = admit_for_session(
            FootBinding(left=local, right=RIGHT_MAC), [local, RIGHT_MAC]
        )
        assert not verdict.admitted
        assert any("换一台主机就认不出" in p for p in verdict.problems)


class TestStaleIdentityKind:
    """验收 5：键种类不匹配报为「绑定需重建」，不是静默失配。"""

    def test_matching_kind_is_not_stale(self):
        assert stale_identity_kinds(BOUND, "mac") == ()

    def test_a_changed_identity_source_is_reported(self):
        assert stale_identity_kinds(BOUND, "serial") == ("mac",)

    def test_empty_binding_is_never_stale(self):
        assert stale_identity_kinds(FootBinding(), "serial") == ()

    def test_admission_separates_rebuild_from_absence(self):
        # 没有 kind 这一维时，这个场景与「设备不在场」在数据上无法区分 ——
        # 而两者需要的动作不同。这条测试就是那个区分本身。
        verdict = admit_for_session(BOUND, [], current_kind="serial")
        assert not verdict.admitted
        assert any("绑定需重建" in p for p in verdict.problems)

    def test_rebuild_is_not_reported_when_the_source_matches(self):
        verdict = admit_for_session(BOUND, [LEFT_MAC, RIGHT_MAC], current_kind="mac")
        assert verdict.admitted

    def test_unknown_current_kind_is_refused(self):
        with pytest.raises(BindingError, match="未知的身份种类"):
            stale_identity_kinds(BOUND, "uuid")


class TestVerdictSelfConsistency:
    def test_admitted_with_problems_is_refused_at_construction(self):
        # 「通过了但有问题」会逼调用方在两个字段之间猜哪个算数。
        with pytest.raises(BindingError, match="两头猜"):
            AdmissionVerdict(admitted=True, problems=("x",))

    def test_bad_types_are_refused_rather_than_silently_ignored(self):
        with pytest.raises(BindingError, match="FootBinding"):
            admit_for_session("not a binding", [])
        with pytest.raises(BindingError, match="DeviceIdentity"):
            admit_for_session(BOUND, ["not an identity"])
        with pytest.raises(BindingError, match="DeviceIdentity"):
            BOUND.bind("L", "not an identity")
