"""RAY-287 R3 `contract-1-2-bump`：会话元数据里两个新字段的**形状**，以及升版本的后果。

本 scope 只定形状、不产生值 —— 值由 `wear-confirm-metadata` 填。所以这里钉的是：
什么样的记录进得去、什么样的进不去，以及旧版本会话在产品路径上怎么被对待。
"""

from __future__ import annotations

import json
import math

import pytest

from gait.app.service import TerminalService
from gait.app.sources import StubDeviceSource
from gait.contracts import WEAR_CONFIRMATION_RESULTS, ContractError
from gait.io.session import META_FILENAME, create_session, read_meta, session_directory
from gait.protocolflow.timed_walk import CHECK_FAIL, CHECK_PASS, CHECK_UNKNOWN
from tests.test_contracts import make_session_meta
from tests.test_session_format import make_meta

CONFIRMED = {"result": "pass", "confirmed_at": "2026-09-28T02:30:00+00:00"}
COMPUTED = {"state": "computed", "difference": 0.12, "significance": 2.5, "strides_used": 9}
NOT_COMPUTED = {"state": "not_computed", "reason": "只有 1 个支撑相可用，算不出摆动相横滚"}


def test_both_fields_default_to_not_recorded():
    """`None` 是「未记录」—— 与「未确认」「算过、为零」都不是一回事。"""
    meta = make_session_meta()
    assert meta.wear_confirmation is None
    assert meta.inversion_signature is None


def test_confirmation_results_are_the_timed_walk_vocabulary():
    """确认记录与 `SessionVerdict.wearing` 说的是同一件事，得用同一套词。"""
    assert {CHECK_PASS, CHECK_FAIL, CHECK_UNKNOWN} == WEAR_CONFIRMATION_RESULTS


@pytest.mark.parametrize("signature", [COMPUTED, NOT_COMPUTED, {**COMPUTED, "significance": None}])
def test_well_formed_records_are_accepted(signature):
    meta = make_session_meta(wear_confirmation=CONFIRMED, inversion_signature=signature)
    assert meta.wear_confirmation == CONFIRMED


def test_an_unconfirmed_session_records_no_confirmation_time():
    make_session_meta(wear_confirmation={"result": "unknown", "confirmed_at": None})
    with pytest.raises(ContractError, match="没有确认就没有确认时刻"):
        make_session_meta(
            wear_confirmation={"result": "unknown", "confirmed_at": "2026-09-28T02:30:00Z"}
        )


def test_a_pass_must_say_when():
    with pytest.raises(ContractError, match="confirmed_at"):
        make_session_meta(wear_confirmation={**CONFIRMED, "confirmed_at": None})


@pytest.mark.parametrize("extra_key", ["operator_id", "operatorId", "confirmed_by", "swapped"])
def test_operator_identity_has_no_place_to_go(extra_key):
    """RAY-287 R3 / RAY-323 R1 决定 3：身份不进会话元数据。键集合封闭，多一个都不收。

    `swapped` 也在这里：R4 撤掉了它（P-07 已无对调动作，那一格只会恒为假）。
    """
    with pytest.raises(ContractError, match="不记操作员身份"):
        make_session_meta(wear_confirmation={**CONFIRMED, extra_key: "op-001"})


@pytest.mark.parametrize(
    "bad",
    [
        {**CONFIRMED, "result": "confirmed"},
        {**CONFIRMED, "result": ["pass"]},
        {**CONFIRMED, "confirmed_at": "张三"},
        {**CONFIRMED, "confirmed_at": "yesterday"},
        {"result": "pass"},
    ],
)
def test_malformed_confirmations_are_refused(bad):
    """全部以 `ContractError` 拒绝 —— 不是别的异常。`read_meta` 只把契约错误转成
    `SessionFormatError`，别的异常会穿过 `listRecords` 的保护，弄垮整张检测记录。

    `confirmed_at` 必须是时刻：键集合封闭之后，这一格是身份唯一还能混进来的地方。
    """
    with pytest.raises(ContractError):
        make_session_meta(wear_confirmation=bad)


@pytest.mark.parametrize(
    "bad",
    [
        {**COMPUTED, "significance": math.inf},
        {**COMPUTED, "difference": math.nan},
        {**COMPUTED, "strides_used": 0},
        {**COMPUTED, "strides_used": True},
        {**COMPUTED, "verdict": "worn_reversed"},
        {"state": "not_computed", "reason": ""},
        {"state": "not_computed", "reason": None},
        {"state": "not_computed", "reason": 0},
        {**COMPUTED, "difference": 10**400},
        {"state": "computed"},
        {"difference": 0.1},
    ],
)
def test_malformed_signatures_are_refused(bad):
    """`verdict` 那条尤其要拦：签名**不参与判定**，元数据里不该出现一个像判定的东西。"""
    with pytest.raises(ContractError):
        make_session_meta(inversion_signature=bad)


def test_both_fields_round_trip_and_pass_the_fr02_scan(tmp_path):
    """新键名要过《05》§5 的写盘前扫描 —— 那道扫描曾两次误伤过真机字段（RAY-502）。"""
    meta = make_meta(wear_confirmation=CONFIRMED, inversion_signature=COMPUTED)
    directory = create_session(tmp_path, meta)
    back = read_meta(directory)
    assert back.wear_confirmation == CONFIRMED
    assert back.inversion_signature == COMPUTED
    # 落盘必须是标准 JSON：没有 Infinity / NaN。
    json.loads(
        (directory / META_FILENAME).read_text(encoding="utf-8"),
        parse_constant=lambda token: pytest.fail(f"非标准 JSON 常量 {token}"),
    )


def test_a_previous_contract_session_is_skipped_not_fatal(tmp_path):
    """1.1 → 1.2 不迁移（RAY-373 / 05 §7.1），装过 Preview 的机器上必然留有 1.1 会话。

    `read_meta` 照旧拒绝解读它（05 §4，读取函数不猜）；而检测记录不能因为一份读不了的
    旧会话就整张失败 —— 升级后打开记录页就报错，是这次升版本在产品上最可能的样子。
    """
    service = TerminalService(source=StubDeviceSource(), session_root=tmp_path)
    current = service.handle({"id": "s", "method": "startSession", "params": {"now": 0.0}})[
        "result"
    ]["sessionId"]
    service.handle({"id": "t", "method": "stopSession", "params": {"now": 5.0}})

    old = create_session(tmp_path, make_meta("20260924T052339Z-79541a56"))
    path = old / META_FILENAME
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["contract_version"] = "1.1"
    del payload["wear_confirmation"], payload["inversion_signature"]
    path.write_text(json.dumps(payload), encoding="utf-8")

    response = service.handle({"id": "l", "method": "listRecords"})
    assert [record["id"] for record in response["result"]] == [current]
    # 旧会话没被删 —— 跳过的是列表，不是数据。
    assert (session_directory(tmp_path, "20260924T052339Z-79541a56") / META_FILENAME).is_file()


@pytest.mark.parametrize(
    "corrupt",
    [
        lambda payload: payload.update(wear_confirmation={"result": [], "confirmed_at": None}),
        lambda payload: payload.pop("subject_uuid"),
    ],
    ids=["unhashable-result", "missing-required-field"],
)
def test_a_corrupt_current_version_session_is_skipped_too(tmp_path, corrupt):
    """保护写的是「旧版本**或已坏**」，那就得真的接得住坏文件，而不只是旧版本号。"""
    service = TerminalService(source=StubDeviceSource(), session_root=tmp_path)
    broken = create_session(tmp_path, make_meta("20260928T000000Z-0badf00d"))
    path = broken / META_FILENAME
    payload = json.loads(path.read_text(encoding="utf-8"))
    corrupt(payload)
    path.write_text(json.dumps(payload), encoding="utf-8")

    assert service.handle({"id": "l", "method": "listRecords"})["result"] == []
