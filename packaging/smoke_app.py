"""对 electron-builder 打出的**未安装**应用做一次真实启动（macOS / Windows 同一个脚本）。

    python packaging/smoke_app.py apps/terminal/main/release

它找 `mac-arm64/GaitIMU Preview.app/Contents/MacOS/GaitIMU Preview` 或
`win-unpacked/GaitIMU Preview.exe`，带 `GAIT_SMOKE_QUIT_AFTER_MS` 启动（`main.js` 到点自己
退出并打印渲染端文本），然后断言三件事：

1. `sidecar state: ready` —— 冻结 sidecar 从 `<resources>/sidecar/` 真的拉起来了；
2. `renderer did-finish-load` —— asar 里的 `renderer/index.html` 找得到；
3. `renderer text` 里有「工作台」—— 渲染端拿到 sidecar 状态并画出了主页，不是白屏。

sidecar 构件冒烟（smoke_sidecar.py）证明不了这些：那里没有 asar、没有 extraResources 路径、
没有主进程替换出来的环境。
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

QUIT_AFTER_MS = 20_000
TIMEOUT_S = 120
EXPECTED = ("sidecar state: ready", "renderer did-finish-load", "工作台")


def _executable(release: Path) -> Path:
    candidates = [
        release / "mac-arm64" / "GaitIMU Preview.app" / "Contents" / "MacOS" / "GaitIMU Preview",
        release / "win-unpacked" / "GaitIMU Preview.exe",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    raise SystemExit(f"在 {release} 下找不到打包后的应用：{[str(c) for c in candidates]}")


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(__doc__, file=sys.stderr)
        return 2
    exe = _executable(Path(argv[0]))
    print(f"launch: {exe}")
    with tempfile.TemporaryDirectory(prefix="gait-app-smoke-", ignore_cleanup_errors=True) as home:
        env = {k: v for k, v in os.environ.items() if not k.startswith("GAIT_")}
        env["GAIT_SMOKE_QUIT_AFTER_MS"] = str(QUIT_AFTER_MS)
        # 用一次性的 userData：本机上不碰自己的预览设置与会话，CI 上保证是首次启动的默认值。
        # 改 HOME / APPDATA 没用 —— Electron 取系统目录不看这两个变量；Chromium 开关才管用。
        proc = subprocess.run(
            [str(exe), f"--user-data-dir={home}"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=TIMEOUT_S,
            check=False,
        )
        # sidecar 是否已随应用退出：看它还在不在跑，而不是只信主进程的退出码。
        sidecar_left = _sidecar_running(exe)
    print(proc.stdout)
    if proc.stderr.strip():
        print(f"stderr:\n{proc.stderr}")
    print(f"exit={proc.returncode}")
    missing = [needle for needle in EXPECTED if needle not in proc.stdout]
    if missing:
        raise SystemExit(f"打包后的应用没有走到预期状态，缺：{missing}")
    if sidecar_left:
        raise SystemExit("应用退出后 gait-sidecar 仍在运行")
    print("app smoke ok")
    return 0


def _sidecar_running(exe: Path) -> bool:
    if sys.platform == "win32":
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq gait-sidecar.exe"],
            capture_output=True, text=True, errors="replace", check=False,
        ).stdout
        return "gait-sidecar.exe" in out
    out = subprocess.run(["ps", "-axo", "command"], capture_output=True, text=True, check=False).stdout
    return any("gait-sidecar" in line and str(exe.parents[1]) in line for line in out.splitlines())


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
