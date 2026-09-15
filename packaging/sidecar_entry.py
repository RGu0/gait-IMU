"""冻结 sidecar 的入口（RAY-250 preview-installers）。

PyInstaller 需要一个脚本文件作起点，而 `python -m gait.app` 是模块入口 —— 这里只做
一件事：调同一个 `gait.app.__main__.main()`。**不在这里加任何行为**，否则打包态与
开发态就成了两份 sidecar。

## `GAIT_SELFTEST=imports`

冻结最常见的坏法不是崩溃，而是**缺模块**：PyInstaller 的静态分析看不见延迟 import
（bleak 按平台选后端、`gait.app.blesource` 在真正连设备时才 import），产物照样能
回应 `describe`，直到机构现场第一次点「连接设备」才 `ModuleNotFoundError`。

所以这里把「会被延迟加载的那几个模块」在冻结产物里当场 import 一遍。CI 两个平台都跑
它，缺一个就红。`gait.app.replay` / `gait.app.blesource` 由并行 scope 引入，本分支上
可能还不存在 —— 只有这两个容忍缺失，并打印出来，别的缺了一律失败。
"""

from __future__ import annotations

import importlib
import sys

#: 由并行 scope（RAY-493 sidecar-preview-runtime / ble-device-source）引入的模块。
#: 它们尚未合入时，缺失是预期的；合入后 spec 的 collect_submodules("gait") 会自动带上。
OPTIONAL_MODULES = ("gait.app.replay", "gait.app.blesource")


def _required_modules(platform: str) -> list[str]:
    modules = ["bleak", "numpy", "wt901", "gait.validate.synthetic", "gait.report"]
    if platform == "darwin":
        modules.append("bleak.backends.corebluetooth.client")
    elif platform == "win32":
        modules.append("bleak.backends.winrt.client")
    return modules


def selftest_imports() -> int:
    for name in _required_modules(sys.platform):
        importlib.import_module(name)
        print(f"import ok: {name}")
    missing: list[str] = []
    for name in OPTIONAL_MODULES:
        try:
            importlib.import_module(name)
        except ModuleNotFoundError as exc:
            # 只容忍「这个模块本身不存在」；它存在但它的依赖缺了，照样失败。
            if exc.name != name:
                raise
            missing.append(name)
        else:
            print(f"import ok: {name}")
    if missing:
        print(f"optional modules not present: {', '.join(missing)}")
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
