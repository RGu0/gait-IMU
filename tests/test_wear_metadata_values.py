"""RAY-287 R4 `wear-confirm-metadata`：两个字段在产品路径上**真的有值**。

`contract-1-2-bump` 只定了形状（产品路径上恒为 `null`）。这里钉的是谁在什么时候写进去：

* P-07 的确认随 `startSession` 到达，写进**开始时**的元数据 —— 进程中途被杀也留得住；
* `inversion_signature()` 在第一次出报告时算、只记一次，**不改变报告的任何一个字**。
"""

from __future__ import annotations

import math
from dataclasses import replace
from pathlib import Path

import pytest

from gait.app import protocol
from gait.app.service import TerminalService, _signature_record
from gait.app.sources import StubDeviceSource
from gait.io.session import list_sessions, read_meta, session_directory, write_meta
from tests.test_app_cycles_pipeline import recorded_session  # noqa: F401  (fixture)

CONFIRMED_AT = "2026-09-28T03:00:00.000Z"


def _start(root: Path, params: dict) -> tuple[TerminalService, str]:
    service = TerminalService(source=StubDeviceSource(), session_root=root)
    response = service.handle({"id": "s", "method": "startSession", "params": {"now": 0.0, **params}})
    return service, response["result"]["sessionId"]


def _meta(root: Path, session_id: str):
    return read_meta(session_directory(root, session_id))


def test_the_confirmation_is_on_disk_before_the_walk_ends(tmp_path):
    """开走时就落盘：一个被杀掉的会话也该说得出「开始前确认过」。"""
    _, session_id = _start(tmp_path, {"wearConfirmation": {"result": "pass", "confirmedAt": CONFIRMED_AT}})
    assert _meta(tmp_path, session_id).wear_confirmation == {"result": "pass", "confirmed_at": CONFIRMED_AT}


def test_the_confirmation_survives_the_end_of_session_rewrite(tmp_path):
    service, session_id = _start(
        tmp_path, {"wearConfirmation": {"result": "pass", "confirmedAt": CONFIRMED_AT}}
    )
    service.handle({"id": "t", "method": "stopSession", "params": {"now": 5.0}})
    assert _meta(tmp_path, session_id).wear_confirmation["result"] == "pass"


def test_no_confirmation_is_recorded_as_unknown_not_as_missing(tmp_path):
    """sidecar 确切知道这场会话开始时没有收到确认 —— 那是 `unknown`，不是 `null`。"""
    _, session_id = _start(tmp_path, {})
    assert _meta(tmp_path, session_id).wear_confirmation == {"result": "unknown", "confirmed_at": None}


@pytest.mark.parametrize(
    "bad",
    [
        {"result": "pass"},
        {"result": "pass", "confirmedAt": "张三"},
        {"result": "pass", "confirmedAt": CONFIRMED_AT, "operatorId": "op-1"},
        {"result": "confirmed", "confirmedAt": CONFIRMED_AT},
        "pass",
    ],
)
def test_a_malformed_confirmation_refuses_to_start(tmp_path, bad):
    """送错了就当场暴露，而不是悄悄改记成 unknown —— 也不留半开的会话。"""
    service = TerminalService(source=StubDeviceSource(), session_root=tmp_path)
    with pytest.raises(protocol.ProtocolError):
        service.handle(
            {"id": "s", "method": "startSession", "params": {"now": 0.0, "wearConfirmation": bad}}
        )
    assert service.walk is None
    assert list_sessions(tmp_path) == []


def test_operator_identity_does_not_reach_the_session_file(tmp_path):
    """RAY-323 R1 决定 3：身份不进会话元数据。

    这里没有登录（预览终端本来就没有）；**带身份的确认输入**由上一条用例在入口拒绝。
    本条钉的是落盘结果：会话文件里根本没有 operator 这个词。
    """
    _, session_id = _start(
        tmp_path, {"wearConfirmation": {"result": "pass", "confirmedAt": CONFIRMED_AT}}
    )
    text = (session_directory(tmp_path, session_id) / "meta.json").read_text(encoding="utf-8")
    assert "operator" not in text


# ── inversion_signature ──────────────────────────────────────────────────


def test_the_first_report_records_the_signature(recorded_session):  # noqa: F811
    root, session_id = recorded_session
    assert _meta(root, session_id).inversion_signature is None

    TerminalService(session_root=root)._do_reportFor({"sessionId": session_id})

    recorded = _meta(root, session_id).inversion_signature
    # 合成录制两足同长、时间轴一致，所以这里一定算得出。
    # 合成模型没有横滚（RAY-206 的已声明限制），差值只是噪声 —— 这里只验「算得对、记得下」。
    assert recorded["state"] == "computed"
    assert math.isfinite(recorded["difference"])
    assert recorded["strides_used"] >= 1


def test_the_signature_changes_nothing_in_the_report(recorded_session):  # noqa: F811
    """不参与判定、不进报告：有它没它，报告逐字相同。"""
    root, session_id = recorded_session
    service = TerminalService(session_root=root)
    first = service._do_reportFor({"sessionId": session_id, "reportId": "R"})
    assert _meta(root, session_id).inversion_signature is not None
    second = service._do_reportFor({"sessionId": session_id, "reportId": "R"})
    assert first == second
    assert "inversion" not in str(first)
    assert "signature" not in str(first)


def test_reopening_a_report_does_not_rewrite_the_signature(recorded_session):  # noqa: F811
    root, session_id = recorded_session
    directory = session_directory(root, session_id)
    marker = {"state": "not_computed", "reason": "预先写入的值，不应被覆盖"}
    write_meta(directory, replace(read_meta(directory), inversion_signature=marker))

    TerminalService(session_root=root)._do_reportFor({"sessionId": session_id})
    assert _meta(root, session_id).inversion_signature == marker


class _Nav:
    def __init__(self, navigation):
        self.navigation = navigation


class _Chain:
    def __init__(self, feet):
        self.feet = feet


def test_one_foot_is_recorded_as_not_computed():
    record = _signature_record(_Chain({"L": _Nav(None)}))
    assert record["state"] == "not_computed"
    assert "两只脚" in record["reason"]


def test_an_infinite_significance_is_stored_as_null(monkeypatch):
    """零方差时 `inversion_signature()` 给 inf；JSON 没有无穷大，契约要求 null。"""
    from gait.app import service as service_module
    from gait.core.dualfoot import InversionSignature

    monkeypatch.setattr(
        service_module,
        "inversion_signature",
        lambda left, right: InversionSignature(difference=0.0, significance=math.inf, strides_used=3),
    )
    record = _signature_record(_Chain({"L": _Nav(None), "R": _Nav(None)}))
    assert record == {"state": "computed", "difference": 0.0, "significance": None, "strides_used": 3}
