"""IPC 契约测试（RAY-248）。

验收三条各自有对应的断言，且都是**能失败**的断言：

1. schema 版本化并纳入契约测试 —— `test_versions_*`
2. 渲染进程不得自造错误文案 —— `test_error_requires_*`（错误不带文案就构造不出来）
3. 200 Hz 原始数据不跨 IPC —— `test_rejects_bulk_*`

外加：未实现的能力必须以第三种结局出境，不能伪装成成功或失败。
"""

from __future__ import annotations

import io
import json
import re

import pytest
from wt901 import Battery

from gait.app import protocol
from gait.app.__main__ import serve
from gait.app.errors import ContractError, TerminalError, check_code, contract
from gait.app.service import TerminalService
from gait.app.sources import StubDeviceSource
from gait.config import ProtocolConfig
from gait.contracts import CONTRACT_VERSION

# ── 版本 ──────────────────────────────────────────────────────────────────


def test_versions_are_declared() -> None:
    assert protocol.IPC_CONTRACT_VERSION
    assert protocol.describe()["ipc_contract_version"] == protocol.IPC_CONTRACT_VERSION


def test_ipc_version_is_separate_from_the_data_contract() -> None:
    """两个版本号必须分开报出。

    合并成一个号，等于让任何一方的变更都去谎报另一方也变了 —— 而三个月后判断某份
    会话用的是哪版结构，靠的正是这些号。
    """
    described = protocol.describe()
    assert described["data_contract_version"] == CONTRACT_VERSION
    assert "ipc_contract_version" in described


def test_every_response_carries_the_version() -> None:
    for message in (
        protocol.ok("1", {"a": 1}),
        protocol.error("2", TerminalError("E-BLE-1001", "未连接。", "请重连。")),
        protocol.unimplemented("3", "calibration"),
        protocol.event("session.tick", 1, {"remainingSeconds": 3}),
    ):
        assert message["v"] == protocol.IPC_CONTRACT_VERSION


# ── 错误码与文案 ──────────────────────────────────────────────────────────


def test_six_domains_exactly() -> None:
    assert set(contract()["error_domains"]) == {
        "E-BLE",
        "E-WEAR",
        "E-CAL",
        "E-SYNC",
        "E-QLT",
        "E-NET",
    }


@pytest.mark.parametrize(
    "code",
    ["E-BLE-9999", "E-XXX-1001", "E-BLE-1006", "EBLE1001", "E-BLE-abc"],
)
def test_check_code_rejects(code: str) -> None:
    with pytest.raises(ContractError):
        check_code(code)


def test_check_code_accepts_registered() -> None:
    assert check_code("E-QLT-5002") == "E-QLT-5002"


def test_error_requires_message() -> None:
    with pytest.raises(ContractError):
        TerminalError("E-BLE-1001", "   ", "请重连。")


def test_error_requires_action() -> None:
    """只说出了什么事、不说该做什么的错误，对操作员等于没说。"""
    with pytest.raises(ContractError):
        TerminalError("E-BLE-1001", "模块未连接。", "")


def test_error_envelope_refuses_bare_strings() -> None:
    """裸字符串会让渲染进程失去码与动作，而它没有权限自己补。"""
    with pytest.raises(protocol.ProtocolError):
        protocol.error("1", "模块未连接")  # type: ignore[arg-type]


# ── 红线 R-1：200 Hz 不跨 IPC ─────────────────────────────────────────────


@pytest.mark.parametrize("field", ["acc", "gyr", "frames", "samples", "raw_frames"])
def test_rejects_bulk_field_names(field: str) -> None:
    with pytest.raises(protocol.ProtocolError, match="不跨 IPC"):
        protocol.ok("1", {field: [1, 2, 3]})


def test_rejects_bulk_field_when_nested() -> None:
    with pytest.raises(protocol.ProtocolError):
        protocol.ok("1", {"session": {"left": {"gyr": []}}})


def test_rejects_long_series() -> None:
    with pytest.raises(protocol.ProtocolError, match="像采集数据"):
        protocol.ok("1", {"marks": list(range(protocol.MAX_SERIES_LENGTH + 1))})


def test_allows_state_sized_series() -> None:
    protocol.ok("1", {"marks": list(range(protocol.MAX_SERIES_LENGTH))})


def test_events_are_guarded_too() -> None:
    """事件是量最大的一条路 —— 红线在这里失守才是最可能发生的。"""
    with pytest.raises(protocol.ProtocolError):
        protocol.event("session.tick", 1, {"acc": [[0, 0, 0]]})


# ── 三态 ──────────────────────────────────────────────────────────────────


def test_an_implemented_capability_is_no_longer_a_gap() -> None:
    """`report` 于 2026-09-03 翻面（RAY-224 `basic-report`）。

    这条测试的存在方式本身是一条经验：上一版这里把「report 是缺口」当成事实钉住，
    于是它在能力实现的那天变红。**那是对的** —— 契约与实现必须同时翻面
    （见 `test_unimplemented_refuses_an_implemented_capability`）。红了就改，
    而不是给翻面留一条不会失败的后路。
    """
    assert contract()["capabilities"]["report"]["implemented"] is True
    with pytest.raises(protocol.ProtocolError, match="同时翻面"):
        protocol.unimplemented("1", "report")


def test_unimplemented_is_neither_ok_nor_error() -> None:
    message = protocol.unimplemented("1", "calibration")
    assert message["status"] == protocol.STATUS_UNIMPLEMENTED
    assert "result" not in message and "error" not in message
    assert message["unimplemented"]["issue"] == "RAY-208"


def test_unimplemented_rejects_unknown_capability() -> None:
    with pytest.raises(protocol.ProtocolError):
        protocol.unimplemented("1", "teleportation")


def test_unimplemented_refuses_an_implemented_capability(monkeypatch) -> None:
    """契约与实现必须同时翻面，否则界面会继续显示一个其实已经好了的缺口。"""
    patched = json.loads(json.dumps(contract()))
    patched["capabilities"]["report"]["implemented"] = True
    monkeypatch.setattr("gait.app.errors.contract", lambda: patched)
    monkeypatch.setattr("gait.app.protocol.contract", lambda: patched)
    with pytest.raises(protocol.ProtocolError, match="同时翻面"):
        protocol.unimplemented("1", "report")


def test_unknown_method_and_topic() -> None:
    with pytest.raises(protocol.ProtocolError):
        protocol.request("1", "definitelyNotAMethod")
    with pytest.raises(protocol.ProtocolError):
        protocol.event("session.whatever", 1, {})


# ── 真实后端通路 ──────────────────────────────────────────────────────────


def _battery_item(items: list[dict]) -> dict:
    return next(item for item in items if item["id"] == "battery")


def test_preflight_blocks_on_low_battery_with_the_real_admission() -> None:
    source = StubDeviceSource(
        batteries={"L": Battery(raw=22, percent=22), "R": Battery(raw=76, percent=76)}
    )
    items = TerminalService(source=source).handle(
        {"id": "1", "method": "runPreflight"}
    )["result"]
    item = _battery_item(items)
    assert item["status"] == "fail"
    assert item["error"]["code"] == "E-BLE-1005"
    assert "22%" in item["error"]["message"]


def test_preflight_keeps_unreadable_battery_distinct_from_low_battery() -> None:
    """两者的动作不同：一个去查连接，一个去换电池。合并这两句就是抹掉这个区分。"""
    low = TerminalService(
        source=StubDeviceSource(
            batteries={
                "L": Battery(raw=22, percent=22),
                "R": Battery(raw=76, percent=76),
            }
        )
    ).handle({"id": "1", "method": "runPreflight"})["result"]
    unread = TerminalService(
        source=StubDeviceSource(batteries={"L": None, "R": Battery(raw=76, percent=76)})
    ).handle({"id": "1", "method": "runPreflight"})["result"]

    assert (
        _battery_item(low)["error"]["message"]
        != _battery_item(unread)["error"]["message"]
    )
    assert "换电池解决不了" in _battery_item(unread)["error"]["message"]


def test_preflight_passes_when_everything_reads_well() -> None:
    items = TerminalService(source=StubDeviceSource()).handle(
        {"id": "1", "method": "runPreflight"}
    )["result"]
    assert all(item["status"] == "pass" for item in items)
    assert all(item["error"] is None for item in items)


def test_preflight_blocks_on_missing_factory_calibration() -> None:
    source = StubDeviceSource(calibrated={"L": True, "R": False})
    items = TerminalService(source=source).handle(
        {"id": "1", "method": "runPreflight"}
    )["result"]
    item = next(i for i in items if i["id"] == "factory-cal")
    assert item["status"] == "fail"
    assert item["error"]["code"] == "E-CAL-3001"


def test_tick_counts_down_against_real_protocol_time() -> None:
    source = StubDeviceSource()
    service = TerminalService(source=source, config=ProtocolConfig(duration_s=120))
    service.handle({"id": "1", "method": "startSession", "params": {"now": 0.0}})
    source.advance(left=60, right=59)
    payload = service.tick(30.0)["payload"]
    assert payload["remainingSeconds"] == pytest.approx(90.0)
    assert payload["steps"] == {"left": 60, "right": 59}
    assert payload["link"] == {"left": "good", "right": "good"}


def test_tick_carries_only_the_three_things_p08_may_show() -> None:
    """PRD §6.1：采集中只显示剩余时间、步数、链路三档。这个 payload 刻意是贫瘠的。"""
    service = TerminalService(source=StubDeviceSource())
    service.handle({"id": "1", "method": "startSession", "params": {"now": 0.0}})
    assert set(service.tick(1.0)["payload"]) == {"remainingSeconds", "steps", "link"}


def test_session_result_is_indeterminate_until_wearing_is_ruled() -> None:
    """RAY-260：左右戴反位置法不可判定，v1.4 改为 P-06 手工裁定。

    在裁定之前诚实的答案是「评不了」—— 把它默认成通过，正是 PRD §13 唯一硬拦截
    被悄悄架空的方式。
    """
    service = _finished_session()
    result = service.handle({"id": "r", "method": "sessionResult", "params": {}})[
        "result"
    ]
    assert result["overall"] == "indeterminate"
    assert "wearing_unknown" in result["verdict"]["reasons"]


def test_session_result_is_valid_once_the_operator_rules_on_wearing() -> None:
    service = _finished_session()
    result = service.handle(
        {"id": "r", "method": "sessionResult", "params": {"wearing": "pass"}}
    )["result"]
    assert result["overall"] == "valid"


def test_session_result_reports_short_walk_with_a_quality_code() -> None:
    service = _finished_session(stop_at=30.0)
    result = service.handle(
        {"id": "r", "method": "sessionResult", "params": {"wearing": "pass"}}
    )["result"]
    assert result["overall"] == "invalid"
    assert result["error"]["code"] == "E-QLT-5002"


def test_session_integrity_is_accounted_separately_from_validity() -> None:
    """`complete` 为假不表示数据不可用 —— 单足指标可能仍然可算。"""
    service = _finished_session()
    result = service.handle(
        {
            "id": "r",
            "method": "sessionResult",
            "params": {"wearing": "pass", "disconnectedAt": {"L": 41.0}},
        }
    )["result"]
    assert result["integrity"]["complete"] is False
    assert result["overall"] == "invalid"
    assert any("断连" in problem for problem in result["integrity"]["problems"])


def test_session_result_declares_report_ready_when_session_is_valid() -> None:
    """RAY-345：报告已接通。有效会话的判定声明「可生成」，而不是缺口。"""
    service = _finished_session()
    result = service.handle(
        {"id": "r", "method": "sessionResult", "params": {"wearing": "pass"}}
    )["result"]
    assert result["report"]["status"] == "ready"
    assert result["report"]["sessionId"] == service.session_id


def test_session_result_declares_report_invalid_when_session_is_invalid() -> None:
    """会话级无效不生成报告（PRD §13）：判定里声明 report 不可生成。"""
    service = _finished_session(stop_at=30.0)
    result = service.handle(
        {"id": "r", "method": "sessionResult", "params": {"wearing": "pass"}}
    )["result"]
    assert result["overall"] == "invalid"
    assert result["report"]["status"] == "invalid"


def test_every_capability_declares_who_owns_it() -> None:
    """每个缺口都要么指向一个 Issue，要么显式地说「没人认领」。

    `issue` **允许为 None** —— 那表示这个缺口还没有 Issue 认领，界面会照实说。
    编一个号会把这件事藏起来，所以这里不要求非空；要求的是这个字段**存在**，
    以及非空时长得像一个 Issue 号（`RAY-123`）。一个拼错的号比没有号更糟：
    它看起来可以追查，点进去却什么也没有。
    """
    for name, entry in contract()["capabilities"].items():
        assert "issue" in entry, f"{name} 没有声明归属"
        assert entry["summary"].strip(), f"{name} 缺少说明"
        if entry["issue"] is not None:
            assert re.fullmatch(r"RAY-\d+", entry["issue"]), (
                f"{name} 的 issue {entry['issue']!r} 不像一个 Issue 号"
            )


def test_login_does_not_pretend_there_is_an_auth_backend() -> None:
    """非空就放行等于没有认证 —— 那是在假装一个后端存在。

    也不该给它套一个 `E-BLE` 码：那说的是采集现场的连接故障，用它表示登录问题
    会在日志里造出一个查无此事的设备故障。
    """
    response = TerminalService().handle(
        {"id": "1", "method": "login", "params": {"organization": "康健", "password": "x"}}
    )
    assert response["status"] == protocol.STATUS_UNIMPLEMENTED
    assert response["unimplemented"]["capability"] == "operator-auth"


def test_session_result_tolerates_absent_link_params() -> None:
    service = _finished_session()
    result = service.handle(
        {
            "id": "r",
            "method": "sessionResult",
            "params": {"wearing": "pass", "disconnectedAt": None, "reconnects": None},
        }
    )["result"]
    assert result["integrity"]["complete"] is True


def test_create_subject_uses_the_real_uuid_source() -> None:
    import uuid

    result = TerminalService().handle({"id": "1", "method": "createSubject"})["result"]
    uuid.UUID(result["subjectUuid"])  # 非 UUID 会抛
    assert result["consentValid"] is False


@pytest.mark.parametrize(
    ("method", "capability", "issue"),
    [
        ("runCalibration", "calibration", "RAY-208"),
        ("lookupSubject", "subject-directory", "RAY-322"),
        ("login", "operator-auth", "RAY-323"),
    ],
)
def test_gaps_are_visible_not_faked(
    method: str, capability: str, issue: str | None
) -> None:
    """本 scope 剩下的缺口都必须以第三种结局出境（report 已在 RAY-345 接通）。

    返回一个看起来正常的假值会让「流程已端到端验证」变成一句空话 —— 那正是这条
    测试要挡住的事。
    """
    response = TerminalService().handle({"id": "1", "method": method})
    assert response["status"] == protocol.STATUS_UNIMPLEMENTED
    assert response["unimplemented"]["capability"] == capability
    assert response["unimplemented"]["issue"] == issue


# ── stdio 往返 ────────────────────────────────────────────────────────────


def test_serve_round_trips_json_lines() -> None:
    stdin = io.StringIO(
        '{"id":"1","method":"describe","params":{}}\n'
        '{"id":"2","method":"runCalibration","params":{}}\n'
    )
    stdout = io.StringIO()
    serve(stdin, stdout, TerminalService(source=StubDeviceSource()))
    lines = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert lines[0]["status"] == "ok"
    assert lines[1]["status"] == protocol.STATUS_UNIMPLEMENTED


def test_serve_reports_protocol_failures_without_inventing_a_device_code() -> None:
    """两端说的话对不上，不是六个域里的任何一个。

    给它编一个 E-BLE-xxxx，日志里就会出现一个查无此事的设备故障。
    """
    stdout = io.StringIO()
    serve(io.StringIO('{"id":"9","method":"nope"}\n'), stdout, TerminalService())
    message = json.loads(stdout.getvalue())
    assert message["status"] == "error"
    assert "protocolError" in message
    assert "error" not in message


def test_serve_survives_malformed_json() -> None:
    stdout = io.StringIO()
    serve(
        io.StringIO('not json\n{"id":"1","method":"describe"}\n'),
        stdout,
        TerminalService(),
    )
    lines = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert "protocolError" in lines[0]
    assert lines[1]["status"] == "ok"


def _finished_session(*, stop_at: float = 200.0) -> TerminalService:
    service = TerminalService(
        source=StubDeviceSource(), config=ProtocolConfig(duration_s=180)
    )
    service.handle({"id": "s", "method": "startSession", "params": {"now": 0.0}})
    service.handle({"id": "e", "method": "stopSession", "params": {"now": stop_at}})
    return service
