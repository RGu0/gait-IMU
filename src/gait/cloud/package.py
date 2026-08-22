"""会话打包：压缩、摘要清单、按件切分。契约 §1 的 `cloud/package.py`（F6.2 的前半）。

PRD §6.1：会话结束后打包（压缩 + 摘要校验）后台上传。本模块只做**打包**，不联网 ——
上传队列是 `cloud/upload.py`（RAY-226 的第二个 scope）。

## 这个模块最重要的性质：字节可复现

上传的幂等键由**归档的摘要**导出（参考 FeetForcePlate `persistent_upload.py` 的
`f"session:{id}:{manifest_sha256[:16]}"`）。所以同一个会话目录必须**每次打出完全相同
的字节** —— 否则重试会算出不同的键，服务端把它当成一次新的上传，断点续传与幂等去重
一起失效，而且**不会有任何东西报错**：每次重试都"成功"，只是每次都上传一份新的。

这不是理论风险，标准库默认就会踩：

* `gzip.compress(data, 6)` 不传 `mtime` 时把**当前时间**写进头部 —— 两次调用相隔一秒
  就产生不同的字节。
* `tarfile.gettarinfo()` 从真实文件取 mtime、uid、gid、uname，全都随环境变。

所以本模块把归档的每一个可变量都钉死：成员按名字排序、`mtime=0`、`uid/gid=0`、
`uname/gname=""`、`mode=0o644`、PAX 格式、`gzip mtime=0`。`test_package.py` 里有一条
测试专门守它。

## 为什么先 tar 后压，而不是逐件压

**不是**为了共享字典 —— 那个收益实测只有 gzip 0.01%、lzma 0.63%（左右足数据结构相似，
但 gzip 的窗口只有 32 KB，跨文件根本够不着）。

真正的理由是**断点续传要按字节切件**。切件必须切在一个连续的字节流上，件与件之间才
能只靠"第几件"定位；逐件压则要按文件边界续传，而一个 8 MB 的文件传到一半断了仍然
得从头来。

## 压缩档位

实测 30 分钟双足会话（约 15.8 MB 原始）：

| 方案 | 压缩比 | 吞吐 |
| --- | --- | --- |
| gzip -6 | 1.38 | 34.8 MB/s |
| bzip2 -9 | 1.86 | 28.4 MB/s |
| lzma preset 6 | 1.96 | 4.8 MB/s |

比值都不高 —— int16 传感器读数的低位本来就接近随机。默认取 `gzip`：它最快、最通用，
而在这个数据量下多省的那几 MB 换不来什么。

**但档位记在清单里**，不是隐含约定。默认值以后要是改了，昨天打好、还在队列里等着重
传的包必须仍然解得开 —— 把 codec 写进清单，这件事就是自动成立的。

## 两层摘要，缺一不可

* **逐件摘要**（压缩后的字节）：传输完整性。传坏了当场就知道，不必等整包解压。
* **逐文件摘要**（压缩前的字节）：内容完整性。它保证解压出来的东西与采集时写下的
  一致 —— 压缩/解压本身出错、或归档格式将来变了，这一层能抓住。

只留一层都不够：只有压缩后的，解压之后就没有可比的基准了；只有压缩前的，一次传输
损坏要到整包解压时才暴露，而那时已经白传了整包。
"""

from __future__ import annotations

import bz2
import gzip
import hashlib
import io
import lzma
import tarfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from gait.io.session import META_FILENAME, SessionFormatError, session_directory

#: 清单的结构版本。它随包走，好让服务端知道自己在读什么。
PACKAGE_FORMAT_VERSION: Final[str] = "1.0"

#: 可选的压缩方式。**档位记在清单里**，默认值改变不得让旧包读不出来。
CODECS: Final[tuple[str, ...]] = ("gzip", "bzip2", "lzma")
DEFAULT_CODEC: Final[str] = "gzip"

#: 默认切件大小，字节。4 MiB。
#:
#: 取值权衡：件太大，一次断网重传的浪费就大；件太小，清单本身与每件的往返开销占比
#: 上升。实测 30 分钟双足会话压后约 11 MB，4 MiB 切成 3 件 —— 断在任何一件里最多
#: 白传 4 MiB。
DEFAULT_PART_SIZE: Final[int] = 4 * 1024 * 1024

#: 归档里成员的固定属性。全部钉死是为了字节可复现，理由见模块文档。
_ARCHIVE_MODE: Final[int] = 0o644


class PackageError(ValueError):
    """打包或校验失败。"""


@dataclass(frozen=True)
class PackageEntry:
    """归档里的一个源文件。摘要是**压缩前**的，管的是内容完整性。"""

    #: 相对会话目录的 POSIX 路径，例如 `raw/left.raw`。
    name: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True)
class PackagePart:
    """归档的一件。摘要是**压缩后**的，管的是传输完整性。"""

    index: int
    #: 在归档字节流里的起始偏移。件是连续、无缝、覆盖整个归档的。
    offset: int
    size_bytes: int
    sha256: str


@dataclass(frozen=True)
class PackageManifest:
    """一个包的全部元信息。它随上传请求走，服务端靠它验收。"""

    session_id: str
    #: 压缩方式。**必须记下来** —— 默认值以后改了，队列里的旧包还要解得开。
    codec: str
    #: 压缩前的源文件清单，按 `name` 排序。
    entries: tuple[PackageEntry, ...]
    #: 整个归档（压缩后）的摘要与长度。
    archive_sha256: str
    archive_size: int
    parts: tuple[PackagePart, ...]
    part_size: int
    version: str = PACKAGE_FORMAT_VERSION

    @property
    def total_uncompressed(self) -> int:
        return sum(entry.size_bytes for entry in self.entries)

    @property
    def compression_ratio(self) -> float:
        """压缩前除以压缩后。归档为空时返回 1.0 而不是除零。"""
        if not self.archive_size:
            return 1.0
        return self.total_uncompressed / self.archive_size

    @property
    def idempotency_key(self) -> str:
        """上传用的幂等键。**由内容导出**，所以重试必定产生同一个键。

        取摘要的前 16 个十六进制字符（64 位）。会话 id 已经在前面，这一段只需要区分
        "同一个会话的不同内容"，64 位对这个用途绰绰有余。
        """
        return f"session:{self.session_id}:{self.archive_sha256[:16]}"

    def snapshot(self) -> dict[str, Any]:
        """随上传请求走的普通字典。"""
        return {
            "session_id": self.session_id,
            "codec": self.codec,
            "entries": [
                {"name": entry.name, "size_bytes": entry.size_bytes, "sha256": entry.sha256}
                for entry in self.entries
            ],
            "archive_sha256": self.archive_sha256,
            "archive_size": self.archive_size,
            "parts": [
                {
                    "index": part.index,
                    "offset": part.offset,
                    "size_bytes": part.size_bytes,
                    "sha256": part.sha256,
                }
                for part in self.parts
            ],
            "part_size": self.part_size,
            "total_uncompressed": self.total_uncompressed,
            "version": self.version,
        }


@dataclass(frozen=True)
class SessionPackage:
    """打好的包：清单 + 归档字节。"""

    manifest: PackageManifest
    archive: bytes

    def part(self, index: int) -> bytes:
        """取第 `index` 件的字节。"""
        for candidate in self.manifest.parts:
            if candidate.index == index:
                return self.archive[candidate.offset : candidate.offset + candidate.size_bytes]
        raise PackageError(f"没有第 {index} 件；本包共 {len(self.manifest.parts)} 件")


def _digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _compress(payload: bytes, codec: str) -> bytes:
    """压缩。**每一种都必须是确定性的** —— 见模块文档。"""
    if codec == "gzip":
        # `mtime=0` 不是可选的：不传它，gzip 会把当前时间写进头部，同一份数据隔一秒
        # 压出来就是不同的字节，幂等键随之改变。
        return gzip.compress(payload, compresslevel=6, mtime=0)
    if codec == "bzip2":
        return bz2.compress(payload, compresslevel=9)
    if codec == "lzma":
        return lzma.compress(payload, preset=6)
    raise PackageError(f"未知的压缩方式 {codec!r}；可选 {CODECS}")


def _decompress(payload: bytes, codec: str) -> bytes:
    try:
        if codec == "gzip":
            return gzip.decompress(payload)
        if codec == "bzip2":
            return bz2.decompress(payload)
        if codec == "lzma":
            return lzma.decompress(payload)
    except (OSError, EOFError, lzma.LZMAError) as exc:
        raise PackageError(f"归档无法解压（codec={codec}）：{exc}") from exc
    raise PackageError(f"未知的压缩方式 {codec!r}；可选 {CODECS}")


def _collect(directory: Path) -> list[tuple[str, bytes]]:
    """会话目录里的全部文件，按 POSIX 相对路径排序。

    排序是**字节可复现的一部分**：文件系统的遍历顺序不保证稳定，不排序的话同一个
    目录在两台机器上会打出不同的归档。
    """
    root = Path(directory)
    if not root.is_dir():
        raise PackageError(f"不是一个目录：{root}")
    members: list[tuple[str, bytes]] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        if path.is_dir():
            continue
        if not path.is_file():
            # 符号链接、设备文件之类不进包 —— 它们要么指向包外，要么根本不是数据。
            raise PackageError(f"会话目录里有非普通文件：{path}")
        members.append((path.relative_to(root).as_posix(), path.read_bytes()))
    if not members:
        raise PackageError(f"会话目录里没有文件：{root}")
    if META_FILENAME not in {name for name, _ in members}:
        raise PackageError(
            f"会话目录里没有 {META_FILENAME}：{root}。"
            "没有元数据的包在服务端无法归属到任何一次会话。"
        )
    return members


def _archive_bytes(members: list[tuple[str, bytes]]) -> bytes:
    """确定性的 tar。每一个随环境变的字段都被钉死，理由见模块文档。"""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for name, payload in members:
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            info.mtime = 0
            info.mode = _ARCHIVE_MODE
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            info.type = tarfile.REGTYPE
            archive.addfile(info, io.BytesIO(payload))
    return buffer.getvalue()


def _split(archive: bytes, part_size: int) -> tuple[PackagePart, ...]:
    """按字节切件。件是连续、无缝、覆盖整个归档的。

    空归档也给一件（长度 0）：让"件数至少为一"成立，调用方就不必到处判空。
    """
    if part_size < 1:
        raise PackageError(f"part_size 必须为正，收到 {part_size}")
    parts: list[PackagePart] = []
    for index, offset in enumerate(range(0, max(len(archive), 1), part_size)):
        chunk = archive[offset : offset + part_size]
        parts.append(
            PackagePart(
                index=index, offset=offset, size_bytes=len(chunk), sha256=_digest(chunk)
            )
        )
    return tuple(parts)


def build_package(
    directory: Path | str,
    *,
    session_id: str | None = None,
    codec: str = DEFAULT_CODEC,
    part_size: int = DEFAULT_PART_SIZE,
) -> SessionPackage:
    """把一个会话目录打成包。

    `session_id` 默认取目录名 —— `io/session.py` 的布局就是这样定的，多传一遍只会
    多一处能对不上的地方。

    **同一个目录必须打出完全相同的字节。** 这是幂等键成立的前提，见模块文档。
    """
    root = Path(directory)
    identifier = session_id if session_id is not None else root.name
    members = _collect(root)
    raw = _archive_bytes(members)
    archive = _compress(raw, codec)
    manifest = PackageManifest(
        session_id=identifier,
        codec=codec,
        entries=tuple(
            PackageEntry(name=name, size_bytes=len(payload), sha256=_digest(payload))
            for name, payload in members
        ),
        archive_sha256=_digest(archive),
        archive_size=len(archive),
        parts=_split(archive, part_size),
        part_size=part_size,
    )
    return SessionPackage(manifest=manifest, archive=archive)


def package_session(
    root: Path | str, session_id: str, **kwargs: Any
) -> SessionPackage:
    """按会话根目录与 id 打包。`io/session.py` 的路径约定的薄封装。"""
    try:
        directory = session_directory(Path(root), session_id)
    except SessionFormatError as exc:
        raise PackageError(str(exc)) from exc
    return build_package(directory, session_id=session_id, **kwargs)


def verify_parts(parts: Mapping[int, bytes], manifest: PackageManifest) -> None:
    """逐件校验。传输完整性这一层。"""
    expected = {part.index: part for part in manifest.parts}
    unknown = set(parts) - set(expected)
    if unknown:
        raise PackageError(f"清单里没有这些件：{sorted(unknown)}")
    for index, payload in parts.items():
        part = expected[index]
        if len(payload) != part.size_bytes:
            raise PackageError(
                f"第 {index} 件长度不符：清单 {part.size_bytes}，实际 {len(payload)}"
            )
        if _digest(payload) != part.sha256:
            raise PackageError(f"第 {index} 件摘要不符 —— 传输过程中损坏了")


def reassemble(parts: Mapping[int, bytes], manifest: PackageManifest) -> bytes:
    """把件拼回归档，并校验。缺件时明确说缺哪几件。"""
    missing = sorted({part.index for part in manifest.parts} - set(parts))
    if missing:
        raise PackageError(f"缺少第 {missing} 件，拼不出完整归档")
    verify_parts(parts, manifest)
    archive = b"".join(parts[part.index] for part in sorted(manifest.parts, key=lambda p: p.index))
    verify_archive(archive, manifest)
    return archive


def verify_archive(archive: bytes, manifest: PackageManifest) -> None:
    """整包校验。"""
    if len(archive) != manifest.archive_size:
        raise PackageError(
            f"归档长度不符：清单 {manifest.archive_size}，实际 {len(archive)}"
        )
    if _digest(archive) != manifest.archive_sha256:
        raise PackageError("归档摘要不符 —— 内容与清单描述的不是同一份数据")


def extract_package(
    archive: bytes, manifest: PackageManifest, destination: Path | str
) -> Path:
    """解包到 `destination / session_id`，并逐文件校验内容摘要。

    **先校验整包，再解压，最后逐文件校验。** 三道都要：整包摘要抓传输损坏，逐文件
    摘要抓压缩/解压环节与归档格式变更 —— 后者在整包摘要下是完全看不见的，因为归档
    本身没坏，坏的是它的解释。

    成员路径必须留在目标目录内。tar 的路径逃逸是个老问题，而这个包来自网络。
    """
    verify_archive(archive, manifest)
    raw = _decompress(archive, manifest.codec)
    target = Path(destination) / manifest.session_id
    target.mkdir(parents=True, exist_ok=True)
    resolved_target = target.resolve()

    expected = {entry.name: entry for entry in manifest.entries}
    seen: set[str] = set()
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as tar:
        for info in tar.getmembers():
            if not info.isfile():
                raise PackageError(f"归档里有非普通成员：{info.name}")
            destination_path = (target / info.name).resolve()
            if resolved_target != destination_path and resolved_target not in destination_path.parents:
                raise PackageError(f"归档成员的路径逃出了目标目录：{info.name}")
            if info.name not in expected:
                raise PackageError(f"归档里有清单未列出的成员：{info.name}")
            handle = tar.extractfile(info)
            payload = handle.read() if handle is not None else b""
            entry = expected[info.name]
            if len(payload) != entry.size_bytes or _digest(payload) != entry.sha256:
                raise PackageError(f"成员 {info.name} 的内容与清单不符")
            destination_path.parent.mkdir(parents=True, exist_ok=True)
            destination_path.write_bytes(payload)
            seen.add(info.name)

    missing = sorted(set(expected) - seen)
    if missing:
        raise PackageError(f"归档缺少清单列出的成员：{missing}")
    return target
