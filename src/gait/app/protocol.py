"""渲染进程 ↔ Python sidecar 的消息信封，以及两条红线的可执行形式。

## 版本号为什么与 `contracts.CONTRACT_VERSION` 分开

`gait/contracts.py` 的 `CONTRACT_VERSION`（当前 1.1）说的是 `FootSeries` 这些**数据
结构**的形状；本模块的 `IPC_CONTRACT_VERSION` 说的是**跨进程消息**的形状。两者变更
的理由不同：给 P-08 加一个事件字段不会动 `FootSeries`，把 gyr 改成 rad/s（R2）也不
会动信封。合并成一个号，等于让任何一方的变更都去谎报另一方变了。

所以这里**引用**而不合并：`describe()` 同时报出两个号，谁需要对照谁自己对照。

## 待确认：这个版本号进不进会话元数据

UI 设计 §11.3 建议「IPC schema 版本号进会话元数据，与 `algo_version` /
`config_snapshot` 同级」。RAY-248 明写**本 Issue 不自行决定**，因为那要动 RAY-193
的元数据 schema。因此本模块**不写会话元数据**，只把版本号放进 `describe()` 与每一条
响应里。要接进元数据时，改的是 RAY-193 那侧，这里不需要动。

## 红线 R-1 的可执行形式：200 Hz 原始数据不跨 IPC

RAY-248 的验收第三条。它容易在某次「顺手把波形也传过去给前端画个图」里破掉，而破掉
之后没有任何东西会报错 —— UI 照样能画，只是 FR-05（主线程不做长任务）和 G-04 一起
悄悄失效。所以这里把它变成一次真实的检查：任何要出境的 payload 都过
`reject_bulk_payload`，带原始信号字段名的、或长到不像状态量的数组，当场拒绝。

阈值取 512：P-08 的落步刻痕最多几十个，报告的时序条也是几十个量级；而 200 Hz 下
哪怕一秒的原始数据也有 200×3 个数。两者之间差着量级，不需要卡得很紧就能分开。
"""

from __future__ import annotations

from typing import Any, Final

from gait.app.errors import TerminalError, contract

IPC_CONTRACT_VERSION: Final[str] = contract()["ipc_contract_version"]

KIND_REQUEST: Final[str] = "request"
KIND_RESPONSE: Final[str] = "response"
KIND_EVENT: Final[str] = "event"

#: 响应的三种结局。**刻意不是布尔** —— 「成功 / 失败」装不下「这个能力还没实现」。
#: 把未实现挤进 error，前端就得靠猜某个错误码是不是「其实是没做」；挤进 ok 并返回
#: 一个占位值，则是拿假数据冒充真结果 —— 那正是本 scope 要避免的那种流程。
STATUS_OK: Final[str] = "ok"
STATUS_ERROR: Final[str] = "error"
STATUS_UNIMPLEMENTED: Final[str] = "unimplemented"
STATUSES: Final[frozenset[str]] = frozenset(
    {STATUS_OK, STATUS_ERROR, STATUS_UNIMPLEMENTED}
)

METHODS: Final[frozenset[str]] = frozenset(contract()["methods"])
EVENT_TOPICS: Final[frozenset[str]] = frozenset(contract()["event_topics"])

MAX_SERIES_LENGTH: Final[int] = 512

#: 原始信号的字段名。取自 `gait/contracts.py` 的 `RawFrame` / `FootSeries`：这些名字
#: 出现在出境 payload 里，基本只有一个原因 —— 有人把采集数据本身送过来了。
BULK_FIELD_NAMES: Final[frozenset[str]] = frozenset(
    {"acc", "gyr", "mag", "frames", "samples", "raw_frames", "payload_bytes", "series"}
)


class ProtocolError(ValueError):
    """信封本身不合法 —— 未知方法、未知话题、越界的 payload。"""


def reject_bulk_payload(value: Any, *, path: str = "payload") -> None:
    """红线 R-1 的守卫；就地递归，出境前调用。"""
    if isinstance(value, dict):
        for key, item in value.items():
            if key in BULK_FIELD_NAMES:
                raise ProtocolError(
                    f"{path}.{key} 是原始信号字段名：200 Hz 采集数据不跨 IPC，"
                    "数据面完全在 sidecar 内（RAY-248 验收第三条 / UI 设计 R-1）。"
                )
            reject_bulk_payload(item, path=f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        if len(value) > MAX_SERIES_LENGTH:
            raise ProtocolError(
                f"{path} 有 {len(value)} 个元素，超过 {MAX_SERIES_LENGTH}："
                "这个长度不像状态量，像采集数据。跨 IPC 的只能是状态与结果。"
            )
        for index, item in enumerate(value):
            reject_bulk_payload(item, path=f"{path}[{index}]")


def request(
    request_id: str, method: str, params: dict[str, Any] | None = None
) -> dict[str, Any]:
    if method not in METHODS:
        raise ProtocolError(f"未知方法 {method!r}；已登记的是 {sorted(METHODS)}")
    params = params or {}
    reject_bulk_payload(params, path="params")
    return {
        "kind": KIND_REQUEST,
        "v": IPC_CONTRACT_VERSION,
        "id": request_id,
        "method": method,
        "params": params,
    }


def _envelope(request_id: str, status: str) -> dict[str, Any]:
    if status not in STATUSES:
        raise ProtocolError(f"未知 status {status!r}")
    return {
        "kind": KIND_RESPONSE,
        "v": IPC_CONTRACT_VERSION,
        "id": request_id,
        "status": status,
    }


def ok(request_id: str, result: Any) -> dict[str, Any]:
    reject_bulk_payload(result, path="result")
    return {**_envelope(request_id, STATUS_OK), "result": result}


def error(request_id: str, failure: TerminalError) -> dict[str, Any]:
    if not isinstance(failure, TerminalError):
        raise ProtocolError(
            "错误必须是 TerminalError —— 裸字符串会让渲染进程失去码与动作，"
            "而它没有权限自己补（RAY-248 验收第二条）。"
        )
    return {**_envelope(request_id, STATUS_ERROR), "error": failure.snapshot()}


def unimplemented(request_id: str, capability: str) -> dict[str, Any]:
    """一个还没接通的能力。

    它带着 Issue 号出境，因为界面上要显示的是「这一步尚未接通（RAY-207）」而不是
    一句无从追查的「暂不可用」。
    """
    known = contract()["capabilities"]
    if capability not in known:
        raise ProtocolError(f"未登记的能力 {capability!r}；已登记 {sorted(known)}")
    entry = known[capability]
    if entry.get("implemented"):
        raise ProtocolError(
            f"{capability} 在 contract.json 里已标记为 implemented，"
            "不能再返回 unimplemented —— 契约与实现必须同时翻面。"
        )
    return {
        **_envelope(request_id, STATUS_UNIMPLEMENTED),
        "unimplemented": {
            "capability": capability,
            "issue": entry["issue"],
            "summary": entry["summary"],
        },
    }


def event(topic: str, seq: int, payload: dict[str, Any]) -> dict[str, Any]:
    if topic not in EVENT_TOPICS:
        raise ProtocolError(
            f"未知事件话题 {topic!r}；已登记的是 {sorted(EVENT_TOPICS)}"
        )
    if seq < 0:
        raise ProtocolError("事件序号不能为负")
    reject_bulk_payload(payload, path="payload")
    return {
        "kind": KIND_EVENT,
        "v": IPC_CONTRACT_VERSION,
        "topic": topic,
        "seq": seq,
        "payload": payload,
    }


def describe() -> dict[str, Any]:
    """两个契约版本号一起报出，谁要对照谁自己对照。"""
    return {
        "ipc_contract_version": IPC_CONTRACT_VERSION,
        "data_contract_version": contract()["data_contract_version"],
        "methods": sorted(METHODS),
        "event_topics": sorted(EVENT_TOPICS),
        "capabilities": contract()["capabilities"],
    }
