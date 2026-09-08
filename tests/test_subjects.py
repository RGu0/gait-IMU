"""`gait.cloud.subjects` —— P-02 按机构档案号查找。RAY-322。

守的是四件事，前三件是验收原文，第四件是拍板的产品决定：

* **命中、未命中、冲突三条路都成立，且冲突不产生自动合并** —— 合并一旦错了，两个人
  的数据就永久混在一起，而且不会有任何一步报错；
* **本地不落任何身份明文**（FR-02）—— 用断言，不靠人看；
* **失败文案由 sidecar 给出**，三段齐全（现象 + 动作 + 码），渲染端不自造；
* **断网是降级不是阻断**（待确认 1，2026-09-08 拍板）：失败一律 `blocking=False`，
  且动作那一段指向「无编号，快速建档」，而不是「稍后重试」。
"""

from __future__ import annotations

import io
import json
import ssl
import urllib.error
import urllib.request
from typing import Any, Self

import pytest

from gait.app import protocol
from gait.app.errors import TerminalError, check_code
from gait.app.service import TerminalService
from gait.cloud.subjects import (
    ID_TYPE,
    Conflict,
    Found,
    HttpSubjectDirectory,
    SubjectCard,
    SubjectLookupFailed,
)
from gait.cloud.tenancy import DeviceBinding, DeviceGroup, TerminalIdentity
from gait.io.session import is_identity_free

TOKEN = "terminal-secret"
BASE_URL = "https://cloud.example.invalid"


def identity() -> TerminalIdentity:
    return TerminalIdentity(
        tenant_id="tenant-1",
        terminal_id="terminal-1",
        api_base_url=BASE_URL,
        device_group=DeviceGroup(
            group_id="g1",
            revision=1,
            bindings=(
                DeviceBinding(foot="L", address="aa:bb:cc:dd:ee:01", address_kind="mac", calibration_id="cal-L"),
                DeviceBinding(foot="R", address="aa:bb:cc:dd:ee:02", address_kind="mac", calibration_id="cal-R"),
            ),
            issued_at="2026-09-08T00:00:00+00:00",
        ),
    )


class FakeResponse:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return self._payload

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


def envelope(data: dict[str, Any]) -> bytes:
    return json.dumps({"data": data, "meta": {"request_id": "r"}}).encode("utf-8")


def http_error(status: int, *, code: str = "", message: str = "") -> urllib.error.HTTPError:
    body = json.dumps({"error": {"code": code, "message": message}}).encode("utf-8")
    return urllib.error.HTTPError(BASE_URL, status, "boom", {}, io.BytesIO(body))  # type: ignore[arg-type]


class Opener:
    """把请求换成预设响应，并记下每一次调用。"""

    def __init__(self, payload: bytes | None = None, *, error: Exception | None = None):
        self.payload = payload
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def open(self, request: urllib.request.Request, timeout: float | None = None):
        self.calls.append(
            {
                "url": request.full_url,
                "timeout": timeout,
                "headers": dict(request.header_items()),
                "body": json.loads((request.data or b"{}").decode("utf-8")),
            }
        )
        if self.error is not None:
            raise self.error
        return FakeResponse(self.payload or b"")


FOUND = envelope(
    {
        "kind": "found",
        "subject": {
            "external_id_masked": "**2781",
            "age_band": "60–74",
            "sex": "女",
            "last_assessed_at": "2026-06-14",
            "last_protocol_seconds": 180,
            "consent_valid": True,
        },
    }
)

CONFLICT = envelope(
    {
        "kind": "conflict",
        "candidates": [
            {"external_id_masked": "**9000", "age_band": "60–74", "sex": "女", "last_assessed_at": "2026-05-08"},
            {"external_id_masked": "**9000", "age_band": "60–74", "sex": "女", "last_assessed_at": "2026-07-19"},
        ],
    }
)


def directory(opener: Opener) -> HttpSubjectDirectory:
    return HttpSubjectDirectory(identity(), lambda: TOKEN, opener=opener)


# ── 三条路 ──────────────────────────────────────────────────────────────────


def test_a_hit_carries_the_masked_card_including_the_last_protocol_length() -> None:
    """上次时长配置是**必须显示**的一行（PRD §11 P-02 / C-13），不是装饰。"""
    opener = Opener(FOUND)
    result = directory(opener).lookup("2781")

    assert isinstance(result, Found)
    assert result.subject.masked_id == "**2781"
    assert result.subject.last_protocol_seconds == 180
    assert result.snapshot()["subject"]["lastProtocolSeconds"] == 180


def test_the_request_carries_the_triple_the_server_needs() -> None:
    opener = Opener(FOUND)
    directory(opener).lookup("2781")

    call = opener.calls[0]
    assert call["url"] == f"{BASE_URL}/v1/subjects/resolve"
    assert call["body"] == {"issuer": "tenant-1", "id_type": ID_TYPE, "external_id": "2781"}
    # 租户身份来自凭据侧的 identity，**不由操作员输入自证**（抄录卷 §1.3）。
    assert call["headers"]["Authorization"] == f"Bearer {TOKEN}"


def test_a_conflict_returns_every_candidate_and_merges_nothing() -> None:
    """PRD §6.1：冲突**不自动合并**。合并要判断「是不是同一个人」，只有现场的人做得了。"""
    result = directory(Opener(CONFLICT)).lookup("9000")

    assert isinstance(result, Conflict)
    assert len(result.candidates) == 2
    # 两条候选原样留着 —— 没有任何一条被挑掉或并成一条。
    assert [c.last_assessed_at for c in result.candidates] == ["2026-05-08", "2026-07-19"]
    assert len(result.snapshot()["candidates"]) == 2


def test_a_conflict_with_one_candidate_is_refused() -> None:
    """只有一条却报冲突，界面会显示一个选不了的选择题。"""
    with pytest.raises(ValueError):
        Conflict((SubjectCard("**1", "60–74", "女", "2026-01-01"),))


def test_not_found_is_an_error_with_all_three_parts() -> None:
    failure = _failure(Opener(error=http_error(404)), "0000")
    assert failure.code == "E-NET-6020"
    assert "0000" in failure.message
    assert "快速建档" in failure.action


# ── 断网降级：待确认 1 的拍板 ────────────────────────────────────────────────


@pytest.mark.parametrize(
    "error",
    [
        TimeoutError("slow"),
        urllib.error.URLError(OSError("no route")),
        urllib.error.URLError(ssl.SSLError("bad cert")),
        ssl.SSLError("bad cert"),
        ConnectionResetError("peer reset"),
        http_error(500),
        http_error(429),
    ],
)
def test_every_lookup_failure_degrades_instead_of_blocking(error: Exception) -> None:
    """待确认 1 拍板为**降级**：查不了不该让这次检测停下。

    所以每一条失败都 `blocking=False`，且动作指向「无编号，快速建档」——
    而不是「恢复网络后重试」。这一条不定，本模块根本写不出那句话（RAY-248 要求
    错误必须带现象 + 动作 + 码）。
    """
    failure = _failure(Opener(error=error), "2781")
    assert failure.blocking is False
    assert "快速建档" in failure.action


@pytest.mark.parametrize("status", [401, 403])
def test_rejected_credentials_also_degrade_but_name_the_real_cause(status: int) -> None:
    failure = _failure(Opener(error=http_error(status)), "2781")
    assert failure.code == "E-NET-6024"
    assert failure.blocking is False
    assert "服务方" in failure.action


def test_an_unreadable_response_is_not_disguised_as_not_found() -> None:
    """读不懂的响应与「查无此人」是两个结局，不能被抹平成同一个。"""
    failure = _failure(Opener(b"not json at all"), "2781")
    assert failure.code == "E-NET-6023"


def test_a_masked_id_that_is_not_masked_is_refused() -> None:
    """掩码把原号原样带回来等于没掩 —— 那会把身份摊开给核对页旁边的人看。"""
    leaked = envelope({"kind": "found", "subject": {"external_id_masked": "2781"}})
    failure = _failure(Opener(leaked), "2781")
    assert failure.code == "E-NET-6023"


# ── 契约与文案 ──────────────────────────────────────────────────────────────


def test_every_code_this_module_emits_is_registered_in_the_contract() -> None:
    """未登记的码在前端只能显示成一个数字。"""
    for code in ("E-NET-6020", "E-NET-6022", "E-NET-6023", "E-NET-6024"):
        assert check_code(code) == code


def test_subject_directory_is_no_longer_an_unimplemented_capability() -> None:
    with pytest.raises(protocol.ProtocolError):
        protocol.unimplemented("req-1", "subject-directory")


def test_the_card_never_carries_identity_plaintext() -> None:
    """FR-02 用断言守，不靠人看。"""
    card = Found(subject=SubjectCard("**2781", "60–74", "女", "2026-06-14", 180, True))
    assert is_identity_free(card.snapshot())


# ── sidecar 接线 ────────────────────────────────────────────────────────────


class StubDirectory:
    def __init__(self, result: Any = None, *, failure: SubjectLookupFailed | None = None):
        self.result = result
        self.failure = failure
        self.asked: list[str] = []

    def lookup(self, external_id: str):
        self.asked.append(external_id)
        if self.failure is not None:
            raise self.failure
        return self.result


def test_the_sidecar_returns_the_lookup_shape_the_renderer_consumes() -> None:
    found = Found(subject=SubjectCard("**2781", "60–74", "女", "2026-06-14", 180, True))
    service = TerminalService(subjects=StubDirectory(found))

    response = service.handle({"id": "1", "method": "lookupSubject", "params": {"enteredId": "2781"}})

    assert response["status"] == protocol.STATUS_OK
    assert response["result"]["kind"] == "found"
    assert response["result"]["subject"]["lastProtocolSeconds"] == 180


def test_the_sidecar_passes_the_failure_through_without_rewrapping_it() -> None:
    """再包一层会让文案多一个出处，而多一个出处就是两份分头漂移的开始。"""
    failure = SubjectLookupFailed(
        TerminalError("E-NET-6020", "没有这个编号。", "核对编号。", blocking=False)
    )
    service = TerminalService(subjects=StubDirectory(failure=failure))

    response = service.handle({"id": "1", "method": "lookupSubject", "params": {"enteredId": "x"}})

    assert response["status"] == protocol.STATUS_ERROR
    assert response["error"]["code"] == "E-NET-6020"
    assert response["error"]["action"] == "核对编号。"
    assert response["error"]["blocking"] is False


def test_an_unprovisioned_terminal_says_so_instead_of_reporting_a_gap() -> None:
    """契约里 `subject-directory` 已翻成 implemented，再报缺口就是在骗界面。"""
    service = TerminalService(subjects=None)

    response = service.handle({"id": "1", "method": "lookupSubject", "params": {"enteredId": "2781"}})

    assert response["status"] == protocol.STATUS_ERROR
    assert response["error"]["code"] == "E-NET-6024"
    assert response["error"]["blocking"] is False


def _failure(opener: Opener, entered: str) -> TerminalError:
    with pytest.raises(SubjectLookupFailed) as caught:
        directory(opener).lookup(entered)
    return caught.value.failure
