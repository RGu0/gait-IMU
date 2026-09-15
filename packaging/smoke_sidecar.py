"""对冻结 sidecar 做冒烟：本机与 CI（macOS / Windows）跑的是同一个脚本。

    python packaging/smoke_sidecar.py dist/gait-sidecar/gait-sidecar[.exe]

四步，任一步不符即非零退出：

1. `GAIT_SELFTEST=imports` —— 延迟 import 的模块是否真的被冻结进去（见 sidecar_entry.py）。
2. 管道一条 `describe` 请求 —— 协议往返、`contract.json` 是否作为包数据带上
   （缺了它进程在 import 期就崩，根本回不了话）。
3. 管道一条 `snapshot` 请求，**不设 `GAIT_DEVICE_SOURCE`** —— 默认设备源在冻结产物里能起。
4. 按 Electron 主进程给预览版的那套环境（`runtimeConfig.sidecarEnv`：synthetic 设备源、
   `GAIT_PREVIEW=1`、60 s 协议、临时会话目录）起一次，`snapshot` 必须报 `source: synthetic`，
   `runPreflight` 的出厂标定项必须是 `waived` —— 这是安装包打开后默认走的那条路。

写成 Python 而不是 bash + pwsh 各一份：两份断言迟早对不上，而 CI 上 uv 本来就在。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
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


def _requests(exe: str, methods: list[str], env: dict[str, str]) -> list:
    """在同一个 sidecar 进程里依次发 `methods`，返回各自的 result。"""
    version = json.loads(CONTRACT.read_text(encoding="utf-8"))["ipc_contract_version"]
    lines_in = [
        json.dumps({"kind": "request", "v": version, "id": f"smoke-{m}", "method": m, "params": {}})
        for m in methods
    ]
    proc = _run(exe, stdin="\n".join(lines_in) + "\n", env=env)
    if proc.returncode != 0:
        raise SystemExit(f"{methods}: sidecar 退出码 {proc.returncode}")
    lines = [raw for raw in proc.stdout.splitlines() if raw.strip()]
    if len(lines) != len(methods):
        raise SystemExit(f"{methods}: 期望 {len(methods)} 行响应，得到 {len(lines)} 行：{proc.stdout!r}")
    results = []
    for method, line in zip(methods, lines, strict=True):
        response = json.loads(line)
        print(f"  {method}: {line[:400]}")
        if response.get("kind") != "response" or response.get("id") != f"smoke-{method}":
            raise SystemExit(f"{method}: 响应信封不对：{response}")
        if response.get("status") != "ok":
            raise SystemExit(f"{method}: status={response.get('status')!r}：{response}")
        results.append(response["result"])
    return results


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(__doc__, file=sys.stderr)
        return 2
    exe = argv[0]

    print("[1/4] GAIT_SELFTEST=imports")
    proc = _run(exe, stdin="", env=_env(GAIT_SELFTEST="imports"))
    print("  " + proc.stdout.strip().replace("\n", "\n  "))
    if proc.returncode != 0 or "selftest ok" not in proc.stdout:
        raise SystemExit("selftest 失败")

    print("[2/4] describe")
    (result,) = _requests(exe, ["describe"], _env())
    if "describe" not in result.get("methods", []):
        raise SystemExit(f"describe 结果里没有方法表：{result}")

    print("[3/4] snapshot（GAIT_DEVICE_SOURCE 未设）")
    _requests(exe, ["snapshot"], _env())

    print("[4/4] 预览版环境：synthetic + GAIT_PREVIEW=1 → snapshot / runPreflight")
    with tempfile.TemporaryDirectory(prefix="gait-smoke-") as session_root:
        env = _env(
            GAIT_DEVICE_SOURCE="synthetic",
            GAIT_PREVIEW="1",
            GAIT_PROTOCOL_SECONDS="60",
            GAIT_SESSION_ROOT=session_root,
        )
        snapshot, preflight = _requests(exe, ["snapshot", "runPreflight"], env)
    if snapshot.get("source") != "synthetic" or snapshot.get("protocolSeconds") != 60:
        raise SystemExit(f"snapshot 没有按预览环境起来：{snapshot}")
    factory = [item for item in preflight if item.get("id") == "factory-cal"]
    if not factory or factory[0].get("status") != "waived":
        raise SystemExit(f"runPreflight 的出厂标定项不是 waived：{preflight}")

    print("sidecar smoke ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
