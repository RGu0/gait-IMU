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
import sys
from typing import Any, TextIO

from gait.app.protocol import ProtocolError
from gait.app.service import TerminalService


def serve(stdin: TextIO, stdout: TextIO, service: TerminalService | None = None) -> int:
    service = service or TerminalService()
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
