"""`tools/check_upstream_refs.py` 的测试。

对着真实的 `tools/upstream_refs.json` 跑只能证明「现在没有违规」，证明不了「有违规时
会失败」—— 而后者才是这个检查存在的理由。所以主体是在临时目录里造出真实的违规。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import check_upstream_refs as chk

DIGEST_A = "a" * 64
UPSTREAM_TEXT = "上游内容\n"


@pytest.fixture(autouse=True)
def _no_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """环境变量会盖过目录搜索。本机上真跑着的那两个仓库不能影响临时目录里的用例。"""
    for key in ("GAIT_UPSTREAM_FFP", "GAIT_UPSTREAM_FOUNDATION", "GAIT_UPSTREAM_UP"):
        monkeypatch.delenv(key, raising=False)


def _pin(files: dict) -> dict:
    return {
        "upstreams": {
            "up": {
                "name": "上游",
                "repo_dir": "some-upstream",
                "inner": "main",
                "markers": ["up:", "SomeUpstream"],
                "files": files,
            }
        }
    }


def _entry(digest: str = DIGEST_A, cited: list[str] | None = None) -> dict:
    return {"sha256": digest, "bytes": 1, "cited_by": cited or ["docs/x.md"]}


def _repo(tmp_path: Path) -> Path:
    """本仓库在容器里的位置。上游是它的**兄弟**，所以两者都要在 tmp_path 之内 ——
    直接用 tmp_path.parent 会落到 pytest 的 `pytest-NNN/`，那是本次运行**所有用例共享
    的**目录，一个用例造的上游会被另一个用例找到。第一版就是这么写的，于是
    「上游不在本机」的用例取决于跑的顺序。"""
    repo = tmp_path / "container" / "gait-IMU"
    repo.mkdir(parents=True, exist_ok=True)
    return repo


def _upstream(tmp_path: Path, rel: str, text: str = UPSTREAM_TEXT) -> Path:
    """在本仓库的兄弟位置造一个上游仓库，模拟本机同级目录的布局。"""
    path = tmp_path / "container" / "some-upstream" / "main" / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    # 显式 newline="\n"：默认文本模式在 Windows 上把 \n 翻成 \r\n，夹具的内容
    # 就会随平台变，用例便测不准自己想测的东西。
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


# --------------------------------------------------------------- 第一层：完整性


def test_同行引用未声明时报错(tmp_path: Path) -> None:
    (tmp_path / "doc.md").write_text(
        "参考 SomeUpstream `pkg/thing.py`：某个做法。\n", encoding="utf-8"
    )
    problems = chk.undeclared_citations(tmp_path, ["doc.md"], _pin({"pkg/other.py": _entry()}))
    assert len(problems) == 1
    assert "pkg/thing.py" in problems[0]
    assert "doc.md:1" in problems[0]


def test_同行引用已声明时通过(tmp_path: Path) -> None:
    (tmp_path / "doc.md").write_text(
        "参考 SomeUpstream `pkg/thing.py`：某个做法。\n", encoding="utf-8"
    )
    assert chk.undeclared_citations(tmp_path, ["doc.md"], _pin({"pkg/thing.py": _entry()})) == []


def test_文档只写文件名也算命中(tmp_path: Path) -> None:
    """两卷与 docs 里常只写 `iam.py`，声明里写的是完整路径。按路径段后缀对齐。"""
    (tmp_path / "doc.md").write_text("SomeUpstream 改了 `thing.py`\n", encoding="utf-8")
    pin = _pin({"src/pkg/thing.py": _entry()})
    assert chk.undeclared_citations(tmp_path, ["doc.md"], pin) == []


def test_前缀式引用不需要同行标记(tmp_path: Path) -> None:
    """`up:pkg/thing.py` 是精确形式，不走同行启发式。"""
    (tmp_path / "doc.md").write_text("见 `up:pkg/thing.py:12-20`。\n", encoding="utf-8")
    assert chk.undeclared_citations(tmp_path, ["doc.md"], _pin({"pkg/thing.py": _entry()})) == []

    problems = chk.undeclared_citations(tmp_path, ["doc.md"], _pin({"pkg/other.py": _entry()}))
    assert len(problems) == 1
    assert "pkg/thing.py" in problems[0]


def test_没有上游标记的路径不管(tmp_path: Path) -> None:
    """否则 packages/design-system 里那些外部路径会被当成上游引用。实测过：
    窗口一放宽到 ±3 行就会误报，所以窗口取 0。"""
    (tmp_path / "doc.md").write_text("见 `HealthDesignSystem/README.md`。\n", encoding="utf-8")
    assert chk.undeclared_citations(tmp_path, ["doc.md"], _pin({"pkg/thing.py": _entry()})) == []


def test_标记与引用不同行不算命中(tmp_path: Path) -> None:
    """这是本规则**已知的假阴性**，写成测试是为了它变了会被看见，不是为了赞成它。
    兜底靠声明里的 cited_by（见模块文档）。"""
    (tmp_path / "doc.md").write_text("SomeUpstream 那边\n改了 `pkg/thing.py`。\n", encoding="utf-8")
    assert chk.undeclared_citations(tmp_path, ["doc.md"], _pin({"pkg/other.py": _entry()})) == []


def test_指向本仓库自己的路径不算上游引用(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "thing.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "doc.md").write_text("SomeUpstream 也有 `thing.py`，本仓库的见此。\n", encoding="utf-8")
    pin = _pin({"pkg/other.py": _entry()})
    assert chk.undeclared_citations(tmp_path, ["doc.md", "src/thing.py"], pin) == []


def test_读不开的文件被报出来而不是跳过(tmp_path: Path) -> None:
    """静默跳过等于给检查开一个看不见的口子 —— 那正是本检查要修的毛病。"""
    (tmp_path / "bad.md").write_bytes(b"\xff\xfe\x00\x01 not utf-8")
    problems = chk.undeclared_citations(tmp_path, ["bad.md"], _pin({"pkg/t.py": _entry()}))
    assert len(problems) == 1
    assert "无法读取" in problems[0]


def test_不扫二进制与锁文件(tmp_path: Path) -> None:
    (tmp_path / "uv.lock").write_text("SomeUpstream `pkg/thing.py`\n", encoding="utf-8")
    assert chk.undeclared_citations(tmp_path, ["uv.lock"], _pin({"pkg/o.py": _entry()})) == []


# ----------------------------------------------------------------- 第二层：比对


def test_上游内容变了就红(tmp_path: Path) -> None:
    _upstream(tmp_path, "pkg/thing.py", "改过的内容\n")
    drift, unverified = chk.compare_upstream(_pin({"pkg/thing.py": _entry()}), _repo(tmp_path))
    assert unverified == []
    assert len(drift) == 1
    assert "内容已变" in drift[0]
    assert "docs/x.md" in drift[0]  # cited_by 要出现在报错里，否则不知道该去重读什么


def test_上游内容未变就通过(tmp_path: Path) -> None:
    import hashlib

    _upstream(tmp_path, "pkg/thing.py")
    digest = hashlib.sha256(UPSTREAM_TEXT.encode("utf-8")).hexdigest()
    drift, unverified = chk.compare_upstream(_pin({"pkg/thing.py": _entry(digest)}), _repo(tmp_path))
    assert (drift, unverified) == ([], [])


def test_上游是crlf检出时不算漂移(tmp_path: Path) -> None:
    """Windows 上带 core.autocrlf 的检出会把每一行都变成 CRLF。若按原样取摘要，
    14 个文件会全部报红而上游一个字符没改 —— 那种红按平台触发而非按事实触发，
    且唯一的消解办法是重钉，等于把这道闸训练成「看到红就 --update」。"""
    import hashlib

    path = _upstream(tmp_path, "pkg/thing.py")
    path.write_bytes(UPSTREAM_TEXT.replace("\n", "\r\n").encode("utf-8"))
    digest = hashlib.sha256(UPSTREAM_TEXT.encode("utf-8")).hexdigest()
    drift, _ = chk.compare_upstream(_pin({"pkg/thing.py": _entry(digest)}), _repo(tmp_path))
    assert drift == []


def test_上游文件消失是最强的漂移(tmp_path: Path) -> None:
    _upstream(tmp_path, "pkg/other.py")
    drift, _ = chk.compare_upstream(_pin({"pkg/thing.py": _entry()}), _repo(tmp_path))
    assert len(drift) == 1
    assert "已不存在" in drift[0]


def test_上游不在本机时如实报告而不是静默通过(tmp_path: Path) -> None:
    drift, unverified = chk.compare_upstream(_pin({"pkg/thing.py": _entry()}), _repo(tmp_path))
    assert drift == []
    assert len(unverified) == 1
    assert "不在本机" in unverified[0]
    assert "1 个文件未核对" in unverified[0]


def test_环境变量可以指定上游位置(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    elsewhere = tmp_path / "somewhere-else"
    elsewhere.mkdir()
    monkeypatch.setenv("GAIT_UPSTREAM_UP", str(elsewhere))
    spec = _pin({})["upstreams"]["up"]
    assert chk.upstream_root("up", spec, _repo(tmp_path)) == elsewhere


def test_逐级向上找上游而不是写死相对深度(tmp_path: Path) -> None:
    """main/ worktree 与 .worktrees/<issue>/<scope>/ 到同级目录的深度不同。"""
    _upstream(tmp_path, "pkg/thing.py")
    deep = _repo(tmp_path) / "a" / "b" / "c"
    deep.mkdir(parents=True)
    spec = _pin({})["upstreams"]["up"]
    expected = tmp_path / "container" / "some-upstream" / "main"
    assert chk.upstream_root("up", spec, deep) == expected


# ----------------------------------------------------------------- 声明本身


def _write_pin(tmp_path: Path, payload: object) -> Path:
    path = tmp_path / "pin.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def test_声明不存在时抛错而不是当成空(tmp_path: Path) -> None:
    with pytest.raises(chk.PinError, match="不存在"):
        chk.load_pin(tmp_path / "missing.json")


def test_声明不是合法json时抛错(tmp_path: Path) -> None:
    path = tmp_path / "pin.json"
    path.write_text("{ 坏的", encoding="utf-8")
    with pytest.raises(chk.PinError, match="合法 JSON"):
        chk.load_pin(path)


@pytest.mark.parametrize(
    ("payload", "match"),
    [
        ({}, "upstreams"),
        ({"upstreams": {}}, "upstreams"),
        ({"upstreams": {"up": {"markers": ["x"], "files": {"a.py": {}}}}}, "repo_dir"),
        ({"upstreams": {"up": {"repo_dir": "d", "markers": [], "files": {"a.py": {}}}}}, "markers"),
        ({"upstreams": {"up": {"repo_dir": "d", "markers": ["x"], "files": {}}}}, "files"),
    ],
)
def test_声明结构不全时抛错(tmp_path: Path, payload: object, match: str) -> None:
    with pytest.raises(chk.PinError, match=match):
        chk.load_pin(_write_pin(tmp_path, payload))


def test_上游路径不是字符串时抛错(tmp_path: Path) -> None:
    """否则 `ancestor / repo_dir` 会抛 TypeError —— 一次崩溃看起来像检查坏了，
    而不像声明坏了。"""
    payload = _pin({"a.py": _entry()})
    payload["upstreams"]["up"]["repo_dir"] = ["d"]
    with pytest.raises(chk.PinError, match="必须是字符串"):
        chk.load_pin(_write_pin(tmp_path, payload))


def test_摘要格式不对时抛错(tmp_path: Path) -> None:
    payload = _pin({"a.py": {"sha256": "短", "cited_by": ["x"]}})
    with pytest.raises(chk.PinError, match="十六进制"):
        chk.load_pin(_write_pin(tmp_path, payload))


def test_没人引用的声明是死条目(tmp_path: Path) -> None:
    """它只会让人以为覆盖面比实际大。"""
    payload = _pin({"a.py": {"sha256": DIGEST_A, "cited_by": []}})
    with pytest.raises(chk.PinError, match="cited_by"):
        chk.load_pin(_write_pin(tmp_path, payload))


# ----------------------------------------------------------------- 真实仓库


def test_本仓库当前状态干净() -> None:
    pin = chk.load_pin()
    tracked = chk.tracked_files()
    assert chk.undeclared_citations(chk.REPO_ROOT, tracked, pin) == []


def test_文件清单用nul分隔() -> None:
    """仓库里有中文文件名。按空白切分会切出读不开的路径，然后被静默跳过。"""
    tracked = chk.tracked_files()
    assert "docs/云端接口约定.md" in tracked
    assert all((chk.REPO_ROOT / f).exists() for f in tracked)
