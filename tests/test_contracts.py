"""数据契约的测试。

校验代码的价值全在"该拒的时候会不会拒"。所以这里几乎每个用例都是先造一个
**合法**实例，再只改坏一处，断言它被拒 —— 而不是断言合法实例能构造成功。后者
一个空的 `__post_init__` 也能通过。
"""

import dataclasses

import numpy as np
import pytest

from gait.contracts import (
    CONTRACT_VERSION,
    FIELD_UNITS,
    MANDATORY_METADATA,
    ContractError,
    FootSeries,
    GaitCycle,
    NavResult,
    Quality,
    RawFrame,
    SessionMeta,
)

SUBJECT_UUID = "6f1a2c8e-19a4-4f0e-9a3d-2b5c7d8e9f01"


def make_raw_frame(**overrides):
    values = {
        "t_host": 1.5,
        "acc_raw": np.zeros(3, dtype=np.int16),
        "gyr_raw": np.zeros(3, dtype=np.int16),
        "ang_raw": np.zeros(3, dtype=np.int16),
        "saturated": False,
    }
    values.update(overrides)
    return RawFrame(**values)


def make_foot_series(n=10, **overrides):
    values = {
        "label": "L",
        "t": np.linspace(0.0, 1.0, n),
        "acc": np.zeros((n, 3)),
        "gyr": np.zeros((n, 3)),
        "quality": np.zeros(n, dtype=np.uint8),
        "segments": [(0, n)],
        "fs": 200.0,
    }
    values.update(overrides)
    return FootSeries(**values)


def make_nav_result(n=10, **overrides):
    values = {
        "t": np.linspace(0.0, 1.0, n),
        "q": np.zeros((n, 4)),
        "v": np.zeros((n, 3)),
        "p": np.zeros((n, 3)),
        "bg": np.zeros((n, 3)),
        "ba": np.zeros((n, 3)),
        "zupt": np.zeros(n, dtype=np.bool_),
        "stances": [(0, 4), (6, n)],
        "degraded": np.zeros(n, dtype=np.bool_),
        "score": np.zeros(n),
    }
    values.update(overrides)
    return NavResult(**values)


def make_gait_cycle(**overrides):
    values = {
        "foot": "R",
        "idx": 0,
        "t_ic": 1.0,
        "t_to": 1.6,
        "t_ic_next": 2.1,
        "stride_length": 1.2,
        "stride_time": 1.1,
        "gait_speed": 1.09,
        "stance_time": 0.6,
        "swing_time": 0.5,
        "stance_ratio": 54.5,
        "toe_clearance": 0.02,
        "strike_angle": 18.0,
        "valid": True,
        "confidence": "normal",
    }
    values.update(overrides)
    return GaitCycle(**values)


def make_session_meta(**overrides):
    values = {
        "session_id": "S-20260820-0001",
        "created_at": "2026-08-20T10:00:00Z",
        "subject_uuid": SUBJECT_UUID,
        "scenario": "walk",
        "devices": {"L": {"mac": "AA"}, "R": {"mac": "BB"}},
        "config_snapshot": {"rate_hz": 200},
        "calib_snapshot": {"L": {"bias": [0, 0, 0]}},
        "algo_version": "0.1.0",
        "algo_params": {"zupt_window": 15},
        "sync_report": {"anchors": 4},
        "integrity_report": {"loss_rate": 0.001},
        "protocol_config": {"duration_s": 180},
    }
    values.update(overrides)
    return SessionMeta(**values)


# --- 合法实例先立住，后面的用例都从它改坏一处 -----------------------------


def test_valid_instances_construct():
    assert make_raw_frame().saturated is False
    assert make_foot_series().fs == 200.0
    assert len(make_nav_result().stances) == 2
    assert make_gait_cycle().confidence == "normal"
    assert make_session_meta().contract_version == CONTRACT_VERSION


# --- RawFrame -------------------------------------------------------------


@pytest.mark.parametrize("field_name", ["acc_raw", "gyr_raw", "ang_raw"])
def test_raw_frame_rejects_wrong_dtype(field_name):
    """int16 是原始码值，float 意味着有人已经换算过了。"""
    with pytest.raises(ContractError, match="dtype"):
        make_raw_frame(**{field_name: np.zeros(3, dtype=np.float64)})


@pytest.mark.parametrize("field_name", ["acc_raw", "gyr_raw", "ang_raw"])
def test_raw_frame_rejects_wrong_length(field_name):
    with pytest.raises(ContractError, match="第 0 维应为 3"):
        make_raw_frame(**{field_name: np.zeros(4, dtype=np.int16)})


def test_raw_frame_rejects_non_array():
    with pytest.raises(ContractError, match="必须是 np.ndarray"):
        make_raw_frame(acc_raw=[0, 0, 0])


def test_raw_frame_is_frozen():
    """设备层输出一旦落盘就不该再被改动。"""
    frame = make_raw_frame()
    with pytest.raises(dataclasses.FrozenInstanceError):
        frame.t_host = 2.0


# --- FootSeries -----------------------------------------------------------


def test_foot_series_rejects_transposed_arrays():
    """(3,n) 与 (n,3) 弄反是最典型的一种错，且不拦住就会一路传到 ESKF。"""
    with pytest.raises(ContractError, match="第 1 维应为 3"):
        make_foot_series(acc=np.zeros((3, 10)))


def test_foot_series_rejects_length_mismatch():
    series_args = {"t": np.linspace(0, 1, 9)}
    with pytest.raises(ContractError, match="长度必须一致"):
        make_foot_series(**series_args)


def test_foot_series_rejects_bad_label():
    with pytest.raises(ContractError, match="label"):
        make_foot_series(label="left")


def test_foot_series_rejects_non_positive_fs():
    """fs 是实测采样率；0 或负数说明锚点解算失败却被当成了结果。"""
    with pytest.raises(ContractError, match="fs"):
        make_foot_series(fs=0.0)


def test_foot_series_rejects_quality_dtype():
    with pytest.raises(ContractError, match="dtype"):
        make_foot_series(quality=np.zeros(10, dtype=np.int32))


@pytest.mark.parametrize(
    ("segments", "reason"),
    [
        ([(0, 11)], "越界"),
        ([(5, 5)], "越界"),
        ([(-1, 5)], "越界"),
        ([(6, 10), (0, 4)], "升序"),
        ([(0, 6), (4, 10)], "升序"),
    ],
    ids=["超出末端", "空区间", "负起点", "乱序", "重叠"],
)
def test_foot_series_rejects_bad_segments(segments, reason):
    with pytest.raises(ContractError, match=reason):
        make_foot_series(segments=segments)


def test_foot_series_accepts_gap_split_segments():
    """空洞切分出的多段是正常形态，不该被误拒。"""
    series = make_foot_series(segments=[(0, 4), (7, 10)])
    assert len(series.segments) == 2


# --- NavResult ------------------------------------------------------------


def test_nav_result_rejects_quaternion_with_three_components():
    with pytest.raises(ContractError, match="第 1 维应为 4"):
        make_nav_result(q=np.zeros((10, 3)))


def test_nav_result_rejects_non_bool_zupt():
    with pytest.raises(ContractError, match="dtype"):
        make_nav_result(zupt=np.zeros(10, dtype=np.uint8))


def test_nav_result_rejects_length_mismatch_in_any_field():
    with pytest.raises(ContractError, match="长度必须一致"):
        make_nav_result(bg=np.zeros((9, 3)))


def test_nav_result_rejects_stances_outside_samples():
    with pytest.raises(ContractError, match="越界"):
        make_nav_result(stances=[(0, 99)])


# --- GaitCycle ------------------------------------------------------------


def test_gait_cycle_rejects_unknown_confidence():
    with pytest.raises(ContractError, match="confidence"):
        make_gait_cycle(confidence="low")


@pytest.mark.parametrize(
    "times",
    [
        {"t_ic": 2.0, "t_to": 1.6, "t_ic_next": 2.1},
        {"t_ic": 1.0, "t_to": 2.5, "t_ic_next": 2.1},
        {"t_ic": 1.0, "t_to": 1.0, "t_ic_next": 2.1},
    ],
    ids=["触地晚于离地", "离地晚于下次触地", "两事件同刻"],
)
def test_gait_cycle_rejects_non_monotonic_events(times):
    with pytest.raises(ContractError, match="严格递增"):
        make_gait_cycle(**times)


def test_invalid_cycle_still_carries_its_numbers():
    """PRD §13：指标全量计算 + 质量标注，无指标级门控。

    一条不可信的步态仍然要带着数值输出，由 confidence 说明可信程度，而不是被
    丢掉 —— 所以 valid=False 必须能构造成功。
    """
    cycle = make_gait_cycle(valid=False, confidence="invalid", stride_length=0.31)
    assert cycle.stride_length == 0.31


# --- SessionMeta ----------------------------------------------------------


def test_session_meta_rejects_institution_record_number_as_subject():
    """FR-02 的防线：`subject_uuid` 只接受 UUID。"""
    with pytest.raises(ContractError, match="FR-02"):
        make_session_meta(subject_uuid="住院号-2026-0413")


def test_session_meta_rejects_empty_subject_uuid():
    with pytest.raises(ContractError, match="subject_uuid"):
        make_session_meta(subject_uuid="")


@pytest.mark.parametrize("field_name", MANDATORY_METADATA)
def test_session_meta_rejects_每个强制字段为空(field_name):
    """PRD §6.1 的强制字段，空字典与缺席对复现而言是一回事。"""
    empty = "" if isinstance(getattr(make_session_meta(), field_name), str) else {}
    with pytest.raises(ContractError, match="强制包含"):
        make_session_meta(**{field_name: empty})


def test_mandatory_metadata_matches_prd_list():
    """强制字段清单本身也要被钉住 —— 少一个就是可追溯性的缺口。"""
    assert set(MANDATORY_METADATA) == {
        "algo_version",
        "algo_params",
        "calib_snapshot",
        "config_snapshot",
        "sync_report",
        "integrity_report",
        "protocol_config",
    }


def test_session_meta_records_the_contract_version():
    """三个月后判断某份历史报告用的是哪版结构，靠的就是这个字段。

    这里不写字面版本号：字面量只由 `test_contract_version_records_the_unit_change`
    一处持有，好让"升版本"始终是一个需要改那一处、因而必须想一想的动作。散在多处
    的话，升版本就退化成批量替换。
    """
    assert make_session_meta().contract_version == CONTRACT_VERSION


def test_session_meta_has_no_subject_id_field():
    """契约 §3.5 的 subject_id 已按 FR-02 改名，旧名不得残留。"""
    names = {f.name for f in dataclasses.fields(SessionMeta)}
    assert "subject_id" not in names
    assert "subject_uuid" in names
    assert "protocol_config" in names


# --- Quality --------------------------------------------------------------


def test_quality_flags_combine_and_test():
    mask = Quality.SATURATED | Quality.GAP_EDGE
    assert Quality.SATURATED in mask
    assert Quality.INTERPOLATED not in mask
    assert int(mask) == 5


def test_quality_fits_in_uint8():
    """quality 数组的 dtype 是 uint8，标志位不得溢出。"""
    every = Quality.SATURATED | Quality.INTERPOLATED | Quality.GAP_EDGE
    assert 0 <= int(every) <= np.iinfo(np.uint8).max


# --- 单位（R2：SI） ---------------------------------------------------------


def test_angular_velocity_is_si():
    """R2 的核心：角速度是 rad/s，不是 deg/s。

    与 wt901 的 `ImuSample.gyro` 对齐，也是 ESKF/INS 本来就工作的单位。少一次
    转换就少一处忘记转换的机会 —— 而转换错了不报错，只出错数。
    """
    assert FIELD_UNITS["FootSeries.gyr"] == "rad/s"


def test_gyro_bias_follows_the_gyro():
    """`bg` 是陀螺零偏，它的单位必须跟着陀螺走。

    单独列一条，是因为改单位时最容易漏掉的就是这种"跟随字段"：`gyr` 显眼，
    `bg` 不显眼，而两者不一致会让零偏补偿静默地错 57.3 倍。
    """
    assert FIELD_UNITS["NavResult.bg"] == FIELD_UNITS["FootSeries.gyr"] == "rad/s"


def test_strike_angle_is_the_only_degree_field():
    """混用单位是真实风险，所以把"只有这一处"钉死。

    `strike_angle` 保持 deg 是有意的：它是面向报告读者的指标，不是算法中间量。
    但一个例外容易变成两个、三个 —— 任何人再往表里加 deg，这条会失败并逼他解释。
    """
    in_degrees = sorted(name for name, unit in FIELD_UNITS.items() if unit == "deg")
    assert in_degrees == ["GaitCycle.strike_angle"]


#: 没有物理量纲的字段，逐个写明理由。放在测试里而不是 contracts.py 里，是因为
#: 它是校验用的簿记，不是公开契约的一部分。
NOT_DIMENSIONAL = {
    # 未换算的 int16 码值，没有物理单位
    "RawFrame.acc_raw", "RawFrame.gyr_raw", "RawFrame.ang_raw",
    "RawFrame.t_host",  # 主机接收时刻，秒，但它不是物理量而是时钟读数
    "RawFrame.saturated",
    "FootSeries.label", "FootSeries.quality", "FootSeries.segments",
    "NavResult.zupt", "NavResult.stances", "NavResult.degraded",
    "GaitCycle.foot", "GaitCycle.idx", "GaitCycle.valid", "GaitCycle.confidence",
}


def test_every_dimensional_field_is_in_the_unit_table():
    """反方向：结构上新增一个带量纲的字段而忘了登记单位，必须失败。

    只查"表里的名字都存在"是不够的 —— 那种单向检查会让新字段静默地没有单位，
    而没有单位的数值在跨模块传递时就是一个待发生的错误。
    """
    import dataclasses

    missing = []
    for structure in (RawFrame, FootSeries, NavResult, GaitCycle):
        for f in dataclasses.fields(structure):
            key = f"{structure.__name__}.{f.name}"
            if key in NOT_DIMENSIONAL or key in FIELD_UNITS:
                continue
            missing.append(key)
    assert missing == [], f"这些字段既未登记单位、也未声明无量纲：{missing}"


def test_unit_table_names_only_real_fields():
    """正方向：表里不得出现结构上不存在的字段。"""
    import dataclasses

    known = {
        f"{s.__name__}.{f.name}"
        for s in (RawFrame, FootSeries, NavResult, GaitCycle)
        for f in dataclasses.fields(s)
    }
    stale = sorted(set(FIELD_UNITS) - known)
    assert stale == [], f"单位表引用了不存在的字段：{stale}"


def test_contract_version_records_the_unit_change():
    """单位变更改的是数值的含义而非结构，没有版本号就没人能把两代会话分开。"""
    assert CONTRACT_VERSION == "1.1"
    assert make_session_meta().contract_version == "1.1"
