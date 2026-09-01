"""sidecar 的请求处理层：把渲染进程的方法调用接到仓库里既有的真实实现上。

## 它刻意不认识传输

`handle()` 收一个 dict、返回一个 dict，不碰 stdin/stdout、不碰 socket。理由有两条：
一是 RAY-250 才决定传输形态（Electron 主进程拉起、stdio 还是别的），把传输焊死在这里
会让那个决定变成一次重写；二是**契约可以在没有进程边界的情况下被完整测试** —— 而
「不需要硬件就能验的东西不该靠上机来发现」（RAY-319）在这里同样适用：跨进程才能测的
契约，等于要等到打包完成才第一次被验。

## 哪些是真的

| 方法 | 背后 |
|---|---|
| `runPreflight` | `device/orchestration.preflight_battery` 的三态准入 + 到达率 + 出厂标定 + 磁盘 |
| `startSession` / `stopSession` / tick | `protocolflow/timed_walk.TimedWalk`（真实计时、停顿扣除、有效时长） |
| `sessionResult` | `TimedWalk.verdict()` + `orchestration.summarize_session` 的双足完整性 |
| `createSubject` | `io/session.new_subject_uuid()` |
| `listRecords` | `io/session.list_sessions()` + `read_meta()` |

硬件读数经 `sources.DeviceSource` 注入 —— 见该模块开头关于「stub 不是 mock」的说明。

## 哪些**不是**，且必须看得出来

`runCalibration`（RAY-208）与 `reportFor`（RAY-224）返回 `status="unimplemented"`。
它们**不返回一个看起来正常的假值**。一个能从头走到尾、中间两步是假的流程，比一个
诚实断在半路的流程危险 —— 前者会让人以为流程已经验证过了。
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from gait.app import protocol
from gait.app.errors import TerminalError
from gait.app.sources import DeviceSource, StubDeviceSource
from gait.config import ProtocolConfig
from gait.device.orchestration import (
    MIN_BATTERY_PERCENT,
    LinkOutcome,
    preflight_battery,
    summarize_session,
)
from gait.io.session import (
    list_sessions,
    new_subject_uuid,
    read_meta,
    session_directory,
)
from gait.protocolflow.timed_walk import (
    CHECK_FAIL,
    CHECK_PASS,
    CHECK_UNKNOWN,
    VERDICT_INVALID,
    TimedWalk,
)

_SIDES = {"L": "左", "R": "右"}

#: 到达率低于此值即自检不通过。PRD §6.1「到达率（≥ 5 s 观察）」没有给数字，
#: 这个门限取自 RAY-210 的到达率监控口径；它是**准入**门限，不是质量分级门限
#: （后者只在 gait/quality/ 实现一次，FR-08）。
MIN_ARRIVAL_RATE = 0.95

MIN_DISK_FREE_BYTES = 2 * 1024**3


class TerminalService:
    """一次终端会话的服务端状态。"""

    def __init__(
        self,
        *,
        source: DeviceSource | None = None,
        config: ProtocolConfig | None = None,
        session_root: Path | None = None,
    ) -> None:
        self.source: DeviceSource = source or StubDeviceSource()
        self.config = config or ProtocolConfig()
        self.session_root = session_root
        self.operator: dict[str, Any] | None = None
        self.walk: TimedWalk | None = None
        self._event_seq = 0
        self._aborted: dict[str, Any] | None = None
        #: `TimedWalk.elapsed_seconds` 停表后才有值（它按 walking_stopped 算），
        #: 而 P-08 的倒计时要在**走的过程中**回答「还剩多久」。这里自己记开走时刻，
        #: 而不是去读 TimedWalk 的私有字段 —— 读私有等于把它的内部当公开接口。
        self._walk_started_at: float | None = None

    # ── 分发 ──────────────────────────────────────────────────────────────

    def handle(self, message: dict[str, Any]) -> dict[str, Any]:
        request_id = str(message.get("id", ""))
        method = message.get("method")
        handler: Callable[[dict[str, Any]], Any] | None = getattr(
            self, f"_do_{str(method).replace('.', '_')}", None
        )
        if method not in protocol.METHODS or handler is None:
            raise protocol.ProtocolError(f"未登记的方法 {method!r}")
        params = message.get("params") or {}
        outcome = handler(params)
        if isinstance(outcome, TerminalError):
            return protocol.error(request_id, outcome)
        if isinstance(outcome, _Unimplemented):
            return protocol.unimplemented(request_id, outcome.capability)
        return protocol.ok(request_id, outcome)

    # ── 真实通路 ──────────────────────────────────────────────────────────

    def _do_describe(self, _: dict[str, Any]) -> dict[str, Any]:
        return protocol.describe()

    def _do_login(self, _: dict[str, Any]) -> Any:
        """P-00 机构登录 —— **没有后端**。

        FR-01 写明操作员在 P-00 登录的是**机构账号**，用于识别「谁在操作」，与终端的
        预配置技术凭据是两件事。RAY-225 交付的是后者（`cloud/tenancy.py` 的终端身份
        与设备绑定），前者在本仓库没有任何实现。

        先前这里写过一个「账号密码非空就放行」的检查，并给它套了 `E-BLE-1001`。
        那有两处错：一是它在假装存在一个认证后端 —— 非空就通过等于没有认证；二是
        `E-BLE` 说的是采集现场的连接故障，拿它表示一个登录问题，会在日志里造出一个
        查无此事的设备故障（`__main__._fatal` 拒绝这么做的理由完全相同）。

        字段非空这类表单校验留在渲染进程：它没有错误码，因此不受「文案与错误码同源」
        约束 —— 那条约束管的是错误，不是表单。
        """
        return _Unimplemented("operator-auth")

    def _do_snapshot(self, _: dict[str, Any]) -> dict[str, Any]:
        return self._snapshot()

    def _do_recheckDevices(self, _: dict[str, Any]) -> dict[str, Any]:
        return self._snapshot()

    def _do_runPreflight(self, _: dict[str, Any]) -> list[dict[str, Any]]:
        """P-05 自检。每一项的结论都由真实实现推出来，不是写死的。"""
        items: list[dict[str, Any]] = []
        calibrated = self.source.factory_calibrated()
        arrival = self.arrival_rates_checked()
        batteries = self.source.read_batteries()
        verdict = preflight_battery(batteries)

        for label in ("L", "R"):
            connected = batteries.get(label) is not None or arrival.get(label, 0) > 0
            items.append(
                self._item(
                    f"link-{label.lower()}",
                    f"{_SIDES[label]}模块连接",
                    connected,
                    pass_hint="已连接",
                    fail=TerminalError(
                        code="E-BLE-1001",
                        message=f"{_SIDES[label]}模块未连接。",
                        action="请确认模块已开机并在范围内，然后重新检查。",
                    ),
                )
            )

        all_calibrated = all(calibrated.get(label, False) for label in ("L", "R"))
        items.append(
            self._item(
                "factory-cal",
                "出厂标定参数",
                all_calibrated,
                pass_hint="已匹配",
                fail=TerminalError(
                    code="E-CAL-3001",
                    message="有模块没有匹配到出厂标定参数。",
                    action="请联系服务方按模块 MAC 下发标定参数；机构侧不做六面法。",
                ),
            )
        )

        disk_free = self.source.disk_free_bytes()
        items.append(
            self._item(
                "disk",
                "磁盘空间",
                disk_free >= MIN_DISK_FREE_BYTES,
                pass_hint=f"剩余 {disk_free / 1024**3:.0f} GB",
                fail=TerminalError(
                    code="E-BLE-1020",
                    message=f"磁盘剩余 {disk_free / 1024**3:.1f} GB，不足以安全落盘。",
                    action="请清理磁盘后重新检查。",
                ),
            )
        )

        # 电量：三态准入的结论原样搬过来。problems 已经是可执行文案，
        # 且把「读不到」与「电量低」分开写了 —— 在这里重写一句会把那个区分抹平。
        items.append(
            {
                "id": "battery",
                "label": "左右模块电量",
                "status": "pass" if verdict.admitted else "fail",
                "hint": (
                    " · ".join(
                        f"{_SIDES[label]} {verdict.readings[label]['percent']}%"
                        for label in ("L", "R")
                        if verdict.readings[label]["read"]
                    )
                    if verdict.admitted
                    else None
                ),
                "error": (
                    None
                    if verdict.admitted
                    else TerminalError(
                        code="E-BLE-1005",
                        message=" ".join(verdict.problems),
                        action=f"低于 {MIN_BATTERY_PERCENT}% 无法开始；请按上面的说明处理后重新检查。",
                    ).snapshot()
                ),
            }
        )

        arrival_ok = all(rate >= MIN_ARRIVAL_RATE for rate in arrival.values())
        items.append(
            self._item(
                "arrival",
                "链路到达率",
                arrival_ok,
                pass_hint=" · ".join(
                    f"{_SIDES[k]} {v:.0%}" for k, v in sorted(arrival.items())
                ),
                fail=TerminalError(
                    code="E-BLE-1010",
                    message=" · ".join(
                        f"{_SIDES[k]}到达率 {v:.0%}"
                        for k, v in sorted(arrival.items())
                        if v < MIN_ARRIVAL_RATE
                    )
                    + f"，低于 {MIN_ARRIVAL_RATE:.0%}。",
                    action="请缩短与模块的距离、避开遮挡，然后重新检查。",
                ),
            )
        )
        return items

    def _do_startSession(self, params: dict[str, Any]) -> dict[str, Any]:
        now = float(params.get("now", 0.0))
        self.walk = TimedWalk(self.config)
        self._aborted = None
        self.walk.start_baseline(now)
        self.walk.start_calibration(now)
        self.walk.start_walking(now)
        self._walk_started_at = now
        return {
            "totalSeconds": self.config.duration_s,
            "instruction": "请按平时走路的速度，在两个标志之间来回走",
            "steps": self._steps(),
            "link": self._links(),
            "remainingSeconds": self.config.duration_s,
        }

    def _do_stopSession(self, params: dict[str, Any]) -> dict[str, Any]:
        walk = self._require_walk()
        walk.stop(float(params.get("now", 0.0)))
        return {"state": walk.state, "validSeconds": round(walk.valid_seconds, 3)}

    def _do_sessionResult(self, params: dict[str, Any]) -> dict[str, Any]:
        """会话有效性 + 双足完整性。两者分开记账，因为它们判的不是一件事。"""
        walk = self._require_walk()
        links = tuple(
            LinkOutcome(
                foot=label,  # type: ignore[arg-type]
                disconnected_at=(params.get("disconnectedAt") or {}).get(label),
                reconnects=int((params.get("reconnects") or {}).get(label, 0)),
            )
            for label in ("L", "R")
        )
        outcome = summarize_session(links)
        verdict = walk.verdict(
            # 佩戴由 P-06 的操作员裁定给出；调用方不给就是 unknown，
            # 而不是替它答 pass。
            wearing=params.get("wearing", CHECK_UNKNOWN),
            link=CHECK_PASS if outcome.complete else CHECK_FAIL,
        )
        # **三态而不是布尔。** `wearing` 默认 unknown（RAY-260：左右戴反在位置法下
        # 数学上不可判定，改为 P-06 手工裁定），所以「评不了」是一个真实的结局，
        # 与「无效」不是一回事：前者要操作员回去确认左右，后者要重测。把两者压成
        # 一个 valid 布尔，正是 PRD §13 唯一硬拦截被悄悄架空的方式。
        result: dict[str, Any] = {
            "overall": verdict.overall,
            "verdict": verdict.snapshot(),
            "integrity": outcome.snapshot(),
            "validSteps": sum(self._steps().values()),
        }
        if verdict.overall == VERDICT_INVALID and verdict.duration == CHECK_FAIL:
            result["error"] = TerminalError(
                code="E-QLT-5002",
                message=(
                    f"有效步行时长 {verdict.valid_seconds:.0f} 秒，"
                    f"低于本次配置 {self.config.duration_s} 秒的 "
                    f"{self.config.valid_fraction:.0%}。"
                ),
                action="请确认通道长度与转身标志位置，然后重新检测。",
            ).snapshot()
        # 报告是另一件事：会话有效不等于报告已生成。
        result["report"] = {
            "status": protocol.STATUS_UNIMPLEMENTED,
            "capability": "report",
            "issue": "RAY-224",
        }
        return result

    def _do_createSubject(self, _: dict[str, Any]) -> dict[str, Any]:
        """P-02 快速建档：一个随机 `subject_uuid`，不含任何身份明文（FR-02）。"""
        return {"subjectUuid": new_subject_uuid(), "consentValid": False}

    def _do_listRecords(self, _: dict[str, Any]) -> list[dict[str, Any]]:
        if self.session_root is None:
            return []
        records = []
        for session_id in list_sessions(self.session_root):
            meta = read_meta(session_directory(self.session_root, session_id))
            records.append(
                {
                    "id": session_id,
                    "subjectUuid": meta.subject_uuid,
                    "protocolSeconds": meta.protocol_config.get("duration_s"),
                    "algoVersion": meta.algo_version,
                }
            )
        return records

    def _do_deviceSupport(self, _: dict[str, Any]) -> dict[str, Any]:
        return {
            "modules": self.source.module_info(),
            "ipcContractVersion": protocol.IPC_CONTRACT_VERSION,
        }

    # ── 显式缺口 ──────────────────────────────────────────────────────────

    def _do_runCalibration(self, _: dict[str, Any]) -> Any:
        return _Unimplemented("calibration")

    def _do_reportFor(self, _: dict[str, Any]) -> Any:
        return _Unimplemented("report")

    def _do_lookupSubject(self, _: dict[str, Any]) -> Any:
        return _Unimplemented("subject-directory")

    # ── 事件流 ────────────────────────────────────────────────────────────

    def tick(self, now: float) -> dict[str, Any]:
        """P-08 每拍推给渲染进程的三样东西，一样不多。

        FR-07：采集中链路健康只以到达率表达；PRD §6.1：采集中只显示剩余时间、步数、
        链路三档，不显示专业指标、不显示上传进度。所以这个 payload 是有意贫瘠的。
        """
        self._require_walk()  # 未开始就问剩余时间，是调用方的错，不是 0 秒
        started = self._walk_started_at
        if started is None:  # pragma: no cover - start_walking 保证已设
            raise protocol.ProtocolError("会话尚未开走")
        remaining = max(0.0, self.config.duration_s - (now - started))
        self._event_seq += 1
        return protocol.event(
            "session.tick",
            self._event_seq,
            {
                "remainingSeconds": round(remaining, 1),
                "steps": self._steps(),
                "link": self._links(),
            },
        )

    def notice(self, text: str) -> dict[str, Any]:
        self._event_seq += 1
        return protocol.event("session.notice", self._event_seq, {"text": text})

    def abort(self, now: float, failure: TerminalError) -> dict[str, Any]:
        walk = self._require_walk()
        walk.abort(now, failure.message)
        self._aborted = failure.snapshot()
        self._event_seq += 1
        return protocol.event(
            "session.aborted", self._event_seq, {"error": failure.snapshot()}
        )

    # ── 内部 ──────────────────────────────────────────────────────────────

    def arrival_rates_checked(self) -> dict[str, float]:
        rates = self.source.arrival_rates()
        for label, rate in rates.items():
            if not 0.0 <= rate <= 1.0:
                raise ValueError(f"{label} 的到达率 {rate} 不在 0–1 内")
        return rates

    def _require_walk(self) -> TimedWalk:
        if self.walk is None:
            raise protocol.ProtocolError("会话尚未开始")
        return self.walk

    def _steps(self) -> dict[str, int]:
        counts = self.source.step_counts()
        return {"left": counts.get("L", 0), "right": counts.get("R", 0)}

    def _links(self) -> dict[str, str]:
        grades = self.source.link_grades()
        return {"left": grades.get("L", "bad"), "right": grades.get("R", "bad")}

    def _snapshot(self) -> dict[str, Any]:
        verdict = preflight_battery(self.source.read_batteries())
        return {
            "operator": self.operator,
            "protocolSeconds": self.config.duration_s,
            "deviceSummary": {
                "ready": verdict.admitted,
                "issues": list(verdict.problems),
            },
            "ipcContractVersion": protocol.IPC_CONTRACT_VERSION,
        }

    @staticmethod
    def _item(
        item_id: str, label: str, passed: bool, *, pass_hint: str, fail: TerminalError
    ) -> dict[str, Any]:
        return {
            "id": item_id,
            "label": label,
            "status": "pass" if passed else "fail",
            "hint": pass_hint if passed else None,
            "error": None if passed else fail.snapshot(),
        }


class _Unimplemented:
    __slots__ = ("capability",)

    def __init__(self, capability: str) -> None:
        self.capability = capability
