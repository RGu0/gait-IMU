"""`gait.device.ble`：配置下发时序、回读校验（含超时容忍）、低速电量读取。

假设备按手册语义应答：``FF AA 27 <reg> 00`` 回一帧 ``0x55 0x71``，携带起始地址起
4 个寄存器；其余 ``FF AA`` 写直接落进寄存器表。它不模拟延时 —— 时序（解锁→写→
保存、间隔）由 wt901 的写事务保证，这里断言的是**顺序、内容与超时容忍**。
"""

from __future__ import annotations

import asyncio
import struct

import pytest
from wt901 import AlgorithmMode, Mounting, Transport, WT901Device
from wt901.errors import UnsupportedRegisterError
from wt901.protocol.registers import Register

from gait.device.ble import (
    BANDWIDTH_42HZ,
    MOTION_OUTPUT,
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
            0x23: 0,
            0x96: 0,
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
        assert applied.algorithm_readback == AlgorithmMode.SIX_AXIS
        assert applied.mounting_readback == Mounting.HORIZONTAL
        assert applied.output_mode_readback == MOTION_OUTPUT
        assert applied.rate_readback == 0x0B
        await device.close()

    asyncio.run(scenario())

    # 适配文档 §3.1 的 ②③④⑤⑥ 都写到了设备上（①⑦ 由 wt901 的原子写事务自带）。
    assert transport.registers[int(Register.BANDWIDTH)] == BANDWIDTH_42HZ
    assert transport.registers[int(Register.ALGORITHM)] == AlgorithmMode.SIX_AXIS
    assert transport.registers[int(Register.MOUNTING)] == Mounting.HORIZONTAL
    assert transport.registers[int(Register.DISPLACEMENT_OUTPUT)] == MOTION_OUTPUT
    assert transport.registers[int(Register.RRATE)] == 0x0B
    # 顺序即适配文档 §3.1 的 ③④⑤⑥，只把速率（②）挪到最后 ——
    # 200 Hz 下寄存器读指令来不及回复，先提速率其余项就没法回读校验了。
    assert _config_writes(transport) == [
        (int(Register.BANDWIDTH), BANDWIDTH_42HZ),
        (int(Register.ALGORITHM), AlgorithmMode.SIX_AXIS),
        (int(Register.DISPLACEMENT_OUTPUT), MOTION_OUTPUT),
        (int(Register.MOUNTING), Mounting.HORIZONTAL),
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
    """固件收下写入却没生效时，回读校验必须把它暴露出来。

    具名 API 挡得住「取值写错」，挡不住「设备静默忽略」—— 后者只有回读看得见。
    """
    transport = FakeDeviceTransport()
    transport.reject_writes.add(int(Register.ALGORITHM))

    async def scenario() -> None:
        device = await _connect(transport)
        applied = await configure_streaming(device, StreamConfig())
        assert not applied.verified
        assert any("algorithm" in m for m in applied.mismatches)
        # 其余项不受连坐：带宽照常校验通过。
        assert applied.bandwidth_readback == BANDWIDTH_42HZ
        await device.close()

    asyncio.run(scenario())


def test_unregistered_algorithm_value_never_reaches_the_device() -> None:
    """手册没登记的取值必须在写出去之前被拒，而不是靠回读发现。

    这是换用具名 API（RAY-242）真正买到的东西。走通用 `write(0x24, v)` 时任何
    整数都会被原样写进设备：设备不报错，只是进入未文档化的状态，而「姿态数据
    不对但连接正常」是最难定位的一类故障。
    """
    transport = FakeDeviceTransport()

    async def scenario() -> None:
        device = await _connect(transport)
        try:
            with pytest.raises(UnsupportedRegisterError):
                await configure_streaming(device, StreamConfig(algorithm=2))
            # 关键断言：设备侧的算法寄存器仍是出厂值，一个字节都没写出去。
            assert transport.registers[int(Register.ALGORITHM)] == 0
            assert not [
                command
                for command in transport.commands
                if command[0] == int(Register.ALGORITHM)
            ]
        finally:
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
        assert applied.algorithm_readback == AlgorithmMode.SIX_AXIS
        await device.close()

    asyncio.run(scenario())


def test_algorithm_readback_timeout_is_reported_as_mismatch_not_raised() -> None:
    transport = FakeDeviceTransport()
    transport.mute_reads.add(int(Register.ALGORITHM))

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


def test_a_device_left_in_displacement_mode_is_caught_not_parsed() -> None:
    """⑤ 真正防的东西：模块残留 0x96 = 1 时必须被拦下。

    位移帧与运动帧**复用同一个标志位且字节布局无法区分**（wt901 `OutputMode`
    文档）。写不进去而没人发现，采到的就是一份看着正常、数值全错的数据 ——
    这是「静默给出错误结果」，不是「报错停下」。所以写完必须回读。
    """
    transport = FakeDeviceTransport()
    # 设备停在位移输出模式，且拒绝这次写入（固件忽略/写失败）。
    transport.registers[int(Register.DISPLACEMENT_OUTPUT)] = 1
    transport.reject_writes.add(int(Register.DISPLACEMENT_OUTPUT))

    async def scenario() -> None:
        device = await _connect(transport)
        applied = await configure_streaming(device, StreamConfig())
        assert not applied.verified
        assert applied.output_mode_readback == 1
        assert any("位移输出模式" in m for m in applied.mismatches)
        # 其余项不受连坐。
        assert applied.bandwidth_readback == BANDWIDTH_42HZ
        await device.close()

    asyncio.run(scenario())


def test_the_output_mode_readback_reaches_the_config_snapshot() -> None:
    """回读值必须进 config_snapshot，否则「这次会话是运动数据模式」只是假设。

    下一个人拿到一份被静默解析错的历史数据时，靠它才能判断当时 0x96 是几。
    """
    transport = FakeDeviceTransport()

    async def scenario() -> dict:
        device = await _connect(transport)
        applied = await configure_streaming(device, StreamConfig())
        await device.close()
        return applied.snapshot()

    snapshot = asyncio.run(scenario())
    assert snapshot["output_mode_readback"] == MOTION_OUTPUT
    assert snapshot["requested"]["output_mode"] == MOTION_OUTPUT


def test_a_device_mounted_vertically_is_caught_not_silently_wrong() -> None:
    """⑥ 防的东西：写不进去时必须被拦下。

    装反的表现是**姿态解算的重力轴对不上，数据一直偏**，而链路、速率、丢包这些
    可观测量全部正常 —— 没有任何一个指标会告诉你出了事。只能靠回读。
    """
    transport = FakeDeviceTransport()
    transport.registers[int(Register.MOUNTING)] = int(Mounting.VERTICAL)
    transport.reject_writes.add(int(Register.MOUNTING))

    async def scenario() -> None:
        device = await _connect(transport)
        applied = await configure_streaming(device, StreamConfig())
        assert not applied.verified
        assert applied.mounting_readback == int(Mounting.VERTICAL)
        assert any("mounting" in m and "重力轴" in m for m in applied.mismatches)
        # 其余项不受连坐。
        assert applied.output_mode_readback == MOTION_OUTPUT
        await device.close()

    asyncio.run(scenario())


def test_the_mounting_readback_reaches_the_config_snapshot() -> None:
    """与 0x96 同一条理由：让「这次会话按水平安装解算」成为可追溯事实。

    上游明说没核实过 0 是不是出厂默认，所以「反正是默认值」这个假设不成立 ——
    快照里必须有它，否则事后无从判断当时 0x23 是几。
    """
    transport = FakeDeviceTransport()

    async def scenario() -> dict:
        device = await _connect(transport)
        applied = await configure_streaming(device, StreamConfig())
        await device.close()
        return applied.snapshot()

    snapshot = asyncio.run(scenario())
    assert snapshot["mounting_readback"] == int(Mounting.HORIZONTAL)
    assert snapshot["requested"]["mounting"] == int(Mounting.HORIZONTAL)


def test_an_unregistered_mounting_value_never_reaches_the_device() -> None:
    """具名 API 的价值：未登记取值在写出去之前被拒。

    与 RAY-242 的算法模式同一条 —— 通用 write 会把任何整数原样写进设备。
    """
    transport = FakeDeviceTransport()

    async def scenario() -> None:
        device = await _connect(transport)
        try:
            with pytest.raises(UnsupportedRegisterError):
                await configure_streaming(device, StreamConfig(mounting=7))
            assert transport.registers[int(Register.MOUNTING)] == 0
            assert not [c for c in transport.commands if c[0] == int(Register.MOUNTING)]
        finally:
            await device.close()

    asyncio.run(scenario())
