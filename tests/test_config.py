"""配置体系的测试。

两条原则贯穿全文：

1. **不断言未标定的数字。** `AlgoConfig` 的阈值属于 RAY-203/RAY-204，现在的值是结构
   占位。把 `0.35` 写进断言，等于让标定时必须同步改测试 —— 那样的测试没有守住任何
   东西，只是在重复实现。断言的是**关系**（更长、更松、强制 ZARU），标定之后依然成立。
2. **往返相等是可复现性的可测形式。** "任一历史会话可凭元数据精确复现算法输入"这句
   验收标准，落到代码上就是 `from_snapshot(snapshot()) == 原对象`。
"""

import dataclasses

import pytest

from gait.config import (
    CONFIG_VERSION,
    DEFAULT_DURATION_S,
    DURATION_PRESETS,
    AlgoConfig,
    ConfigError,
    ProtocolConfig,
    SessionConfig,
)

# --- ProtocolConfig：预设时长 ------------------------------------------------


def test_default_duration_is_180():
    """PRD §7：默认 180 s。"""
    assert ProtocolConfig().duration_s == DEFAULT_DURATION_S == 180


@pytest.mark.parametrize("duration", DURATION_PRESETS)
def test_every_preset_duration_is_accepted(duration):
    assert ProtocolConfig(duration_s=duration).duration_s == duration


@pytest.mark.parametrize("duration", [0, 45, 175, 181, 300, -180])
def test_non_preset_duration_is_refused(duration):
    """一个"差不多"的 175 s 会产生既不能与 180 s 组比较、也不属于任何已知协议的数据。"""
    with pytest.raises(ConfigError, match="预设之一"):
        ProtocolConfig(duration_s=duration)


def test_refusal_explains_why_rather_than_only_what():
    """拒绝信息要说明理由 —— 否则下一个人的第一反应是把校验删掉。"""
    with pytest.raises(ConfigError) as caught:
        ProtocolConfig(duration_s=175)
    message = str(caught.value)
    assert "不同时长视为不同协议" in message
    assert "PRD §7" in message


# --- ProtocolConfig：由配置算出的规则 ----------------------------------------


def test_fatigue_decay_only_at_180():
    """PRD §7 明写疲劳衰减只在 180 s 配置下输出。"""
    assert ProtocolConfig(duration_s=180).fatigue_decay_available is True
    assert ProtocolConfig(duration_s=120).fatigue_decay_available is False
    assert ProtocolConfig(duration_s=60).fatigue_decay_available is False


def test_minimum_valid_seconds_is_derived_not_configured():
    """下限由时长与比例算出。

    两个都可配的话它们迟早互相矛盾（180 s 配 200 s 的下限），而矛盾发生时没有任何
    一方是权威 —— 所以它是属性，不是字段。
    """
    assert ProtocolConfig(duration_s=180).minimum_valid_seconds == pytest.approx(126.0)
    assert ProtocolConfig(duration_s=60).minimum_valid_seconds == pytest.approx(42.0)
    assert "minimum_valid_seconds" not in {
        f.name for f in dataclasses.fields(ProtocolConfig)
    }


@pytest.mark.parametrize("fraction", [0.0, -0.1, 1.5])
def test_valid_fraction_out_of_range_is_refused(fraction):
    with pytest.raises(ConfigError, match="valid_fraction"):
        ProtocolConfig(valid_fraction=fraction)


def test_pause_threshold_must_be_positive():
    with pytest.raises(ConfigError, match="pause_threshold_s"):
        ProtocolConfig(pause_threshold_s=0.0)


def test_trim_steps_may_be_zero_but_not_negative():
    """0 是有意义的取值（不剔除），负数不是。"""
    assert ProtocolConfig(trim_steps_per_segment=0).trim_steps_per_segment == 0
    with pytest.raises(ConfigError, match="trim_steps_per_segment"):
        ProtocolConfig(trim_steps_per_segment=-1)


def test_protocol_config_is_frozen():
    """协议配置在会话开始时固定并写入元数据；中途可变会让元数据不再可信。"""
    config = ProtocolConfig()
    with pytest.raises(dataclasses.FrozenInstanceError):
        config.duration_s = 60


# --- AlgoConfig：低速预设的关系，而非数字 -------------------------------------


def test_low_speed_preset_is_longer_looser_and_forces_zaru():
    """PRD §7 的原话：更长窗口、更松阈值、强制 ZARU。

    断言方向而不是倍数 —— 倍数属于 RAY-203 的标定，方向不属于。
    """
    base = AlgoConfig()
    low = AlgoConfig.low_speed()
    assert low.preset == "low_speed"
    assert low.zupt_window_samples > base.zupt_window_samples, "窗口必须更长"
    assert low.zupt_acc_threshold > base.zupt_acc_threshold, "加速度阈值必须更松"
    assert low.zupt_gyr_threshold > base.zupt_gyr_threshold, "角速度阈值必须更松"
    assert low.force_zaru is True, "低速预设必须强制 ZARU"
    assert base.force_zaru is False


def test_default_preset_is_not_low_speed():
    assert AlgoConfig().preset == "default"


def test_unknown_preset_is_refused():
    with pytest.raises(ConfigError, match="preset"):
        AlgoConfig(preset="pathological")


@pytest.mark.parametrize(
    ("kwargs", "field_name"),
    [
        ({"zupt_window_samples": 0}, "zupt_window_samples"),
        ({"zupt_window_samples": -5}, "zupt_window_samples"),
        ({"zupt_acc_threshold": 0.0}, "zupt_acc_threshold"),
        ({"zupt_gyr_threshold": -1.0}, "zupt_gyr_threshold"),
    ],
)
def test_non_positive_algo_parameters_are_refused(kwargs, field_name):
    with pytest.raises(ConfigError, match=field_name):
        AlgoConfig(**kwargs)


# --- 版本化（FR-09）与往返 ---------------------------------------------------


@pytest.mark.parametrize(
    "config",
    [
        ProtocolConfig(),
        ProtocolConfig(duration_s=60, trim_steps_per_segment=2),
        AlgoConfig(),
        AlgoConfig.low_speed(),
    ],
    ids=["协议默认", "协议 60s", "算法默认", "算法低速"],
)
def test_snapshot_round_trip_is_exact(config):
    """"凭元数据精确复现算法输入"落到代码上就是这一条。"""
    restored = type(config).from_snapshot(config.snapshot())
    assert restored == config


def test_snapshot_carries_the_version():
    assert ProtocolConfig().snapshot()["version"] == CONFIG_VERSION
    assert AlgoConfig().snapshot()["version"] == CONFIG_VERSION


@pytest.mark.parametrize("cls", [ProtocolConfig, AlgoConfig])
def test_unknown_version_is_refused_not_best_effort(cls):
    """认不出的版本必须拒绝，不能按当前字段"尽力"解读。

    含义已经改变的字段会被静默当成现在的含义 —— 那正是复现出错却无人知晓的方式。
    没有这条，FR-09 的"版本化"就只是把一个数字存下来而已。
    """
    snapshot = cls().snapshot()
    snapshot["version"] = "0.9"
    with pytest.raises(ConfigError, match="只认识"):
        cls.from_snapshot(snapshot)


@pytest.mark.parametrize("cls", [ProtocolConfig, AlgoConfig])
def test_missing_field_in_snapshot_is_refused(cls):
    snapshot = cls().snapshot()
    dropped = next(iter(k for k in snapshot if k != "version"))
    del snapshot[dropped]
    with pytest.raises(ConfigError, match="缺少字段"):
        cls.from_snapshot(snapshot)


@pytest.mark.parametrize("cls", [ProtocolConfig, AlgoConfig])
def test_unknown_field_in_snapshot_is_refused(cls):
    """多出来的字段说明快照来自别的版本或别的结构，静默忽略会掩盖那件事。"""
    snapshot = cls().snapshot()
    snapshot["unexpected"] = 1
    with pytest.raises(ConfigError, match="未知字段"):
        cls.from_snapshot(snapshot)


def test_snapshot_covers_every_field():
    """快照必须含全部字段 —— 漏掉的那个正是复现失败的原因。"""
    for cls in (ProtocolConfig, AlgoConfig):
        assert set(cls().snapshot()) == {f.name for f in dataclasses.fields(cls)}


# --- SessionConfig：与 SessionMeta 的字段名对齐 -------------------------------


def test_session_snapshot_uses_the_mandatory_metadata_field_names():
    """键名必须与 PRD §6.1 的强制字段一致，调用方才不必记住谁写进哪里。"""
    from gait.contracts import MANDATORY_METADATA

    snapshot = SessionConfig().snapshot()
    assert set(snapshot) == {"protocol_config", "algo_params"}
    assert set(snapshot) <= set(MANDATORY_METADATA)


def test_session_config_round_trip():
    config = SessionConfig(
        protocol=ProtocolConfig(duration_s=120), algo=AlgoConfig.low_speed()
    )
    assert SessionConfig.from_snapshot(config.snapshot()) == config


@pytest.mark.parametrize("missing", ["protocol_config", "algo_params"])
def test_session_snapshot_missing_half_is_refused(missing):
    """分两次写就有一次被漏掉的可能，而漏掉的那半正是复现不出来的那半。"""
    snapshot = SessionConfig().snapshot()
    del snapshot[missing]
    with pytest.raises(ConfigError, match=missing):
        SessionConfig.from_snapshot(snapshot)


def test_session_config_holds_no_device_configuration():
    """R2：设备配置走 wt901，不在本仓库重复实现。

    这条测试守的是一个边界而不是一个行为：谁若在这里加回 DeviceConfig，就是把设备
    知识散回业务仓库，而引入 wt901 的初衷正是不这么做。
    """
    names = {f.name for f in dataclasses.fields(SessionConfig)}
    assert names == {"protocol", "algo"}
