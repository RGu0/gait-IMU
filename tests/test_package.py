"""`gait.cloud.package` 的会话打包。

PRD §6.1：会话结束后打包（压缩 + 摘要校验）后台上传。

这个文件里最重要的一组测试是**字节可复现**。上传的幂等键由归档摘要导出，所以同一个
会话目录必须每次打出完全相同的字节 —— 否则重试算出不同的键，服务端把它当成新的上传，
断点续传与幂等去重一起失效，而且**不会有任何东西报错**：每次重试都"成功"，只是每次
都上传一份新的。

标准库默认就会踩这个坑（`gzip.compress` 不传 mtime 写当前时间；`tarfile.gettarinfo`
取真实 mtime/uid/uname），所以那几条测试守的是具体的踩法，不是抽象的性质。
"""

import gzip
import hashlib
import io
import struct
import tarfile
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path

import pytest

from gait.cloud.package import (
    CODECS,
    DEFAULT_CODEC,
    PACKAGE_FORMAT_VERSION,
    PackageError,
    build_package,
    extract_package,
    package_session,
    reassemble,
    verify_archive,
    verify_parts,
)
from gait.contracts import SessionMeta
from gait.io.session import (
    META_FILENAME,
    create_session,
    new_session_id,
    new_subject_uuid,
    raw_path,
)


def make_meta(session_id: str) -> SessionMeta:
    return SessionMeta(
        session_id=session_id,
        created_at=datetime.now(UTC).isoformat(),
        subject_uuid=new_subject_uuid(),
        scenario="walk",
        devices={"L": {"mac": "aa:bb:cc:dd:ee:01"}, "R": {"mac": "aa:bb:cc:dd:ee:02"}},
        config_snapshot={"fs": 200, "range": "2g"},
        calib_snapshot={"bias": [0.0, 0.0, 0.0]},
        algo_version="0.1.0",
        algo_params={"zupt_window": 40},
        sync_report={"fs": 200.3},
        integrity_report={"grade": "normal"},
        protocol_config={"duration_s": 1800},
    )


@pytest.fixture
def session(tmp_path: Path) -> tuple[Path, str]:
    """一个有 meta 与双足原始数据的会话目录。返回 `(root, session_id)`。"""
    root = tmp_path / "sessions"
    root.mkdir()
    session_id = new_session_id()
    create_session(root, make_meta(session_id))
    for foot, filler in (("L", b"\x55\x51"), ("R", b"\x55\x52")):
        raw_path(root, session_id, foot).write_bytes(filler * 5000)
    return root, session_id


# ── 字节可复现 ────────────────────────────────────────────────────────────────


def test_the_same_directory_always_packs_to_the_same_bytes(session):
    """幂等键成立的前提。

    不成立的后果特别隐蔽：每次重试都会"成功"，只是每次都在服务端多留一份副本，而
    客户端与服务端都不会报任何错。
    """
    root, session_id = session
    first = build_package(root / session_id)
    second = build_package(root / session_id)

    assert first.archive == second.archive
    assert first.manifest.archive_sha256 == second.manifest.archive_sha256
    assert first.manifest.idempotency_key == second.manifest.idempotency_key


def test_gzip_would_embed_the_current_time_if_the_mtime_were_not_pinned():
    """**记录一个具体的踩法。** `gzip.compress(data, 6)` 把当前时间写进头部。

    gzip 头部第 5~8 字节是 MTIME。不钉它，同一份数据隔一秒压出来就是不同的字节，
    摘要随之改变 —— 幂等键跟着变。
    """
    payload = b"gait" * 1000
    (unpinned,) = struct.unpack("<I", gzip.compress(payload, 6)[4:8])
    (pinned,) = struct.unpack("<I", gzip.compress(payload, 6, mtime=0)[4:8])

    assert unpinned != 0  # 当前时间
    assert pinned == 0
    assert gzip.compress(payload, 6, mtime=1000) != gzip.compress(payload, 6, mtime=1001)


def test_the_archive_carries_no_mtime_uid_or_username(session):
    """**记录另一个踩法，它同时是一条隐私要求。**

    `tarfile.gettarinfo()` 从真实文件取 mtime、uid、gid、uname —— 那不只让归档随环境
    变，还会把**操作员的系统用户名**写进每一个上传的包。`io/session.py` 费了很大力气
    把身份明文挡在 `meta.json` 之外（FR-02），tar 头部却会从后门把它放进去。

    所以每个成员的属性都是手工钉死的，这条测试逐字段守它。
    """
    root, session_id = session
    package = build_package(root / session_id)
    raw = gzip.decompress(package.archive)

    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as archive:
        members = archive.getmembers()
    assert members
    for info in members:
        assert info.mtime == 0
        assert info.uid == 0 and info.gid == 0
        assert info.uname == "" and info.gname == ""
        assert info.mode == 0o644


def test_members_are_ordered_by_name_not_by_filesystem_walk(session, tmp_path):
    """遍历顺序不保证稳定，不排序的话同一个目录在两台机器上打出不同的归档。

    这里通过**换一个创建顺序**重建同一份内容来检验：内容相同，字节就必须相同。
    """
    root, session_id = session
    original = build_package(root / session_id)

    rebuilt_root = tmp_path / "rebuilt"
    rebuilt_root.mkdir()
    target = rebuilt_root / session_id
    (target / "raw").mkdir(parents=True)
    # 刻意反着写：先右后左，最后才写 meta。
    for name in ("raw/right.raw", "raw/left.raw", META_FILENAME):
        (target / name).write_bytes((root / session_id / name).read_bytes())

    assert build_package(target).archive == original.archive


def test_changing_one_byte_changes_the_idempotency_key(session):
    """反过来的要求：内容变了，键必须变。否则新数据会被服务端当成重复上传丢掉。"""
    root, session_id = session
    before = build_package(root / session_id)

    path = raw_path(root, session_id, "L")
    data = bytearray(path.read_bytes())
    data[len(data) // 2] ^= 0x01
    path.write_bytes(bytes(data))
    after = build_package(root / session_id)

    assert after.archive != before.archive
    assert after.manifest.idempotency_key != before.manifest.idempotency_key


@pytest.mark.parametrize("codec", CODECS)
def test_every_codec_is_deterministic(codec, session):
    """三种压缩方式都必须可复现 —— 换 codec 不该让幂等失效。"""
    root, session_id = session
    first = build_package(root / session_id, codec=codec)
    second = build_package(root / session_id, codec=codec)

    assert first.archive == second.archive


# ── 清单 ──────────────────────────────────────────────────────────────────────


def test_the_manifest_records_the_codec_so_old_packages_stay_readable(session):
    """默认值以后改了，队列里等着重传的旧包必须仍然解得开。

    codec 写进清单，这件事就是自动成立的；靠"默认值"隐含约定则会在改默认值的那天
    悄悄坏掉一批待传的包。
    """
    root, session_id = session
    package = build_package(root / session_id, codec="lzma")

    assert package.manifest.codec == "lzma"
    # 用清单里的 codec 解包，而不是用当前默认值。
    assert package.manifest.codec != DEFAULT_CODEC
    extract_package(package.archive, package.manifest, root.parent / "out")


def test_the_manifest_lists_every_file_with_its_uncompressed_digest(session):
    root, session_id = session
    package = build_package(root / session_id)
    names = {entry.name for entry in package.manifest.entries}

    assert names == {META_FILENAME, "raw/left.raw", "raw/right.raw"}
    for entry in package.manifest.entries:
        payload = (root / session_id / entry.name).read_bytes()
        assert entry.size_bytes == len(payload)
        assert entry.sha256 == hashlib.sha256(payload).hexdigest()


def test_the_manifest_snapshot_is_plain_json_types(session):
    import json

    root, session_id = session
    snapshot = build_package(root / session_id).manifest.snapshot()

    text = json.dumps(snapshot, ensure_ascii=False)
    assert json.loads(text)["version"] == PACKAGE_FORMAT_VERSION
    assert isinstance(snapshot["parts"][0]["size_bytes"], int)


def test_the_idempotency_key_names_the_session_and_the_content(session):
    """键要能同时回答"哪次会话"与"哪一份内容"。"""
    root, session_id = session
    key = build_package(root / session_id).manifest.idempotency_key

    assert key.startswith(f"session:{session_id}:")
    assert len(key.rsplit(":", 1)[1]) == 16


# ── 切件 ──────────────────────────────────────────────────────────────────────


def test_parts_tile_the_archive_contiguously(session):
    """件必须连续、无缝、覆盖整个归档 —— 断点续传靠"第几件"定位，不靠别的。"""
    root, session_id = session
    package = build_package(root / session_id, part_size=512)
    parts = package.manifest.parts

    assert parts[0].offset == 0
    for previous, current in pairwise(parts):
        assert previous.offset + previous.size_bytes == current.offset
    assert parts[-1].offset + parts[-1].size_bytes == package.manifest.archive_size


def test_a_part_smaller_than_the_archive_produces_several_parts(session):
    root, session_id = session
    package = build_package(root / session_id, part_size=512)

    assert len(package.manifest.parts) > 1
    assert all(part.size_bytes <= 512 for part in package.manifest.parts)


def test_a_part_size_larger_than_the_archive_produces_exactly_one_part(session):
    root, session_id = session
    package = build_package(root / session_id, part_size=1 << 30)

    assert len(package.manifest.parts) == 1


def test_asking_for_a_part_that_does_not_exist_is_an_error(session):
    root, session_id = session
    package = build_package(root / session_id)

    with pytest.raises(PackageError, match="没有第"):
        package.part(99)


def test_a_non_positive_part_size_is_rejected(session):
    root, session_id = session
    with pytest.raises(PackageError, match="part_size"):
        build_package(root / session_id, part_size=0)


# ── 往返 ──────────────────────────────────────────────────────────────────────


def test_parts_reassemble_into_the_original_archive(session):
    root, session_id = session
    package = build_package(root / session_id, part_size=512)
    parts = {part.index: package.part(part.index) for part in package.manifest.parts}

    assert reassemble(parts, package.manifest) == package.archive


def test_extracting_reproduces_every_source_file_byte_for_byte(session, tmp_path):
    root, session_id = session
    package = build_package(root / session_id)
    extracted = extract_package(package.archive, package.manifest, tmp_path / "out")

    for entry in package.manifest.entries:
        assert (extracted / entry.name).read_bytes() == (root / session_id / entry.name).read_bytes()


def test_package_session_takes_the_root_and_id(session):
    """`io/session.py` 的路径约定的薄封装 —— 两处不该各写各的。"""
    root, session_id = session

    assert package_session(root, session_id).archive == build_package(root / session_id).archive


def test_packaging_an_unknown_session_id_is_a_package_error(tmp_path):
    """`io/session.py` 的 `SessionFormatError` 不该漏到调用方 —— 它在这里是打包失败。"""
    with pytest.raises(PackageError):
        package_session(tmp_path, "not-a-session-id")


# ── 校验 ──────────────────────────────────────────────────────────────────────


def test_a_corrupted_part_is_caught_before_anything_is_written(session):
    """传输完整性这一层。传坏了当场就知道，不必等整包解压。"""
    root, session_id = session
    package = build_package(root / session_id, part_size=512)
    parts = {part.index: package.part(part.index) for part in package.manifest.parts}
    damaged = bytearray(parts[1])
    damaged[0] ^= 0xFF
    parts[1] = bytes(damaged)

    with pytest.raises(PackageError, match="第 1 件摘要不符"):
        reassemble(parts, package.manifest)


def test_a_missing_part_says_which_ones_are_missing(session):
    root, session_id = session
    package = build_package(root / session_id, part_size=64)
    parts = {part.index: package.part(part.index) for part in package.manifest.parts}
    assert len(parts) >= 3
    del parts[2]

    with pytest.raises(PackageError, match=r"缺少第 \[2\] 件"):
        reassemble(parts, package.manifest)


def test_a_part_the_manifest_never_listed_is_rejected(session):
    """服务端多出一件，说明它记的不是这个包 —— 那是冲突，不是可以忽略的多余数据。"""
    root, session_id = session
    package = build_package(root / session_id)

    with pytest.raises(PackageError, match="清单里没有这些件"):
        verify_parts({99: b"stray"}, package.manifest)


def test_a_truncated_archive_is_caught_by_length_before_the_digest(session):
    root, session_id = session
    package = build_package(root / session_id)

    with pytest.raises(PackageError, match="归档长度不符"):
        verify_archive(package.archive[:-20], package.manifest)


def test_an_archive_of_the_right_length_but_wrong_content_is_caught_by_the_digest(session):
    root, session_id = session
    package = build_package(root / session_id)
    tampered = bytearray(package.archive)
    tampered[len(tampered) // 2] ^= 0xFF

    with pytest.raises(PackageError, match="归档摘要不符"):
        verify_archive(bytes(tampered), package.manifest)


def test_an_archive_that_does_not_decompress_is_a_package_error(session):
    """解压失败要变成本模块的错误类型，而不是漏一个 `OSError` 给调用方。"""
    root, session_id = session
    package = build_package(root / session_id)
    garbage = b"\x1f\x8b" + b"\x00" * (package.manifest.archive_size - 2)
    broken = type(package.manifest)(
        **{
            **package.manifest.__dict__,
            "archive_sha256": hashlib.sha256(garbage).hexdigest(),
        }
    )

    with pytest.raises(PackageError, match="无法解压"):
        extract_package(garbage, broken, Path(root).parent / "out")


def test_the_content_digest_catches_what_the_archive_digest_cannot(session, tmp_path):
    """两层摘要缺一不可。

    整包摘要只说"这堆字节没传坏"。它说不了"解压出来的东西对不对" —— 一个归档格式
    的变更、或压缩/解压环节的错误，都会让归档本身完好而内容错误。这里模拟后者：
    归档是好的，清单里某个文件的摘要被改了，解包必须拒绝。
    """
    root, session_id = session
    package = build_package(root / session_id)
    entries = list(package.manifest.entries)
    entries[0] = type(entries[0])(
        name=entries[0].name, size_bytes=entries[0].size_bytes, sha256="0" * 64
    )
    manifest = type(package.manifest)(
        **{**package.manifest.__dict__, "entries": tuple(entries)}
    )

    with pytest.raises(PackageError, match="内容与清单不符"):
        extract_package(package.archive, manifest, tmp_path / "out")


def test_a_member_missing_from_the_archive_is_reported(session, tmp_path):
    """清单列了、归档里没有 —— 那是包本身不完整，不是可以忽略的缺省。"""
    root, session_id = session
    package = build_package(root / session_id)
    extra = type(package.manifest.entries[0])(name="raw/ghost.raw", size_bytes=1, sha256="0" * 64)
    manifest = type(package.manifest)(
        **{**package.manifest.__dict__, "entries": (*package.manifest.entries, extra)}
    )

    with pytest.raises(PackageError, match="缺少清单列出的成员"):
        extract_package(package.archive, manifest, tmp_path / "out")


# ── 输入 ──────────────────────────────────────────────────────────────────────


def test_a_session_without_meta_json_is_refused(tmp_path):
    """没有元数据的包在服务端无法归属到任何一次会话。"""
    directory = tmp_path / "20260822T100000Z-deadbeef"
    (directory / "raw").mkdir(parents=True)
    (directory / "raw" / "left.raw").write_bytes(b"\x55" * 100)

    with pytest.raises(PackageError, match=META_FILENAME):
        build_package(directory)


def test_an_empty_directory_is_refused(tmp_path):
    directory = tmp_path / "empty"
    directory.mkdir()

    with pytest.raises(PackageError, match="没有文件"):
        build_package(directory)


def test_a_path_that_is_not_a_directory_is_refused(tmp_path):
    path = tmp_path / "file.txt"
    path.write_text("x")

    with pytest.raises(PackageError, match="不是一个目录"):
        build_package(path)


def test_an_unknown_codec_is_refused(session):
    root, session_id = session

    with pytest.raises(PackageError, match="未知的压缩方式"):
        build_package(root / session_id, codec="brotli")


def test_a_symlink_in_the_session_directory_is_refused(session):
    """符号链接要么指向包外，要么根本不是数据。两种都不该被静默打进包里。"""
    root, session_id = session
    link = root / session_id / "raw" / "elsewhere.raw"
    try:
        link.symlink_to(root.parent / "outside.bin")
    except OSError:  # pragma: no cover - Windows 未开发者模式时无权建链接
        pytest.skip("此平台不允许创建符号链接")

    with pytest.raises(PackageError, match="非普通文件"):
        build_package(root / session_id)


def test_the_compression_ratio_of_an_empty_archive_does_not_divide_by_zero(session):
    root, session_id = session
    manifest = build_package(root / session_id).manifest
    empty = type(manifest)(**{**manifest.__dict__, "archive_size": 0})

    assert empty.compression_ratio == 1.0
