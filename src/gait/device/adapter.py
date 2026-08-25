"""wt901 样本到契约 `RawFrame` 的适配，含加速度饱和判定。

契约 §1 的 `device/` 层（F1.6 → RAY-195，需求修订 R2）。

## 这一层为什么还需要存在

RAY-193 R2 之后，0x55 帧解析、单位换算、FF AA 指令封装都由 wt901 提供，本仓库
不重复实现（`pyproject.toml` 写明了这条边界）。剩下的是两件 wt901 不做、也不
该做的事：

* **饱和判定** —— wt901 如实转交码值，不替上层判断量程有没有用满。那是一个与
  用途绑定的判断：步态分析在意加速度削顶，别的用途未必。把它放进依赖等于替
  所有调用方做决定。
* **契约边界** —— `RawFrame` 是本仓库的契约类型，wt901 不该知道它。适配集中
  在这一个文件里，wt901 换版本时也只有这一个文件要跟着改。

## 饱和的判据是码值触顶，与量程配置无关

《WT9011DCL-BT50 设备手册摘要》§1 记载加速度量程**不可改**：「固件内部自适应
—— 加速度 <2g 时用 2g 量程，超过自动切 16g」，而协议解析公式恒按 16 g 换算
（§3.1：``Data / 32768 * 16``）。所以「用满」在协议这一层的唯一表现就是 int16
码值顶到边界，**不存在「量程改了阈值要跟着改」这回事**。

**这一条没有真机证据。** 器件真实过载时是否确实吐出 ±32767，而不是提前钳位到
一个更小的值，尚未验证。判据写成码值触顶，是因为那是协议层唯一观测得到的
东西，不是因为已经量过。真机首轮（RAY-230）应当专门确认一次：若器件实际钳在
更低的码值上，这里就会**永远判不出饱和**，而那种失败是安静的。

## 只判加速度，不判陀螺

契约 `Quality.SATURATED` 写的是「任一**加速度**轴触及量程」。把陀螺一并计入会
悄悄改变这个标志在下游的含义 —— 同一个 ``saturated=True``，有人读成加速度削顶
（影响步长积分），有人读成角速度削顶（影响姿态），而报告里只有一个标志。陀螺
饱和若要标注，应当是契约里的**另一个位**，不是把这一位的含义撑大。

## 为什么不逐个校验码值是否落在 int16 内

契约模块写明「校验绝不扫描数组内容」—— `RawFrame` 在 200 Hz 双设备下每秒构造
400 次。这里同样不加那道扫描，理由还更强一层：wt901 是从两个字节按 signed
short 解出计数的，**结构上不可能**产出越界值。万一它哪天变了，`np.asarray(...,
dtype=np.int16)` 会当场抛 `OverflowError` 而不是静默回绕（numpy ≥ 2 的行为，
已写成测试钉住）。

**帧长度则必须显式检查。** 三个切片的位置是手册 §3.1 定死的字段顺序；wt901 若
改变帧布局，切片会把陀螺静默地搬进姿态角字段 —— 那是一个不报错、只是安静地给
出错误结果的失败，正是最该拦住的一类。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

import numpy as np
from wt901 import ImuSample

from gait.contracts import ContractError, RawFrame

__all__ = [
    "ACCEL_AXES",
    "COUNTS_PER_FRAME",
    "INT16_MAX",
    "INT16_MIN",
    "accel_saturated",
    "to_raw_frame",
]

#: 0x61 帧携带的 int16 计数个数。手册 §3.1 的字段顺序：
#: ``ax ay az | wx wy wz | roll pitch yaw``。
COUNTS_PER_FRAME: Final[int] = 9

#: 加速度三轴在 `wt901.RawImuCounts.values` 中的位置。**只有这里知道帧布局**，
#: 别处需要时从这里取，不要再写一遍 0:3。
ACCEL_AXES: Final[slice] = slice(0, 3)
_GYRO_AXES: Final[slice] = slice(3, 6)
_ANGLE_AXES: Final[slice] = slice(6, 9)

#: int16 的边界。饱和的判据就是码值顶到这里 —— 见模块文档。
INT16_MIN: Final[int] = -32768
INT16_MAX: Final[int] = 32767


def accel_saturated(counts: Sequence[int]) -> bool:
    """任一加速度轴的码值是否触及 int16 边界。

    入参是**整帧**的 9 个计数而不是切好的三个，好让帧布局的知识只留在本模块。

    两端都算：负向触底与正向触顶是同一件事（量程用满），而只判正向会让一半的
    削顶悄悄漏过去 —— 步态里足跟触地的冲击恰恰是负向的。
    """
    return any(
        value <= INT16_MIN or value >= INT16_MAX for value in counts[ACCEL_AXES]
    )


def to_raw_frame(sample: ImuSample) -> RawFrame:
    """把一个 wt901 样本适配成契约的 `RawFrame`。

    取的是 ``sample.raw``（未换算的 int16 计数）而不是 ``sample.accel`` /
    ``gyro`` / ``euler``：契约 §3.1 的 `RawFrame` 三个字段就是码值，标定补偿要在
    码值上做。用已换算的 SI 值回推码值会引入一次多余的浮点往返。

    `RawFrame.t_host` 是**主机接收时刻**，不是采样时刻 —— 契约里这句是加粗的，
    这里原样传递 ``sample.t_host``，不做任何插值或修正。
    """
    counts = sample.raw.values
    if len(counts) != COUNTS_PER_FRAME:
        raise ContractError(
            f"0x61 帧应有 {COUNTS_PER_FRAME} 个 int16 计数，收到 {len(counts)} 个。"
            "字段切片的位置由手册 §3.1 的顺序固定，长度对不上说明帧布局变了，"
            "此时按原切片继续会把陀螺静默地搬进姿态角字段。"
        )
    return RawFrame(
        t_host=sample.t_host,
        acc_raw=np.asarray(counts[ACCEL_AXES], dtype=np.int16),
        gyr_raw=np.asarray(counts[_GYRO_AXES], dtype=np.int16),
        ang_raw=np.asarray(counts[_ANGLE_AXES], dtype=np.int16),
        saturated=accel_saturated(counts),
    )
