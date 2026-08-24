"""`gait.device.ble.configure_streaming`：时序、校验、200 Hz 下读不到速率的容忍。

假设备按手册语义应答：``FF AA 27 <reg> 00`` 回一帧 ``0x55 0x71``，携带起始地址起
4 个寄存器；其余 ``FF AA`` 写直接落进寄存器表。它不模拟延时 —— 时序（解锁→写→
保存、间隔）由 wt901 的写事务保证，这里断言的是**顺序与内容**。
"""

from __future__ import annotations

import asyncio
import struct

from wt901 import Transport, WT901Device
from wt901.protocol.registers import Register

from gait.device.ble import (
    ALGORITHM_REGISTER,
    ALGORITHM_SIX_AXIS,
    BANDWIDTH_42HZ,
    StreamConfig,
    configure_streaming,
)

_UNLOCK = 0x69
_SAVE = 0x00
_READ = 0x27


class FakeDeviceTransport(Transport):
    """应答寄存器读、记录全部下行指令的假设备。"""

    def __init__(self) -> None:
        super().__init__()
        self._connected = False
        # 出厂默认：10 Hz、带宽 20 Hz、9 轴。
        self.registers: dict[int, int] = {0x03: 0x06, 0x1F: 0x04, 0x24: 0}
        self.commands: list[tuple[int, int]] = []
        #: 写入被固件静默忽略的寄存器（模拟不支持的配置项）。
        self.reject_writes: set[int] = set()
        #: 读不回来的寄存器（模拟 200 Hz 下读指令来不及回复）。
        self.mute_reads: set[int] = set()

    @property
    def device_id(self) -> str:
        return "fake"

    @property
    def is_connected(self) -> bool:
        return self._connected

    async def connect(self) -> None:
        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False

    async def write(self, data: bytes) -> None:
        assert data[:2] == b"\xff\xaa", f"非法指令帧：{data.hex()}"
        register, value = data[2], data[3] | (data[4] << 8)
        self.commands.append((register, value))
        if register == _READ:
            target = data[3]
            if target in self.mute_reads:
                return
            values = [self.registers.get(target + i, 0) for i in range(4)]
            payload = struct.pack("<H4h", target, *values)
            self._emit_data(b"\x55\x71" + payload + bytes(18 - len(payload)))
        elif register in (_UNLOCK, _SAVE):
            pass
        elif register not in self.reject_writes:
            self.registers[register] = value


async def _connect(transport: FakeDeviceTransport) -> WT901Device:
    device = WT901Device(transport)
    device.registers.write_delay = 0.0
    device.registers.save_delay = 0.0
    device.registers.read_timeout = 0.1
    device.registers.read_retries = 0
    await device.open()
    return device


def _config_writes(transport: FakeDeviceTransport) -> list[tuple[int, int]]:
    return [
        (register, value)
        for register, value in transport.commands
        if register not in (_UNLOCK, _SAVE, _READ)
    ]


def test_full_sequence_applies_and_verifies() -> None:
    transport = FakeDeviceTransport()

    async def scenario() -> None:
        device = await _connect(transport)
        applied = await configure_streaming(device, StreamConfig())
        assert applied.verified, applied.mismatches
        assert applied.bandwidth_readback == BANDWIDTH_42HZ
        assert applied.algorithm_readback == ALGORITHM_SIX_AXIS
        assert applied.rate_readback == 0x0B
        await device.close()

    asyncio.run(scenario())

    # PRD 的三项都写到了设备上。
    assert transport.registers[int(Register.BANDWIDTH)] == BANDWIDTH_42HZ
    assert transport.registers[ALGORITHM_REGISTER] == ALGORITHM_SIX_AXIS
    assert transport.registers[int(Register.RRATE)] == 0x0B
    # 速率最后写（否则 200 Hz 下带宽/算法没法回读校验）。
    assert _config_writes(transport) == [
        (int(Register.BANDWIDTH), BANDWIDTH_42HZ),
        (ALGORITHM_REGISTER, ALGORITHM_SIX_AXIS),
        (int(Register.RRATE), 0x0B),
    ]


def test_every_config_write_is_unlocked_then_saved() -> None:
    transport = FakeDeviceTransport()

    async def scenario() -> None:
        device = await _connect(transport)
        await configure_streaming(device)
        await device.close()

    asyncio.run(scenario())

    commands = transport.commands
    for index, (register, _) in enumerate(commands):
        if register in (_UNLOCK, _SAVE, _READ):
            continue
        assert commands[index - 1][0] == _UNLOCK, f"0x{register:02X} 写前未解锁"
        assert commands[index + 1][0] == _SAVE, f"0x{register:02X} 写后未保存"


def test_silently_rejected_write_is_reported_not_trusted() -> None:
    """固件不认 0x24 时（RAY-242 的最坏情形），回读校验必须把它暴露出来。"""
    transport = FakeDeviceTransport()
    transport.reject_writes.add(ALGORITHM_REGISTER)

    async def scenario() -> None:
        device = await _connect(transport)
        applied = await configure_streaming(device)
        assert not applied.verified
        assert any("algorithm" in m for m in applied.mismatches)
        # 其余项不受连坐：带宽照常校验通过。
        assert applied.bandwidth_readback == BANDWIDTH_42HZ
        await device.close()

    asyncio.run(scenario())


def test_rate_readback_timeout_is_tolerated() -> None:
    """200 Hz 下读速率超时是预期行为：记 None，不算失败。"""
    transport = FakeDeviceTransport()
    transport.mute_reads.add(int(Register.RRATE))

    async def scenario() -> None:
        device = await _connect(transport)
        applied = await configure_streaming(device)
        assert applied.rate_readback is None
        assert applied.verified
        await device.close()

    asyncio.run(scenario())
