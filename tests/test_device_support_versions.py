"""RAY-752 `support-versions`：「设备与支持」的软件版本与算法版本。

此前 `deviceSupport` 不给 support 块，渲染端四项全填「未配置」—— 而报告页写着
gait-contract-1.2，同一个事实两页说法相反。现在：算法版本与会话元数据（报告读它）取同一个
常量；软件版本由主进程递来的版本号加构建提交拼成；没有来源的项不出现，渲染端照实写「未配置」。
"""

from __future__ import annotations

from pathlib import Path

from gait.app.__main__ import service_from_environment
from gait.app.service import ALGO_VERSION, TerminalService, app_version_label
from gait.app.sources import StubDeviceSource
from gait.contracts import CONTRACT_VERSION
from gait.io.session import read_meta


def _support(service: TerminalService) -> dict:
    response = service.handle({"id": "1", "method": "deviceSupport", "params": {}})
    assert response["status"] == "ok", response
    return response["result"]["support"]


def test_algo_version_is_the_one_the_report_reads(tmp_path: Path) -> None:
    service = TerminalService(source=StubDeviceSource(), session_root=tmp_path)
    assert service.handle({"id": "1", "method": "startSession", "params": {}})["status"] == "ok"
    meta = read_meta(tmp_path / service.session_id)
    # 报告页显示的是 meta.algo_version；设备页必须是同一个值，取自同一个常量。
    assert _support(service)["algoVersion"] == meta.algo_version == ALGO_VERSION
    assert ALGO_VERSION == f"gait-contract-{CONTRACT_VERSION}"


def test_app_version_carries_the_build_commit() -> None:
    service = TerminalService(source=StubDeviceSource(), app_version="0.1.0", build_commit="375f6086299855b7")
    assert _support(service)["appVersion"] == "0.1.0（375f608）"


def test_without_a_build_commit_it_says_development_build() -> None:
    # 开发态没有构建提交：如实写「开发构建」，不编一个提交号。
    for commit in (None, "", "   "):
        assert app_version_label("0.1.0", commit) == "0.1.0（开发构建）"
    service = TerminalService(source=StubDeviceSource(), app_version="0.1.0", build_commit="")
    assert _support(service)["appVersion"] == "0.1.0（开发构建）"


def test_no_version_from_the_main_process_means_no_claim() -> None:
    # CLI 直接起 sidecar、没有主进程递版本号：不出现 appVersion，渲染端显示「未配置」。
    support = _support(TerminalService(source=StubDeviceSource()))
    assert "appVersion" not in support
    assert app_version_label(None, "375f608") is None
    assert app_version_label("  ", "375f608") is None


def test_phone_and_terminal_id_stay_unconfigured() -> None:
    # Preview 没有这两项的配置来源，sidecar 不编，交给渲染端写「未配置」。
    support = _support(TerminalService(source=StubDeviceSource(), app_version="0.1.0"))
    assert set(support) == {"algoVersion", "appVersion"}


def test_the_environment_wires_the_versions_through() -> None:
    service = service_from_environment({"GAIT_APP_VERSION": "0.1.0", "GAIT_BUILD_COMMIT": "abcdef1234"})
    assert _support(service)["appVersion"] == "0.1.0（abcdef1）"
