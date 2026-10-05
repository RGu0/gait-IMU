"""Entrypoint policy stays isolated from caller uv environment overrides."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(os.name == "nt", reason="POSIX execution; Windows parity checked statically")
@pytest.mark.parametrize("features", ["other", "other,centralized-project-envs"])
def test_python_probes_use_central_policy_without_sync(tmp_path, features):
    uv = tmp_path / "uv"
    uv.write_text('#!/bin/sh\nprintf "%s|%s|%s|%s|%s|%s|%s\\n" "$*" "$UV_PREVIEW_FEATURES" "${UV_PROJECT_ENVIRONMENT-unset}" "$UV_CONFIG_FILE" "${VIRTUAL_ENV-unset}" "${CONDA_PREFIX-unset}" "${UV_SYSTEM_PYTHON-unset}"\n')
    uv.chmod(0o755)
    env = dict(os.environ, PATH=f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
               UV_PROJECT_ENVIRONMENT="/foreign", UV_PREVIEW_FEATURES=features,
               VIRTUAL_ENV="/foreign", CONDA_PREFIX="/foreign", UV_SYSTEM_PYTHON="1")
    for action, arguments in (("python-info", "python find --show-version"), ("python-path", "python find")):
        result = subprocess.run([str(ROOT / "dev"), action], env=env, capture_output=True, text=True, check=False)
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == f"{arguments}|other,centralized-project-envs|unset|/dev/null|unset|unset|unset"


def test_manifest_and_windows_expose_the_python_probe_contract():
    assert ".venv" in (ROOT / ".gitignore").read_text().splitlines()
    assert 'python_resolution: "entrypoint"' in (ROOT / ".ai-project/project.yaml").read_text()
    source = (ROOT / "dev.ps1").read_text()
    for action in ("python-info", "python-path"):
        assert f'"{action}"' in source
    assert 'Remove-Item Env:UV_PROJECT_ENVIRONMENT' in source
    assert 'centralized-project-envs' in source
    assert '@("python", "find", "--show-version")' in source
    for name in ("VIRTUAL_ENV", "CONDA_PREFIX", "UV_SYSTEM_PYTHON"):
        assert f"Remove-Item Env:{name}" in source


@pytest.mark.skipif(os.name == "nt", reason="POSIX guard; Windows is checked statically")
def test_python_path_requires_a_project_locator(tmp_path):
    dev = tmp_path / "dev"
    shutil.copy2(ROOT / "dev", dev)
    result = subprocess.run([str(dev), "python-path"], capture_output=True, text=True, check=False)
    assert result.returncode != 0
    assert "locator" in result.stderr
