"""红线检查：本仓库对**别的仓库**的引用必须被钉住，且钉住的内容要与上游一致。

《抄录卷》转录了足压平台已跑通的实现，《待确认卷》与 `docs/云端接口约定.md` 引用了
techflex-cloud-foundation 的类型。这些文件**不在本仓库**，本仓库的任何测试、`ruff`、
既有三条红线都看不见它们的变化。

## 为什么这条要做成检查，而不是写进文档就够

对照 `check_calibration_channel.py` 的三条理由，这里同样成立：

1. **写它的人多半是想做正事。** 据实记录「足压平台的 `cloud/api/errors.py:85-88` 是
   这么做的」是对的，问题是那份记录会过期。
2. **过期了不报错。** 一份说错话的文档不会让任何测试变红。
3. **事后查不出来。** RAY-417 那次是用户问「该会话可以 archive 吗」时顺手核查才发现
   的 —— 三份文档差一点带着一条已知为假的陈述被归档。从写下到失效**不足一天**，其中
   一条在 RAY-355 登记 scope 前三分钟就已失效。

## 两层，因为 CI 上没有那些仓库

`feet-force-plate` 与 `techflex-cloud-foundation` 是**本机同级目录**，GitHub Actions
runner 上根本不存在。所以本检查分两层，**各自查自己真的知道的事**：

| 层 | 查什么 | 何时跑 |
| -- | -- | -- |
| 一、声明完整性 | 仓库文件里引用到的每一个上游文件都已在 `upstream_refs.json` 里 | **总是** |
| 二、内容比对 | 重读上游文件、比 sha256 | 上游在本机时 |

第一层只看本仓库自己的内容，所以**在 CI 上也能真的失败** —— 抓的是「新加了一处引用
却没钉住」，而那个失败**归因正确**：就是这个改动干的。

第二层在上游缺席时**明说哪几条没能核对**，不静静返回 0。

## 为什么没有「超期未复核即红」的日期时钟

RAY-420 原提案里有这么一条，写实现时判定它是错的：日期时钟会在**不相干的改动**上变
红，惩罚一个既没能力也没上下文去核对上游的人；而它的自然反应是把日期改大 —— 那不只
是绕过检查，是向一个名为「已核对」的字段写入一次没发生过的核对。**那比没有检查更坏，
它制造虚假的保证。** 复核节奏属于完成闸门（RAY-375），不属于一条 lint。

## 本检查够不到的地方（都是实情，不掩盖）

* 《抄录卷》《待确认卷》在**云端库**（`.project-context/` 是 symlink），不在 git 里，
  **CI 看不见**。它们的 34 处引用只有第二层能核，且只在本机。
* **Issue 状态漂移查不了**。RAY-417 咬到的其实是这一类：上游源码一字未改，变的是
  「那条阻断还成不成立」。本脚本拿不到 Linear 凭据。这一半留在 RAY-420 里作未完成项。
* `packages/design-system/` 也是同步进来的外部产物，本次未纳入声明。

## 引用怎么写才会被第一层认出来

两种形式，任一即可：

    `ffp:cloud/api/errors.py:85–88`         <- 前缀式，两卷用的就是它
    参考 FeetForcePlate `client/sync/persistent_upload.py`   <- 同行带上游标记

第二种要求路径与上游标记（`markers`）**同行**。窗口取 0 行不是偷懒：对着当前仓库实测
过，同行命中 9 处全部为真；放宽到 ±3 行就会把 `packages/design-system/DESIGN-GUIDE.md`
里的产品名当成代码引用。一条红线要让人**看一眼就知道自己这行受不受管**。

代价是「引用与标记跨行」会漏掉。所以 `upstream_refs.json` 的 `cited_by` 是第二双眼睛：
声明里记着每个上游文件被谁引用，人工复核时对得上。

## 重新钉住

    ./dev node true >/dev/null; uv run python tools/check_upstream_refs.py --update

`--update` 会打印每个文件的新旧摘要。**它不替你做核对** —— 摘要变了意味着引用它的那几
份文档需要人重读一遍，脚本没有能力判断结论是否还成立。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")

REPO_ROOT = Path(__file__).resolve().parent.parent
PIN_PATH = REPO_ROOT / "tools" / "upstream_refs.json"

#: 被扫描的文本类型。二进制与锁文件里不会有人工写下的引用。
SCANNED_SUFFIXES: frozenset[str] = frozenset(
    {".py", ".md", ".js", ".jsx", ".ts", ".json", ".txt"}
)

#: 反引号里的路径样 token。三段都是必要的：
#:   前缀 `ffp:` —— 两卷的精确形式，命中它就不必再猜；
#:   路径本体；
#:   尾随 `:行号` / `:行区间` —— 两卷写的是 `ffp:cloud/api/app.py:648–651, 665–668`。
#: 第一版漏了前缀那段，结果**两卷的精确形式一条都匹配不上**，只剩同行启发式在干活。
CITATION_RE = re.compile(
    r"`(?:([A-Za-z0-9_]+):)?"
    r"([A-Za-z0-9_./\-]+\.(?:py|md|json|js|jsx|ts|toml|yaml|yml))"
    r"(?::[0-9,–\- ]+)?`"
)

#: 上游标记与路径必须同行。见模块文档：0 行是实测出来的，不是随手取的。
MARKER_WINDOW = 0


class PinError(Exception):
    """声明文件本身不合法。这不是漂移，是这份声明坏了。"""


# --------------------------------------------------------------------------- 声明


def load_pin(path: Path = PIN_PATH) -> dict:
    """读并校验声明。格式错**一律抛异常**，不做容错 —— 一份读不懂的声明等于没有声明。"""
    try:
        pin = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise PinError(f"声明文件不存在：{path}") from exc
    except json.JSONDecodeError as exc:
        raise PinError(f"声明文件不是合法 JSON：{path}（{exc}）") from exc

    upstreams = pin.get("upstreams")
    if not isinstance(upstreams, dict) or not upstreams:
        raise PinError("声明缺少非空的 `upstreams`")

    for key, spec in upstreams.items():
        if not isinstance(spec, dict):
            raise PinError(f"upstreams.{key} 不是对象")
        pinned = spec.get("pinned_by")
        if pinned is not None:
            _check_pinned_by(key, pinned)
        required = ("markers", "files") if pinned else ("repo_dir", "markers", "files")
        for field in required:
            if field not in spec:
                raise PinError(f"upstreams.{key} 缺少 `{field}`")
        # 类型也要校。`repo_dir` 若不是字符串，`ancestor / repo_dir` 会抛
        # TypeError —— 一个「声明写坏了」的问题会以一次崩溃的形式出现，
        # 而崩溃看起来像检查本身坏了，不像声明坏了。
        for field in ("repo_dir", "inner"):
            value = spec.get(field, "")
            if not isinstance(value, str):
                raise PinError(f"upstreams.{key}.{field} 必须是字符串")
        if not isinstance(spec["markers"], list) or not spec["markers"]:
            raise PinError(f"upstreams.{key}.markers 必须是非空数组")
        files = spec["files"]
        if not isinstance(files, dict) or not files:
            raise PinError(f"upstreams.{key}.files 必须是非空对象")
        for rel, entry in files.items():
            if not isinstance(entry, dict):
                raise PinError(f"upstreams.{key}.files['{rel}'] 不是对象")
            if spec.get("pinned_by") is None:
                digest = entry.get("sha256")
                if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
                    raise PinError(
                        f"upstreams.{key}.files['{rel}'].sha256 不是 64 位小写十六进制"
                    )
            elif "sha256" in entry:
                # 一个既按锁文件钉、又逐文件记摘要的条目有两个真相源，而两份真相
                # 迟早对不上。锁文件那个哈希覆盖整个 wheel，比三个逐文件摘要更宽。
                raise PinError(
                    f"upstreams.{key}.files['{rel}'] 同时有 `sha256` 与上游的 "
                    "`pinned_by`；按锁文件钉时不要再逐文件记摘要"
                )
            cited = entry.get("cited_by")
            if not isinstance(cited, list) or not cited:
                # 一条谁都不引用的声明是死条目：它只会让人以为覆盖面比实际大。
                raise PinError(
                    f"upstreams.{key}.files['{rel}'] 没有 `cited_by` —— "
                    "没人引用的上游文件不该被钉住，删掉它"
                )
    return pin


def _check_pinned_by(key: str, pinned: object) -> None:
    if not isinstance(pinned, dict) or set(pinned) != {"lock", "package", "version"}:
        raise PinError(
            f"upstreams.{key}.pinned_by 必须是 {{lock, package, version}} 三个键的对象"
        )
    if not all(isinstance(value, str) and value for value in pinned.values()):
        raise PinError(f"upstreams.{key}.pinned_by 的两个值都必须是非空字符串")


def locked_version(pinned: dict, repo_root: Path = REPO_ROOT) -> tuple[bool, str]:
    """(是否与声明一致, 说明)。核对锁文件里该包的版本是否还是声明记的那一个。

    ## 这条守的是升版，不是内容漂移

    内容漂移在这里**已经不可能**了：`uv.lock` 对 url / path / registry 来源都记
    `hash = "sha256:…"`，`--locked` 每次安装都校验；git 来源不记哈希，但 rev 是 commit，
    本身就不可变。**换句话说，钉子没法被静默弄丢** —— 第一版的这条检查是照着一个
    不存在的失效模式写的，实测（把 `url =` 换成 `path =`，哈希照样在）之后改成了本条。

    真正会静默出错的是**升版**：改一行 URL 把 Foundation 从 0.3.0 换到 0.4.0，
    `uv lock` 会照做、测试照样绿 —— 而《抄录卷》《待确认卷》里逐行转录 `iam.py` /
    `device_trust.py` 的那些内容**可能已经不成立**，没有任何一步会提醒。

    所以声明里记住版本，升版时这条会红，报错里带上 `cited_by`：升版因此变成
    **一次必须回头重读那几份文档的显式动作**，而不是一行 diff。这正是 RAY-420
    方案 B 说的「漂移变成一次显式的升级事件」。
    """
    lock_path = repo_root / pinned["lock"]
    try:
        data = tomllib.loads(lock_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, f"锁文件不存在：{pinned['lock']}"
    except tomllib.TOMLDecodeError as exc:
        return None, f"锁文件不是合法 TOML：{pinned['lock']}（{exc}）"

    packages = [
        entry
        for entry in data.get("package", [])
        if isinstance(entry, dict) and entry.get("name") == pinned["package"]
    ]
    if not packages:
        return None, f"{pinned['lock']} 里没有 `{pinned['package']}` 这个包"
    if len(packages) > 1:
        return None, f"{pinned['lock']} 里有 {len(packages)} 个 `{pinned['package']}`，无法判定"

    actual = packages[0].get("version")
    if actual != pinned["version"]:
        return False, (
            f"{pinned['package']} 的版本变了：声明 {pinned['version']}，"
            f"{pinned['lock']} 里是 {actual}"
        )
    return True, f"{pinned['package']} {actual} 按 {pinned['lock']} 钉住（声明一致）"


def upstream_root(key: str, spec: dict, repo_root: Path = REPO_ROOT) -> Path | None:
    """定位上游仓库。返回 None 表示不在本机（不是错误）。

    不能用相对路径写死：`main/` worktree 与 `.worktrees/<issue>/<scope>/` 到同级目录
    的深度不同，写死哪一个另一个就断。所以逐级向上找同名目录。
    """
    override = os.environ.get(f"GAIT_UPSTREAM_{key.upper()}")
    if override:
        candidate = Path(override).expanduser()
        return candidate if candidate.is_dir() else None

    repo_dir = spec["repo_dir"]
    inner = spec.get("inner", "")
    for ancestor in [repo_root, *repo_root.parents]:
        base = ancestor / repo_dir
        if base.is_dir():
            found = base / inner if inner else base
            if found.is_dir():
                return found
    return None


def sha256_of(path: Path) -> str:
    """摘要取 **LF 归一化后**的字节，不取磁盘原样。

    这份声明要在多台机器之间比对，而 Windows 上带 `core.autocrlf=true` 的检出会把上游
    每一行的结尾都变成 CRLF —— 原样摘要下**全部 14 个文件都会报漂移**，而上游一个字符
    都没改。那种红是最坏的一种：它按平台而非按事实触发，且唯一的消解办法是重钉摘要，
    于是把这道闸训练成一个「看到红就 --update」的仪式。

    代价是纯换行符变更不再被察觉。那不是语义变更，本来也不该惊动引用它的文档。
    """
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


# ----------------------------------------------------------------- 第一层：完整性


def tracked_files(repo_root: Path = REPO_ROOT) -> list[str]:
    """git 跟踪的文件。用 `-z`：仓库里有中文文件名，默认输出会带引号转义，
    按空白切分会切出一个读不开的路径，然后被静默跳过 —— 那正是本检查要修的毛病。"""
    proc = subprocess.run(
        ["git", "-C", str(repo_root), "ls-files", "-z"],
        capture_output=True,
        check=True,
    )
    return [f for f in proc.stdout.decode("utf-8").split("\0") if f]


def _resolves_in_repo(token: str, tracked: list[str]) -> bool:
    """token 是否指向本仓库自己的文件（按路径段后缀对齐）。

    本仓库到处用裸文件名互相引用（`walk.py`、`cloud/upload.py`），后缀对齐能把它们
    认出来，剩下的才可能是外部引用。
    """
    parts = tuple(token.split("/"))
    n = len(parts)
    for f in tracked:
        seg = f.split("/")
        if len(seg) >= n and tuple(seg[-n:]) == parts:
            return True
    return False


def _declared(token: str, files: dict) -> bool:
    """token 是否命中某个已声明的上游文件（同样按后缀对齐：文档里常只写 `iam.py`）。"""
    parts = tuple(token.split("/"))
    n = len(parts)
    for rel in files:
        seg = rel.split("/")
        if len(seg) >= n and tuple(seg[-n:]) == parts:
            return True
    return False


def undeclared_citations(
    repo_root: Path, tracked: list[str], pin: dict
) -> list[str]:
    """仓库文件里引用了上游、却没被声明的位置。"""
    upstreams = pin["upstreams"]
    problems: list[str] = []
    for rel in tracked:
        path = repo_root / rel
        if path.suffix not in SCANNED_SUFFIXES:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            # 读不开就报出来。静默跳过等于给检查开了一个看不见的口子。
            problems.append(f"{rel}: 无法读取（{exc}）")
            continue
        lines = text.splitlines()
        for i, line in enumerate(lines):
            window = "\n".join(
                lines[max(0, i - MARKER_WINDOW) : i + MARKER_WINDOW + 1]
            )
            for match in CITATION_RE.finditer(line):
                key, token = match.group(1), match.group(2)
                if key in upstreams:
                    if not _declared(token, upstreams[key]["files"]):
                        problems.append(
                            f"{rel}:{i + 1}: 引用了 {key} 的 `{token}`，但它不在声明里"
                        )
                    continue
                if _resolves_in_repo(token, tracked):
                    continue
                hit = [
                    k
                    for k, spec in upstreams.items()
                    if any(m in window for m in spec["markers"])
                ]
                if not hit:
                    continue
                if not any(_declared(token, upstreams[k]["files"]) for k in hit):
                    problems.append(
                        f"{rel}:{i + 1}: 与上游标记（{'/'.join(hit)}）同行引用了 "
                        f"`{token}`，但它不在声明里"
                    )
    return problems


# ------------------------------------------------------------------- 第二层：比对


def compare_upstream(pin: dict, repo_root: Path = REPO_ROOT) -> tuple[list[str], list[str]]:
    """(漂移, 未能核对)。未能核对**不是**失败，但必须被打印出来。"""
    drift: list[str] = []
    unverified: list[str] = []
    for key, spec in pin["upstreams"].items():
        pinned = spec.get("pinned_by")
        if pinned is not None:
            # 按锁文件钉的上游：**没有「未能核对」这一档**。锁文件在库里，任何机器、
            # 任何 CI 上都读得到，所以这一条要么通过要么红 —— 这正是把外部上游变成
            # 带版本依赖的收益（RAY-420 的方案 B）。
            ok, note = locked_version(pinned, repo_root)
            if not ok:
                cited = sorted(
                    {doc for entry in spec["files"].values() for doc in entry["cited_by"]}
                )
                drift.append(
                    f"{key} 升版了：{note}\n"
                    f"      逐行转录它的是：{'、'.join(cited)}\n"
                    f"      重读那几份，确认转录的内容在新版里还成立，再改声明里的 version"
                )
            continue
        root = upstream_root(key, spec, repo_root)
        if root is None:
            unverified.append(
                f"{key}（{spec['repo_dir']}）不在本机，{len(spec['files'])} 个文件未核对"
            )
            continue
        for rel, entry in spec["files"].items():
            path = root / rel
            if not path.is_file():
                drift.append(f"{key}:{rel} 在上游已不存在（找过 {path}）")
                continue
            actual = sha256_of(path)
            if actual != entry["sha256"]:
                cited = "、".join(entry["cited_by"])
                drift.append(
                    f"{key}:{rel} 内容已变\n"
                    f"      声明 {entry['sha256'][:16]}…  实际 {actual[:16]}…\n"
                    f"      引用它的是：{cited}"
                )
    return drift, unverified


def recent_commits(root: Path, rel: str, count: int = 3) -> list[str]:
    """上游里动过这个文件的最近几次提交。尽力而为：上游不是 git 仓库就返回空。"""
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "log", f"-{count}", "--oneline", "--", rel],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,  # 提示信息而已，上游不是 git 仓库不该让本检查崩掉
        )
    except (OSError, subprocess.SubprocessError):
        return []
    return proc.stdout.strip().splitlines() if proc.returncode == 0 else []


# --------------------------------------------------------------------------- 入口


def update(pin: dict, repo_root: Path = REPO_ROOT) -> int:
    changed = 0
    for key, spec in pin["upstreams"].items():
        if spec.get("pinned_by") is not None:
            # 按锁文件钉的上游没有逐文件摘要可重钉。它的版本由 pyproject 的
            # [tool.uv.sources] 决定，声明里的 version 要**手改**，因为改它的前提是
            # 已经重读过 cited_by 里那几份文档 —— 那件事脚本做不了。
            print(
                f"{key}：按 {spec['pinned_by']['lock']} 钉住，无逐文件摘要可重钉。"
                f"升版后手改声明里的 pinned_by.version（先重读 cited_by 的文档）",
                file=sys.stderr,
            )
            continue
        root = upstream_root(key, spec, repo_root)
        if root is None:
            print(f"{key}：不在本机，跳过（{spec['repo_dir']}）", file=sys.stderr)
            continue
        for rel, entry in spec["files"].items():
            path = root / rel
            if not path.is_file():
                print(f"  ! {key}:{rel} 上游已不存在，未更新", file=sys.stderr)
                continue
            actual = sha256_of(path)
            if actual != entry.get("sha256"):
                print(f"  {key}:{rel}\n    {entry.get('sha256', '(无)')[:16]}… -> {actual[:16]}…")
                entry["sha256"] = actual
                changed += 1
    PIN_PATH.write_text(
        json.dumps(pin, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    if changed:
        print(
            f"\n已重钉 {changed} 个文件。**摘要变了意味着引用它们的文档需要人重读一遍** —— "
            "脚本判断不了那些结论是否还成立。",
            file=sys.stderr,
        )
    else:
        print("无变化。")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="上游引用声明与漂移检查")
    parser.add_argument(
        "--update", action="store_true", help="按上游当前内容重新钉住摘要"
    )
    args = parser.parse_args(argv)

    try:
        pin = load_pin(PIN_PATH)
    except PinError as exc:
        print(f"上游引用声明不可用：{exc}", file=sys.stderr)
        return 1

    if args.update:
        return update(pin, REPO_ROOT)

    try:
        tracked = tracked_files(REPO_ROOT)
    except (OSError, subprocess.CalledProcessError) as exc:
        # 拿不到文件清单就没法做第一层。这时候返回 0 等于假装检查过了。
        print(f"无法列出 git 跟踪的文件，第一层检查无法进行：{exc}", file=sys.stderr)
        return 1

    problems = undeclared_citations(REPO_ROOT, tracked, pin)
    # 两类上游要分开算：按锁文件钉的没有「未能核对」这一档，也没有上游 git 仓库
    # 可查提交。**必须在下面任何报告之前定义** —— 第一版把它放在漂移报告之后，
    # 于是那条路径一旦真的走到就抛 UnboundLocalError，而那是唯一要紧的路径。
    by_lock = {k: s for k, s in pin["upstreams"].items() if s.get("pinned_by")}
    by_sibling = {k: s for k, s in pin["upstreams"].items() if not s.get("pinned_by")}
    drift, unverified = compare_upstream(pin, REPO_ROOT)

    if problems:
        print("上游引用未声明：", file=sys.stderr)
        for line in problems:
            print(f"  {line}", file=sys.stderr)
        print(
            "\n引用了别的仓库的文件，就要在 tools/upstream_refs.json 里钉住它 —— "
            "否则那边改了这边不会知道，而一份说错话的文档不会让任何测试变红。",
            file=sys.stderr,
        )

    if drift:
        print("上游已漂移：", file=sys.stderr)
        for line in drift:
            print(f"  {line}", file=sys.stderr)
        for key, spec in by_sibling.items():
            root = upstream_root(key, spec, REPO_ROOT)
            if root is None:
                continue
            for rel in spec["files"]:
                if any(line.startswith(f"{key}:{rel} ") for line in drift):
                    for commit in recent_commits(root, rel):
                        print(f"      上游提交 {commit}", file=sys.stderr)
        print(
            "\n先读引用它的那几份文档，确认结论是否还成立。改完之后：\n"
            "  · 同级目录型上游（ffp）——`--update` 重钉逐文件摘要；\n"
            "  · 按锁文件钉的上游（Foundation）——**手改**声明里的 `pinned_by.version`。\n"
            "两种都不要在没重读文档之前做：那等于把一次没做过的核对记成做过了。",
            file=sys.stderr,
        )

    total = sum(len(s["files"]) for s in by_sibling.values())
    checked = total - sum(
        len(s["files"]) for k, s in by_sibling.items() if upstream_root(k, s, REPO_ROOT) is None
    )
    if unverified:
        print("未能核对（上游不在本机，属实情，非跳过）：", file=sys.stderr)
        for line in unverified:
            print(f"  {line}", file=sys.stderr)

    if problems or drift:
        return 1

    locked = "；".join(locked_version(s["pinned_by"], REPO_ROOT)[1] for s in by_lock.values())
    print(
        f"上游引用检查通过：声明完整（扫了 {len(tracked)} 个跟踪文件），"
        f"内容比对 {checked}/{total} 个上游文件"
        + (f"；{locked}" if locked else "")
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
