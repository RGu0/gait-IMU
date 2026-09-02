"""流式配置下发与回读校验。契约 §1 的 `device/ble.py`（F1.1–1.3 → RAY-196/197）。

目前只有 RAY-200 压测所需的最小实现：PRD §6.1 的固定时序配置下发
（解锁→速率→带宽→6 轴→保存，间隔 ≥50 ms 并校验）。扫描与连接直接用
wt901（`wt901.scan` / `WT901Device.connect`），不在此重复；MAC 绑定左右、
重连策略等完整交付仍归 RAY-196/197。

## 算法选择（6 轴）走 wt901 的具名 API

PRD 要求配置序列含「6 轴」。这一项曾经只能走 `RegisterAccess.write(0x24, 1)`，
把「1 表示 6 轴相对航向」这条设备知识留在本仓库 —— 而它反直觉（值大的 1 反而
是轴少的那个），写反了不报错，只会让航向退化成依赖磁力计的绝对航向，在机构
室内金属环境下悄悄劣化。

wt901 现已提供 `AlgorithmMode` 与 `RegisterAccess.set_algorithm`（RAY-241，
本仓库钉住的 rev 已含），所以这里改用具名 API：取值不在手册登记的两档内会被
`UnsupportedRegisterError` **当场拒绝**，而不是写进设备后靠回读才发现。写入后
仍然回读校验，不盲信。

## 安装方向（0x23）：写一个可能本来就对的值，理由与 0x96 同源

适配文档 §3.1 的 ⑥ 是 `FF AA 23 00 00`（水平）。它多半是幂等的 —— 但**上游明说
没核实过 0 是不是出厂默认**：手册没写，也没人读过一台出厂状态设备的 `0x23`。

正因为不确定，这一步更不能省。它的价值不在于改变什么，而在于让「一份配置快照
完全决定设备状态」成立；省掉它就等于依赖设备残留的配置。

失败同样是安静的：装反了只是让姿态解算的重力轴对不上，数据一直偏，而链路、
速率、丢包这些可观测量**全部正常**。所以写完照样回读。

## 输出内容（0x96）为什么写一个「默认值」还要回读

适配文档 §3.1 的 ⑤ 是 `FF AA 96 00 00`，注释写着「默认值，**显式设置以确保
状态确定**」。写默认值看起来是冗余的，删掉它的诱惑很大 —— 但这一项写错的后果
**不报错**：

wt901 的 `OutputMode` 文档写明，寄存器 `0x96` 置 1 后位移帧**复用同一个标志位
且字节布局无法区分**，「解析方必须自带这个上下文」。所以模块若残留 `0x96 = 1`，
`0x61` 帧会被当成运动数据解析，得到一份看着正常、数值全错的数据。

而模块的配置固化在 flash、又会被别处用过，所以「反正是默认值」这个假设不成立。
写它是为了让状态确定；**回读**它是因为写了没生效同样没有迹象；把回读值放进
`config_snapshot` 是为了让「本次会话确实处于运动数据模式」成为可追溯事实 ——
否则下一个人拿到历史数据，无从判断当时 `0x96` 是几。

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

from dataclasses import asdict, dataclass, replace

from wt901 import (
    AlgorithmMode,
    Battery,
    Mounting,
    Register,
    ReturnRate,
    TransportTimeoutError,
    WT901Device,
    WT901Error,
)

__all__ = [
    "BANDWIDTH_42HZ",
    "MOTION_OUTPUT",
    "AppliedConfig",
    "StreamConfig",
    "configure_streaming",
    "read_battery_at_low_rate",
    "start_streaming",
]

#: 手册 §4.2 带宽档位表的 42 Hz 编码。尚未在真机核实（见模块 docstring）。
BANDWIDTH_42HZ = 0x03

#: `Register.DISPLACEMENT_OUTPUT`（`0x96`）取 0 = 输出运动数据。
#: 取 1 是位移输出，而位移帧与运动帧**无法从字节上区分** —— 见模块文档。
MOTION_OUTPUT = 0


@dataclass(frozen=True, slots=True)
class StreamConfig:
    """一次采集会话的流式配置。默认值即 PRD §6.1 的采集配置。"""

    rate: int = int(ReturnRate.HZ_200)
    bandwidth: int = BANDWIDTH_42HZ
    algorithm: AlgorithmMode = AlgorithmMode.SIX_AXIS
    mounting: Mounting = Mounting.HORIZONTAL
    """安装方向（`0x23`）。适配文档 §3.1 的 ⑥ 要求的就是水平。

    **它是不是出厂默认，上游明说没核实过** —— 手册没写，也没人读过一台出厂状态
    设备的 `0x23`。正因为不确定，这一步更不能省：见模块文档。
    """
    output_mode: int = MOTION_OUTPUT
    """位移输出开关（`0x96`）。**0 = 运动数据**（加速度/角速度/角度）。

    适配文档 §3.1 的 ⑤ 把它列进固定序列并注明「默认值，**显式设置以确保状态
    确定**」。写默认值看似冗余，但见模块文档 —— 这一项写错的后果是**静默的**。
    """


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
    mounting_readback: int | None
    output_mode_readback: int | None
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
            "mounting_readback": self.mounting_readback,
            "output_mode_readback": self.output_mode_readback,
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


async def _write_rate(
    device: WT901Device, config: StreamConfig, mismatches: list[str]
) -> int | None:
    """写速率寄存器并回读。**这一步就是开流** —— 设备自此满速推流。"""
    await device.registers.set_output_rate(config.rate)
    try:
        rate_readback = await device.registers.read_output_rate()
    except TransportTimeoutError:
        # 200 Hz 下读指令来不及回复（手册 §6），是预期行为而不是故障。
        return None
    if rate_readback != config.rate:
        mismatches.append(f"rate: 写 0x{config.rate:02X}，读回 0x{rate_readback:02X}")
    return rate_readback


async def start_streaming(
    device: WT901Device, config: StreamConfig, applied: AppliedConfig
) -> AppliedConfig:
    """写速率寄存器开流，把结果并回 `configure_streaming(defer_rate=True)` 的产出。

    与 `defer_rate` 配套：双设备先各自配置完毕，再由调用方**同时**调用本函数，
    使两条流的启动间隔降到一次 BLE 写的往返（原为 3~5 s，见 `configure_streaming`）。

    返回**合并后**的 `AppliedConfig` 而不是只返回回读值 —— `config_snapshot`
    （PRD §6.1 可观测性）要求配置下发的结果是完整的一份；拆成两半后若不合并，
    快照里就会少掉 `rate_readback`，而那正是「速率写对没有」的唯一证据。
    """
    mismatches = list(applied.mismatches)
    rate_readback = await _write_rate(device, config, mismatches)
    return replace(applied, rate_readback=rate_readback, mismatches=tuple(mismatches))


async def configure_streaming(
    device: WT901Device,
    config: StreamConfig = _DEFAULT_STREAM_CONFIG,
    *,
    defer_rate: bool = False,
) -> AppliedConfig:
    """按 PRD §6.1 的固定时序下发流式配置并回读校验。

    ## `defer_rate`：把「开流」从「配置」里拆出来

    **写速率寄存器就是开流** —— 设备从那一刻起满速推流。而本函数在写速率**之前**
    有四次写事务（带宽、算法、输出模式、安装方向）加四次回读；每次写事务是
    `2 × write_delay + save_delay = 0.7 s`，四次即 **2.8 s**，再加回读与 BLE 往返。

    双设备**逐台**调用本函数时，第一台写完速率就开始满速推流，而第二台还要走完
    自己那 3~5 秒的配置 —— **第一台独自推流的这几秒，正是第二台开流后过渡期的成因**。
    实测：第二台在开流后第 2~6 秒掉到 160~184 样本/秒（稳态 200），**7/7 复现**，
    且跟随**连接顺序**而非器件（RAY-213 `T-213-02`）。

    `defer_rate=True` 跳过速率写入，由调用方在**两台都配置完毕后**用
    `start_streaming` 一起开流，把两条流的启动间隔从 3~5 s 压到一次 BLE 写的往返。

    默认 `False` —— 单设备场景没有这个问题，不该为它增加调用方的负担。

    ## 写与回读的约定

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
    await registers.set_algorithm(config.algorithm)
    await registers.write(Register.DISPLACEMENT_OUTPUT, config.output_mode)
    await registers.set_mounting(config.mounting)

    bandwidth_readback = await _read_back(
        device, Register.BANDWIDTH, "bandwidth", mismatches
    )
    if bandwidth_readback is not None and bandwidth_readback != config.bandwidth:
        mismatches.append(
            f"bandwidth: 写 0x{config.bandwidth:02X}，读回 0x{bandwidth_readback:02X}"
        )
    algorithm_readback = await _read_back(
        device, Register.ALGORITHM, "algorithm", mismatches
    )
    if algorithm_readback is not None and algorithm_readback != int(config.algorithm):
        mismatches.append(
            f"algorithm: 写 {int(config.algorithm)}（{config.algorithm.name}），"
            f"读回 {algorithm_readback}"
        )
    mounting_readback = await _read_back(
        device, Register.MOUNTING, "mounting", mismatches
    )
    if mounting_readback is not None and mounting_readback != int(config.mounting):
        mismatches.append(
            f"mounting: 写 {int(config.mounting)}（{config.mounting.name}），"
            f"读回 {mounting_readback}。装反只会让姿态解算的重力轴对不上 —— "
            "链路、速率、丢包全部正常，数据一直偏。"
        )
    output_mode_readback = await _read_back(
        device, Register.DISPLACEMENT_OUTPUT, "output_mode", mismatches
    )
    if output_mode_readback is not None and output_mode_readback != config.output_mode:
        mismatches.append(
            f"output_mode: 写 {config.output_mode}，读回 {output_mode_readback}。"
            "非 0 表示设备在位移输出模式，而位移帧与运动帧字节布局无法区分 —— "
            "继续采集会得到一份被静默解析错的数据。"
        )

    rate_readback = None if defer_rate else await _write_rate(device, config, mismatches)

    return AppliedConfig(
        requested=config,
        bandwidth_readback=bandwidth_readback,
        algorithm_readback=algorithm_readback,
        mounting_readback=mounting_readback,
        output_mode_readback=output_mode_readback,
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
