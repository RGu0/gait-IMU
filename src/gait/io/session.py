"""会话目录与元数据读写。契约 §1 的 `io/session.py`（F0.2）。

正式定义见《05 数据格式规范》。本模块是那份文档的可执行部分 —— 文档描述布局，
这里强制它。

## FR-02 是这个模块最重要的约束

"身份字段仅存云端加密库；本地会话文件只含 `subject_uuid`，不落身份明文。"

`SessionMeta` 已经拒绝非 UUID 的 `subject_uuid`，但它有一个 `extra` 字典 —— 那是
身份明文最可能溜进来的地方。写入前因此再查一遍：**元数据里任何一层出现疑似身份的
键名，就拒绝落盘。**

这个检查的边界必须说清楚：它拦得住"顺手把 `patient_name` 塞进 extra"这种真实的
疏忽，拦不住存心绕过的人（把它命名为 `x` 就过了）。声称它是隐私保证会是错的；
它是一道**防手滑**的闸，价值在于让常见错误当场失败而不是三个月后在一份导出的
会话里被发现。

## 为什么写入是原子的

PRD §15 要求"写入点断电可恢复已关闭数据"。半份 `meta.json` 比没有更糟 —— 它看起来
存在，解析却失败，而调用方多半没有区分这两种情况。先写临时文件再 `os.replace`，
同目录内的 replace 在 POSIX 与 Windows 上都是原子的。
"""

from __future__ import annotations

import json
import os
import re
import secrets
import uuid
from dataclasses import fields
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from gait.contracts import CONTRACT_VERSION, ContractError, SessionMeta

#: 会话目录内的文件名。集中在这里，规范文档与代码不会各写各的。
META_FILENAME: Final[str] = "meta.json"
RAW_DIRNAME: Final[str] = "raw"

#: 原始帧文件名，按足区分。落盘本身是 RAY-198 的职责，这里只定名字，
#: 好让两个 scope 不会各起一套。
RAW_FILENAMES: Final[dict[str, str]] = {"L": "left.raw", "R": "right.raw"}

#: `session_id` 的形状：UTC 时间戳 + 随机后缀。
#:
#: 时间戳让目录天然按时间排序，随机后缀避免同秒内的两次会话相撞。**不含任何来自
#: 受试者的信息** —— PRD §12 要求"脱敏文件名"，而一个含档案号的会话目录名会让
#: FR-02 在文件系统层面失效，哪怕 `meta.json` 本身是干净的。
SESSION_ID_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^\d{8}T\d{6}Z-[0-9a-f]{8}$"
)

#: 疑似身份明文的键名。小写比较，子串匹配。
#:
#: 这份清单挡的是疏忽，不是恶意 —— 见模块文档。它宁可误伤（一个叫 `operator_name`
#: 的字段会被拦下）也不放过：误伤会当场暴露并被讨论，放过则不会。
IDENTITY_KEY_FRAGMENTS: Final[tuple[str, ...]] = (
    "name",
    "姓名",
    "phone",
    "电话",
    "mobile",
    "email",
    "address",
    "地址",
    "id_card",
    "idcard",
    "身份证",
    "medical_record",
    "record_no",
    "档案号",
    "住院号",
    "birth",
    "出生",
)


class SessionFormatError(ValueError):
    """会话目录或元数据不符合《05 数据格式规范》。"""


def new_session_id(*, now: datetime | None = None) -> str:
    """生成一个会话 id。

    `now` 可注入，好让测试不依赖真实时钟；默认取 UTC。用 UTC 而非本地时间，是因为
    机构可能跨时区部署，而目录名一旦混用时区就再也无法可靠排序。
    """
    moment = (now or datetime.now(UTC)).astimezone(UTC)
    return f"{moment.strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(4)}"


def _check_session_id(session_id: str) -> str:
    if not SESSION_ID_PATTERN.match(session_id):
        raise SessionFormatError(
            f"session_id 不符合规范：{session_id!r}。"
            "形状为 YYYYMMDDTHHMMSSZ-xxxxxxxx（UTC 时间戳 + 8 位十六进制随机后缀），"
            "且不得包含任何来自受试者的信息 —— 目录名也在 FR-02 的范围内。"
        )
    return session_id


def _identity_offences(payload: Any, path: str = "") -> list[str]:
    """元数据中疑似身份明文的键，递归查找。"""
    found: list[str] = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            here = f"{path}.{key}" if path else str(key)
            lowered = str(key).lower()
            if any(fragment in lowered for fragment in IDENTITY_KEY_FRAGMENTS):
                found.append(here)
            found.extend(_identity_offences(value, here))
    elif isinstance(payload, list):
        for index, item in enumerate(payload):
            found.extend(_identity_offences(item, f"{path}[{index}]"))
    return found


def session_directory(root: Path, session_id: str) -> Path:
    """一个会话的目录。布局定义见《05 数据格式规范》。"""
    return Path(root) / _check_session_id(session_id)


def raw_path(root: Path, session_id: str, foot: str) -> Path:
    """某一只脚的原始帧文件。落盘由 RAY-198 实现，路径在这里定。"""
    if foot not in RAW_FILENAMES:
        raise SessionFormatError(f"foot 应为 'L' 或 'R'，收到 {foot!r}")
    return session_directory(root, session_id) / RAW_DIRNAME / RAW_FILENAMES[foot]


def create_session(root: Path, meta: SessionMeta) -> Path:
    """建立会话目录并写入元数据，返回目录路径。

    目录必须**不存在**：一个已存在的会话目录意味着 id 相撞或重复采集，两者都不该
    被静默覆盖 —— 覆盖会毁掉一份已经采到的数据，而那是不可再生的。
    """
    directory = session_directory(root, meta.session_id)
    if directory.exists():
        raise SessionFormatError(
            f"会话目录已存在：{directory}。id 相撞或重复采集，两者都不覆盖 —— "
            "已采集的数据不可再生。"
        )
    (directory / RAW_DIRNAME).mkdir(parents=True)
    write_meta(directory, meta)
    return directory


def write_meta(directory: Path, meta: SessionMeta) -> Path:
    """原子地写入 `meta.json`。

    FR-02 检查在写盘**之前**：一旦落盘，身份明文就已经存在于本地磁盘上了，事后
    删除也无法保证没有被同步、备份或打包上传。
    """
    if not isinstance(meta, SessionMeta):
        raise SessionFormatError(f"meta 必须是 SessionMeta，收到 {type(meta).__name__}")
    _check_session_id(meta.session_id)

    payload = {f.name: getattr(meta, f.name) for f in fields(meta)}
    offences = _identity_offences(payload)
    if offences:
        raise SessionFormatError(
            f"元数据里出现疑似身份明文的键：{offences}。"
            "FR-02：身份字段仅存云端加密库，本地会话文件只含 subject_uuid。"
            "（此检查针对疏忽，不针对刻意绕过 —— 见模块文档。）"
        )

    directory = Path(directory)
    if not directory.is_dir():
        raise SessionFormatError(f"会话目录不存在：{directory}")
    target = directory / META_FILENAME
    # 同目录内的临时文件：os.replace 只在同一文件系统内保证原子性。
    temporary = directory / f".{META_FILENAME}.partial"
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, target)
    return target


def read_meta(directory: Path) -> SessionMeta:
    """读回 `meta.json`。

    契约版本不匹配即拒绝，不按当前字段解读。R2 把角速度从 deg/s 改成 rad/s 时
    升过一次契约版本 —— 把一份 1.0 的会话按 1.1 读回，`gyr` 会静默地差 57.3 倍。
    历史会话该由迁移工具处理，不该由读取函数猜。
    """
    path = Path(directory) / META_FILENAME
    if not path.is_file():
        raise SessionFormatError(f"会话元数据不存在：{path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise SessionFormatError(f"会话元数据不是合法 JSON：{path}") from error
    except OSError as error:
        # 读不到与内容坏了要分开报：前者去查权限，后者去查文件。
        raise SessionFormatError(f"无法读取会话元数据：{path}（{error.strerror}）") from error

    if not isinstance(payload, dict):
        raise SessionFormatError(f"会话元数据必须是 JSON 对象：{path}")

    version = payload.get("contract_version")
    if version != CONTRACT_VERSION:
        raise SessionFormatError(
            f"会话元数据的契约版本是 {version!r}，本代码只认识 {CONTRACT_VERSION!r}："
            f"{path}。拒绝按当前字段解读历史会话 —— 单位与字段含义可能已经改变，"
            "静默解读会产生看似正常的错误数值。历史会话应经迁移工具处理。"
        )

    known = {f.name for f in fields(SessionMeta)}
    unknown = sorted(set(payload) - known)
    if unknown:
        raise SessionFormatError(f"会话元数据含未知字段：{unknown}（{path}）")
    try:
        return SessionMeta(**payload)
    except ContractError as error:
        raise SessionFormatError(f"会话元数据不满足契约：{error}（{path}）") from error


def list_sessions(root: Path) -> list[str]:
    """`root` 下所有会话 id，按时间升序。

    排序靠 id 本身的时间戳前缀，不靠文件系统的 mtime —— 复制、同步、恢复都会改
    mtime，而它们都不改变会话真正发生的时刻。
    """
    root = Path(root)
    if not root.is_dir():
        return []
    return sorted(
        entry.name
        for entry in root.iterdir()
        if entry.is_dir() and SESSION_ID_PATTERN.match(entry.name)
    )


def is_identity_free(payload: dict[str, Any]) -> bool:
    """FR-02 检查的公开形式，供上传打包等环节复用。

    暴露出来是为了让"打包前再查一次"成为可能，而不是让每个调用方各写一份关键词表。
    """
    return not _identity_offences(payload)


def new_subject_uuid() -> str:
    """随机 `subject_uuid`。PRD §6.1：无编号快速建档时使用。

    uuid4 而非基于姓名/档案号的 uuid5 —— 后者可由已知输入反算出来，等于把身份
    以另一种形式带进本地文件。
    """
    return str(uuid.uuid4())
