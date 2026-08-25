"""物理对碰锚点分析。cli 的 `anchor`（RAY-212，工程模式）。

**不进机构产品流程。** 这是 V3′（RAY-213）的实验室工具：两个模块外壳对碰是
同一个物理事件，冲击峰在左右数据流里的时刻差就是双足时基偏差的真值。产品流程
的同步机制是主机侧接收时刻（`gait.sync.timebase`），本工具量的正是它的误差。

用法（离线，消费 RAY-198/200 的落盘格式）::

    python -m gait.cli.anchor --left raw/left.raw --right raw/right.raw --out out/
    python -m gait.cli.anchor --session <会话目录> --out out/

输入是 wt901 录制文件（JSON Lines，`{"t": …, "hex": …}`）。`t` 是 BLE 回调的
真实到达时刻（`ThreadedRecordingWriter` 在回调里取 `time.monotonic()`），与
`ImuSample.t_host` 同源同钟 —— 所以离线分析给出的时基与现场完全一致，这条
性质继承自 `linktest.analyze_recordings`（RAY-200）。

## 两份录制文件的时钟零点不同 —— 这决定了哪些统计量可信

wt901 的 `RecordingWriter` 把每份文件的 `t` 归零到**该文件自己的第一段字节**。
左右两份录制的零点差是一个未知常数（两台设备开流先后之差，可达秒级）。
本工具按是否拿得到零点信息分两档工作：

- **给出 `--left-epoch/--right-epoch`**（各文件 t=0 对应的绝对 `time.monotonic()`
  值，即首块字节的回调时刻）：两条时间轴恢复共钟，**均值即绝对跨足偏差**。
  epoch 的采集侧持久化归设备层（RAY-198/200 的后续），本工具只消费。
- **不给 epoch**：先用对碰序列自身做粗对齐（`sync.anchor.coarse_alignment`，
  平移量记进报告的 `alignment_applied_s`），否则秒级零点差直接冲破配对窗。
  代价：均值按构造在零附近，**不携带绝对偏移的信息**；标准差、分位散布与漂移
  不受常数平移影响 —— RAY-212 的验收量（20 次对碰的标准差）与 RAY-263 差分法
  的散布互比仍然成立。

## 实验流程提示（RAY-213 用）

站立静止，两模块佩戴位或手持相碰，**干脆地碰、间隔至少半秒**：间隔小于合并
窗口（默认 0.1 s）的连击会被并成一次；拖着蹭会展宽事件、降低峰时刻精度。
削顶（±16 g 满量程）不作废数据，但对应的对会标 `degraded` —— 力道以报告里
不出现削顶为宜。
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import numpy as np
from wt901.protocol.frames import FrameDecoder, FrameFlag, decode_data_frame
from wt901.protocol.units import STANDARD_GRAVITY, accel_to_m_s2
from wt901.recording import read_recording

from gait.config import AlgoConfig
from gait.io.session import RAW_DIRNAME, RAW_FILENAMES
from gait.sync.anchor import AnchorError, AnchorReport, FootSignal, measure_offsets
from gait.sync.timebase import TimebaseError

__all__ = ["load_foot_signal", "main"]

#: 触及满量程的原始计数值门槛。int16 满量程是 ±32768（对应 ±16 g），器件在
#: 削顶时未必精确停在 32767，留 2% 余量。正常步行的踝部加速度离 15.7 g 很远，
#: 不会误标。
CLIP_COUNT = int(0.98 * 32768)


@dataclass(frozen=True)
class LoadedFoot:
    """一份录制文件解出的检测输入与来源信息。"""

    signal: FootSignal
    device_id: str
    source: Path

    @property
    def frames(self) -> int:
        """样本数。派生而不是存储：若将来有裁剪步骤，存的数会静默报出裁剪前的值。"""
        return int(self.signal.magnitude.size)


def load_foot_signal(path: Path, epoch: float = 0.0) -> LoadedFoot:
    """录制文件 → 逐样本 (模值, 到达时刻, 削顶标志)。

    与 `linktest.analyze_recordings` 相同的解法：一段字节里的多帧共享该段的
    到达时刻 —— 现场采集也是如此（同一次通知里的样本 `t_host` 只差几微秒）。
    削顶从**原始计数值**判定：模值层面看不出单轴削顶（另两轴的贡献把它垫高），
    原始计数值看得出。

    `epoch` 是该文件 t=0 对应的绝对 `time.monotonic()` 值。传入即把时间轴平移
    回共钟；默认 0 保持文件自己的零点（见模块文档"时钟零点"一节）。
    """
    recording = read_recording(path)
    decoder = FrameDecoder()
    magnitude: list[float] = []
    arrival: list[float] = []
    clipped: list[bool] = []
    for chunk in recording.chunks:
        for frame in decoder.feed(chunk.data):
            if frame.flag is not FrameFlag.DATA:
                continue
            counts = decode_data_frame(frame)
            acc = [accel_to_m_s2(value) for value in counts[0:3]]
            magnitude.append(math.hypot(*acc))
            arrival.append(chunk.t + epoch)
            clipped.append(any(abs(value) >= CLIP_COUNT for value in counts[0:3]))
    return LoadedFoot(
        signal=FootSignal(
            magnitude=np.asarray(magnitude),
            arrival=np.asarray(arrival),
            clipped=np.asarray(clipped, dtype=bool),
        ),
        device_id=recording.device_id,
        source=path,
    )


def _foot_paths(args: argparse.Namespace) -> tuple[Path, Path]:
    if args.session is not None:
        raw = Path(args.session) / RAW_DIRNAME
        return raw / RAW_FILENAMES["L"], raw / RAW_FILENAMES["R"]
    return Path(args.left), Path(args.right)


def _summary(
    report: AnchorReport, left: LoadedFoot, right: LoadedFoot, common_clock: bool, echo
) -> None:
    echo(f"左足 {left.device_id}：{left.frames} 样本，来自 {left.source}")
    echo(f"右足 {right.device_id}：{right.frames} 样本，来自 {right.source}")
    for label, sync in (("左", report.left_sync), ("右", report.right_sync)):
        stability = "稳定" if sync.stable else "⚠️ 不稳定（分窗采样率差 ≥ 0.1%）"
        echo(
            f"{label}足时基：实测 {sync.fs:.4f} Hz"
            f"（偏差 {sync.fs_deviation_ppm:+.0f} ppm），{stability}"
        )
    echo("")
    if not report.pairs:
        echo("未配对到任何对碰。检查：录制里是否包含对碰段？阈值（默认 3 g 模值）是否过高？")
    for number, pair in enumerate(report.pairs, start=1):
        flags = []
        if pair.left.peak.clipped or pair.right.peak.clipped:
            flags.append("削顶")
        elif not (pair.left.peak.interpolated and pair.right.peak.interpolated):
            # 削顶必然不插值，只在未削顶却插值失败时单独说。
            flags.append("未插值")
        note = f"（{'、'.join(flags)}）" if flags else ""
        echo(
            f"对碰 #{number}: 左 t_host={pair.left.t_host:.4f}s"
            f" 右 t_host={pair.right.t_host:.4f}s"
            f" Δ={pair.delta * 1e3:+.2f} ms"
            f" 峰 {pair.left.peak.magnitude / STANDARD_GRAVITY:.1f}/"
            f"{pair.right.peak.magnitude / STANDARD_GRAVITY:.1f} g{note}"
        )
    if report.unpaired_left or report.unpaired_right:
        echo(
            f"落单峰：左 {len(report.unpaired_left)} 个、右 {len(report.unpaired_right)} 个"
            "（轻碰单侧漏检或阈值边缘，未计入统计）"
        )
    if report.pairs:
        echo("")
        echo(
            f"跨足偏移（左 − 右，主机时基）：n={len(report.pairs)}"
            f" 均值 {report.offset_mean * 1e3:+.2f} ms"
            f" 标准差 {report.offset_std * 1e3:.2f} ms"
        )
        echo(
            f"  中位 {report.offset_median * 1e3:+.2f} ms"
            f" | |Δ| 90 分位 {report.offset_p90_abs * 1e3:.2f} ms"
            f" | |Δ| 最大 {report.offset_max_abs * 1e3:.2f} ms"
            f" | 漂移 {report.drift_s_per_min * 1e3:+.3f} ms/min"
        )
        if common_clock:
            echo(
                "两侧时间轴已按 epoch 恢复共钟：均值即主机侧同步的跨足误差真值"
                "（物理事件时刻在两侧相减时消掉，剩下两台设备固有链路延迟之差）。"
            )
        else:
            shift = report.alignment_applied_s
            shown = f"{shift:+.4f} s" if shift is not None else "无"
            echo(
                f"⚠️ 未提供 epoch：两份录制各自归零，已按对碰序列粗对齐（平移左侧 {shown}）。"
                "均值按构造在零附近，**不携带绝对偏移的信息**；标准差、分位散布与"
                "漂移不受常数平移影响，仍然有效。要得到绝对偏差请提供 epoch。"
            )


def _payload(
    report: AnchorReport,
    left: LoadedFoot,
    right: LoadedFoot,
    nominal_fs: float,
    common_clock: bool,
) -> dict[str, Any]:
    body = report.snapshot()
    body["nominal_fs"] = nominal_fs
    # False 表示两份录制各自归零：offset.mean_s 含未知零点差常数，只有散布与
    # 漂移可信。读报告的人（RAY-213）第一个要看的就是这个标志。
    body["common_clock"] = common_clock
    body["left_source"] = {
        "device_id": left.device_id,
        "path": str(left.source),
        "frames": left.frames,
    }
    body["right_source"] = {
        "device_id": right.device_id,
        "path": str(right.source),
        "frames": right.frames,
    }
    return body


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m gait.cli.anchor",
        description="物理对碰锚点分析（工程模式）：从双足录制文件量出主机侧同步的跨足误差真值。",
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--session", help="会话目录（读 raw/left.raw 与 raw/right.raw）")
    source.add_argument("--left", help="左足录制文件（wt901 JSON Lines）")
    parser.add_argument("--right", help="右足录制文件，与 --left 配套")
    parser.add_argument("--nominal-fs", type=float, default=200.0, help="标称采样率，Hz（默认 200）")
    parser.add_argument(
        "--threshold-g",
        type=float,
        default=None,
        help="冲击检测阈值，g（模值）。默认用 AlgoConfig 的 3 g。",
    )
    parser.add_argument("--out", type=Path, default=None, help="报告输出目录（写 anchor_report.json）")
    parser.add_argument(
        "--left-epoch",
        type=float,
        default=None,
        help="左足录制 t=0 对应的绝对 time.monotonic() 值。两侧都给出时恢复共钟，均值才是绝对偏差。",
    )
    parser.add_argument("--right-epoch", type=float, default=None, help="右足录制的同上")
    args = parser.parse_args(argv)
    if args.left is not None and args.right is None:
        parser.error("--left 需要配套的 --right")
    if args.session is not None and args.right is not None:
        # 静默忽略比报错危险：换单侧文件重跑时误带 --session，会分析错误的录制
        # 而退出码还是 0。
        parser.error("--right 只与 --left 配套；--session 自带左右文件路径")
    if (args.left_epoch is None) != (args.right_epoch is None):
        parser.error("--left-epoch 与 --right-epoch 必须成对给出：单边平移只是换一个未知常数")
    common_clock = args.left_epoch is not None

    left_path, right_path = _foot_paths(args)
    try:
        # 阈值换算放在 try 内：ConfigError 是 ValueError，让 --threshold-g 0
        # 走同一条"锚点分析失败"路径，而不是裸栈退出。
        cfg = AlgoConfig()
        if args.threshold_g is not None:
            cfg = replace(cfg, anchor_threshold_m_s2=args.threshold_g * STANDARD_GRAVITY)
        left = load_foot_signal(left_path, epoch=args.left_epoch or 0.0)
        right = load_foot_signal(right_path, epoch=args.right_epoch or 0.0)
        # 无 epoch 时两份文件各自归零，零点差可达秒级，必须先粗对齐才配得上对。
        report = measure_offsets(
            left.signal, right.signal, args.nominal_fs, cfg, coarse_align=not common_clock
        )
    except (AnchorError, TimebaseError, ValueError, OSError) as error:
        print(f"锚点分析失败：{error}", file=sys.stderr)
        return 2

    _summary(report, left, right, common_clock, echo=print)
    if args.out is not None:
        args.out.mkdir(parents=True, exist_ok=True)
        target = args.out / "anchor_report.json"
        target.write_text(
            json.dumps(
                _payload(report, left, right, args.nominal_fs, common_clock),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        print(f"报告已写入 {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
