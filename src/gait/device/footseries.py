"""原始帧 → `FootSeries` 的数据桥（RAY-345 最小 MVP 的 `--replay` 路径）。

`FootSeries` 是「标定与同步之后、算法之前」的契约（§3.2），它上游封装了协议解析、
标定、同步三件事。真正完整的实现属 RAY-208（标定）+ RAY-209/210（时基/空洞）。
本模块是**最小桥**：只把已经能拿到的东西——wt901 的 SI 换算、F2.3 的固定坐标系
重排、标称采样率——接成一份能喂给 `cloud.run_basic_chain` 的 `FootSeries`。

## 这里明写哪些「精度」被暂时牺牲

1. **不做标定补偿**（F2.1 六面法 / F2.2 静止零偏）。原始码值只过 wt901 的固定
   量程换算，没有零偏/标度校正。`FootSeries` 契约说「已标定补偿」，这里违背它，
   是 MVP 有意为之的简化——详见 RAY-345 的降范围说明。
2. **时轴用标称采样率**。回放路径的 `t_host` 是「回放那一刻」的时钟，不是原始
   采集时刻（`capture.py` 明写），拿它回归会得到被压缩的假采样率。真正的时基
   （`sync/timebase.build_timebase` + 原始到达时刻）留到接真机采集时再用；MVP 用
   `t[k] = k / nominal_fs` 的均匀标称时轴，`fs` 也取标称 200 Hz。
3. **不切空洞、不做插值**。`segments` 就一整段，`quality` 只标 `SATURATED`。
   空洞切分（RAY-210）与插值不在 MVP 范围。

这三条牺牲共同保证一件事：回放数据能走通整条链、产出报告，数字**有限且量级正确**，
但精确值不作保证——这正是「暂不追求精度」的含义。

## 坐标系重排（F2.3）是本模块唯一「有物理对错」的地方

《BS-BT91 硬件适配》§2 与《MVP-v1 功能清单》F2.3 都写明：

* 模块体体系：**X 左 / Y 前 / Z 上**
* 足部解剖系：**X 前 / Y 外侧 / Z 上**

因此固定重排为 `foot = [module_y, ±module_x, module_z]`，其中 Y 的符号由脚决定：
左足外侧 = 左 = `+module_x`，右足外侧 = 右 = `−module_x`。**这个转换必须显式写成
常量并加单元测试**（硬件适配文档原话），因为一旦错，后续所有角度/方向指标的符号
都会静默地错——而那不是报错，是给出一份看着正常的错误报告。
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Final

import numpy as np
from wt901.protocol.units import accel_to_m_s2, angular_velocity_to_rad_s

from gait.contracts import FootLabel, FootSeries, Quality, RawFrame
from gait.device.capture import replay_session_foot

__all__ = [
    "NOMINAL_FS",
    "FootSeriesError",
    "frames_to_foot_series",
    "load_session_foot",
    "reorder_module_to_foot",
]

#: 器件标称采样率，Hz。真实值会由 `sync/timebase` 从到达时刻解出，MVP 直接用它。
NOMINAL_FS: Final[float] = 200.0

#: 足部系各分量取自模块体系的哪个下标（F2.3）：`foot[0]=module[1]`（前=前）、
#: `foot[1]=module[0]`（外=左）、`foot[2]=module[2]`（上=上）。
_AXIS_INDEX: Final[tuple[int, int, int]] = (1, 0, 2)

#: Y（外侧）分量的符号：左足外侧朝左（=+X），右足外侧朝右（=−X）。
_Y_SIGN: Final[dict[str, float]] = {"L": 1.0, "R": -1.0}


class FootSeriesError(ValueError):
    """原始帧不足以构成一条可用的 `FootSeries`。"""


def reorder_module_to_foot(
    acc: np.ndarray, gyr: np.ndarray, label: FootLabel
) -> tuple[np.ndarray, np.ndarray]:
    """把模块体体系（X左/Y前/Z上）的比力与角速度重排到足部系（X前/Y外/Z上）。

    `acc`/`gyr` 都是 `(n,3)` 的 SI 数组，顺序为模块体系的 (x,y,z)。返回同样形状的
    足部系数组。左足 Y 取 +X，右足 Y 取 −X —— 见模块文档。
    """
    if label not in _Y_SIGN:
        raise FootSeriesError(f"label 应为 'L' 或 'R'，收到 {label!r}")
    sign = _Y_SIGN[label]
    index = _AXIS_INDEX

    foot_acc = np.empty_like(acc, dtype=np.float64)
    foot_acc[:, 0] = acc[:, index[0]]
    foot_acc[:, 1] = sign * acc[:, index[1]]
    foot_acc[:, 2] = acc[:, index[2]]

    foot_gyr = np.empty_like(gyr, dtype=np.float64)
    foot_gyr[:, 0] = gyr[:, index[0]]
    foot_gyr[:, 1] = sign * gyr[:, index[1]]
    foot_gyr[:, 2] = gyr[:, index[2]]
    return foot_acc, foot_gyr


def _to_si(acc_raw: np.ndarray, gyr_raw: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """码值 → SI。换算公式的唯一实现是 wt901（`protocol/units`），这里逐元素调用。"""
    vector_accel = np.vectorize(accel_to_m_s2, otypes=[np.float64])
    vector_gyro = np.vectorize(angular_velocity_to_rad_s, otypes=[np.float64])
    return vector_accel(acc_raw), vector_gyro(gyr_raw)


def frames_to_foot_series(
    frames: Sequence[RawFrame],
    label: FootLabel,
    *,
    nominal_fs: float = NOMINAL_FS,
) -> FootSeries:
    """把一串原始帧拼成一条 `FootSeries`。

    纯函数（不碰 I/O），好测试。`frames` 按样本顺序排列；`t_host` 不参与时轴——
    时轴用标称采样率均匀铺开，见模块文档。
    """
    if label not in ("L", "R"):
        raise FootSeriesError(f"label 应为 'L' 或 'R'，收到 {label!r}")
    if not frames:
        raise FootSeriesError("没有原始帧，无法构成 FootSeries")
    if not nominal_fs > 0:
        raise FootSeriesError(f"nominal_fs 必须为正，收到 {nominal_fs}")

    acc_raw = np.stack([frame.acc_raw for frame in frames])
    gyr_raw = np.stack([frame.gyr_raw for frame in frames])
    acc_si, gyr_si = _to_si(acc_raw, gyr_raw)
    acc, gyr = reorder_module_to_foot(acc_si, gyr_si, label)

    n = len(frames)
    quality = np.zeros(n, dtype=np.uint8)
    for index, frame in enumerate(frames):
        if frame.saturated:
            quality[index] = int(Quality.SATURATED)

    return FootSeries(
        label=label,
        t=np.arange(n, dtype=np.float64) / nominal_fs,
        acc=acc,
        gyr=gyr,
        quality=quality,
        segments=[(0, n)] if n else [],
        fs=float(nominal_fs),
    )


async def load_session_foot(
    root: Path,
    session_id: str,
    label: FootLabel,
    *,
    nominal_fs: float = NOMINAL_FS,
) -> FootSeries:
    """按会话目录回放某一只脚，产出 `FootSeries`。

    `--replay` 路径的入口：路径由 `io.session.raw_path` 决定，解析由 `capture.py`
    的 `replay_session_foot` 完成，这里只负责「收集 → 桥接」。
    """
    frames = [frame async for frame in replay_session_foot(root, session_id, label)]
    return frames_to_foot_series(frames, label, nominal_fs=nominal_fs)
