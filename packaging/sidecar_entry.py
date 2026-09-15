"""冻结 sidecar 的入口（RAY-250 preview-installers）。

PyInstaller 需要一个脚本文件作起点，而 `python -m gait.app` 是模块入口 —— 这里只做
一件事：调同一个 `gait.app.__main__.main()`。**不在这里加任何行为**，否则打包态与
开发态就成了两份 sidecar。

## `GAIT_SELFTEST=imports`

冻结最常见的坏法不是崩溃，而是**缺模块**：PyInstaller 的静态分析看不见延迟 import
（bleak 按平台选后端、`gait.app.blesource` 在真正连设备时才 import），产物照样能
回应 `describe`，直到机构现场第一次点「连接设备」才 `ModuleNotFoundError`。

所以这里把「会被延迟加载的那几个模块」在冻结产物里当场 import 一遍。CI 两个平台都跑
它，缺一个就红 —— 包括 `gait.app.replay` 与 `gait.app.blesource`（RAY-493 已合入，
不再容忍缺失）。
"""

from __future__ import annotations

import importlib
import sys


def _required_modules(platform: str) -> list[str]:
    modules = [
        "bleak",
        "numpy",
        "wt901",
        "gait.validate.synthetic",
        "gait.report",
        # 设备源按 GAIT_DEVICE_SOURCE 延迟 import（`gait.app.__main__`），静态分析看不见。
        "gait.app.replay",
        "gait.app.blesource",
    ]
    if platform == "darwin":
        modules.append("bleak.backends.corebluetooth.client")
    elif platform == "win32":
        modules.append("bleak.backends.winrt.client")
    return modules


def selftest_imports() -> int:
    for name in _required_modules(sys.platform):
        importlib.import_module(name)
        print(f"import ok: {name}")
    print("selftest ok")
    return 0


def run() -> int:
    import os

    if os.environ.get("GAIT_SELFTEST") == "imports":
        return selftest_imports()
    from gait.app.__main__ import main

    return main()


if __name__ == "__main__":
    raise SystemExit(run())
