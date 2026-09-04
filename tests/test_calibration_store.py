"""出厂标定参数库的测试（RAY-207 R3）。

判据成对：**拒绝坏的**与**接受好的**必须都在。只有前者时，把 `admit` 写成一律拒绝就能
全绿 —— 而那样的参数库会阻断每一次正常会话，比没有它更糟（`calib.still` 的松动检测、
`calib.accel` 的姿态判据都因为同一条理由配了反向断言）。

R3 的失效判据是**身份推导变化**与**固件变化**，不是时间。因此这里没有任何「过了多少天」
的测试 —— 那个需求在 PRD FR-04 里不存在（原文只有「缺失即阻断新会话」）。
"""

import json

import numpy as np
import pytest

from gait.calib.accel import AccelCalibration
from gait.calib.still import CalibrationError
from gait.calib.store import (
    FIRMWARE_UNKNOWN,
    SCHEMA_VERSION,
    CalibrationRecord,
    CalibrationStore,
    record_from_calibration,
)

PROVENANCE = "wt901-read-mac/le-reversed/2026-08-27"
FIRMWARE = "1.4.2"
MAC = "F9:B3:4F:46:C9:31"


def calibration(device: str = MAC) -> AccelCalibration:
    return AccelCalibration(
        device=device,
        matrix=np.eye(3),
        offset=np.zeros(3),
        residual_mg=0.54,
        loo_mg=1.41,
        condition_number=18.0,
        orientations=(),
    )


def record(**overrides) -> CalibrationRecord:
    base = {
        "kind": "mac",
        "value": MAC,
        "provenance": PROVENANCE,
        "firmware": FIRMWARE,
        "recorded_at": "2026-09-04T00:00:00+00:00",
        "calib_snapshot": calibration().snapshot(),
    }
    return CalibrationRecord(**{**base, **overrides})


def test_round_trip_through_disk(tmp_path):
    store = CalibrationStore(tmp_path)
    store.put(record())
    loaded = store.get("mac", MAC)

    assert loaded is not None
    assert loaded.key == f"mac:{MAC}"
    assert loaded.firmware == FIRMWARE
    # 参数本身要一字不差地回来 —— 它是复现一份历史报告的依据。
    assert loaded.calib_snapshot["bias_magnitude_mg"] == pytest.approx(0.0, abs=1e-9)
    assert loaded.calib_snapshot["method"] == "multi-orientation-magnitude"


def test_a_calibrated_device_is_admitted(tmp_path):
    """**反向断言。** 没有它，把 `admit` 写成一律拒绝也能让下面每一条通过。"""
    store = CalibrationStore(tmp_path)
    store.put(record())
    verdict = store.admit(
        "mac", MAC, current_provenance=PROVENANCE, current_firmware=FIRMWARE
    )
    assert verdict.admitted
    assert verdict.reason == "ok"
    assert verdict.problems == ()


def test_missing_calibration_blocks(tmp_path):
    verdict = CalibrationStore(tmp_path).admit(
        "mac", MAC, current_provenance=PROVENANCE, current_firmware=FIRMWARE
    )
    assert not verdict.admitted
    assert verdict.reason == "missing"
    assert "服务方" in verdict.problems[0]


def test_a_changed_identity_provenance_invalidates(tmp_path):
    """R3 失效判据之一。`kind` 还是 `mac`、`value` 也还在库里，但推导变了 ——
    同一台设备在两套推导下得到**不同的键**，旧记录挂的是旧键。

    这是 `device/binding.py` 明确警告过的场景（`read_mac()` 的字节排布是推出来的）。
    """
    store = CalibrationStore(tmp_path)
    store.put(record())
    verdict = store.admit(
        "mac",
        MAC,
        current_provenance="wt901-read-mac/be/2027-01-01",
        current_firmware=FIRMWARE,
    )
    assert not verdict.admitted
    assert verdict.reason == "stale-provenance"
    assert "不是设备换了一台" in verdict.problems[0]


def test_a_changed_firmware_invalidates(tmp_path):
    store = CalibrationStore(tmp_path)
    store.put(record())
    verdict = store.admit(
        "mac", MAC, current_provenance=PROVENANCE, current_firmware="1.5.0"
    )
    assert not verdict.admitted
    assert verdict.reason == "stale-firmware"
    assert "重新标定" in verdict.problems[0]


def test_unknown_firmware_blocks_rather_than_passes(tmp_path):
    """读不到固件时**阻断**，与 `preflight_battery` 的三路判定同一口径：
    够电放行、低电阻断、**读不到也阻断**。

    「匹配不上」与「不知道匹不匹配」对下游是同一件事 —— 都不能保证这份参数描述的是
    这台设备。放行会让一份可能不适用的参数静默进到数据里。
    """
    store = CalibrationStore(tmp_path)
    store.put(record())
    verdict = store.admit(
        "mac", MAC, current_provenance=PROVENANCE, current_firmware=FIRMWARE_UNKNOWN
    )
    assert not verdict.admitted
    assert verdict.reason == "stale-firmware"


def test_mac_case_and_separator_do_not_lose_the_record(tmp_path):
    """同一个 MAC 写成 `aa-bb…` 与 `AA:BB…` 是同一台设备。

    规范化与 `DeviceIdentity.__post_init__` 必须一致，否则写进去的键取不出来 ——
    而那种失败在日志里看起来和「这台模块没标定过」一模一样。
    """
    store = CalibrationStore(tmp_path)
    store.put(record(value=MAC.lower().replace(":", "-")))
    assert store.get("mac", MAC) is not None
    assert store.admit(
        "mac", MAC.lower(), current_provenance=PROVENANCE, current_firmware=FIRMWARE
    ).admitted


def test_two_devices_do_not_collide(tmp_path):
    """一台一个文件：一台的写入不该碰到另一台。"""
    store = CalibrationStore(tmp_path)
    other = "AA:BB:CC:DD:EE:FF"
    store.put(record())
    store.put(record(value=other, firmware="2.0.0"))

    assert store.get("mac", MAC).firmware == FIRMWARE
    assert store.get("mac", other).firmware == "2.0.0"


def test_a_corrupt_file_is_not_reported_as_missing(tmp_path):
    """损坏与缺失是两件事：前者重新**下发**即可，后者要联系服务方**标定**。

    把损坏报成缺失会让服务方去做一次不必要的十分钟标定。
    """
    store = CalibrationStore(tmp_path)
    store.put(record())
    store.path_for("mac", MAC).write_text("{ 半个 JSON", encoding="utf-8")

    with pytest.raises(CalibrationError, match="读不出来"):
        store.get("mac", MAC)

    verdict = store.admit(
        "mac", MAC, current_provenance=PROVENANCE, current_firmware=FIRMWARE
    )
    assert not verdict.admitted
    assert verdict.reason == "unreadable"
    assert verdict.reason != "missing"


def test_an_unknown_schema_version_is_refused_not_guessed(tmp_path):
    """格式不认得时不猜字段。按错误的字段名读出一组「看起来正常」的参数，
    比拒绝危险得多 —— 它会静默地进到数据里。"""
    store = CalibrationStore(tmp_path)
    store.put(record())
    target = store.path_for("mac", MAC)
    data = json.loads(target.read_text(encoding="utf-8"))
    data["schema_version"] = SCHEMA_VERSION + 1
    target.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(CalibrationError, match="schema_version"):
        store.get("mac", MAC)


def test_a_record_missing_a_field_is_refused(tmp_path):
    store = CalibrationStore(tmp_path)
    store.put(record())
    target = store.path_for("mac", MAC)
    data = json.loads(target.read_text(encoding="utf-8"))
    del data["firmware"]
    target.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(CalibrationError, match="缺字段"):
        store.get("mac", MAC)


def test_an_empty_calibration_is_refused():
    """一条没有参数的记录不是记录。没有这条，一个空 dict 会被存进去，
    而它在 `admit` 眼里与一份真参数没有区别。"""
    with pytest.raises(CalibrationError, match="不能为空"):
        record(calib_snapshot={})


def test_verdict_cannot_pass_without_reason_or_fail_without_problems():
    """判定形状对齐 `CalibrationVerdict`：通过时不带原因、不通过必须给原因。"""
    from gait.calib.store import StoreVerdict

    with pytest.raises(CalibrationError):
        StoreVerdict(admitted=True, reason="ok", problems=("多余的原因",))
    with pytest.raises(CalibrationError):
        StoreVerdict(admitted=False, reason="missing")


def test_record_from_calibration_captures_the_snapshot():
    made = record_from_calibration(
        calibration(),
        kind="mac",
        value=MAC,
        provenance=PROVENANCE,
        firmware=FIRMWARE,
    )
    assert made.calib_snapshot == calibration().snapshot()
    assert made.recorded_at  # 自动填的时间戳
    assert made.key == f"mac:{MAC}"


def test_record_from_calibration_refuses_something_without_snapshot():
    with pytest.raises(CalibrationError, match="snapshot"):
        record_from_calibration(
            object(), kind="mac", value=MAC, provenance=PROVENANCE, firmware=FIRMWARE
        )


def test_a_partial_write_cannot_replace_a_good_record(tmp_path):
    """`put` 先写临时文件再改名。这条钉住临时文件不会被当成记录读走 ——
    否则一次断电会让下一次 `get` 读到半个 JSON。"""
    store = CalibrationStore(tmp_path)
    store.put(record())
    leftovers = list(store.root.glob("*.tmp"))
    assert leftovers == [], f"临时文件没有被改名清掉：{leftovers}"


# ── 跨 scope 契约：RAY-360 的消费侧 ────────────────────────────────────────


def test_the_real_calibration_satisfies_ray360_protocol():
    """**这条补的是一个真实的接口窟窿。**

    RAY-360 的 `device/footseries.calibrated_foot_series` 收的是它自己定义的
    `AccelCalibration` Protocol（要求 `name` 与 `apply`），而 RAY-207 交付的类当时
    只有 `apply` —— 两个 scope 各自都跑得通、合在一起却传不进去，`isinstance` 为假。

    这正是 RAY-360 那条 Issue 自己列举过四次的形状：「功能齐全、测试齐全，只是没人
    把它接起来」。加一条 `isinstance` 断言，让第五次在 CI 里就被发现。
    """
    from gait.device.footseries import AccelCalibration as Protocol

    assert isinstance(calibration(), Protocol)


def test_the_calibration_name_says_whether_it_was_calibrated():
    """`name` 进会话元数据，要能一眼看出标没标定。与 `NoAccelCalibration.name` 成对。"""
    from gait.device.footseries import NO_ACCEL_CALIBRATION

    assert "未做出厂加计标定" in NO_ACCEL_CALIBRATION.name
    assert NO_ACCEL_CALIBRATION.name != calibration().name
    assert MAC in calibration().name


def test_a_stored_record_can_be_replayed_into_a_working_calibration(tmp_path):
    """从库里读回来的快照要能重建出一个能 `apply` 的标定 —— 否则参数库存的是
    一堆事后无法使用的数字，而复现一份历史报告正需要它。"""
    store = CalibrationStore(tmp_path)
    original = calibration()
    store.put(record(calib_snapshot=original.snapshot()))
    snapshot = store.get("mac", MAC).calib_snapshot

    restored = AccelCalibration(
        device=snapshot["device"],
        matrix=np.array(snapshot["matrix"]),
        offset=np.array(snapshot["offset"]),
        residual_mg=snapshot["residual_mg"],
        loo_mg=snapshot["loo_mg"],
        condition_number=snapshot["condition_number"],
        orientations=(),
    )
    probe = np.array([0.1, 0.2, 9.7])
    np.testing.assert_allclose(restored.apply(probe), original.apply(probe), atol=1e-12)


def test_admit_devices_derives_verdicts_from_readings(tmp_path):
    """入参是**读数**、出参是判定 —— 与 `preflight_battery(readings)` 同一形状。

    `app/sources.py` 的模块文档把这条写成了原则：stub 只被允许提供读数，不能决定准入。
    """
    from gait.calib.store import admit_devices

    store = CalibrationStore(tmp_path)
    store.put(record())  # 只有左脚这台标定过
    verdicts = admit_devices(
        store,
        {
            "L": {"kind": "mac", "value": MAC, "firmware": FIRMWARE},
            "R": {"kind": "mac", "value": "AA:BB:CC:DD:EE:FF", "firmware": FIRMWARE},
        },
        current_provenance=PROVENANCE,
    )
    assert verdicts["L"].admitted
    assert not verdicts["R"].admitted
    assert verdicts["R"].reason == "missing"


def test_admit_devices_refuses_a_one_footed_check(tmp_path):
    """少一只脚不是「那只脚没问题」，是这次自检没覆盖它 ——
    与 `calib.still.verdict` / `preflight_battery` 同一口径。"""
    from gait.calib.store import admit_devices

    with pytest.raises(CalibrationError, match="缺少这些脚"):
        admit_devices(
            CalibrationStore(tmp_path),
            {"L": {"kind": "mac", "value": MAC, "firmware": FIRMWARE}},
            current_provenance=PROVENANCE,
        )


def test_admit_devices_treats_a_missing_firmware_reading_as_unknown(tmp_path):
    """读数里没有固件字段时按 `unknown` 处理，而 `unknown` 是阻断的。
    不能因为读数缺一项就静默放行。"""
    from gait.calib.store import admit_devices

    store = CalibrationStore(tmp_path)
    store.put(record())
    verdicts = admit_devices(
        store,
        {
            "L": {"kind": "mac", "value": MAC},
            "R": {"kind": "mac", "value": MAC, "firmware": FIRMWARE},
        },
        current_provenance=PROVENANCE,
    )
    assert not verdicts["L"].admitted
    assert verdicts["L"].reason == "stale-firmware"
    assert verdicts["R"].admitted
