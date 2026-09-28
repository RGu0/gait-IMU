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

## `GAIT_SELFTEST=clock`

打印冻结产物**自带的**解释器版本与 `time.monotonic()` 的实现和分辨率（JSON 一行）。
到达时刻 `t_host` 全靠它（包括上游 wt901），而 Windows + Python 3.12 的它只有 15.6 ms
—— 200 Hz 的 5 ms 周期被量化成台阶，链路 / 丢包全失真（RAY-545）。开发机上的 Python
证明不了安装包里的是哪个，所以由 `packaging/smoke_sidecar.py` 在两个平台对冻结产物本身断言。
"""

from __future__ import annotations

import importlib
import json
import platform
import sys
import time


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


def selftest_clock() -> int:
    info = time.get_clock_info("monotonic")
    print(json.dumps({
        "python": platform.python_version(),
        "monotonic_implementation": info.implementation,
        "monotonic_resolution_s": info.resolution,
    }))
    return 0


def run() -> int:
    import os

    selftest = os.environ.get("GAIT_SELFTEST")
    if selftest == "imports":
        return selftest_imports()
    if selftest == "clock":
        return selftest_clock()
    from gait.app.__main__ import main

    return main()


if __name__ == "__main__":
    raise SystemExit(run())
