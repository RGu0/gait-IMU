"""采集落盘的设备身份（RAY-441 R2）。

本 Issue 要消的是一个**静默**的坑：录下来的数据事后认不出是哪台模块，而它看起来
一切正常 —— 文件在、能读、有个像标识的字符串，只是那个字符串换台主机就没了意义。

判据成对：拿得到 MAC 时必须是 `kind="mac"`，**拿不到时必须如实降级而不是伪造**。
只验前者的话，一个恒返回 `mac_identity("00:00:00:00:00:00")` 的实现也能全绿。
"""

import asyncio
import json

import numpy as np
import pytest

from gait.calib.store import CalibrationRecord, CalibrationStore
from gait.contracts import SessionMeta
from gait.device.binding import DeviceIdentity
from gait.device.identity import (
    MAC_PROVENANCE,
    PLATFORM_PROVENANCE,
    platform_identity,
    resolve_recording_identity,
)

MAC = "F9:B3:4F:46:C9:31"
HANDLE = "26F34505-CCEA-EC2D-CE64-B0AEE6E340DE"


@pytest.fixture
def session(tmp_path):
    """建一个真实的会话目录。`SessionCapture` 刻意不自己建目录 —— 「会话已登记」
    只能有一个来源（`io.session.create_session`）。"""
    from gait.io.session import create_session, new_session_id, new_subject_uuid

    session_id = new_session_id()
    create_session(
        tmp_path,
        SessionMeta(
            session_id=session_id,
            created_at="2026-09-08T00:00:00+00:00",
            subject_uuid=new_subject_uuid(),
            scenario="walk",
            devices={"L": {}, "R": {}},
            config_snapshot={"state": "test"},
            calib_snapshot={"state": "test"},
            algo_version="test",
            algo_params={"duration_s": 1},
            sync_report={"state": "test"},
            integrity_report={"state": "test"},
            protocol_config={"duration_s": 1},
        ),
    )
    return tmp_path, session_id


class _Telemetry:
    def __init__(self, mac=None, error=None):
        self._mac, self._error = mac, error

    async def read_mac(self):
        if self._error is not None:
            raise self._error
        return self._mac


class _Device:
    def __init__(self, mac=None, error=None):
        self.telemetry = _Telemetry(mac, error)


# ── 读得到 MAC 与读不到，必须成对 ──────────────────────────────────────────


def _resolve(device):
    """本仓库的异步测试惯例是直接 `asyncio.run`（见 `test_device_identity.py`），
    没有 pytest-asyncio 插件。"""
    return asyncio.run(resolve_recording_identity(device, platform_address=HANDLE))


def test_a_readable_mac_becomes_a_portable_identity():
    identity, degraded = _resolve(_Device(mac=MAC))

    assert identity.kind == "mac"
    assert identity.value == MAC
    assert identity.provenance == MAC_PROVENANCE
    assert identity.portable
    assert degraded is None


def test_an_unreadable_mac_degrades_honestly_instead_of_fabricating():
    """**本 Issue 的核心断言。** 读不到就记平台地址，并说出来。

    没有这一条，实现可以在读不到时返回一个占位 MAC —— 而占位 MAC 与真 MAC 在
    数据里长得一模一样，事后无从分辨。降级则是**写出来的事实**。
    """
    identity, degraded = _resolve(_Device(error=TimeoutError("no answer")))

    assert identity.kind == "platform-address"
    assert identity.value == HANDLE
    assert identity.provenance == PLATFORM_PROVENANCE
    assert not identity.portable, "平台地址换台主机就认不出，不能算可移植"
    assert degraded and "读不到" in degraded


def test_degrading_does_not_lose_the_whole_capture():
    """降级而不是抛 —— 与 `read_device_identity`（绑定用）相反，理由写在它文档里：
    录制的取舍是「身份弱一点」对「数据全丢」，后者明显更糟。"""
    identity, _ = _resolve(_Device(error=RuntimeError("boom")))
    assert identity is not None  # 没抛出来


def test_binding_still_refuses_rather_than_degrades():
    """**两条策略并存不是自相矛盾。** 绑定那条仍然不吞异常 —— 一份用占位键建的
    绑定比没有绑定更糟，因为它看起来是好的。这里钉住它没被本 Issue 顺手改掉。"""
    from gait.device.identity import read_device_identity

    with pytest.raises(TimeoutError):
        asyncio.run(read_device_identity(_Device(error=TimeoutError("no answer"))))


# ── 落盘：新格式带得到身份，旧格式如实降级 ────────────────────────────────


def test_a_new_archive_carries_the_identity_and_an_old_one_degrades(tmp_path):
    """`identity_from_archive` 的往返：新档读回 `mac`，旧档（没有身份键）读成
    `platform-address` —— 而**不是**拿 `left_device` 里的平台句柄假装成 MAC。

    直接测这个纯函数而不是走 `load_trial_dir`：后者会跑整条时基管线（要几百个样本
    与锚点），把「旧档怎么读」绑在那上面，测的就不再只是这条判据。
    """
    from gait.cli.v3prime import identity_from_archive

    def archive(*, with_identity):
        payload = {"left_device": np.asarray(HANDLE)}
        if with_identity:
            payload["left_identity"] = np.asarray(
                json.dumps(DeviceIdentity("mac", MAC, MAC_PROVENANCE).snapshot())
            )
        path = tmp_path / ("new.npz" if with_identity else "old.npz")
        np.savez(path, **payload)
        return np.load(path, allow_pickle=False)

    new = identity_from_archive(archive(with_identity=True), "left", "left_device")
    assert (new.kind, new.value, new.provenance) == ("mac", MAC, MAC_PROVENANCE)
    assert new.portable

    old = identity_from_archive(archive(with_identity=False), "left", "left_device")
    assert old.kind == "platform-address"
    assert old.value == HANDLE
    assert not old.portable, "旧档必须被认出是不可移植身份 —— 那是它的真实状态"


def test_a_recording_transport_without_an_injected_identity_degrades(session):
    """`SessionCapture` 只拿得到 `Transport`，读不了 MAC。不注入时如实降级，
    而不是假装身份这回事不存在。"""
    from wt901.transport.memory import MemoryTransport

    from gait.device.capture import SessionCapture

    root, session_id = session
    capture = SessionCapture(root, session_id)
    capture.wrap("L", MemoryTransport(device_id=HANDLE))
    identity = capture.identities["L"]
    assert identity.kind == "platform-address"
    assert not identity.portable
    capture.close()


def test_an_injected_identity_is_kept(session):
    """反向断言：注入了就用注入的那个。没有它，把 `wrap` 写成永远降级也能让
    上一条通过 —— 而那样注入参数就是摆设。"""
    from wt901.transport.memory import MemoryTransport

    from gait.device.capture import SessionCapture

    root, session_id = session
    capture = SessionCapture(root, session_id)
    capture.wrap(
        "L",
        MemoryTransport(device_id=HANDLE),
        identity=DeviceIdentity("mac", MAC, MAC_PROVENANCE),
    )
    assert capture.identities["L"].value == MAC
    assert capture.identities["L"].kind == "mac"
    capture.close()


# ── 与标定参数库真的对得上（本 Issue 的动机） ────────────────────────────


def test_a_new_format_identity_matches_the_calibration_store(tmp_path):
    """**这条是本 Issue 存在的理由。**

    RAY-207 的参数库按身份存取。用新格式落盘的身份去 `admit()`，能匹配上；
    而旧格式那个平台句柄匹配不上 —— 两者一起断言，才说明「换过来」确实有效。
    """
    store = CalibrationStore(tmp_path)
    store.put(
        CalibrationRecord(
            kind="mac",
            value=MAC,
            provenance=MAC_PROVENANCE,
            firmware="1.4.2",
            recorded_at="2026-09-08T00:00:00+00:00",
            calib_snapshot={"method": "multi-orientation-magnitude"},
        )
    )

    new = DeviceIdentity("mac", MAC, MAC_PROVENANCE)
    assert store.admit(
        new.kind, new.value, current_provenance=new.provenance, current_firmware="1.4.2"
    ).admitted

    old = platform_identity(HANDLE)
    verdict = store.admit(
        old.kind, old.value, current_provenance=old.provenance, current_firmware="1.4.2"
    )
    assert not verdict.admitted
    assert verdict.reason == "missing", (
        "平台句柄在库里当然找不到 —— 这正是 RAY-441 之前所有采集的处境"
    )
