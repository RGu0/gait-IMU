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
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from gait.app import protocol
from gait.app.errors import TerminalError
from gait.app.sources import DeviceSource, StubDeviceSource
from gait.app.transportloop import TransportLoop
from gait.cloud.chain import ChainResult, run_basic_chain
from gait.cloud.upload import UploadQueue, enqueue_session
from gait.config import ProtocolConfig
from gait.contracts import CONTRACT_VERSION, FootLabel, FootSeries, SessionMeta
from gait.device.capture import SessionCapture
from gait.device.footseries import frames_to_foot_series, load_session_frames
from gait.device.orchestration import (
    MIN_BATTERY_PERCENT,
    LinkOutcome,
    preflight_battery,
    summarize_session,
)
from gait.io.session import (
    create_session,
    list_sessions,
    new_session_id,
    new_subject_uuid,
    read_meta,
    session_directory,
    write_meta,
)
from gait.protocolflow.timed_walk import (
    CHECK_FAIL,
    CHECK_PASS,
    CHECK_UNKNOWN,
    TERMINAL,
    VERDICT_INVALID,
    TimedWalk,
)
from gait.report import build_report

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
        self.capture: SessionCapture | None = None
        self.session_id: str | None = None
        self.loop = TransportLoop()
        self._wrapped: dict[str, Any] = {}
        #: 每只脚的写盘错误，来自 `CaptureStatus.problems`。它必须一路进到会话
        #: 结论里去（`LinkOutcome.recording_error`），否则「这次采集有一半没落盘」
        #: 就停在一个事件里，而事件是会被错过的。
        self._recording_errors: dict[str, str] = {}
        #: 待传队列。与会话目录同根 —— 队列记的就是那些目录，分开放会让「哪份数据
        #: 属于哪个队列」变成一个要靠约定维持的事实。
        self.uploads = (
            UploadQueue(Path(session_root) / "upload-queue.sqlite3")
            if session_root is not None
            else None
        )
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
        self._recording_errors = {}
        self.walk.start_baseline(now)
        self.walk.start_calibration(now)
        self.walk.start_walking(now)
        self._walk_started_at = now
        self._open_capture()
        return {
            "totalSeconds": self.config.duration_s,
            "instruction": "请按平时走路的速度，在两个标志之间来回走",
            "steps": self._steps(),
            "link": self._links(),
            "remainingSeconds": self.config.duration_s,
            "sessionId": self.session_id,
        }

    def _open_capture(self) -> None:
        """建会话目录、写元数据、把两条传输包上录制层。

        ## 顺序就是 G-04 本身

        目录与元数据先落，再包传输，再开始收字节 —— 「先落盘再计算再上传」（原则 6）
        在这里是一个可以被 kill 打断并检验的顺序，不是一句口号。进程若在任何一点
        被杀，磁盘上留下的都是一份**能被认出来的、未完成的**会话。

        没有 `session_root` 就不落盘：那是「这次运行不写盘」的显式配置，
        而不是悄悄地什么也没写。
        """
        if self.session_root is None:
            return
        self.session_id = new_session_id()
        create_session(self.session_root, self._meta_at_start())
        self.capture = SessionCapture(self.session_root, self.session_id)
        # 顺序要紧：先 wrap 再 connect。`RecordingTransport` 在 `connect()` 里才
        # 接上 `on_data` —— 反过来做，字节会先到 inner，录制静悄悄地录不到东西。
        self.loop.start()
        self._wrapped = {
            label: self.capture.wrap(label, transport)
            for label, transport in self.source.transports().items()
        }
        for wrapped in self._wrapped.values():
            self.loop.submit(wrapped.connect())
        # 流最后开：在录制层接好之前开流，最早那几帧会绕过录制。
        self.source.begin_stream()

    def _meta_at_start(self) -> SessionMeta:
        """会话开始时能诚实写下的元数据。

        ## 三个字段此刻还不知道，于是写「尚未产出」而不是写零

        `sync_report`（锚点、残差、实测采样率）与 `integrity_report`（逐秒缺失率、
        空洞）只有会话结束后才存在；`calib_snapshot` 需要会话标定，而它还没实现
        （RAY-208）。契约要求这些字段**非空** —— 「空值与缺席对复现而言是一回事」——
        所以不能留 `{}`。

        写一个 `{"loss_rate": 0.0}` 之类的零值会更糟：那是一个**看起来已经算过**
        的结论。写 `state: pending` 则说的是实话，而且有一个额外的好处：**进程若在
        中途被杀，磁盘上那份元数据会永远停在 pending** —— 一份未完成的会话因此自己
        就说得清楚，不需要任何外部记录来判断它完没完成。

        `provenance` 随元数据落盘：一份 stub 产生的会话与一份真机会话在磁盘上长得
        一模一样，没有它就再也分不开。
        """
        return SessionMeta(
            session_id=self.session_id or new_session_id(),
            created_at=datetime.now(UTC).isoformat(timespec="seconds"),
            subject_uuid=new_subject_uuid(),
            scenario="walk",
            devices={
                label: {"device_id": t.device_id}
                for label, t in self.source.transports().items()
            },
            config_snapshot={
                "state": "pending",
                "reason": "配置下发快照在会话结束时补齐",
            },
            calib_snapshot={"state": "unimplemented", "issue": "RAY-208"},
            algo_version=f"gait-contract-{CONTRACT_VERSION}",
            algo_params=self.config.snapshot()
            if hasattr(self.config, "snapshot")
            else {"duration_s": self.config.duration_s},
            sync_report={"state": "pending", "reason": "同步报告在会话结束后才存在"},
            integrity_report={
                "state": "pending",
                "reason": "完整性报告在会话结束后才存在",
            },
            protocol_config=self.walk.protocol_snapshot()
            if self.walk
            else {"duration_s": self.config.duration_s},
            contract_version=CONTRACT_VERSION,
            notes="本地采集会话；元数据在会话结束时改写。停在 pending 即表示未正常结束。",
            extra={"provenance": self.source.provenance()},
        )

    def _do_stopSession(self, params: dict[str, Any]) -> dict[str, Any]:
        walk = self._require_walk()
        walk.stop(float(params.get("now", 0.0)))
        status = self._close_capture()
        return {
            "state": walk.state,
            "validSeconds": round(walk.valid_seconds, 3),
            "capture": _capture_snapshot(status),
        }

    def _close_capture(self) -> Any:
        """收尾落盘，并把元数据从 pending 改写成真实结论。

        改写在 `capture.close()` **之后**：那一步排空写队列并定下 `CaptureStatus`，
        在它之前写元数据，写下的就是一个还不成立的结论。
        """
        if self.capture is None or self.session_root is None or self.session_id is None:
            return None
        # 停流 → 断开 → close。每一步都让「还会有新字节吗」这个答案更确定，
        # 顺序反了就会有字节落在一个已经定了终态的 capture 上。
        self.source.end_stream()
        for wrapped in self._wrapped.values():
            try:
                self.loop.submit(wrapped.disconnect())
            except (RuntimeError, TimeoutError):
                # 断不开不该挡住收尾 —— 已经落盘的数据比一次干净的断开重要。
                pass
        self._wrapped = {}
        status = self.capture.close()
        self.loop.stop()
        # 把每只脚的写盘失败挑出来，供 `sessionResult` 构造 `LinkOutcome`。
        for problem in status.problems:
            for label in ("L", "R"):
                if problem.startswith(f"{label} 的原始数据写盘失败"):
                    self._recording_errors[label] = problem
        directory = session_directory(self.session_root, self.session_id)
        meta = read_meta(directory)
        write_meta(
            directory,
            replace(
                meta,
                integrity_report={
                    "complete": status.complete,
                    "chunks_written": dict(status.chunks_written),
                    "problems": list(status.problems),
                },
                sync_report={
                    "state": "not_computed",
                    "reason": "主机侧同步在离线重算阶段产出",
                },
            ),
        )
        self.capture = None
        self._enqueue_for_upload()
        return status

    def _enqueue_for_upload(self) -> None:
        """把刚收尾的会话排进待传队列。

        ## 不完整的会话**也要**上传

        一份因写盘失败而安全停止的会话，数据是残的 —— 但它的元数据自己说了它是残的
        （`integrity_report.complete = False`），而原始数据是「数据评估的生命线」
        （RAY-226）。不排它，等于**恰恰把记录了一次故障的那份数据丢掉**，而 G-04
        要的是数据不静默丢失，不是只保住顺利的那些。

        ## 入队与发送是两件事

        这里只入队。真正发出去需要一个 `IngestionClient` 实现，而服务端不在本仓库
        （同 RAY-225 的边界）—— 见 `contract.json` 的 `upload-transport` 缺口。
        入队本身已经有意义：它让「服务端确认前不删本地」有了记账处，也让 P-01 的
        待传条数有真实来源。
        """
        if self.uploads is None or self.session_root is None or self.session_id is None:
            return
        enqueue_session(self.uploads, self.session_root, self.session_id)

    def _do_sessionResult(self, params: dict[str, Any]) -> dict[str, Any]:
        """会话有效性 + 双足完整性。两者分开记账，因为它们判的不是一件事。"""
        walk = self._require_walk()
        links = tuple(
            LinkOutcome(
                foot=label,  # type: ignore[arg-type]
                disconnected_at=(params.get("disconnectedAt") or {}).get(label),
                reconnects=int((params.get("reconnects") or {}).get(label, 0)),
                # 写盘失败由 sidecar 自己知道，不该等调用方转述 —— 转述会漏。
                recording_error=self._recording_errors.get(label),
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
        # 报告是另一件事：会话有效不等于报告已生成。这里只声明「可生成」——
        # 真正的报告由 `reportFor` 生成（RAY-224 basic-report 已接通，不再是缺口）。
        # 会话级无效则不生成报告（PRD §13），状态让渲染端直接走「未通过 + 重测」。
        result["report"] = {
            "status": "invalid" if verdict.overall == VERDICT_INVALID else "ready",
            "sessionId": self.session_id,
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

    def _do_reportFor(self, params: dict[str, Any]) -> Any:
        """一份已完成会话的基础报告。

        报告层不判会话有效性 —— PRD §13「会话级无效不生成报告」是**调用前**的判断，
        由 `sessionResult` 给出。这里只在拿不到步态周期时拒绝，而拒绝的理由说的正是
        这件事，免得调用方以为报告层会替它把关。
        """
        try:
            cycles = self._cycles_for(params)
        except (OSError, ValueError):
            # 会话目录里的录制读不回来：文件不在、被截断到读不出、或格式不认识。
            # **不能落到 E-QLT-5003**（「质量不足」）—— 那会把一次读盘失败说成一次
            # 采集质量问题，让操作员去重做一场其实采得好好的检测。
            # `FootSeriesError` 是 `ValueError` 的子类，一并落在这里。
            return TerminalError(
                code="E-BLE-1021",
                message="这次会话的原始数据读不回来，无法重新计算。",
                action="请确认会话目录仍然完整；若数据已损坏，这次检测需要重做。",
                blocking=True,
            )
        if not cycles:
            return TerminalError(
                code="E-QLT-5003",
                message="这次检测没有可用的步态周期，无法生成报告。",
                action="请确认会话有效性判定的结果；会话级无效不生成报告。",
            )
        walk = self.walk
        return build_report(
            cycles,
            report_id=str(
                params.get("reportId")
                # 给哪一次会话出报告，`reportId` 就该跟着那一次 —— 而重算历史会话时
                # `self.session_id` 是空的（那是**本进程**当前的会话）。漏了这一层，
                # 每一份历史报告的 id 都会是 "R-unknown"，而报告 id 是它唯一的把手。
                or params.get("sessionId")
                or self.session_id
                or "R-unknown"
            ),
            organization=(self.operator or {}).get("organization", "本机构"),
            subject_label=str(params.get("subjectLabel") or "未提供"),
            assessed_at=datetime.now(UTC).date().isoformat(),
            duration_s=self.config.duration_s,
            algo_version=f"gait-contract-{CONTRACT_VERSION}",
            protocol_version=str(self.config.version),
            valid_seconds=walk.valid_seconds if walk else 0.0,
            turns=params.get("turns"),
            annotations_text=self._report_annotations(params),
        )

    #: 从会话目录重算时必须写进报告的一句话。见 `_chain_for` 的文档。
    _UNCALIBRATED_NOTE = (
        "本次报告由采集端就地重算，未使用标定参数（出厂加计标定与会话安装角）。"
        "各项数值可用于对比同一台设备的多次检测，不作为绝对精度依据。"
    )

    def _report_annotations(self, params: dict[str, Any]) -> list[str]:
        """报告顶部的标注条。

        调用方给的标注照原样带上；**另外**，当周期是本层从会话目录重算出来的时候，
        补一句「未使用标定参数」。

        为什么非写不可：这条路径走的是 MVP 桥，没有标定补偿（见 `_chain_for`）。
        一份没有标定的报告与一份有标定的报告在版面上**长得一模一样** —— 指标齐全、
        质量标注全绿。读的人无从分辨，除非这里说出来。

        调用方直接传了 `cycles` 就不加这句：那些周期从哪来本层不知道，替它声明
        「未标定」同样是在编。
        """
        given = [str(text) for text in (params.get("annotations") or ())]
        if params.get("cycles"):
            return given
        return [*given, self._UNCALIBRATED_NOTE]

    def _cycles_for(self, params: dict[str, Any]) -> list[Any]:
        """本次会话的步态周期。

        两条来路，顺序有意：

        1. 调用方直接传 `cycles`（离线重算与测试走这条），
        2. 否则**从会话目录把录制读回来跑一遍基础链**。

        直传优先，是因为一个明确给了周期的调用方不该被一次磁盘读覆盖掉；而没给的
        时候，从前一直是空列表 —— 那正是 RAY-360 要补的那一段。
        """
        provided = list(params.get("cycles") or [])
        if provided:
            return provided
        chain = self._chain_for(params)
        if chain is None:
            return []
        # `selected` 是分段筛选之后的中段步，也就是分析层认为可用的那些。传全部
        # `cycles` 会把转身那几步算进指标里 —— 那是 `analysis/segments` 存在的理由。
        return [cycle for outcome in chain.feet.values() for cycle in outcome.selected]

    def _chain_for(self, params: dict[str, Any]) -> ChainResult | None:
        """把一次已落盘的会话跑过基础链。拿不到数据就返回 `None`。

        **只用基础链**（前向 ESKF，不做 RTS 平滑 / 零速锚定 / 双足距离约束）：完整链
        属 `cloud/`，由重算侧跑，采集端不在这里替它做决定。

        走 MVP 桥 `frames_to_foot_series` 而不是 `calibrated_foot_series`：后者要
        `StillCalibration` 与 `MountingCalibration`，而会话目录里只有 `calib_snapshot`
        这份**快照**，从快照重建标定对象是另一件事。所以这条路上的数据**没有标定
        补偿**，报告里会写明（见 `_do_reportFor`）—— 不写明地用一份未标定的数据出
        一份看不出区别的报告，比出不了报告危险得多。
        """
        session_id = params.get("sessionId") or self.session_id
        if not session_id or self.session_root is None:
            return None
        series_by_foot: dict[FootLabel, FootSeries] = {}
        for label in ("L", "R"):
            frames = load_session_frames(self.session_root, str(session_id), label)
            if not frames:
                continue
            series_by_foot[label] = frames_to_foot_series(frames, label)
        if not series_by_foot:
            return None
        return run_basic_chain(series_by_foot, protocol_seconds=self.config.duration_s)

    def _do_lookupSubject(self, _: dict[str, Any]) -> Any:
        return _Unimplemented("subject-directory")

    # ── 事件流 ────────────────────────────────────────────────────────────

    def tick(self, now: float) -> dict[str, Any]:
        r"""P-08 每拍推给渲染进程的三样东西，一样不多 —— 外加一次写盘巡检。

        FR-07：采集中链路健康只以到达率表达；PRD §6.1：采集中只显示剩余时间、步数、
        链路三档，不显示专业指标、不显示上传进度。所以这个 payload 是有意贫瘠的。

        ## 为什么巡检长在这里

        `capture.py` 的模块文档写明：「写线程里的错误不会自己冒到事件循环，所以必须
        有人主动看」。在此之前**没人看** —— `grep -rn "\.check()" src/` 只搜得到那句
        文档本身。后果不是报错，是磁盘写满之后倒计时照常走完：操作员陪着受试者走完
        三分钟，结束时才发现什么都没采到。

        tick 是唯一一个「采集期间会反复发生」的调用点，所以巡检长在这里。发现失败时
        本方法**返回 `session.aborted` 而不是 tick** —— 渲染端据此整页接管（UI 设计
        §7：写盘错误是阻断级，不是侧栏图标级）。
        """
        walk = self._require_walk()  # 未开始就问剩余时间，是调用方的错，不是 0 秒
        if walk.state in TERMINAL:
            # 已经停了还在 tick，说明调用方没有理会上一条 `session.aborted`。
            # 继续发 tick 会让倒计时在一个已经安全停止的会话上照常往下走 ——
            # 那正是本 scope 要消灭的那个画面，只是换了个成因。
            raise protocol.ProtocolError(
                f"会话已处于终态 {walk.state!r}，不能再 tick。"
                "收到 session.aborted 之后应当停止推进。"
            )
        aborted = self._check_writes(now)
        if aborted is not None:
            return aborted
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

    def _check_writes(self, now: float) -> dict[str, Any] | None:
        """巡检写盘。有失败就安全停止，返回中止事件；否则返回 `None`。"""
        if self.capture is None:
            return None
        problems = self.capture.failures()
        if not problems:
            return None
        return self.abort(
            now,
            TerminalError(
                code="E-BLE-1020",
                message="原始数据写盘失败，测试已安全停止。" + "".join(problems),
                action="请检查磁盘剩余空间后重新检测。本次数据已尽可能保留，但不完整，不会生成报告。",
            ),
        )

    def notice(self, text: str) -> dict[str, Any]:
        self._event_seq += 1
        return protocol.event("session.notice", self._event_seq, {"text": text})

    def abort(self, now: float, failure: TerminalError) -> dict[str, Any]:
        """安全停止。

        ## 「安全」指的是收尾的顺序，不是「没出事」

        PRD §6.1：断连或写盘错误即安全停止并标记会话不完整。所以这里必须先把采集
        收尾 —— 停流、断开、close、把真实结论改写进元数据 —— 再中止流程。

        在此之前 `abort()` 只中止 `TimedWalk`，采集就那么挂着：写线程还在跑，元数据
        永远停在 pending，而 pending 的含义是「进程没了」。一个被安全停止的会话与一个
        被杀掉的进程在磁盘上因此长得一样，那正好把上个 scope 建立的判据毁掉。
        """
        walk = self._require_walk()
        self._close_capture()
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
            # P-01 顶部的「数据已同步 / 待上传」。数字来自真实队列，不是常量 ——
            # 一个永远显示 0 的待传数会让积压这件事永远不被发现。
            "uploadSummary": self._upload_summary(),
            "ipcContractVersion": protocol.IPC_CONTRACT_VERSION,
        }

    def _upload_summary(self) -> dict[str, Any]:
        """待传积压。没有队列时说「没在记账」，而不是报 0。"""
        if self.uploads is None:
            return {"tracked": False}
        report = self.uploads.backlog()
        return {"tracked": True, **report.snapshot()}

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


def _capture_snapshot(status: Any) -> dict[str, Any] | None:
    """`CaptureStatus` 没有自己的 `snapshot()`，在这里成形。

    `complete` 为假**不表示数据不可用** —— 与 `SessionOutcome` 同理：已经落盘的
    那部分照样在，不可用的是「这是一份完整会话」这个说法。所以 problems 要原样
    带出去，而不是压成一个布尔。
    """
    if status is None:
        return None
    return {
        "complete": status.complete,
        "chunks_written": dict(status.chunks_written),
        "problems": list(status.problems),
    }


class _Unimplemented:
    __slots__ = ("capability",)

    def __init__(self, capability: str) -> None:
        self.capability = capability
