"""主机时钟够不够细，能不能拿来给 200 Hz 的到达时刻计时 —— 全仓唯一的判据（RAY-545）。

每个样本的主机到达时刻 `t_host` 都取 `time.monotonic()`：上游 wt901 在通知回调里取
（`wt901/device.py`），本仓库的录制层与设备源也取它。到达率、链路分档、丢包 / 空洞全建在
这个时刻上。Windows + Python 3.12 的 `monotonic()` 走 `GetTickCount64`，粒度 **15.6 ms**，
而 200 Hz 的采样周期是 5 ms —— 到达时刻被量化成台阶，量出来的「丢包」是时钟的假象
（RAY-200 曾把一段干净录制读成 18% 缺失）。3.13 起它改走 `QueryPerformanceCounter`。

## 判据

`分辨率 × CLOCK_RESOLUTION_RATIO ≤ 采样周期`。取 10：量化误差不超过半个周期的 1/5，
不足以在残差上造出 3 样本（PRD 的空洞阈值）的台阶。15.6 ms 在 200 Hz 下差了 31 倍。

## 为什么有两种量法

- **申报值** `declared_resolution()`：`time.get_clock_info("monotonic").resolution`，
  解释器自己报的。Windows + 3.12 报的正是 0.015625，可信，而且不花时间。
- **实测值** `measured_resolution()`：连续读时钟，取相邻两次不同读数之差的最小值。
  它不信申报，看的是时钟真实跳变的步长。

产品路径（`effective_resolution`）取两者中**较粗**的那个：宁可把一台够细的机器误判为不够，
也不让一台不够细的机器静默出数 —— 前者操作员会看到原因并能改用演示模式，后者谁也不会知道。

此前 `gait.cli.linktest`（申报值）与 `gait.cli.v3prime`（实测值）各自写着同一个 10，
产品路径再抄一份就是三处；三处迟早对不上，所以收在这里。
"""

from __future__ import annotations

import time
from typing import Final

__all__ = [
    "CLOCK_RESOLUTION_RATIO",
    "declared_resolution",
    "effective_resolution",
    "is_adequate",
    "limit_for",
    "measured_resolution",
]

#: 主机单调时钟分辨率必须细于采样周期的这个比例，测量才有意义（见模块文档）。
CLOCK_RESOLUTION_RATIO: Final[int] = 10


def declared_resolution() -> float:
    """解释器申报的 `time.monotonic()` 分辨率，秒。"""
    return time.get_clock_info("monotonic").resolution


def measured_resolution(samples: int = 200) -> float:
    """实测 `time.monotonic()` 的分辨率，秒：连续不同读数之间的最小差。

    一次也没读到跳变时返回 0.0（时钟细到连读 `samples` 次都在同一刻 —— 那只能更细）。
    """
    deltas: list[float] = []
    previous = time.monotonic()
    for _ in range(samples):
        current = time.monotonic()
        if current != previous:
            deltas.append(current - previous)
            previous = current
    return min(deltas) if deltas else 0.0


def effective_resolution() -> float:
    """产品路径用的分辨率：申报值与实测值中较粗的那个（理由见模块文档）。"""
    return max(declared_resolution(), measured_resolution())


def limit_for(nominal_fs: float) -> float:
    """在 `nominal_fs` 采样率下，时钟分辨率允许的上限，秒。"""
    return 1.0 / nominal_fs / CLOCK_RESOLUTION_RATIO


def is_adequate(resolution: float | None, nominal_fs: float) -> bool:
    """这个分辨率够不够给 `nominal_fs` 的到达时刻计时。

    ``None`` 表示**没有检出量化痕迹**（`linktest` 从录制里反推分辨率时的结果）——
    那本身就是时钟够细的证据，按合格处理。
    """
    return resolution is None or resolution * CLOCK_RESOLUTION_RATIO <= 1.0 / nominal_fs
