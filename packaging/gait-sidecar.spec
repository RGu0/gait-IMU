# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec：把 `python -m gait.app` 冻结成 onedir 可执行文件（RAY-250 preview-installers）。

构建与冒烟命令见同目录 README.md。产物 `dist/gait-sidecar/` 由 electron-builder 的
`extraResources` 放进安装包的 `<resources>/sidecar/`，主进程经
`sidecarCommand.js::packagedCommand()` 拉起。

## 为什么是 onedir 而不是 onefile

onefile 每次启动都要把整个运行时解压到临时目录：启动慢，而且 macOS 的蓝牙授权按可执行
文件路径记，路径每次都变就每次都弹。onedir 的路径随安装位置固定。

## 为什么 hiddenimports 要显式收

bleak 在运行时按 `sys.platform` 选后端，`gait.app` 的设备源由环境变量延迟 import ——
静态分析一个都看不见。漏收的后果不是构建失败，而是现场第一次点「连接设备」才报
ModuleNotFoundError。`packaging/sidecar_entry.py` 的 `GAIT_SELFTEST=imports` 专门验这一点。
"""

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

REPO_ROOT = Path(SPECPATH).resolve().parent  # noqa: F821 - SPECPATH 由 PyInstaller 注入
SRC = REPO_ROOT / "src"

# bleak 的其他平台后端（bluezdbus / p4android / 另一个桌面平台）在本平台 import 必然失败，
# 收进来只会刷一屏警告，不会有任何用处。
_FOREIGN_BLEAK_BACKENDS = {
    "darwin": ("bleak.backends.bluezdbus", "bleak.backends.p4android", "bleak.backends.winrt"),
    "win32": ("bleak.backends.bluezdbus", "bleak.backends.p4android", "bleak.backends.corebluetooth"),
}.get(sys.platform, ())


def _native_bleak(name):
    return not name.startswith(_FOREIGN_BLEAK_BACKENDS)


hiddenimports = [
    *collect_submodules("gait"),
    *collect_submodules("bleak", filter=_native_bleak),
    *collect_submodules("wt901"),
]

if sys.platform == "darwin":
    hiddenimports += [
        *collect_submodules("CoreBluetooth"),
        *collect_submodules("Foundation"),
        *collect_submodules("libdispatch"),
        "objc",
    ]
elif sys.platform == "win32":
    # uv.lock 里 bleak 在 win32 上拉的是 winrt-runtime 与 winrt-windows-* 七个包，
    # 它们共用 `winrt` 命名空间，收这一个根就全部带上。
    hiddenimports += collect_submodules("winrt")

# gait 在运行时经 `Path(__file__)` 读的包内数据。目前只有这一个（`grep -rn __file__ src/gait`）。
# 新增运行时数据文件时要加到这里，否则开发态照常、打包态 FileNotFoundError。
datas = [
    (str(SRC / "gait" / "app" / "contract.json"), "gait/app"),
]

a = Analysis(  # noqa: F821
    [str(Path(SPECPATH) / "sidecar_entry.py")],  # noqa: F821
    pathex=[str(SRC)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # 开发工具不进安装包。
    excludes=["pytest", "_pytest", "ruff", "tkinter"],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="gait-sidecar",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    # stdio 就是协议通道（JSON Lines）；Windows 上 console=False 会让 stdin/stdout 不可用。
    # 主进程以 windowsHide 拉起，用户看不到控制台窗口。
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(  # noqa: F821
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="gait-sidecar",
)
