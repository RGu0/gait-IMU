"""RAY-302 `mac-identity-provider`：身份提供者与它的来源标识。

关注点不是「能不能读出 MAC」——那是上游的事——而是**排布若被推翻，旧绑定能不能
被认出来**。全部用合成应答，不需要设备。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from gait.device import identity as identity_module
from gait.device.binding import (
    BINDING_FORMAT_VERSION,
    PROVENANCE_UNKNOWN,
    BindingError,
    DeviceIdentity,
    FootBinding,
    admit_for_session,
    binding_path,
    read_binding,
    stale_provenances,
    write_binding,
)
from gait.device.identity import (
    CONFIRMED_DERIVATIONS,
    MAC_PROVENANCE,
    is_layout_confirmed,
    mac_identity,
    provenance_note,
    read_device_identity,
)

LEFT_MAC = "FD:08:90:1A:11:47"
RIGHT_MAC = "F9:B3:4F:46:C9:31"


class _FakeTelemetry:
    def __init__(self, mac: str | BaseException) -> None:
        self._mac = mac

    async def read_mac(self) -> str:
        if isinstance(self._mac, BaseException):
            raise self._mac
        return self._mac


class _FakeDevice:
    def __init__(self, mac: str | BaseException) -> None:
        self.telemetry = _FakeTelemetry(mac)


class TestTheProvider:
    """验收 1：从已连接设备读出 `DeviceIdentity(kind="mac", ...)`。"""

    def test_it_reads_the_mac_into_an_identity(self):
        identity = asyncio.run(read_device_identity(_FakeDevice(LEFT_MAC)))
        assert identity.kind == "mac"
        assert identity.value == LEFT_MAC

    def test_the_identity_carries_the_derivation(self):
        assert asyncio.run(
            read_device_identity(_FakeDevice(LEFT_MAC))
        ).provenance == MAC_PROVENANCE

    def test_a_failed_read_is_not_swallowed(self):
        """拿不到身份就跳过绑定，正是 RAY-196 要排除的东西。"""
        with pytest.raises(RuntimeError, match="boom"):
            asyncio.run(read_device_identity(_FakeDevice(RuntimeError("boom"))))

    def test_the_pure_helper_needs_no_device(self):
        # 纯函数，测试与回放路径不必去读寄存器。
        assert mac_identity(LEFT_MAC) == DeviceIdentity(
            kind="mac", value=LEFT_MAC, provenance=MAC_PROVENANCE
        )


class TestDerivationSurvivesTheRoundTrip:
    """验收 2：来源标识随绑定持久化。"""

    def test_provenance_reaches_the_file(self, tmp_path: Path):
        binding = FootBinding(left=mac_identity(LEFT_MAC), right=mac_identity(RIGHT_MAC))
        write_binding(tmp_path, binding)

        payload = json.loads(binding_path(tmp_path).read_text(encoding="utf-8"))
        assert payload["left"]["provenance"] == MAC_PROVENANCE
        assert payload["version"] == BINDING_FORMAT_VERSION

    def test_it_round_trips(self, tmp_path: Path):
        binding = FootBinding(left=mac_identity(LEFT_MAC), right=mac_identity(RIGHT_MAC))
        write_binding(tmp_path, binding)
        assert read_binding(tmp_path) == binding


class TestAnOverturnedLayoutIsDetected:
    """验收 3：来源不匹配报「绑定需重建」，与不在场、换设备分开。

    这是本 scope 存在的理由。排布被推翻时 `kind` 还是 `mac`、设备也还在场，
    只有值变了 —— 没有 provenance 就与「换了一台设备」无从区分。
    """

    def test_a_different_derivation_is_reported_stale(self):
        old = FootBinding(
            left=DeviceIdentity(kind="mac", value=LEFT_MAC, provenance="old-layout"),
            right=mac_identity(RIGHT_MAC),
        )
        assert stale_provenances(old, MAC_PROVENANCE) == ("old-layout",)

    def test_the_current_derivation_is_not_stale(self):
        binding = FootBinding(left=mac_identity(LEFT_MAC), right=mac_identity(RIGHT_MAC))
        assert stale_provenances(binding, MAC_PROVENANCE) == ()

    def test_admission_calls_it_a_rebuild_not_an_absence(self):
        old = FootBinding(
            left=DeviceIdentity(kind="mac", value=LEFT_MAC, provenance="old-layout"),
            right=DeviceIdentity(kind="mac", value=RIGHT_MAC, provenance="old-layout"),
        )
        # 设备**在场**，而且 kind 也没变 —— 只有推导变了。
        verdict = admit_for_session(
            old,
            [old.left, old.right],
            current_kind="mac",
            current_provenance=MAC_PROVENANCE,
        )
        assert not verdict.admitted
        # 恰好一条，且是「需重建」——不能顺带报出「不在场」之类别的理由。
        # 注：不能用 `"不在场" in p` 来断言，那条消息本身就含「也不是设备不在场」
        # 这句解释；要认的是缺席问题的特有形状「绑定的设备（…）不在场」。
        assert len(verdict.problems) == 1
        assert "绑定需重建" in verdict.problems[0]
        assert "推导" in verdict.problems[0]
        assert "绑定的设备（" not in verdict.problems[0]

    def test_a_matching_derivation_admits(self):
        binding = FootBinding(left=mac_identity(LEFT_MAC), right=mac_identity(RIGHT_MAC))
        verdict = admit_for_session(
            binding,
            [binding.left, binding.right],
            current_kind="mac",
            current_provenance=MAC_PROVENANCE,
        )
        assert verdict.admitted

    def test_kind_change_and_derivation_change_read_differently(self):
        """两条守的是不同的维，理由文案也该不同。"""
        binding = FootBinding(left=mac_identity(LEFT_MAC), right=mac_identity(RIGHT_MAC))
        present = [binding.left, binding.right]
        kind_changed = admit_for_session(
            binding, present, current_kind="serial"
        ).problems
        derivation_changed = admit_for_session(
            binding, present, current_provenance="something-else"
        ).problems
        assert kind_changed != derivation_changed

    def test_an_empty_derivation_is_refused(self):
        binding = FootBinding(left=mac_identity(LEFT_MAC))
        with pytest.raises(BindingError, match="不能为空"):
            stale_provenances(binding, "  ")


class TestOldBindingsAreReadableNotRefused:
    """验收 4：1.0 格式（无来源标识）读得回来，并被判为需重建。

    refuse 掉丢信息：那只说「读不了」，而实际情况是「读到了，但它是用不明来源的
    键建的」—— 后者才够操作者判断该怎么办。
    """

    def _write_v1_0(self, root: Path) -> None:
        payload = {
            "version": "1.0",
            "left": {"kind": "mac", "value": LEFT_MAC},
            "right": {"kind": "mac", "value": RIGHT_MAC},
        }
        binding_path(root).write_text(json.dumps(payload), encoding="utf-8")

    def test_a_v1_0_binding_still_reads(self, tmp_path: Path):
        self._write_v1_0(tmp_path)
        binding = read_binding(tmp_path)
        assert binding.left is not None
        assert binding.left.value == LEFT_MAC

    def test_its_derivation_reads_as_unknown(self, tmp_path: Path):
        # 「没记录来源」的真实含义就是「来源未知」，不是一个凑数的默认值。
        self._write_v1_0(tmp_path)
        assert read_binding(tmp_path).left.provenance == PROVENANCE_UNKNOWN

    def test_it_is_then_reported_as_needing_a_rebuild(self, tmp_path: Path):
        self._write_v1_0(tmp_path)
        binding = read_binding(tmp_path)
        assert stale_provenances(binding, MAC_PROVENANCE) == (PROVENANCE_UNKNOWN,)

    def test_rewriting_it_upgrades_the_format(self, tmp_path: Path):
        self._write_v1_0(tmp_path)
        write_binding(tmp_path, read_binding(tmp_path))
        payload = json.loads(binding_path(tmp_path).read_text(encoding="utf-8"))
        assert payload["version"] == BINDING_FORMAT_VERSION
        assert payload["left"]["provenance"] == PROVENANCE_UNKNOWN

    def test_a_genuinely_unknown_version_is_still_refused(self, tmp_path: Path):
        binding_path(tmp_path).write_text(
            json.dumps({"version": "9.9", "left": None, "right": None}),
            encoding="utf-8",
        )
        with pytest.raises(BindingError, match="读得了"):
            read_binding(tmp_path)


class TestTheCaveatIsRecorded:
    """验收 5：「排布未经外部证实」这个状态可查。"""

    def test_the_layout_is_not_yet_externally_confirmed(self):
        # 这条是当前事实的快照。哪天真机比对过了，把那个推导加进
        # CONFIRMED_DERIVATIONS 并附证据，它会提醒你。
        assert not is_layout_confirmed(MAC_PROVENANCE)
        assert CONFIRMED_DERIVATIONS == frozenset()

    def test_the_session_note_carries_the_caveat_while_unconfirmed(self):
        note = provenance_note()
        assert note["provenance"] == MAC_PROVENANCE
        assert note["layout_externally_confirmed"] is False
        assert "尚未与另一主机显示的 MAC 比对" in note["caveat"]

    def test_confirmation_must_not_change_the_derivation(self):
        """证实只去掉一条保留，不改变任何值。

        若有人在「证实」时顺手改了 MAC_PROVENANCE，所有已有绑定会被误报成需重建
        —— 这条测试把那个错误动作钉住。
        """
        assert "2026-08-27" in MAC_PROVENANCE
        assert "confirmed" not in MAC_PROVENANCE


class TestConfirmationIsIndexedByDerivation:
    """RAY-303：证实状态按**推导**索引，不是一个描述「当前推导」的裸布尔。

    裸布尔回答不了「这条旧记录的推导证实过没有」，而那正是真机验证计划会问的
    问题。
    """

    def test_an_unconfirmed_derivation_reads_false(self):
        assert is_layout_confirmed("some-derivation") is False

    def test_unknown_provenance_reads_false(self):
        # 1.0 格式的历史绑定：来源都不知道，谈不上证实过。
        assert is_layout_confirmed(PROVENANCE_UNKNOWN) is False

    def test_a_confirmed_derivation_reads_true(self, monkeypatch):
        monkeypatch.setattr(
            identity_module, "CONFIRMED_DERIVATIONS", frozenset({"layout-y"})
        )
        assert is_layout_confirmed("layout-y") is True

    def test_confirming_a_newer_layout_does_not_confirm_the_overturned_one(
        self, monkeypatch
    ):
        """本 Issue 的核心序列，逐步走一遍。

        X 未证实 → 用 X 建了绑定 → 真机比对发现对不上 → 换成 Y → Y 经证实。
        此刻那条 `provenance = X` 的旧记录**仍然**必须读成未证实 —— X 恰恰是
        被推翻的那个。裸布尔在这里会答 `True`。
        """
        overturned, current = "layout-x", "layout-y"
        monkeypatch.setattr(
            identity_module, "CONFIRMED_DERIVATIONS", frozenset({current})
        )

        assert is_layout_confirmed(current) is True
        assert is_layout_confirmed(overturned) is False

    def test_several_derivations_can_be_confirmed_at_once(self, monkeypatch):
        # 证实是累积的：证实 Y 不该把先前证实过的 X 抹掉。
        monkeypatch.setattr(
            identity_module, "CONFIRMED_DERIVATIONS", frozenset({"x", "y"})
        )
        assert is_layout_confirmed("x") and is_layout_confirmed("y")
        assert not is_layout_confirmed("z")

    def test_a_non_string_provenance_is_refused(self):
        with pytest.raises(TypeError, match="必须是 str"):
            is_layout_confirmed(None)


class TestTheSessionNoteFollowsTheDerivation:
    def test_the_note_reports_the_status_of_the_derivation_it_used(self, monkeypatch):
        monkeypatch.setattr(
            identity_module, "CONFIRMED_DERIVATIONS", frozenset({MAC_PROVENANCE})
        )
        note = provenance_note()
        assert note["provenance"] == MAC_PROVENANCE
        assert note["layout_externally_confirmed"] is True
        assert note["caveat"] is None

    def test_confirming_some_other_derivation_leaves_the_caveat(self, monkeypatch):
        # 证实了别的推导，不该让本次所用推导的保留消失。
        monkeypatch.setattr(
            identity_module, "CONFIRMED_DERIVATIONS", frozenset({"unrelated"})
        )
        note = provenance_note()
        assert note["layout_externally_confirmed"] is False
        assert "尚未与另一主机显示的 MAC 比对" in note["caveat"]
