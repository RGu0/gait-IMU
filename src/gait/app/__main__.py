"""sidecar 进程入口：stdin/stdout 上的 JSON Lines。

## 为什么是 JSON Lines，为什么入口这么薄

传输形态由 RAY-250 决定（Electron 主进程怎么拉起、要不要换成别的通道）。本模块因此
只做三件事：读一行、交给 `TerminalService.handle`、写一行。所有判定都在 service 里，
而 service 不认识 stdio —— 换传输时要重写的只有这个文件。

选 JSON Lines 是因为它是**在没有 Electron 的情况下也能被驱动**的最小形态：一条
`echo '{...}' | python -m gait.app` 就能验一次真实往返，契约测试也据此跨语言跑起来。
等 RAY-250 定了形态，这里要么保留、要么换掉，都不影响契约本身。

**stdout 只走协议**：任何诊断输出都必须去 stderr，否则它会被当成一条消息。

## 事件泵（RAY-493）

请求/应答之外，采集中 sidecar 要**主动**推 `session.tick` / `session.aborted`。在此
之前没人调 `service.tick`，于是 P-08 的倒计时不动，写盘巡检（长在 tick 里）也从来
不跑 —— 磁盘写满时操作员要陪着走完全程才发现。泵是一个后台线程：只在
`service.session_running` 时 tick，与请求处理共用一把锁（service 不是线程安全的），
stdout 另一把锁（两个线程写同一个流，行会交错）。

## 环境变量

| 变量 | 含义 |
|---|---|
| `GAIT_DEVICE_SOURCE` | `stub`（默认）/ `synthetic` / `replay` / `ble` |
| `GAIT_REPLAY_SESSION` | `replay` 时要回放的会话目录 |
| `GAIT_PROTOCOL_SECONDS` | 协议时长，取预设 60/120/180 之一 |
| `GAIT_PREVIEW` | `1` = 预览策略（出厂标定可放行，报告注明） |
| `GAIT_SESSION_ROOT` / `GAIT_ACCESS_ROOT` / `GAIT_STUB_FEED_HZ` | 见 `service_from_environment` |
| `GAIT_CONFIG_ROOT` | 设备级配置目录：左右模块绑定（RAY-479）。不设则绑定报 unavailable |
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, TextIO

from gait.app.bindings import BindingStore
from gait.app.protocol import ProtocolError
from gait.app.replay import (
    ReplayDeviceSource,
    UnavailableDeviceSource,
    frames_from_session,
    frames_from_synthetic,
)
from gait.app.service import PreviewPolicy, TerminalService
from gait.app.sources import DeviceSource, StubDeviceSource
from gait.app.uploadloop import build_uploader
from gait.config import ConfigError, ProtocolConfig

#: 合成步行比协议多生成的秒数：开流在自检之后、开走之前，中间有几秒的操作余量。
SYNTHETIC_MARGIN_S = 10.0

#: 事件泵的节拍。P-08 的倒计时按秒显示，更密只是多写几行 stdout。
TICK_INTERVAL_S = 1.0


def service_from_environment(env: Mapping[str, str] | None = None) -> TerminalService:
    """按环境变量配置一个 service。

    `GAIT_SESSION_ROOT` 决定这次运行往哪里落盘。**不设就不落盘** —— 那是一个显式
    的「这次不写」，而不是悄悄什么都没写：`_open_capture` 在没有 root 时直接返回，
    `stopSession` 的 `capture` 字段随之为 `null`，调用方看得见。

    `GAIT_ACCESS_ROOT` 指向服务方安装时写入的预配置目录。**不设就不传** —— 排空
    线程根本不会建，`snapshot` 的 `uploadSummary.drain` 为 `null`，而不是建一个永远
    连不上的线程在那里空转报错。未预配置是 v1 的正常状态，不是故障。

    `GAIT_STUB_FEED_HZ` 只在没有真设备时有意义，它让 stub 产生结构合法的合成字节。
    产生的会话的元数据里会带 `provenance.source = "stub"`，确保它永远不会被当成
    实测数据 —— 见 `sources.StubDeviceSource.provenance`。
    """
    values = os.environ if env is None else env
    root = values.get("GAIT_SESSION_ROOT")
    session_root = Path(root) if root else None
    preview = preview_from_environment(values)
    if preview is not None:
        _log("预览策略已启用（GAIT_PREVIEW=1）：出厂标定未匹配时放行，报告将注明。")
    return TerminalService(
        source=build_source(values),
        config=protocol_config_from_environment(values),
        session_root=session_root,
        uploader=build_uploader(session_root, values.get("GAIT_ACCESS_ROOT")),
        preview=preview,
        bindings=bindings_from_environment(values),
        **build_operator_auth(values.get("GAIT_ACCESS_ROOT")),
    )


def protocol_config_from_environment(values: Mapping[str, str]) -> ProtocolConfig:
    """`GAIT_PROTOCOL_SECONDS` → 协议配置。不合法就用默认并在 stderr 说一声。

    不合法时**不退出**：退出会让 Electron 那侧只看到一个起不来的 sidecar，而原因
    藏在 stderr 里；用默认时长照常起来，至少流程可走、日志里有原话。
    """
    raw = (values.get("GAIT_PROTOCOL_SECONDS") or "").strip()
    if not raw:
        return ProtocolConfig()
    try:
        return ProtocolConfig(duration_s=int(raw))
    except (ValueError, ConfigError) as error:
        _log(f"GAIT_PROTOCOL_SECONDS={raw!r} 不可用，沿用默认时长：{error}")
        return ProtocolConfig()


def bindings_from_environment(values: Mapping[str, str]) -> BindingStore | None:
    """`GAIT_CONFIG_ROOT` → 绑定存放处。**不设就是 None**，界面如实显示「未配置」。

    与会话根分开（见 `gait.app.bindings` 模块文档）：清理会话数据不该顺手清掉左右绑定。
    """
    root = (values.get("GAIT_CONFIG_ROOT") or "").strip()
    return BindingStore(Path(root)) if root else None


def preview_from_environment(values: Mapping[str, str]) -> PreviewPolicy | None:
    if (values.get("GAIT_PREVIEW") or "").strip() != "1":
        return None
    return PreviewPolicy(waive_factory_calibration=True)


def build_source(env: Mapping[str, str] | None = None) -> DeviceSource:
    """按 `GAIT_DEVICE_SOURCE` 选设备源。

    `ble` 起不来时给 `UnavailableDeviceSource` 而**不退回 stub**：退回 stub 自检会全绿，
    操作员以为模块已经连上。不认识的取值则退回 stub 并在 stderr 说明 —— 那是配置
    笔误，不是设备故障。
    """
    values = os.environ if env is None else env
    kind = (values.get("GAIT_DEVICE_SOURCE") or "stub").strip().lower()
    if kind == "stub":
        try:
            feed_hz = float(values.get("GAIT_STUB_FEED_HZ", "0") or "0")
        except ValueError:
            feed_hz = 0.0
        return StubDeviceSource(autofeed_hz=feed_hz)
    if kind == "synthetic":
        seconds = protocol_config_from_environment(values).duration_s + SYNTHETIC_MARGIN_S
        return ReplayDeviceSource(
            loader=lambda: frames_from_synthetic(seconds), label="synthetic"
        )
    if kind == "replay":
        session = (values.get("GAIT_REPLAY_SESSION") or "").strip()
        if not session:
            reason = "GAIT_DEVICE_SOURCE=replay 需要 GAIT_REPLAY_SESSION 指向一个会话目录。"
            _log(reason)
            return UnavailableDeviceSource(reason=reason)
        try:
            chunks = frames_from_session(Path(session))
        except (OSError, ValueError) as error:
            reason = f"回放会话读不回来：{session}（{error}）"
            _log(reason)
            return UnavailableDeviceSource(reason=reason)
        return ReplayDeviceSource(chunks=chunks, label="replay")
    if kind == "ble":
        try:
            from gait.app.blesource import BleDeviceSource

            source: DeviceSource = BleDeviceSource.from_environment(values)
            return source
        except ImportError as error:
            reason = f"本构建不含 BLE 设备源（{error}）。"
        except Exception as error:  # noqa: BLE001 - 任何初始化失败都要如实变成「不可用」
            reason = f"BLE 设备源初始化失败：{error}"
        _log(reason)
        return UnavailableDeviceSource(reason=reason)
    _log(f"未知的 GAIT_DEVICE_SOURCE={kind!r}，退回 stub。可选：stub / synthetic / replay / ble")
    return StubDeviceSource()


def _log(text: str) -> None:
    """诊断只去 stderr —— stdout 上的每一行都会被当成协议消息。"""
    print(f"[gait.app] {text}", file=sys.stderr, flush=True)


def build_operator_auth(access_root: Any) -> dict[str, Any]:  # pragma: no cover - 真实环境
    """按预配置造认证客户端与票据存放处（RAY-323 R1）。**造不出就两个都是 None。**

    与 `build_uploader` 同一条理由放在进程入口：构造它需要 `AccessStore`（预配置目录
    与密钥库），而 service 本身不该知道环境变量长什么样。

    **票据的密钥库与终端凭据共用同一个** `AccessStore.secrets` —— 两者都是「不能落
    文件的秘密」，分两个库只会多一处要各自记得清理的地方。键不同（见
    `operator.TICKET_SECRET_KEY`），所以不会互相覆盖。

    返回一个 dict 而不是二元组：调用点是 `TerminalService(**...)`，用 dict 就不必在
    那里再写一遍两个关键字的名字 —— 少一处会与签名分头漂移的重复。
    """
    from gait.cloud.operator import HttpOperatorAuth, TicketStore
    from gait.cloud.tenancy import AccessError, AccessStore

    if access_root is None:
        return {"auth": None, "tickets": None}
    try:
        store = AccessStore(access_root)
        auth = HttpOperatorAuth.from_access_store(store)
    except (AccessError, OSError):
        # 未预配置的终端没有云端可验 —— 与 `build_uploader` 一样，这是正常状态
        # 而不是错误。**注意由此 startSession 不设登录闸**，理由见
        # `TerminalService.__init__` 里那段注释。
        return {"auth": None, "tickets": None}
    return {"auth": auth, "tickets": TicketStore(store.secrets)}


def serve(
    stdin: TextIO,
    stdout: TextIO,
    service: TerminalService | None = None,
    *,
    tick_interval: float = TICK_INTERVAL_S,
    clock: Callable[[], float] = time.time,
) -> int:
    """`clock` 取 `time.time`：adapter 发来的 `now` 是 `Date.now() / 1000`，同一时基。"""
    service = service or service_from_environment()
    # 排空线程从这里起 —— 「什么时候开始传」是进程入口的调度决定。
    service.start_background_work()
    service_lock = threading.Lock()
    out_lock = threading.Lock()
    stop = threading.Event()
    events = threading.Thread(
        target=_pump_events,
        args=(stdout, service, service_lock, out_lock, stop, tick_interval, clock),
        name="gait-event-pump",
        daemon=True,
    )
    events.start()
    try:
        return _pump(stdin, stdout, service, service_lock, out_lock)
    finally:
        # stdin EOF：先停泵再收尾，免得它在一个正在关闭的 service 上 tick。
        stop.set()
        events.join(timeout=max(tick_interval, 0.1) * 2 + 1.0)
        # 不等正在传的那一件（RAY-416 待确认 3）。
        service.close()


def _pump(
    stdin: TextIO,
    stdout: TextIO,
    service: TerminalService,
    service_lock: threading.Lock | None = None,
    out_lock: threading.Lock | None = None,
) -> int:
    service_lock = service_lock or threading.Lock()
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message: dict[str, Any] = json.loads(line)
        except json.JSONDecodeError as exc:
            _emit(stdout, _fatal("", f"消息不是合法 JSON：{exc}"), out_lock)
            continue
        try:
            with service_lock:
                response = service.handle(message)
        except (ProtocolError, ValueError) as exc:
            response = _fatal(str(message.get("id", "")), str(exc))
        _emit(stdout, response, out_lock)
    return 0


def _pump_events(
    stdout: TextIO,
    service: TerminalService,
    service_lock: threading.Lock,
    out_lock: threading.Lock,
    stop: threading.Event,
    interval: float,
    clock: Callable[[], float],
) -> None:
    """采集中每拍 tick 一次，把结果（tick 或 aborted）推到 stdout。

    事件在持有 service 锁时写出：这样一条 tick 不会插到它之后才处理的 `stopSession`
    应答后面，渲染端看到的顺序与 service 里发生的顺序一致。
    """
    while not stop.wait(interval):
        with service_lock:
            if not service.session_running:
                continue
            try:
                event = service.tick(clock())
            except ProtocolError:
                # running 与 tick 在同一把锁里判，照理撞不上终态；万一 service 自己
                # 认为不该 tick（例如尚未开走），那是状态问题，不是要推给界面的事件。
                continue
            except Exception as error:  # noqa: BLE001 - 泵线程死掉等于倒计时静默冻结
                _log(f"事件泵 tick 失败：{error!r}")
                continue
            _emit(stdout, event, out_lock)


def _emit(
    stdout: TextIO, payload: dict[str, Any], lock: threading.Lock | None = None
) -> None:
    line = json.dumps(payload, ensure_ascii=False) + "\n"
    if lock is None:
        stdout.write(line)
        stdout.flush()
        return
    with lock:
        stdout.write(line)
        stdout.flush()


def _fatal(request_id: str, detail: str) -> dict[str, Any]:
    """协议层失败。

    它**不带六域错误码**，因为它不是六个域里的任何一个 —— 那六个域说的是采集现场
    出了什么事，而这里是两端说的话对不上。给它编一个 `E-BLE-xxxx` 会让日志里出现
    一个查无此事的设备故障。
    """
    return {
        "kind": "response",
        "id": request_id,
        "status": "error",
        "protocolError": detail,
    }


def main() -> int:  # pragma: no cover - 进程入口
    for stream in (sys.stdin, sys.stdout):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    return serve(sys.stdin, sys.stdout)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
