"""`gait.device.ble`：配置下发时序、回读校验（含超时容忍）、低速电量读取。

假设备按手册语义应答：``FF AA 27 <reg> 00`` 回一帧 ``0x55 0x71``，携带起始地址起
4 个寄存器；其余 ``FF AA`` 写直接落进寄存器表。它不模拟延时 —— 时序（解锁→写→
保存、间隔）由 wt901 的写事务保证，这里断言的是**顺序、内容与超时容忍**。
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
    read_battery_at_low_rate,
)

_UNLOCK = 0x69
_SAVE = 0x00
_READ = 0x27
_POWER = int(Register.POWER)


class FakeDeviceTransport(Transport):
    """应答寄存器读、记录全部下行指令的假设备。"""

    def __init__(self) -> None:
        super().__init__()
        self._connected = False
        # 出厂默认：10 Hz、带宽 20 Hz、9 轴、满电。
        self.registers: dict[int, int] = {
            0x03: 0x06,
            0x1F: 0x04,
            0x24: 0,
            _POWER: 400,
        }
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
        await configure_streaming(device, StreamConfig())
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
        applied = await configure_streaming(device, StreamConfig())
        assert not applied.verified
        assert any("algorithm" in m for m in applied.mismatches)
        # 其余项不受连坐：带宽照常校验通过。
        assert applied.bandwidth_readback == BANDWIDTH_42HZ
        await device.close()

    asyncio.run(scenario())


def test_rate_readback_timeout_is_tolerated_and_not_a_mismatch() -> None:
    """200 Hz 下读速率超时是预期行为：记 None，不算失败。"""
    transport = FakeDeviceTransport()
    transport.mute_reads.add(int(Register.RRATE))

    async def scenario() -> None:
        device = await _connect(transport)
        applied = await configure_streaming(device, StreamConfig())
        assert applied.rate_readback is None
        assert applied.verified
        await device.close()

    asyncio.run(scenario())


def test_bandwidth_readback_timeout_is_reported_as_mismatch_not_raised() -> None:
    """带宽/算法回读超时最常见于设备仍在高速流；必须体现为 mismatch，不能抛异常。"""
    transport = FakeDeviceTransport()
    transport.mute_reads.add(int(Register.BANDWIDTH))

    async def scenario() -> None:
        device = await _connect(transport)
        applied = await configure_streaming(device, StreamConfig())
        assert applied.bandwidth_readback is None
        assert not applied.verified
        assert any("bandwidth" in m and "超时" in m for m in applied.mismatches)
        # 算法项不受连坐。
        assert applied.algorithm_readback == ALGORITHM_SIX_AXIS
        await device.close()

    asyncio.run(scenario())


def test_algorithm_readback_timeout_is_reported_as_mismatch_not_raised() -> None:
    transport = FakeDeviceTransport()
    transport.mute_reads.add(ALGORITHM_REGISTER)

    async def scenario() -> None:
        device = await _connect(transport)
        applied = await configure_streaming(device, StreamConfig())
        assert applied.algorithm_readback is None
        assert not applied.verified
        assert any("algorithm" in m and "超时" in m for m in applied.mismatches)
        await device.close()

    asyncio.run(scenario())


def test_rate_uses_validated_encoding_not_a_raw_register_write() -> None:
    """速率经 wt901 的 ReturnRate 校验，不接受未核实的编码——写错速率的表现是

    「连接正常但数据不对」，必须在写之前拦住。
    """
    transport = FakeDeviceTransport()

    async def scenario() -> None:
        device = await _connect(transport)
        try:
            await configure_streaming(
                device, StreamConfig(rate=0x0A)
            )  # 通用表里的编码，未在真机核实（wt901 有意排除）。
        except Exception as exc:  # noqa: BLE001 - 只关心确实拒绝了
            assert "未在真机上核实" in str(exc)
        else:
            raise AssertionError("未核实的速率编码应被拒绝")
        await device.close()

    asyncio.run(scenario())


def test_read_battery_at_low_rate_drops_speed_temporarily_and_reads() -> None:
    transport = FakeDeviceTransport()
    transport.registers[_POWER] = 400  # 满电。

    async def scenario() -> None:
        device = await _connect(transport)
        battery = await read_battery_at_low_rate(device)
        assert battery is not None
        assert battery.percent == 100
        await device.close()

    asyncio.run(scenario())

    # 速率被临时降到 10 Hz，但不落 flash（无 save 指令跟在它后面）。
    rate_writes = [i for i, (r, _) in enumerate(transport.commands) if r == 0x03]
    assert rate_writes, "应下发过速率写入"
    assert transport.commands[rate_writes[-1] + 1][0] != _SAVE


def test_read_battery_at_low_rate_returns_none_on_timeout_not_raise() -> None:
    transport = FakeDeviceTransport()
    transport.mute_reads.add(_POWER)

    async def scenario() -> None:
        device = await _connect(transport)
        battery = await read_battery_at_low_rate(device)
        assert battery is None
        await device.close()

    asyncio.run(scenario())
