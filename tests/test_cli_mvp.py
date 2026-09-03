"""`cli.mvp` —— 端到端闭环的验收形式（RAY-345）。

直接走 `main()` 的 `--synthetic` 路径，断言它真的产出 `report.html` 且带八个段，
以及 `report.json` 能序列化、无 `NaN`。这条就是 Issue 验收第一条的可执行版。
"""

import json

from gait.cli.mvp import main


def test_synthetic_produces_html_and_json(tmp_path):
    code = main(["--synthetic", "--seconds", "20", "--out", str(tmp_path)])
    assert code == 0

    html_path = tmp_path / "report.html"
    json_path = tmp_path / "report.json"
    assert html_path.is_file()
    assert json_path.is_file()

    markup = html_path.read_text(encoding="utf-8")
    for heading in (
        "步态检测报告",
        "筛查摘要",
        "核心指标",
        "左右对比",
        "专业参数",
        "步态时序",
        "测试条件",
        "报告编号",
    ):
        assert heading in markup

    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["reportId"]
    assert payload["algoVersion"]
    assert payload["protocolVersion"]
    assert "NaN" not in json_path.read_text(encoding="utf-8")


def test_synthetic_requires_a_source_flag(tmp_path):
    # 无 --synthetic / --replay 时 argparse 报错，退出码非 0。
    import pytest

    with pytest.raises(SystemExit):
        main(["--out", str(tmp_path)])
