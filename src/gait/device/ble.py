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

from dataclasses import asdict, dataclass

from wt901 import (
    Battery,
    Register,
    ReturnRate,
    TransportTimeoutError,
    WT901Device,
    WT901Error,
)

__all__ = [
    "ALGORITHM_NINE_AXIS",
    "ALGORITHM_REGISTER",
    "ALGORITHM_SIX_AXIS",
    "BANDWIDTH_42HZ",
    "AppliedConfig",
    "StreamConfig",
    "configure_streaming",
    "read_battery_at_low_rate",
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


#: `configure_streaming` 的默认参数。冻结 dataclass 的单例在多次调用间共享安全，
#: 且避免 ruff B008（默认值里禁止函数调用）。
_DEFAULT_STREAM_CONFIG = StreamConfig()


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
            "requested": asdict(self.requested),
            "bandwidth_readback": self.bandwidth_readback,
            "algorithm_readback": self.algorithm_readback,
            "rate_readback": self.rate_readback,
            "mismatches": list(self.mismatches),
            "verified": self.verified,
        }


async def _read_back(
    device: WT901Device, register: int, label: str, mismatches: list[str]
) -> int | None:
    """回读一个寄存器。超时记为一条 mismatch 而不是抛异常。

    回读超时最常见的原因是设备仍在高速流（上一轮把 200 Hz 固化在 flash 里，
    降速写没生效）—— 这正是「校验失败」，调用方需要它出现在 `mismatches` 里
    走阻断路径，而不是拿到一个裸 traceback、连 AppliedConfig 都没有。
    """
    try:
        return await device.registers.read_value(register)
    except TransportTimeoutError:
        mismatches.append(f"{label}: 回读超时（设备可能仍在高速流）")
        return None


async def configure_streaming(
    device: WT901Device, config: StreamConfig = _DEFAULT_STREAM_CONFIG
) -> AppliedConfig:
    """按 PRD §6.1 的固定时序下发流式配置并回读校验。

    每一项都走 wt901 的原子写事务（解锁→写→保存，间隔 100 ms ≥ PRD 的 50 ms
    下限）。回读对不上或超时都记进 `AppliedConfig.mismatches`，**不抛异常**：
    调用方（自检流程/压测工具）需要完整的回读结果来决定阻断还是记录。
    速率不同 —— 它经 wt901 的 `set_output_rate` 校验编码（写错速率的表现是
    「连接正常但数据不对」，必须在写之前拦住），且 200 Hz 下回读超时是手册
    §6 记载的预期行为，不算 mismatch。
    """
    registers = device.registers
    mismatches: list[str] = []

    await registers.write(Register.BANDWIDTH, config.bandwidth)
    await registers.write(ALGORITHM_REGISTER, config.algorithm)

    bandwidth_readback = await _read_back(
        device, Register.BANDWIDTH, "bandwidth", mismatches
    )
    if bandwidth_readback is not None and bandwidth_readback != config.bandwidth:
        mismatches.append(
            f"bandwidth: 写 0x{config.bandwidth:02X}，读回 0x{bandwidth_readback:02X}"
        )
    algorithm_readback = await _read_back(
        device, ALGORITHM_REGISTER, "algorithm", mismatches
    )
    if algorithm_readback is not None and algorithm_readback != config.algorithm:
        mismatches.append(
            f"algorithm: 写 {config.algorithm}，读回 {algorithm_readback}"
        )

    await registers.set_output_rate(config.rate)
    rate_readback: int | None = None
    try:
        rate_readback = await registers.read_output_rate()
    except TransportTimeoutError:
        # 200 Hz 下读指令来不及回复（手册 §6），是预期行为而不是故障。
        pass
    else:
        if rate_readback != config.rate:
            mismatches.append(
                f"rate: 写 0x{config.rate:02X}，读回 0x{rate_readback:02X}"
            )

    return AppliedConfig(
        requested=config,
        bandwidth_readback=bandwidth_readback,
        algorithm_readback=algorithm_readback,
        rate_readback=rate_readback,
        mismatches=tuple(mismatches),
    )


async def read_battery_at_low_rate(device: WT901Device) -> Battery | None:
    """把速率临时降到 10 Hz 后读电量。读不到返回 ``None``，不中止调用方。

    手册 §6：200 Hz 下寄存器读指令来不及回复，而设备把配置固化在 flash ——
    上一轮留下的 200 Hz 会让本轮一连上就是高速流。所以先把速率**临时**降下来
    （``persist=False`` 不写 flash，``remember=False`` 不进重连重放），读完
    电量再由调用方下发正式配置。PRD §6.1 的自检也要求电量在高速流开启前读，
    这个时序将来归采集自检复用。
    """
    try:
        await device.registers.write(
            Register.RRATE, int(ReturnRate.HZ_10), persist=False, remember=False
        )
        return await device.telemetry.read_battery()
    except (TransportTimeoutError, WT901Error):
        return None
