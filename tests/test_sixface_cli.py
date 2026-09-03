"""六面法工装 CLI 的离线测试。

BLE 采集本身跑不进测试（也跑不进非交互进程 —— macOS 的 TCC 会直接中止），但**交互
流程的逻辑**跑得进来：喂一台假设备，让它按脚本产出各个面的读数，验工装收下了正确的
面、拒绝了该拒绝的段、并且能在操作员摆错时让他重来。

这一段值得测，是因为它的失败代价不对称：一个 bug 要等到操作员摆完十分钟才暴露，而
那时候数据已经采废了。
"""

import json

import numpy as np
import pytest

from gait.calib.accel import FACES, MIN_SAMPLES_PER_FACE, STANDARD_GRAVITY
from gait.cli import sixface


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
    `close()`。替身照着**被测代码的错误假设**长，于是这几条测试全绿，真机一跑就
    `TypeError`。替身与真物的接口一致性得单独有人验，否则测的是自己的想象。
    """

    def __init__(self, script):
        self.script = list(script)
        self.closed = False

    async def samples(self):
        current = self.script[0]
        while True:
            yield FakeSample(current)

    async def close(self):
        self.closed = True


def install_fakes(monkeypatch, script, *, samples_per_call=MIN_SAMPLES_PER_FACE * 2):
    """把 BLE 连接、阻塞输入和「采多久」三件事都换成确定性的替身。"""
    device = FakeDevice(script)
    consumed = {"index": 0}

    async def fake_collect(_device, _seconds):
        index = consumed["index"]
        consumed["index"] += 1
        # 每个面消耗两次 `_collect`（静置丢弃 + 正式采集），所以两次取同一个向量。
        vector = script[index // 2]
        return np.tile(np.asarray(vector, dtype=np.float64), (samples_per_call, 1))

    async def fake_connect(_mac, _timeout):
        return device, "AA:BB:CC:DD:EE:FF"

    monkeypatch.setattr(sixface, "_collect", fake_collect)
    monkeypatch.setattr(sixface, "_connect", fake_connect)
    monkeypatch.setattr("builtins.input", lambda *_: "")
    return device


def face_reading(face: str, *, bias=0.0) -> np.ndarray:
    axis = "XYZ".index(face[1])
    vector = np.zeros(3)
    vector[axis] = STANDARD_GRAVITY if face[0] == "+" else -STANDARD_GRAVITY
    return vector + bias


def test_capture_writes_six_faces_and_then_solves(monkeypatch, tmp_path):
    """完整走一遍：六个面依次摆对 -> 落盘 -> solve 出参数。"""
    install_fakes(monkeypatch, [face_reading(face) for face in FACES])

    assert sixface.main(["capture", "--out", str(tmp_path)]) == 0

    meta = json.loads((tmp_path / "meta.json").read_text(encoding="utf-8"))
    assert meta["device"] == "AA:BB:CC:DD:EE:FF"
    assert sorted(meta["faces"]) == sorted(FACES)

    assert sixface.main(["solve", "--dir", str(tmp_path)]) == 0
    calibration = json.loads((tmp_path / "calibration.json").read_text(encoding="utf-8"))
    assert len(calibration["faces"]) == 6
    # 理想数据：解出来应当接近单位阵、零零偏。
    assert calibration["bias_magnitude_mg"] < 1.0
    assert calibration["residual_mg"] < 1.0


def test_capture_recovers_when_the_operator_repeats_a_face(monkeypatch, tmp_path):
    """操作员把第二面又摆成了 +Z：工装必须认出来、让他重摆，而不是收下。

    没有这条，重复的面会被当成新面记下，最终 `solve_six_face` 才报「缺少某面」——
    而那时候十分钟已经花完了。
    """
    script = [
        face_reading("+Z"),  # 第 1 面：对
        face_reading("+Z"),  # 第 2 面：又摆了一次 +Z，应被拒
        face_reading("-Z"),  # 重摆，对
        face_reading("+X"),
        face_reading("-X"),
        face_reading("+Y"),
        face_reading("-Y"),
    ]
    install_fakes(monkeypatch, script)

    assert sixface.main(["capture", "--out", str(tmp_path)]) == 0
    meta = json.loads((tmp_path / "meta.json").read_text(encoding="utf-8"))
    assert sorted(meta["faces"]) == sorted(FACES)


def test_capture_closes_the_device_even_when_interrupted(monkeypatch, tmp_path):
    """Ctrl-C 也要把设备放掉。wt901 的 ble 文档警告过：没断干净的连接会让下一次
    connect 直接失败 —— 操作员会以为是模块坏了。"""
    device = install_fakes(monkeypatch, [face_reading(face) for face in FACES])

    def boom(*_):
        raise KeyboardInterrupt

    monkeypatch.setattr("builtins.input", boom)
    assert sixface.main(["capture", "--out", str(tmp_path)]) == 130
    assert device.closed


def test_solve_refuses_a_directory_that_is_missing_a_face(tmp_path):
    for face in FACES[:-1]:
        name = face.replace("+", "p").replace("-", "m")
        np.save(
            tmp_path / f"face_{name}.npy",
            np.tile(face_reading(face), (MIN_SAMPLES_PER_FACE * 2, 1)),
        )
    with pytest.raises(SystemExit, match="缺少"):
        sixface.main(["solve", "--dir", str(tmp_path)])


def test_the_fake_device_matches_the_real_one():
    """替身用到的每个方法，真的 `WT901Device` 上都得有。

    这条是补一个真实的窟窿：上面那几条测试曾经全绿，而 CLI 在真机上第一行就
    `TypeError` —— 因为替身实现的是 `disconnect()`，跟着当时 CLI 的错误假设走，
    真类只有 `close()`。**替身照着被测代码长，就永远不会揭穿被测代码。**

    只比方法名，不比签名：签名要真调才知道，而那正是这一层测不到的部分（它由真机
    冒烟承担）。但名字对不上这种最粗的错，不该留到操作员摆完六个面才发现。
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

    signature = inspect.signature(WT901Device.connect)
    assert "target" in signature.parameters
    assert inspect.ismethod(WT901Device.connect), "connect 应为类方法"
