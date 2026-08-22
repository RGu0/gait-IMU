"""分层红线检查的测试。

这里最重要的不是"真实的 core/ 目前干净"—— 那是现状，随时会变，而且一个永远返回
空列表的检查也能让它通过。重要的是**有违规时会被抓到**，且四种 import 拼法一条都
不漏。所以每条都在临时目录里造出真实的违规文件，让检查去扫。
"""

import os
from pathlib import Path

import check_layering
import pytest

import gait

FORBIDDEN = {"gait.io", "gait.device", "gait.sync"}


def build_package(root: Path, core_source: str) -> Path:
    """造一个最小的 gait/ 包，core/probe.py 内容由调用方给定。"""
    package = root / "gait"
    (package / "core").mkdir(parents=True)
    (package / "io").mkdir()
    (package / "__init__.py").write_text('"""probe package."""\n', encoding="utf-8")
    (package / "core" / "__init__.py").write_text('"""probe core."""\n', encoding="utf-8")
    (package / "io" / "__init__.py").write_text('"""probe io."""\n', encoding="utf-8")
    (package / "core" / "probe.py").write_text(core_source, encoding="utf-8")
    return package


VIOLATIONS = [
    pytest.param("import gait.io\n", id="import gait.io"),
    pytest.param("from gait.io import session\n", id="from gait.io import x"),
    pytest.param("from gait import io\n", id="from gait import io"),
    pytest.param("from ..io import session\n", id="from ..io import x (相对)"),
    pytest.param("from .. import io\n", id="from .. import io (相对)"),
    pytest.param("import gait.io as storage\n", id="import ... as 别名"),
    pytest.param("def f():\n    import gait.io\n", id="函数体内的延迟 import"),
]


@pytest.mark.parametrize("source", VIOLATIONS)
def test_every_spelling_of_a_violation_is_caught(tmp_path, source):
    package = build_package(tmp_path, source)
    reported = check_layering.scan(package, "core", FORBIDDEN)
    assert reported, f"未能抓到违规写法：{source!r}"
    assert "gait.io" in reported[0]


CLEAN = [
    pytest.param("import math\n", id="标准库"),
    pytest.param("import numpy as np\n", id="第三方"),
    pytest.param("from . import quaternion\n", id="同层相对 import"),
    pytest.param("from gait.core import quaternion\n", id="同层绝对 import"),
    pytest.param("from gait import config\n", id="config（谁都能读）"),
    pytest.param('"""只有文档字符串。"""\n', id="无 import"),
]


@pytest.mark.parametrize("source", CLEAN)
def test_allowed_imports_are_not_flagged(tmp_path, source):
    package = build_package(tmp_path, source)
    assert check_layering.scan(package, "core", FORBIDDEN) == []


def test_report_names_the_file_and_line(tmp_path):
    package = build_package(tmp_path, "import math\nimport gait.device\n")
    reported = check_layering.scan(package, "core", {"gait.device"})
    assert len(reported) == 1
    assert "probe.py:2:" in reported[0]
    assert "gait.device" in reported[0]


def test_nested_modules_are_scanned(tmp_path):
    package = build_package(tmp_path, "import math\n")
    nested = package / "core" / "filters"
    nested.mkdir()
    (nested / "__init__.py").write_text('"""nested."""\n', encoding="utf-8")
    (nested / "deep.py").write_text("from gait import sync\n", encoding="utf-8")
    reported = check_layering.scan(package, "core", FORBIDDEN)
    assert len(reported) == 1
    assert "deep.py" in reported[0]


THIRD_PARTY_VIOLATIONS = [
    pytest.param("import bleak\n", "bleak", id="import bleak"),
    pytest.param("import wt901\n", "wt901", id="import wt901"),
    pytest.param("from wt901 import ImuSample\n", "wt901", id="from wt901 import x"),
    pytest.param(
        "from wt901.models import ImuSample\n",
        "wt901",
        id="from wt901.models import x（只有首段能匹配）",
    ),
    pytest.param("import bleak.backends\n", "bleak", id="import bleak.子模块"),
]


@pytest.mark.parametrize(("source", "offender"), THIRD_PARTY_VIOLATIONS)
def test_forbidden_third_party_packages_are_caught(tmp_path, source, offender):
    """契约 §2 点名的 bleak，以及它的来源 wt901。

    `from wt901.models import X` 是这里最容易漏的一种：它的前两段是
    `wt901.models`，只比对前两段会整个放过去。
    """
    package = build_package(tmp_path, source)
    reported = check_layering.scan(
        package, "core", {"gait.io", "bleak", "wt901"}
    )
    assert reported, f"未能抓到第三方违规：{source!r}"
    assert offender in reported[0]


@pytest.mark.parametrize(
    "source",
    [
        pytest.param("import numpy as np\n", id="numpy 是允许的"),
        pytest.param("from scipy import signal\n", id="scipy 是允许的"),
        pytest.param("import wt901x\n", id="名字相近但不是它"),
    ],
)
def test_allowed_third_party_is_not_flagged(tmp_path, source):
    """禁止的是具体的包，不是按前缀一刀切。"""
    package = build_package(tmp_path, source)
    assert check_layering.scan(package, "core", {"bleak", "wt901"}) == []


def test_one_violation_is_reported_once(tmp_path):
    """`from wt901 import X` 同时产出 wt901 与 wt901.X，两者都命中同一条禁令。

    报两遍不会让检查更严，只会让它看起来坏了 —— 而一个看起来坏了的守卫，下一个
    人的第一反应是关掉它。
    """
    package = build_package(tmp_path, "from wt901 import ImuSample\n")
    reported = check_layering.scan(package, "core", {"wt901"})
    assert len(reported) == 1


def test_forbidden_list_comes_from_the_package_declaration():
    """禁止清单必须来自 gait 自身的声明，而不是检查脚本里另抄一份。"""
    assert check_layering.declared_forbidden() == {
        f"gait.{name}" for name in gait.CORE_FORBIDDEN_IMPORTS
    } | set(gait.CORE_FORBIDDEN_PACKAGES)


def test_third_party_red_line_names_bleak():
    """契约 §2 原文点名 bleak；wt901 是把它拖进来的那条路径。"""
    assert "bleak" in gait.CORE_FORBIDDEN_PACKAGES
    assert "wt901" in gait.CORE_FORBIDDEN_PACKAGES


def test_drifted_declaration_fails_instead_of_passing_quietly(monkeypatch):
    """禁止清单指向不存在的层时，必须失败而不是报告"没有违规"。"""
    monkeypatch.setattr(gait, "CORE_FORBIDDEN_IMPORTS", ("io", "nonexistent"))
    with pytest.raises(ValueError, match="nonexistent"):
        check_layering.declared_forbidden()


def test_the_real_core_is_currently_clean():
    """现状检查。它会失败正说明红线起了作用，不是测试坏了。"""
    assert check_layering.scan(
        check_layering.PACKAGE_ROOT, "core", check_layering.declared_forbidden()
    ) == []


def test_output_survives_a_legacy_codepage(tmp_path):
    """在遗留代码页下不得崩溃。

    RAY-258 首次在 windows-latest 上运行 `dev.ps1` 时，这个脚本炸在它自己的**成功**
    消息上：Windows 的 Python stdout 在管道下用 cp1252，而消息是中文。也就是说仓库
    干净时也会失败 —— 该检查自 RAY-192 合并起在 Windows 上就完全不可用，只是没有
    Windows CI 所以无人知道。

    这条测试用 cp1252 复现当时的环境。它守的不是"支持中文"，而是**输出编码不该由
    调用环境决定**：一个在某些平台上必然崩溃的检查，等于在那些平台上不存在。
    """
    import subprocess
    import sys

    environment = {
        **os.environ,
        "PYTHONIOENCODING": "cp1252",
        "PYTHONUTF8": "0",
    }
    result = subprocess.run(
        [sys.executable, str(Path(check_layering.__file__))],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=environment,
        cwd=Path(check_layering.REPO_ROOT),
        check=False,  # 退出码由下面的断言判断，不由 subprocess 抛异常
    )
    assert result.returncode == 0, result.stderr
    assert "UnicodeEncodeError" not in result.stderr
