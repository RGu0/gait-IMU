"""多姿态标定工装 CLI 的离线测试。

BLE 采集本身跑不进测试（也跑不进非交互进程 —— macOS 的 TCC 会直接中止），但**交互
流程的逻辑**跑得进来：喂一台假设备，让它按脚本产出各姿态读数，验工装收够了姿态、
拒绝了该拒绝的段。

这一段值得测，是因为它的失败代价不对称：一个 bug 要等到操作员摆完二十个姿态才暴露，
而那时候数据已经采废了。真机上确实栽过一次（见 `test_the_fake_device_matches_the_real_one`）。
"""

import json

import numpy as np
import pytest

from gait.calib.accel import (
    MIN_ORIENTATIONS,
    MIN_SAMPLES_PER_ORIENTATION,
    STANDARD_GRAVITY,
)
from gait.cli import accelcal


class FakeVec:
    def __init__(self, values):
        self.x, self.y, self.z = values


class FakeSample:
    def __init__(self, values):
        self.accel = FakeVec(values)


class FakeDevice:
    """按脚本产出比力的假设备。

    **方法名必须与真的 `WT901Device` 对得上**，由
    `test_the_fake_device_matches_the_real_one` 钉住。第一版这里叫 `disconnect()`，
    因为 CLI 当时写的就是 `device.disconnect()` —— 而真的 `WT901Device` 只有
    `close()`。替身照着**被测代码的错误假设**长，于是几条测试全绿，真机一跑就
    `TypeError`。替身与真物的接口一致性得单独有人验，否则测的是自己的想象。
    """

    def __init__(self, script):
        self.script = list(script)
        self.closed = False

    async def samples(self):
        while True:
            yield FakeSample(self.script[0])

    async def close(self):
        self.closed = True


def install_fakes(monkeypatch, script, *, samples=MIN_SAMPLES_PER_ORIENTATION * 2):
    device = FakeDevice(script)
    consumed = {"index": 0}

    async def fake_collect(_device, _seconds):
        index = consumed["index"]
        consumed["index"] += 1
        # 每个姿态消耗两次 `_collect`（静置丢弃 + 正式采集）。
        vector = script[min(index // 2, len(script) - 1)]
        return np.tile(np.asarray(vector, dtype=np.float64), (samples, 1))

    async def fake_connect(_mac, _timeout):
        return device, "AA:BB:CC:DD:EE:FF"

    monkeypatch.setattr(accelcal, "_collect", fake_collect)
    monkeypatch.setattr(accelcal, "_connect", fake_connect)
    monkeypatch.setattr("builtins.input", lambda *_: "")
    return device


def spread(count, seed=3):
    rng = np.random.default_rng(seed)
    out = []
    while len(out) < count:
        v = rng.normal(size=3)
        if np.linalg.norm(v) > 1e-6:
            out.append(v / np.linalg.norm(v) * STANDARD_GRAVITY)
    return out


def test_capture_collects_enough_orientations_and_then_solves(monkeypatch, tmp_path):
    count = MIN_ORIENTATIONS + 4
    install_fakes(monkeypatch, spread(count))

    assert accelcal.main(["capture", "--out", str(tmp_path), "--count", str(count)]) == 0
    meta = json.loads((tmp_path / "meta.json").read_text(encoding="utf-8"))
    assert meta["orientations"] == count
    assert meta["method"] == "multi-orientation-magnitude"

    assert accelcal.main(["solve", "--dir", str(tmp_path)]) == 0
    calibration = json.loads((tmp_path / "calibration.json").read_text(encoding="utf-8"))
    assert len(calibration["orientations"]) == count
    # 理想数据：解出来应当接近单位阵、零零偏。
    assert calibration["bias_magnitude_mg"] < 1.0


def test_capture_never_settles_for_fewer_than_the_minimum(monkeypatch, tmp_path):
    """`--count` 低于下限时按下限走。否则操作员可以用一个参数把验收绕过去，
    而解算端会在二十分钟的采集之后才拒绝。"""
    install_fakes(monkeypatch, spread(MIN_ORIENTATIONS + 4))
    assert accelcal.main(["capture", "--out", str(tmp_path), "--count", "5"]) == 0
    meta = json.loads((tmp_path / "meta.json").read_text(encoding="utf-8"))
    assert meta["orientations"] == MIN_ORIENTATIONS


def test_a_shaky_orientation_does_not_count(monkeypatch, tmp_path):
    """没静置好的一段要被丢掉并重来，而不是记进去 —— 它会污染拟合。"""
    good = spread(MIN_ORIENTATIONS)
    monkeypatch.setattr("builtins.input", lambda *_: "")
    device = FakeDevice([good[0]])
    calls = {"n": 0}

    async def fake_collect(_device, _seconds):
        calls["n"] += 1
        index = (calls["n"] - 1) // 2
        rows = MIN_SAMPLES_PER_ORIENTATION * 2
        if index == 0:  # 第一个姿态：抖
            rng = np.random.default_rng(0)
            return np.tile(good[0], (rows, 1)) + rng.normal(0, 0.5, size=(rows, 3))
        return np.tile(good[min(index - 1, len(good) - 1)], (rows, 1))

    async def fake_connect(_mac, _timeout):
        return device, "AA:BB"

    monkeypatch.setattr(accelcal, "_collect", fake_collect)
    monkeypatch.setattr(accelcal, "_connect", fake_connect)

    assert accelcal.main(["capture", "--out", str(tmp_path), "--count", str(MIN_ORIENTATIONS)]) == 0
    saved = sorted(tmp_path.glob("orientation_*.npy"))
    assert len(saved) == MIN_ORIENTATIONS
    # 抖的那一段没有被存下来。
    for path in saved:
        assert np.load(path).std(axis=0).max() < 0.05


def test_capture_closes_the_device_even_when_interrupted(monkeypatch, tmp_path):
    """Ctrl-C 也要把设备放掉。wt901 的 ble 文档警告过：没断干净的连接会让下一次
    connect 直接失败 —— 操作员会以为是模块坏了。"""
    device = install_fakes(monkeypatch, spread(MIN_ORIENTATIONS + 4))

    def boom(*_):
        raise KeyboardInterrupt

    monkeypatch.setattr("builtins.input", boom)
    assert accelcal.main(["capture", "--out", str(tmp_path)]) == 130
    assert device.closed


def test_solve_refuses_an_empty_directory(tmp_path):
    with pytest.raises(SystemExit, match="没有 orientation"):
        accelcal.main(["solve", "--dir", str(tmp_path)])


def test_the_fake_device_matches_the_real_one():
    """替身用到的每个方法，真的 `WT901Device` 上都得有。

    这条补的是一个真实的窟窿：CLI 测试曾经全绿，而真机第一行就 `TypeError` ——
    替身实现的是 `disconnect()`，跟着当时 CLI 的错误假设走，真类只有 `close()`。
    **替身照着被测代码长，就永远不会揭穿被测代码。**
    """
    from wt901 import WT901Device

    used = {name for name in vars(FakeDevice) if not name.startswith("_")}
    missing = sorted(name for name in used if not hasattr(WT901Device, name))
    assert not missing, f"替身有这些方法，真的 WT901Device 没有：{missing}"


def test_connect_uses_the_real_classmethod_signature():
    """`WT901Device.connect` 是**类方法**且收一个 target。

    CLI 第一版写成 `WT901Device(BleTransport(address))` + `await device.connect()`，
    两处都错：构造器要的是 transport 实例（不是地址），而 `connect` 是类方法。
    """
    import inspect

    from wt901 import WT901Device

    assert "target" in inspect.signature(WT901Device.connect).parameters
    assert inspect.ismethod(WT901Device.connect), "connect 应为类方法"
