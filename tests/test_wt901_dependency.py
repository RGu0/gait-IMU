"""wt901 依赖的存在性与表面契约。

这个文件守的不是 wt901 的正确性 —— 那是它自己的测试套件的职责。这里只回答两个
问题，而这两个问题都只有在依赖真的装上之后才有答案：

1. **它装上了吗。** 一个钉在 git commit 上的依赖，可能因为仓库改名、commit 被
   回收、网络策略变化而装不上；而"装不上"和"我们还没用它"在代码里长得一样。
2. **我们要适配的那几个名字还在吗。** wt901 的 CHANGELOG 明确只对
   ``wt901`` 命名空间里的名字负责。换 pin 时若某个名字没了，应当在这里以一条
   清楚的失败暴露出来，而不是等到 ``device/`` 适配层运行时才炸。
"""

from importlib import metadata

import pytest
import wt901

#: gait 的 `device/` 适配层将要依赖的公开名字。列在这里，换 pin 时它就是清单。
ADAPTER_SURFACE = (
    "ImuSample",  # 单帧样本：t_host + accel/gyro/euler + raw 计数
    "FrameDecoder",  # 0x55 帧同步与解包
    "BleTransport",  # BLE 传输
    "scan",  # 设备发现
    "merge",  # 多设备合流
    "RegisterAccess",  # 寄存器读写与配置事务
    "Settings",  # 输出速率与带宽
    "Battery",  # 自检要读的电量
)


#: `pyproject.toml` 的 `[tool.uv.sources]` 钉的 tag，去掉前缀 `v`。
#: 自 RAY-334 起依赖按 tag 而非 40 位 SHA 钉，这个常量是那个 tag 在测试侧的回声。
PINNED_VERSION = "0.3.0"


def test_wt901_is_installed_at_the_pinned_version():
    """装上的是不是我们钉的那一版。

    这条与 `ADAPTER_SURFACE` 守的不是一回事：那些名字在多个版本里都在，所以
    「表面契约全过」并不能说明装对了版本。按 tag 钉之后尤其要守——tag 是可以被
    上游移动的引用（`uv.lock` 里记着解析出的 commit，但重新 `uv lock` 会跟着
    移动的 tag 走），而 commit 不会。**这条挂了先看 tag 是不是被挪过。**
    """
    assert metadata.version("wt901") == PINNED_VERSION


@pytest.mark.parametrize("name", ADAPTER_SURFACE)
def test_adapter_surface_is_present(name):
    assert hasattr(wt901, name), f"wt901 不再导出 {name}，适配层会失去依托"


def test_imu_sample_carries_host_time_and_raw_counts():
    """两个字段决定了适配层能不能工作，值得单独钉住。

    ``t_host`` 是主机接收时刻 —— 器件不提供时间戳，gait 的 `sync/timebase`
    正是建立在它之上；``raw`` 是未换算的 int16 计数，gait 的 `RawFrame` 与饱和
    判定要用它。任何一个消失，适配层就没法在不改契约的前提下写出来。
    """
    fields = wt901.ImuSample.__dataclass_fields__
    assert "t_host" in fields
    assert "raw" in fields
    assert "accel" in fields
    assert "gyro" in fields


def test_gyro_is_si_which_is_why_the_contract_moved_to_rad_s():
    """R2 的依据：wt901 的角速度是 rad/s。

    单位写在字段的文档字符串里，而 dataclass 不保留它们，所以只能读模块源码。
    这不优雅，但它守的东西是实的：R2 把 gait 的契约从 deg/s 改成 rad/s，理由就是
    与 wt901 对齐。哪天它改回去而没人发现，gait 全链路会静默地差 57.3 倍 ——
    一个不会报错、只会出错数的错误。这条测试是那个假设的看门人。
    """
    import inspect

    import wt901.models

    source = inspect.getsource(wt901.models)
    assert "rad/s" in source, "wt901 不再声明角速度为 rad/s，R2 的依据需重新审视"
