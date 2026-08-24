"""流式配置下发与回读校验。契约 §1 的 `device/ble.py`（F1.1–1.3 → RAY-196/197）。

目前只有 RAY-200 压测所需的最小实现：PRD §6.1 的固定时序配置下发
（解锁→速率→带宽→6 轴→保存，间隔 ≥50 ms 并校验）。扫描与连接直接用
wt901（`wt901.scan` / `WT901Device.connect`），不在此重复；MAC 绑定左右、
重连策略等完整交付仍归 RAY-196/197。

## 寄存器 0x24（算法选择）为什么用裸地址

PRD 要求配置序列含「6 轴」，wt901 没有具名入口（RAY-242 记录了这个缺口），
但 `RegisterAccess.write` 接受任意寄存器地址，解锁→写→保存的时序与间隔由它
保证。0x24 的取值来自《WT9011DCL-BT50 设备手册摘要》§4.2：0 = 9 轴，1 = 6 轴。
写入后回读校验，不盲信。

## 带宽 0x03（42 Hz）为什么绕过 Bandwidth 枚举

wt901 的枚举只登记真机核实过的档位（20/256 Hz），`set_bandwidth` 会拒绝 0x03。
42 Hz 是 PRD 指定的采集带宽，手册档位表给出编码 0x03，但尚未在真机核实 ——
写入后立即回读，压测报告记录回读值；真机首轮验证通过后，应把该档位补进
wt901 的枚举（属 wt901 仓库的后续工作，不在本 scope）。

## 下发顺序为什么把速率放在最后

PRD 的列举是「解锁→200 Hz→带宽→6 轴→保存」。手册 §6 明确 200 Hz 下寄存器
读指令来不及回复 —— 先把速率提上去，带宽与算法就没法回读校验了。本实现把
速率放在最后：语义不变（三项都写、都固化保存），只是让校验发生在还校验得动
的时候。速率本身是否生效由实测到达率回答，那正是压测的测量对象；这里仍**尝试**
回读速率，读到就校验，超时则记 ``None``，不视为失败。
"""

from __future__ import annotations

from dataclasses import dataclass

from wt901 import Register, ReturnRate, TransportTimeoutError, WT901Device

__all__ = [
    "ALGORITHM_NINE_AXIS",
    "ALGORITHM_REGISTER",
    "ALGORITHM_SIX_AXIS",
    "BANDWIDTH_42HZ",
    "AppliedConfig",
    "StreamConfig",
    "configure_streaming",
]

#: 算法选择寄存器。wt901 的 `Register` 枚举没有它（RAY-242），地址来自手册 §4.3。
ALGORITHM_REGISTER = 0x24
ALGORITHM_NINE_AXIS = 0
ALGORITHM_SIX_AXIS = 1

#: 手册 §4.2 带宽档位表的 42 Hz 编码。尚未在真机核实（见模块 docstring）。
BANDWIDTH_42HZ = 0x03


@dataclass(frozen=True, slots=True)
class StreamConfig:
    """一次采集会话的流式配置。默认值即 PRD §6.1 的采集配置。"""

    rate: int = int(ReturnRate.HZ_200)
    bandwidth: int = BANDWIDTH_42HZ
    algorithm: int = ALGORITHM_SIX_AXIS


@dataclass(frozen=True, slots=True)
class AppliedConfig:
    """配置下发的结果：写了什么、读回什么、哪些对不上。

    进压测报告与将来的 `SessionMeta.config_snapshot`（PRD §6.1 可观测性）。
    """

    requested: StreamConfig
    bandwidth_readback: int | None
    algorithm_readback: int | None
    #: 200 Hz 下预期读不到，为 ``None``；它不算 mismatch。
    rate_readback: int | None
    mismatches: tuple[str, ...]

    @property
    def verified(self) -> bool:
        """回读到的每一项都与写入值一致。"""
        return not self.mismatches

    def snapshot(self) -> dict[str, object]:
        return {
            "requested": {
                "rate": self.requested.rate,
                "bandwidth": self.requested.bandwidth,
                "algorithm": self.requested.algorithm,
            },
            "bandwidth_readback": self.bandwidth_readback,
            "algorithm_readback": self.algorithm_readback,
            "rate_readback": self.rate_readback,
            "mismatches": list(self.mismatches),
            "verified": self.verified,
        }


async def configure_streaming(
    device: WT901Device, config: StreamConfig | None = None
) -> AppliedConfig:
    """按 PRD §6.1 的固定时序下发流式配置并回读校验。

    每一项都走 wt901 的原子写事务（解锁→写→保存，间隔 100 ms ≥ PRD 的 50 ms
    下限）。回读对不上时**不抛异常**：调用方（自检流程/压测工具）需要完整的
    `AppliedConfig` 来决定阻断还是记录，抛异常只会把回读值丢掉。
    """
    cfg = config or StreamConfig()
    registers = device.registers
    mismatches: list[str] = []

    await registers.write(Register.BANDWIDTH, cfg.bandwidth)
    await registers.write(ALGORITHM_REGISTER, cfg.algorithm)

    bandwidth_readback = await registers.read_value(Register.BANDWIDTH)
    if bandwidth_readback != cfg.bandwidth:
        mismatches.append(
            f"bandwidth: 写 0x{cfg.bandwidth:02X}，读回 0x{bandwidth_readback:02X}"
        )
    algorithm_readback = await registers.read_value(ALGORITHM_REGISTER)
    if algorithm_readback != cfg.algorithm:
        mismatches.append(
            f"algorithm: 写 {cfg.algorithm}，读回 {algorithm_readback}"
        )

    await registers.write(Register.RRATE, cfg.rate)
    rate_readback: int | None = None
    try:
        rate_readback = await registers.read_value(Register.RRATE)
    except TransportTimeoutError:
        # 200 Hz 下读指令来不及回复（手册 §6），是预期行为而不是故障。
        pass
    else:
        if rate_readback != cfg.rate:
            mismatches.append(
                f"rate: 写 0x{cfg.rate:02X}，读回 0x{rate_readback:02X}"
            )

    return AppliedConfig(
        requested=cfg,
        bandwidth_readback=bandwidth_readback,
        algorithm_readback=algorithm_readback,
        rate_readback=rate_readback,
        mismatches=tuple(mismatches),
    )
