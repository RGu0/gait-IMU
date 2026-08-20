"""分层红线检查的测试。

这里最重要的不是"真实的 core/ 目前干净"—— 那是现状，随时会变，而且一个永远返回
空列表的检查也能让它通过。重要的是**有违规时会被抓到**，且四种 import 拼法一条都
不漏。所以每条都在临时目录里造出真实的违规文件，让检查去扫。
"""

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


def test_forbidden_list_comes_from_the_package_declaration():
    """禁止清单必须来自 gait 自身的声明，而不是检查脚本里另抄一份。"""
    assert check_layering.declared_forbidden() == {
        f"gait.{name}" for name in gait.CORE_FORBIDDEN_IMPORTS
    }


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
