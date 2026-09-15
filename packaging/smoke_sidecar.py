"""对冻结 sidecar 做冒烟：本机与 CI（macOS / Windows）跑的是同一个脚本。

    python packaging/smoke_sidecar.py dist/gait-sidecar/gait-sidecar[.exe]

三步，任一步不符即非零退出：

1. `GAIT_SELFTEST=imports` —— 延迟 import 的模块是否真的被冻结进去（见 sidecar_entry.py）。
2. 管道一条 `describe` 请求 —— 协议往返、`contract.json` 是否作为包数据带上
   （缺了它进程在 import 期就崩，根本回不了话）。
3. 管道一条 `snapshot` 请求，**不设 `GAIT_DEVICE_SOURCE`** —— 默认设备源在冻结产物里能起。

写成 Python 而不是 bash + pwsh 各一份：两份断言迟早对不上，而 CI 上 uv 本来就在。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CONTRACT = REPO_ROOT / "src" / "gait" / "app" / "contract.json"
TIMEOUT_S = 120


def _env(**extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GAIT_")}
    env["PYTHONUTF8"] = "1"
    env.update(extra)
    return env


def _run(exe: str, *, stdin: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    started = time.monotonic()
    proc = subprocess.run(
        [exe],
        input=stdin,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        timeout=TIMEOUT_S,
        check=False,
    )
    print(f"  exit={proc.returncode} elapsed={time.monotonic() - started:.2f}s")
    if proc.stderr.strip():
        print(f"  stderr: {proc.stderr.strip()}")
    return proc


def _request(exe: str, method: str) -> dict:
    version = json.loads(CONTRACT.read_text(encoding="utf-8"))["ipc_contract_version"]
    line = json.dumps(
        {"kind": "request", "v": version, "id": f"smoke-{method}", "method": method, "params": {}}
    )
    proc = _run(exe, stdin=line + "\n", env=_env())
    if proc.returncode != 0:
        raise SystemExit(f"{method}: sidecar 退出码 {proc.returncode}")
    lines = [raw for raw in proc.stdout.splitlines() if raw.strip()]
    if len(lines) != 1:
        raise SystemExit(f"{method}: 期望一行响应，得到 {len(lines)} 行：{proc.stdout!r}")
    response = json.loads(lines[0])
    print(f"  response: {lines[0][:300]}")
    if response.get("kind") != "response" or response.get("id") != f"smoke-{method}":
        raise SystemExit(f"{method}: 响应信封不对：{response}")
    if response.get("status") != "ok":
        raise SystemExit(f"{method}: status={response.get('status')!r}：{response}")
    return response["result"]


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(__doc__, file=sys.stderr)
        return 2
    exe = argv[0]

    print("[1/3] GAIT_SELFTEST=imports")
    proc = _run(exe, stdin="", env=_env(GAIT_SELFTEST="imports"))
    print("  " + proc.stdout.strip().replace("\n", "\n  "))
    if proc.returncode != 0 or "selftest ok" not in proc.stdout:
        raise SystemExit("selftest 失败")

    print("[2/3] describe")
    result = _request(exe, "describe")
    if "describe" not in result.get("methods", []):
        raise SystemExit(f"describe 结果里没有方法表：{result}")

    print("[3/3] snapshot（GAIT_DEVICE_SOURCE 未设）")
    _request(exe, "snapshot")

    print("sidecar smoke ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
