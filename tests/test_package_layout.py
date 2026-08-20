"""The layer skeleton is structure, so the test that guards it is structural.

It checks that every layer declared in ``gait.LAYERS`` actually exists and
imports, and — the half that matters more — that no layer package exists
which ``LAYERS`` does not declare. Without the second direction a package
could be added silently and the layering guard, which reads the same tuple,
would never look at it.
"""

import importlib
import pkgutil

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


def test_config_is_importable_and_depends_on_nothing():
    config = importlib.import_module("gait.config")
    assert config.__doc__
    source = (gait.__path__[0] + "/config.py")
    with open(source, encoding="utf-8") as handle:
        body = handle.read()
    assert "import gait" not in body, "config.py must not depend on any layer"
