"""sidecar 进程入口：stdin/stdout 上的 JSON Lines。

## 为什么是 JSON Lines，为什么入口这么薄

传输形态由 RAY-250 决定（Electron 主进程怎么拉起、要不要换成别的通道）。本模块因此
只做三件事：读一行、交给 `TerminalService.handle`、写一行。所有判定都在 service 里，
而 service 不认识 stdio —— 换传输时要重写的只有这个文件。

选 JSON Lines 是因为它是**在没有 Electron 的情况下也能被驱动**的最小形态：一条
`echo '{...}' | python -m gait.app` 就能验一次真实往返，契约测试也据此跨语言跑起来。
等 RAY-250 定了形态，这里要么保留、要么换掉，都不影响契约本身。

**stdout 只走协议**：任何诊断输出都必须去 stderr，否则它会被当成一条消息。
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any, TextIO

from gait.app.protocol import ProtocolError
from gait.app.service import TerminalService
from gait.app.sources import StubDeviceSource
from gait.app.uploadloop import build_uploader


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
    try:
        feed_hz = float(values.get("GAIT_STUB_FEED_HZ", "0") or "0")
    except ValueError:
        feed_hz = 0.0
    session_root = Path(root) if root else None
    return TerminalService(
        source=StubDeviceSource(autofeed_hz=feed_hz),
        session_root=session_root,
        uploader=build_uploader(session_root, values.get("GAIT_ACCESS_ROOT")),
        **build_operator_auth(values.get("GAIT_ACCESS_ROOT")),
    )


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


def serve(stdin: TextIO, stdout: TextIO, service: TerminalService | None = None) -> int:
    service = service or service_from_environment()
    # 排空线程从这里起 —— 「什么时候开始传」是进程入口的调度决定。
    service.start_background_work()
    try:
        return _pump(stdin, stdout, service)
    finally:
        # 不等正在传的那一件（RAY-416 待确认 3）。
        service.close()


def _pump(stdin: TextIO, stdout: TextIO, service: TerminalService) -> int:
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message: dict[str, Any] = json.loads(line)
        except json.JSONDecodeError as exc:
            _emit(stdout, _fatal("", f"消息不是合法 JSON：{exc}"))
            continue
        try:
            response = service.handle(message)
        except (ProtocolError, ValueError) as exc:
            response = _fatal(str(message.get("id", "")), str(exc))
        _emit(stdout, response)
    return 0


def _emit(stdout: TextIO, payload: dict[str, Any]) -> None:
    stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
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
