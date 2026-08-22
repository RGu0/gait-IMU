"""`gait.cloud.tenancy` 的预配置接入与租户边界。

验收标准两条：**AC-17 租户隔离**；**未绑定模块不可用于正式会话**。

其余几组守的是让那两条不被绕过的机制：

* 地址归一化 —— 不归一化，一个大小写不同的绑定就会被判成"未绑定"，而那是硬拦截，
  现场表现是"设备明明是对的，就是开不了测试"。
* 地址**种类**要一起存 —— macOS 给的是机器相关的 UUID 而不是 MAC，种类不符要报一个
  说得出原因的错，而不是静静地匹配失败。
* 凭据不落文件 —— 会话包会被整个打包上传，配置目录会被备份、同步、排障时被拷走。
* 操作员不能改绑定 —— 只有服务方下发的、修订号严格递增的设备组才生效。
"""

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from gait.cloud.tenancy import (
    ADDRESS_MAC,
    ADDRESS_PLATFORM_UUID,
    PROVISION_SCHEMA_VERSION,
    AccessError,
    AccessStore,
    DeviceBinding,
    DeviceGroup,
    InMemorySecretStore,
    NotProvisioned,
    TenantMismatch,
    TerminalIdentity,
    UnboundModule,
    assert_bound,
    assert_tenant,
    normalize_address,
    session_stamp,
)
from gait.io.session import is_identity_free

TOKEN = "gait-terminal-token-4f9c2a7e5b1d8306"
LEFT = "AA:BB:CC:DD:EE:01"
RIGHT = "AA:BB:CC:DD:EE:02"


def make_group(*, revision: int = 1, left: str = LEFT, right: str = RIGHT) -> DeviceGroup:
    bindings = []
    for foot, spelling, calibration in (("L", left, "cal-L-7"), ("R", right, "cal-R-7")):
        address, kind = normalize_address(spelling)
        bindings.append(
            DeviceBinding(
                foot=foot, address=address, address_kind=kind, calibration_id=calibration
            )
        )
    return DeviceGroup(
        group_id="dg-001",
        revision=revision,
        issued_at="2026-08-22T00:00:00+00:00",
        bindings=tuple(bindings),
    )


def make_identity(**kwargs) -> TerminalIdentity:
    return TerminalIdentity(
        tenant_id=kwargs.get("tenant_id", "tenant-alpha"),
        terminal_id=kwargs.get("terminal_id", "term-01"),
        api_base_url=kwargs.get("api_base_url", "https://api.example.com"),
        device_group=kwargs.get("device_group", make_group()),
    )


@pytest.fixture
def store(tmp_path: Path) -> AccessStore:
    access = AccessStore(tmp_path / "config", InMemorySecretStore())
    access.install(make_identity(), token=TOKEN)
    return access


# ── 验收标准：未绑定模块不可用于正式会话 ──────────────────────────────────────


def test_the_bound_pair_is_accepted_however_the_addresses_are_spelled(store):
    """归一化必须在比对端也生效。

    不生效的表现最难查：设备明明是对的，就是开不了测试，而错误信息说的是"未绑定"。
    """
    identity = store.load()
    resolved = assert_bound(identity, {"L": "aa:bb:cc:dd:ee:01", "R": "AA-BB-CC-DD-EE-02"})

    assert resolved["L"].calibration_id == "cal-L-7"
    assert resolved["R"].calibration_id == "cal-R-7"


def test_a_module_outside_the_device_group_is_refused(store):
    identity = store.load()

    with pytest.raises(UnboundModule, match="不在本终端的设备组里"):
        assert_bound(identity, {"L": LEFT, "R": "AA:BB:CC:DD:EE:99"})


def test_the_two_bound_modules_paired_to_the_wrong_feet_are_refused(store):
    """左右在**配对**这一步接反会被抓住。

    边界要说清楚：这抓的是"哪个模块被当作左脚"这一步配错，不是"正确的模块被戴到了
    错误的脚上"。后者在数据里的表现见 RAY-260 —— 那是位置法在数学上不可判定的那个
    问题，本模块**不解决它**，也不该被当成解决了它。
    """
    identity = store.load()

    with pytest.raises(UnboundModule, match="不在本终端的设备组里"):
        assert_bound(identity, {"L": RIGHT, "R": LEFT})


def test_a_single_foot_cannot_start_a_formal_session(store):
    """单足会话不是本产品的形态 —— PRD 的核心指标是双足的。"""
    identity = store.load()

    with pytest.raises(UnboundModule, match=r"缺少 \['R'\]"):
        assert_bound(identity, {"L": LEFT})


def test_the_gate_raises_rather_than_returning_a_boolean(store):
    """布尔值可以被忽略，而忽略它的后果是一次用错模块（因而用错标定）的采集。

    那次采集的数据看起来完全正常 —— 这正是它必须抛错的理由。
    """
    identity = store.load()

    with pytest.raises(UnboundModule):
        assert_bound(identity, {"L": LEFT, "R": "AA:BB:CC:DD:EE:99"})


def test_a_platform_uuid_against_a_mac_binding_says_why(store):
    """**记录一条来自 bleak 源码的约束。**

    bleak：`The Bluetooth address of the device on this machine (UUID on macOS).`
    注意 "on this machine" —— macOS 上那个标识符还是**机器相关**的。

    所以种类不符时要给专门的错误信息：那多半不是"模块没绑定"，而是这台机器的平台与
    绑定时不同。说成"未绑定"会把人引向完全错误的方向 —— 而最省事的错误反应，是把这
    道硬拦截放松。
    """
    identity = store.load()

    with pytest.raises(UnboundModule, match="macOS"):
        assert_bound(
            identity,
            {
                "L": "B7A1D0E2-3F4C-5A6B-8C9D-0E1F2A3B4C5D",
                "R": "C8B2E1F3-4A5D-6B7C-9D0E-1F2A3B4C5D6E",
            },
        )


# ── 验收标准：AC-17 租户隔离 ──────────────────────────────────────────────────


def test_a_session_from_another_tenant_is_refused(store):
    identity = store.load()

    with pytest.raises(TenantMismatch, match="tenant-beta"):
        assert_tenant(identity, {"tenant_id": "tenant-beta", "terminal_id": "t"})


def test_a_session_from_another_terminal_of_the_same_tenant_is_refused(store):
    """同租户的其他终端的数据也不该由本终端代传 —— 那会让上传来源失去意义。"""
    identity = store.load()

    with pytest.raises(TenantMismatch, match="term-02"):
        assert_tenant(identity, {"tenant_id": "tenant-alpha", "terminal_id": "term-02"})


def test_a_session_without_a_tenant_is_refused_rather_than_assumed_to_be_ours(store):
    """不默认它属于本租户 —— 那正是串号发生的方式。

    这道检查防的不是攻击（攻击者可以改客户端），是**串号**：一台终端读到了另一个
    租户的会话目录（共享盘、恢复的备份、拷错的目录），然后把它当自己的数据上传。
    """
    identity = store.load()

    with pytest.raises(TenantMismatch, match="没有 tenant_id"):
        assert_tenant(identity, {})


def test_our_own_session_passes(store):
    identity = store.load()

    assert_tenant(identity, {"tenant_id": "tenant-alpha", "terminal_id": "term-01"})


def test_a_session_without_a_terminal_id_is_accepted_within_the_tenant(store):
    """终端标识可选：租户对得上就够了。它是隔离的粒度，终端只是来源的说明。"""
    identity = store.load()

    assert_tenant(identity, {"tenant_id": "tenant-alpha"})


# ── 凭据不落文件 ──────────────────────────────────────────────────────────────


def test_the_token_never_appears_in_the_provisioning_file(store):
    """会话包会被整个打包上传，配置目录会被备份、同步、排障时被拷走。

    一个躺在文件里的长期凭据迟早跟着某一份拷贝出门，而且**没有任何一步会报错**。
    """
    text = store.provisioning_path.read_text(encoding="utf-8")

    assert TOKEN not in text
    assert store.token() == TOKEN


def test_writing_a_provisioning_file_that_contains_the_token_is_refused(tmp_path):
    """让常见的错误当场失败 —— 与 `io/session.py` 对身份明文那道检查同一种设计。"""
    access = AccessStore(tmp_path / "config", InMemorySecretStore())
    leaky = make_identity(terminal_id=f"term-{TOKEN}")

    with pytest.raises(AccessError, match="凭据只进密钥库"):
        access.install(leaky, token=TOKEN)


def test_the_provisioning_file_is_owner_only_on_posix_and_not_on_windows(store):
    """**这条测试记录的是一个平台缺口，不是一个特性。**

    写文件时会 `chmod 0o600`，但在 **Windows 上那是空操作** —— Python 的 `os.chmod`
    在 Windows 上只认只读位。windows CI 直接抓到过：期望 `0o600`，实际 `0o666`。

    而 Windows 正是目标平台。所以承诺必须说准：**承重的是"文件里没有秘密"，不是文件
    权限。** 权限只是 POSIX 上顺手加的一层；Windows 上的实际保护来自把配置放在按用户
    隔离的目录（`%LOCALAPPDATA%` 默认带那样的 ACL），那是安装程序的职责。

    这条测试按平台断言不同的事实，好让"Windows 上没有这层防护"这件事**留在代码里**，
    而不是被一个 skip 抹掉。
    """
    mode = store.provisioning_path.stat().st_mode & 0o777

    if os.name == "nt":
        assert mode != 0o600  # 缺口就在这里，不假装它不存在
    else:
        assert mode == 0o600


def test_a_missing_secret_does_not_fall_back_to_the_file(tmp_path):
    access = AccessStore(tmp_path / "config", InMemorySecretStore())
    access.install(make_identity(), token=TOKEN)
    access.secrets.delete_secret("tenant-alpha:term-01")

    with pytest.raises(NotProvisioned, match="密钥库里没有"):
        access.token()


def test_an_unprovisioned_terminal_says_so_clearly(tmp_path):
    access = AccessStore(tmp_path / "empty", InMemorySecretStore())

    with pytest.raises(NotProvisioned, match="还没有被预配置"):
        access.load()


# ── 设备组变更只走服务方 ──────────────────────────────────────────────────────


def test_the_module_has_no_bind_operation_for_the_operator():
    """**这条测试守的是一个"不存在"。**

    现场临时换模块单次看无害，但它让"这台终端用的是哪两个模块"失去唯一答案，而出厂
    标定正是按模块绑的 —— 换模块不换标定，数据会静静地错下去。

    所以唯一的改法是 `apply_device_group`，它要求服务方下发且修订号严格递增。
    """
    from gait.cloud import tenancy

    assert not hasattr(tenancy.AccessStore, "bind")
    assert not hasattr(tenancy.AccessStore, "set_device_group")
    assert hasattr(tenancy.AccessStore, "apply_device_group")


def test_a_replayed_revision_is_refused(store):
    with pytest.raises(AccessError, match="严格递增"):
        store.apply_device_group(make_group(revision=1, right="AA:BB:CC:DD:EE:03"), reason="重放")


def test_a_rolled_back_revision_is_refused(store):
    store.apply_device_group(make_group(revision=5, right="AA:BB:CC:DD:EE:03"), reason="换件")

    with pytest.raises(AccessError, match="严格递增"):
        store.apply_device_group(make_group(revision=2), reason="回滚")


def test_a_change_without_a_reason_is_refused(store):
    """没有理由的记录审计不出任何东西。"""
    with pytest.raises(AccessError, match="必须写明理由"):
        store.apply_device_group(make_group(revision=2), reason="")


def test_a_change_records_both_sides_of_the_swap(store):
    """只记"变了"说明不了换掉的是哪一个模块。"""
    store.apply_device_group(
        make_group(revision=2, right="AA:BB:CC:DD:EE:03"), reason="右模块进水返修"
    )
    entries = store.audit()

    assert len(entries) == 1
    entry = entries[0]
    assert entry.from_revision == 1 and entry.to_revision == 2
    assert {item["address"] for item in entry.before} == {LEFT, RIGHT}
    assert {item["address"] for item in entry.after} == {LEFT, "AA:BB:CC:DD:EE:03"}
    assert "进水" in entry.reason


def test_the_old_module_is_refused_after_a_change(store):
    """变更之后旧模块必须被拒 —— 否则"换过了"这件事在采集端没有效果。"""
    store.apply_device_group(
        make_group(revision=2, right="AA:BB:CC:DD:EE:03"), reason="右模块返修"
    )
    identity = store.load()

    with pytest.raises(UnboundModule):
        assert_bound(identity, {"L": LEFT, "R": RIGHT})
    assert_bound(identity, {"L": LEFT, "R": "AA:BB:CC:DD:EE:03"})


def test_the_audit_survives_a_restart(tmp_path):
    access = AccessStore(tmp_path / "config", InMemorySecretStore())
    access.install(make_identity(), token=TOKEN)
    access.apply_device_group(make_group(revision=2, right="AA:BB:CC:DD:EE:03"), reason="换件")

    reopened = AccessStore(tmp_path / "config", InMemorySecretStore())
    assert len(reopened.audit()) == 1


def test_an_empty_audit_is_an_empty_list(store):
    assert store.audit() == []


def test_a_corrupted_audit_line_is_reported_not_skipped(store):
    """审计记录损坏要报出来 —— 静默跳过等于让审计在最需要它的时候消失。"""
    store.apply_device_group(make_group(revision=2), reason="换件")
    store.audit_path.write_text('{"at": "broken"}\n', encoding="utf-8")

    with pytest.raises(AccessError, match="审计记录损坏"):
        store.audit()


def test_the_token_survives_a_device_group_change(store):
    """变更设备组要重写预配置文件，凭据不能在这一步丢掉。"""
    store.apply_device_group(make_group(revision=2), reason="换件")

    assert store.token() == TOKEN
    assert TOKEN not in store.provisioning_path.read_text(encoding="utf-8")


# ── 地址归一化 ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "spelling",
    ["AA:BB:CC:DD:EE:01", "aa:bb:cc:dd:ee:01", "aa-bb-cc-dd-ee-01", "AABBCCDDEE01", "aabbccddee01"],
)
def test_every_spelling_of_a_mac_normalizes_to_the_same_address(spelling):
    assert normalize_address(spelling) == (LEFT, ADDRESS_MAC)


def test_a_platform_uuid_normalizes_to_lowercase_canonical_form():
    address, kind = normalize_address("B7A1D0E2-3F4C-5A6B-8C9D-0E1F2A3B4C5D")

    assert kind == ADDRESS_PLATFORM_UUID
    assert address == "b7a1d0e2-3f4c-5a6b-8c9d-0e1f2a3b4c5d"


def test_an_unrecognised_address_keeps_its_text_rather_than_being_guessed():
    """猜一个种类比承认不认识更糟 —— 猜错会让种类检查给出误导性的错误信息。"""
    address, kind = normalize_address("some-vendor-handle")

    assert address == "some-vendor-handle"
    assert kind == ADDRESS_PLATFORM_UUID


def test_an_empty_address_is_refused():
    with pytest.raises(AccessError, match="不能为空"):
        normalize_address("   ")


# ── 设备组的形状 ──────────────────────────────────────────────────────────────


def test_a_device_group_must_bind_exactly_both_feet():
    """少一只脚的设备组会让双足指标在会话中途才失败。"""
    address, kind = normalize_address(LEFT)
    only_left = (DeviceBinding(foot="L", address=address, address_kind=kind, calibration_id="c"),)

    with pytest.raises(AccessError, match="恰好绑定左右两只脚"):
        DeviceGroup(
            group_id="dg", revision=1, issued_at="2026-08-22T00:00:00+00:00", bindings=only_left
        )


def test_both_feet_bound_to_one_address_is_refused():
    """它在采集时会表现为两只脚的数据完全相同 —— 那时才发现就晚了。"""
    with pytest.raises(AccessError, match="同一个地址"):
        make_group(left=LEFT, right=LEFT)


def test_a_binding_without_a_calibration_id_is_refused():
    """标定按模块绑定，缺了它就无从知道这个模块该用哪套参数。"""
    address, kind = normalize_address(LEFT)

    with pytest.raises(AccessError, match="出厂标定"):
        DeviceBinding(foot="L", address=address, address_kind=kind, calibration_id="")


def test_a_revision_below_one_is_refused():
    with pytest.raises(AccessError, match="修订号至少为 1"):
        make_group(revision=0)


def test_an_unknown_foot_is_refused():
    address, kind = normalize_address(LEFT)

    with pytest.raises(AccessError, match="foot"):
        DeviceBinding(foot="X", address=address, address_kind=kind, calibration_id="c")


# ── TLS ───────────────────────────────────────────────────────────────────────


def test_a_plain_http_endpoint_is_refused():
    """不给降级到 http 的口子：那种口子在开发机上很方便，也正因为方便会被带进安装包。"""
    with pytest.raises(AccessError, match="必须是 https"):
        make_identity(api_base_url="http://api.example.com")


def test_an_https_url_without_a_host_is_refused():
    with pytest.raises(AccessError, match="缺少主机名"):
        make_identity(api_base_url="https://")


# ── 归属信息 ──────────────────────────────────────────────────────────────────


def test_the_session_stamp_passes_the_identity_free_check(store):
    """归属信息要能写进 `SessionMeta.extra`，而那里有 FR-02 的身份明文检查。

    记的是租户、终端、设备组 —— 三者都是机构侧的标识，不是人的标识。
    """
    stamp = session_stamp(store.load())

    assert is_identity_free(stamp)


def test_the_session_stamp_records_which_calibration_was_used(store):
    """一份数据用的是哪套出厂标定，事后必须查得出来。"""
    stamp = session_stamp(store.load())

    assert stamp["calibration_ids"] == {"L": "cal-L-7", "R": "cal-R-7"}
    assert stamp["device_group_revision"] == 1


def test_the_stamp_and_the_tenant_check_agree(store):
    """盖的章必须过得了自己的检查 —— 否则本终端传不了自己采的数据。"""
    identity = store.load()

    assert_tenant(identity, session_stamp(identity))


def test_the_session_stamp_is_plain_json_types(store):
    stamp = session_stamp(store.load())

    assert json.loads(json.dumps(stamp, ensure_ascii=False)) == stamp


# ── 预配置文件 ────────────────────────────────────────────────────────────────


def test_provisioning_round_trips(store):
    identity = store.load()

    assert identity.tenant_id == "tenant-alpha"
    assert identity.device_group.binding("L").address == LEFT
    assert identity.schema_version == PROVISION_SCHEMA_VERSION


def test_a_provisioning_file_from_a_future_schema_is_refused(store):
    payload = json.loads(store.provisioning_path.read_text(encoding="utf-8"))
    payload["schema_version"] = "9.9"
    store.provisioning_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(AccessError, match="结构版本"):
        store.load()


def test_an_unparsable_provisioning_file_is_reported(store):
    store.provisioning_path.write_text("{not json", encoding="utf-8")

    with pytest.raises(AccessError, match="无法解析"):
        store.load()


def test_a_provisioning_file_missing_a_field_says_which(store):
    payload = json.loads(store.provisioning_path.read_text(encoding="utf-8"))
    del payload["tenant_id"]
    store.provisioning_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(AccessError, match="tenant_id"):
        store.load()


def test_installing_without_a_token_is_refused(tmp_path):
    access = AccessStore(tmp_path / "config", InMemorySecretStore())

    with pytest.raises(AccessError, match="凭据不能为空"):
        access.install(make_identity(), token="")


def test_the_issued_at_of_a_group_is_kept_verbatim(store):
    """下发时刻由服务方给，终端不重新生成 —— 重新生成的那个记的是"我什么时候收到的"。"""
    identity = store.load()

    assert identity.device_group.issued_at == "2026-08-22T00:00:00+00:00"


def test_the_audit_timestamp_is_the_moment_of_application(store):
    moment = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)
    store.apply_device_group(make_group(revision=2), reason="换件", now=moment)

    assert store.audit()[0].at == moment.isoformat()
