"""最小 MVP 端到端闭环：合成/回放 → 基础链 → 报告（RAY-345）。

`python -m gait.cli.mvp --synthetic` 与 `--replay <会话目录>` 两条路径，都把数据跑过
`cloud.run_basic_chain`（前向 ESKF + 事件 + 指标 + 质量标注，不做平滑/锚定/双足约束），
再经 `report.assemble_report` 落成 `report.html`（外加一份 `report.json` 留档）。

## 为什么是基础链，而不是完整链

MVP 的目标是**证明闭环通**，不是数字准。完整链（RTS 平滑、零速锚定、双足距离约束）
每一步都依赖尚未合并的精度工作（RAY-325/328/339/343），把它们拉进来等于把「暂不追求
精度」的范围扩大到了「暂不追求稳定性」。基础链是已经立住、被 `test_cloud_chain` 钉住
的那条，闭环风险最低。

## 同步质量的占位

跨足指标（双支撑期占比）的产出要求 `sync_quality`（PRD §13）。合成数据里没有同步误差，
回放路径的时基在 MVP 里用标称采样率、也没算同步质量。两条路径都填同一份占位
`{"determinate": True, "flagged": False}`，并在页脚与 `report.json` 里不额外声明——
真实同步质量待 RAY-209/213，属已知降范围，不在此重复实现。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from gait.cloud.chain import run_basic_chain
from gait.config import CONFIG_VERSION, DEFAULT_DURATION_S
from gait.contracts import FootLabel, SessionMeta
from gait.device.footseries import load_session_foot
from gait.io.session import new_session_id, new_subject_uuid, read_meta
from gait.report.assemble import assemble_report
from gait.report.html import write_report_html
from gait.validate.synthetic import NoiseModel, WalkSpec, generate_dual_walk

#: MVP 的同步质量占位。见模块文档。
_SYNC_QUALITY: Final[dict[str, Any]] = {"determinate": True, "flagged": False}

#: 合成数据加一点器件量级的噪声，让变异系数这类指标不是「完美 0」——0% 的变异
#: 在真实数据里不存在，一个恒为 0 的 CV 反而像占位符。取值与 test_cloud_chain 的
#: SENSOR 同量级。
_SYNTHETIC_NOISE: Final[NoiseModel] = NoiseModel(
    accel_density=1.5e-3, gyro_density=3.0e-4, seed=3
)


def _synthetic_meta(seconds: int, algo_version: str, session_id: str) -> SessionMeta:
    """合成路径的一份最小可追溯元数据。字段齐了契约的强制项，数值是占位。"""
    return SessionMeta(
        session_id=session_id,
        created_at=datetime.now(UTC).isoformat(),
        subject_uuid=new_subject_uuid(),
        scenario="walk",
        devices={"L": {"mac": "synthetic"}, "R": {"mac": "synthetic"}},
        config_snapshot={"rate_hz": 200},
        calib_snapshot={"L": {}, "R": {}},
        algo_version=algo_version,
        algo_params={"preset": "default"},
        sync_report={"synthetic": True},
        integrity_report={"loss_rate": 0.0},
        protocol_config={"duration_s": seconds, "version": CONFIG_VERSION},
    )


def _synthetic_report(seconds: int) -> dict[str, Any]:
    """合成双足数据 → 基础链 → 报告 dict。"""
    dual = generate_dual_walk(WalkSpec(duration_s=float(seconds)), noise=_SYNTHETIC_NOISE)
    series_by_foot: dict[FootLabel, Any] = {
        label: series for label, (series, _truth) in dual.items()
    }
    chain = run_basic_chain(series_by_foot, sync_quality=_SYNC_QUALITY, protocol_seconds=seconds)
    meta = _synthetic_meta(
        seconds, chain.algo_version, session_id=new_session_id()
    )
    return assemble_report(chain, meta)


async def _replay_report(session_dir: Path) -> dict[str, Any]:
    """按会话目录回放双足 → 基础链 → 报告 dict。"""
    meta = read_meta(session_dir)
    root = session_dir.parent
    session_id = session_dir.name
    seconds = int(meta.protocol_config.get("duration_s", DEFAULT_DURATION_S))

    series_by_foot: dict[FootLabel, Any] = {}
    for foot in ("L", "R"):
        series_by_foot[foot] = await load_session_foot(root, session_id, foot)

    chain = run_basic_chain(series_by_foot, sync_quality=_SYNC_QUALITY, protocol_seconds=seconds)
    return assemble_report(chain, meta)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="gait-mvp",
        description="最小 MVP 端到端闭环：合成/回放 → 基础链 → report.html（RAY-345）",
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--synthetic",
        action="store_true",
        help="用合成双足数据跑闭环（不碰 I/O）",
    )
    source.add_argument(
        "--replay",
        type=Path,
        metavar="SESSION_DIR",
        help="回放一个已采集的会话目录（内含 meta.json 与 raw/）",
    )
    parser.add_argument(
        "--seconds",
        type=int,
        default=60,
        help="合成数据的时长（秒）。仅 --synthetic 生效，默认 60",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="输出目录，默认 mvp-report-<UTC 时间戳>",
    )
    args = parser.parse_args(argv)

    out_dir = args.out or Path(
        f"mvp-report-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"
    )

    if args.synthetic:
        report = _synthetic_report(args.seconds)
    else:
        report = asyncio.run(_replay_report(args.replay))

    out_dir.mkdir(parents=True, exist_ok=True)
    html_path = write_report_html(
        report, out_dir / "report.html", json_out=out_dir / "report.json"
    )
    print(f"报告已生成：{html_path}")
    print(f"  报告编号：{report['reportId']}")
    print(f"  算法版本：{report['algoVersion']}")
    print(f"  协议配置：{report['protocolVersion']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
