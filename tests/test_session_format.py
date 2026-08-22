"""会话目录与元数据读写的测试。

三件事值得守：**FR-02 的防手滑闸真的会拦**、**往返相等**、**版本不匹配拒绝而不猜**。
其余的形状检查是配角。
"""

import dataclasses
import json
from datetime import UTC, datetime

import pytest

from gait.config import SessionConfig
from gait.contracts import CONTRACT_VERSION, SessionMeta
from gait.io.session import (
    META_FILENAME,
    RAW_DIRNAME,
    SessionFormatError,
    create_session,
    is_identity_free,
    list_sessions,
    new_session_id,
    new_subject_uuid,
    raw_path,
    read_meta,
    session_directory,
    write_meta,
)

SUBJECT = "6f1a2c8e-19a4-4f0e-9a3d-2b5c7d8e9f01"


def make_meta(session_id: str | None = None, **overrides) -> SessionMeta:
    config = SessionConfig().snapshot()
    values = {
        "session_id": session_id or new_session_id(),
        "created_at": "2026-08-22T06:00:00Z",
        "subject_uuid": SUBJECT,
        "scenario": "walk",
        "devices": {"L": {"mac": "AA:BB"}, "R": {"mac": "CC:DD"}},
        "config_snapshot": {"applied_writes": [{"register": 3, "value": 11}]},
        "calib_snapshot": {"L": {"bias": [0, 0, 0]}},
        "algo_version": "0.1.0",
        "algo_params": config["algo_params"],
        "sync_report": {"anchors": 4},
        "integrity_report": {"loss_rate": 0.001},
        "protocol_config": config["protocol_config"],
    }
    values.update(overrides)
    return SessionMeta(**values)


# --- session_id 的形状 -------------------------------------------------------


def test_new_session_id_is_utc_timestamp_plus_random_suffix():
    moment = datetime(2026, 8, 22, 6, 5, 4, tzinfo=UTC)
    session_id = new_session_id(now=moment)
    stamp, suffix = session_id.split("-")
    assert stamp == "20260822T060504Z"
    assert len(suffix) == 8


def test_two_ids_in_the_same_second_differ():
    """同秒内的两次会话不得相撞 —— 相撞会让 create_session 拒绝一次真实采集。"""
    moment = datetime(2026, 8, 22, 6, 5, 4, tzinfo=UTC)
    assert new_session_id(now=moment) != new_session_id(now=moment)


@pytest.mark.parametrize(
    "bad",
    [
        "住院号-2026-0413",
        "20260822T060504Z",
        "20260822-abcdefgh",
        "20260822T060504Z-ABCDEFGH",
        "20260822T060504Z-abc",
        "",
    ],
    ids=["含档案号", "缺后缀", "缺 T/Z", "大写十六进制", "后缀过短", "空"],
)
def test_malformed_session_id_is_refused(bad, tmp_path):
    with pytest.raises(SessionFormatError, match="session_id"):
        session_directory(tmp_path, bad)


def test_session_id_refusal_explains_that_directory_names_are_in_scope():
    """目录名也在 FR-02 范围内 —— 这条理由必须在错误信息里。"""
    with pytest.raises(SessionFormatError) as caught:
        session_directory("/tmp", "住院号-2026-0413")
    assert "FR-02" in str(caught.value)


# --- FR-02：防手滑闸 ---------------------------------------------------------


IDENTITY_LEAKS = [
    pytest.param({"patient_name": "张三"}, "patient_name", id="extra 里的姓名"),
    pytest.param({"姓名": "张三"}, "姓名", id="中文键"),
    pytest.param({"contact": {"phone": "13800000000"}}, "phone", id="嵌套一层"),
    pytest.param({"records": [{"id_card": "110101"}]}, "id_card", id="列表里的字典"),
    pytest.param({"档案号": "0413"}, "档案号", id="机构档案号"),
    pytest.param({"birth_date": "1950-01-01"}, "birth", id="出生日期"),
]


@pytest.mark.parametrize(("extra", "fragment"), IDENTITY_LEAKS)
def test_identity_plaintext_is_refused_before_it_reaches_disk(tmp_path, extra, fragment):
    """检查必须在写盘之前 —— 落盘之后再删，无法保证没被同步、备份或打包上传。"""
    meta = make_meta(extra=extra)
    directory = tmp_path / "s"
    directory.mkdir()
    with pytest.raises(SessionFormatError, match="FR-02"):
        write_meta(directory, meta)
    assert not (directory / META_FILENAME).exists(), "拒绝之后不得留下任何文件"


def test_identity_check_names_the_offending_path(tmp_path):
    """指出是哪一个键，否则调用方要自己翻整棵字典。"""
    meta = make_meta(extra={"contact": {"phone": "138"}})
    directory = tmp_path / "s"
    directory.mkdir()
    with pytest.raises(SessionFormatError, match=r"contact\.phone"):
        write_meta(directory, meta)


def test_clean_metadata_passes_the_identity_check():
    assert is_identity_free(
        {"subject_uuid": SUBJECT, "devices": {"L": {"mac": "AA"}}, "notes": "步态平稳"}
    )


def test_identity_check_is_documented_as_fallible():
    """把绕过方式写成测试，是为了让这条限制无法被悄悄遗忘。

    一个叫 `x` 的字段装着姓名会通过。这不是缺陷，是这道闸的边界：它拦疏忽，
    不拦刻意。声称它是隐私保证才是错的。
    """
    assert is_identity_free({"x": "张三"}) is True


def test_subject_uuid_is_random_not_derived():
    """uuid4 而非基于姓名的 uuid5 —— 后者可由已知输入反算，等于把身份带进本地文件。"""
    first, second = new_subject_uuid(), new_subject_uuid()
    assert first != second
    assert len(first) == 36


# --- 目录布局 ----------------------------------------------------------------


def test_create_session_lays_out_the_directory(tmp_path):
    meta = make_meta()
    directory = create_session(tmp_path, meta)
    assert directory.name == meta.session_id
    assert (directory / META_FILENAME).is_file()
    assert (directory / RAW_DIRNAME).is_dir()


def test_raw_paths_are_named_per_foot(tmp_path):
    session_id = new_session_id()
    left = raw_path(tmp_path, session_id, "L")
    right = raw_path(tmp_path, session_id, "R")
    assert left.parent.name == RAW_DIRNAME
    assert left != right
    with pytest.raises(SessionFormatError, match="foot"):
        raw_path(tmp_path, session_id, "left")


def test_existing_session_directory_is_never_overwritten(tmp_path):
    """覆盖会毁掉一份不可再生的数据。"""
    meta = make_meta()
    create_session(tmp_path, meta)
    with pytest.raises(SessionFormatError, match="已存在"):
        create_session(tmp_path, meta)


def test_list_sessions_sorts_by_the_id_not_by_mtime(tmp_path):
    """复制、同步、恢复都会改 mtime，而它们都不改变会话真正发生的时刻。"""
    early = new_session_id(now=datetime(2026, 1, 1, tzinfo=UTC))
    late = new_session_id(now=datetime(2026, 12, 31, tzinfo=UTC))
    create_session(tmp_path, make_meta(session_id=late))
    create_session(tmp_path, make_meta(session_id=early))
    (tmp_path / "not-a-session").mkdir()
    assert list_sessions(tmp_path) == [early, late]


def test_list_sessions_on_missing_root_is_empty(tmp_path):
    assert list_sessions(tmp_path / "nope") == []


# --- 往返与版本 --------------------------------------------------------------


def test_meta_round_trip_is_exact(tmp_path):
    """「任一历史会话可凭元数据精确复现算法输入」落到代码上的另一半。"""
    meta = make_meta()
    directory = create_session(tmp_path, meta)
    assert read_meta(directory) == meta


def test_round_trip_preserves_the_config_snapshots(tmp_path):
    """配置往返在 algo-protocol-config 里测过，这里测它经过文件之后仍然成立。"""
    meta = make_meta()
    directory = create_session(tmp_path, meta)
    restored = read_meta(directory)
    assert SessionConfig.from_snapshot(
        {
            "protocol_config": restored.protocol_config,
            "algo_params": restored.algo_params,
        }
    ) == SessionConfig()


def test_meta_is_written_atomically(tmp_path):
    """不留半份文件：写完之后目录里不该有临时文件。"""
    meta = make_meta()
    directory = create_session(tmp_path, meta)
    leftovers = [p.name for p in directory.iterdir() if p.name.startswith(".")]
    assert leftovers == []


def test_written_json_is_utf8_and_readable_by_a_plain_reader(tmp_path):
    """元数据要能被任何 JSON 工具读，不依赖本仓库的代码 —— 归档格式的基本要求。"""
    meta = make_meta(notes="拖步、小碎步")
    directory = create_session(tmp_path, meta)
    payload = json.loads((directory / META_FILENAME).read_text(encoding="utf-8"))
    assert payload["notes"] == "拖步、小碎步"
    assert payload["subject_uuid"] == SUBJECT


def test_written_keys_are_exactly_the_declared_fields(tmp_path):
    """写出的键集与 SessionMeta 的字段一一对应，不多不少。

    多出来意味着有东西绕过了契约进入文件；少了意味着复现时缺料。
    """
    meta = make_meta()
    directory = create_session(tmp_path, meta)
    payload = json.loads((directory / META_FILENAME).read_text(encoding="utf-8"))
    assert set(payload) == {f.name for f in dataclasses.fields(SessionMeta)}


def test_unknown_contract_version_is_refused_not_reinterpreted(tmp_path):
    """R2 把 gyr 从 deg/s 改成 rad/s 时升过契约版本。

    把 1.0 的会话按 1.1 读回，数值会静默地差 57.3 倍 —— 这正是"看似正常的错误
    数值"，比崩溃危险得多。
    """
    meta = make_meta()
    directory = create_session(tmp_path, meta)
    path = directory / META_FILENAME
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["contract_version"] = "1.0"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(SessionFormatError, match="只认识"):
        read_meta(directory)


def test_current_contract_version_is_what_gets_written(tmp_path):
    meta = make_meta()
    directory = create_session(tmp_path, meta)
    payload = json.loads((directory / META_FILENAME).read_text(encoding="utf-8"))
    assert payload["contract_version"] == CONTRACT_VERSION


def test_unknown_field_in_file_is_refused(tmp_path):
    meta = make_meta()
    directory = create_session(tmp_path, meta)
    path = directory / META_FILENAME
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["surprise"] = 1
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(SessionFormatError, match="未知字段"):
        read_meta(directory)


def test_missing_mandatory_field_surfaces_as_a_contract_failure(tmp_path):
    """PRD §6.1 的强制字段被抹掉时，读取要失败并说明是契约不满足。"""
    meta = make_meta()
    directory = create_session(tmp_path, meta)
    path = directory / META_FILENAME
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["algo_params"] = {}
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(SessionFormatError, match="契约"):
        read_meta(directory)


def test_corrupt_json_and_missing_file_report_differently(tmp_path):
    """两种故障需要不同的处置，不能报成同一句话。"""
    directory = tmp_path / "s"
    directory.mkdir()
    with pytest.raises(SessionFormatError, match="不存在"):
        read_meta(directory)
    (directory / META_FILENAME).write_text("{ not json", encoding="utf-8")
    with pytest.raises(SessionFormatError, match="不是合法 JSON"):
        read_meta(directory)
