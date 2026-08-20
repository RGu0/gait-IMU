"""The layer skeleton is structure, so the test that guards it is structural.

It checks that every layer declared in ``gait.LAYERS`` actually exists and
imports, and — the half that matters more — that no layer package exists
which ``LAYERS`` does not declare. Without the second direction a package
could be added silently and the layering guard, which reads the same tuple,
would never look at it.
"""

import ast
import importlib
import pkgutil
from pathlib import Path

import gait


def test_version_is_exposed():
    assert gait.__version__


def test_every_declared_layer_imports():
    for layer in gait.LAYERS:
        module = importlib.import_module(f"gait.{layer}")
        assert module.__doc__, f"gait.{layer} must document what it is for"


def test_declared_layers_match_the_packages_on_disk():
    found = {
        info.name
        for info in pkgutil.iter_modules(gait.__path__)
        if info.ispkg
    }
    assert found == set(gait.LAYERS)


def test_config_is_importable():
    config = importlib.import_module("gait.config")
    assert config.__doc__


def test_config_depends_on_no_layer():
    """``config.py`` is readable by every layer, so it may depend on none.

    Parsed rather than string-matched. A substring test for ``"import gait"``
    passes for ``from gait import core`` — which is precisely the dependency
    being forbidden — so it would report a clean config file while the rule
    was being broken.
    """
    source = Path(gait.__file__).with_name("config.py")
    imported: set[str] = set()
    for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            # A relative import can only land inside gait, whatever it names.
            imported.add("gait" if node.level else (node.module or ""))

    offenders = sorted(
        name for name in imported if name == "gait" or name.startswith("gait.")
    )
    assert not offenders, f"config.py must not depend on any layer: {offenders}"
