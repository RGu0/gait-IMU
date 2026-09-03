"""加计多姿态标定工装。cli 的 `accelcal`（RAY-207 R2，服务方流程）。

**不进机构产品流程**（FR-04：出厂标定由服务方完成并下发，机构侧不做）。

    # 服务方桌面：模块连本机，换 ~20 个任意稳定姿态
    python -m gait.cli.accelcal capture --out out/AA-BB-CC

    # 解算（不需要硬件，可在别处跑）
    python -m gait.cli.accelcal solve --dir out/AA-BB-CC

    # 静置漂移取证（可选，10 min）
    python -m gait.cli.accelcal still --out out/AA-BB-CC --minutes 10

## 操作员不需要摆准，只需要摆稳

判据是比力模长 `|A·m + c| = g`，**不使用姿态朝向**（理由见 `gait/calib/accel.py` 的
模块文档：R1 的六面法要求精确轴向对齐，而实测 2.8° 的摆放倾斜就会解出 22.8‰ 的假
交叉轴项）。所以提示语是「换一个姿态、放稳、手离开」，不是「把 X 轴朝上」。

工装只在两件事上拦人：这一段**没静置好**，以及**姿态数还不够**。

## 「散不散」只提示，不拦

采集时会告诉操作员这个姿态与之前的接近程度，但那是**建议**。真正的判据是解算时的
雅可比条件数 —— 它直接回答「这九个参数可不可辨」，而两两夹角只是它的一个粗糙代理。
在采集端再设一道夹角闸，就会与解算端的条件数形成两处判据，而两处判据迟早对不上
（`calib.still` 与 `calib.accel` 都因为这条删过闸）。

## 采集与解算分开，是因为采集跑不到解算里

macOS 的 TCC 会在非交互进程里直接中止蓝牙访问，采集只能在操作员自己的终端里跑。
`solve` 因此是独立子命令、只读文件，任何地方都能跑，也便于事后复算。

## 不碰模块的标定寄存器

本工装全程只**读**数据流。加计补偿在上位机完成，模块保持出厂原始态（FR-03）。
wt901 的 `device.calibration` 通道由 `tools/check_calibration_channel.py` 守着。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
from wt901 import WT901Device, scan

from gait.calib.accel import (
    MILLI_G,
    MIN_ORIENTATIONS,
    STANDARD_GRAVITY,
    OrientationObservation,
    observe_orientation,
    solve_orientations,
)
from gait.calib.still import CalibrationError
from gait.device.ble import (
    StreamConfig,
    close_quietly,
    configure_streaming,
    start_streaming,
)
from gait.device.identity import read_device_identity

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")

#: 每个姿态默认采多久。200 Hz 下 3 s ≈ 600 个样本，高于 `MIN_SAMPLES_PER_ORIENTATION`。
#: 比 R1 的 4 s 短，因为姿态数从 6 涨到了 20，总时长要压住。
DEFAULT_SECONDS: float = 3.0

#: 操作员摆好之后、开始收数之前的静置等待。手离开模块时会带一下，这段丢掉。
SETTLE_SECONDS: float = 1.2

#: 多久收不到新样本就当断流。200 Hz 下正常间隔是 5 ms，3 s 是三个数量级的余量。
STALL_TIMEOUT_SECONDS: float = 3.0

#: 提示「这个姿态和之前某个很接近」的夹角。**只提示不拦**，见模块文档。
CLOSE_HINT_DEG: float = 12.0


def _echo(message: str) -> None:
    print(message, flush=True)


class StreamStalled(RuntimeError):
    """采集中途断流。**带着已经收到的样本** —— 见 `_collect` 的文档。"""

    def __init__(self, collected: np.ndarray) -> None:
        super().__init__(f"数据流中断，已收 {collected.shape[0]} 个样本")
        self.collected = collected


async def _collect(device: WT901Device, seconds: float) -> np.ndarray:
    """收 `seconds` 秒的比力（标称 SI），返回 `(n,3)`。

    结束条件是**时钟**，但循环靠样本到达推进 —— 断流时 `async for` 会永远等下去，
    而 `still` 那条路径要采十分钟，静默挂起与「正在采」在终端上长得一模一样。所以
    每个样本都带超时。

    断流时抛 `StreamStalled` 并**把已收到的样本带上**，而不是直接退出。十分钟的静置
    段在第八分钟断掉时，那八分钟仍然是有用的数据；第一版把它整个丢了，只留一句错误
    信息 —— 而重采一次要再花十分钟。调用方决定留不留（`capture` 不留：三秒的残段没有
    价值；`still` 留）。
    """
    samples: list[tuple[float, float, float]] = []
    deadline = time.monotonic() + seconds
    stream = device.samples()
    try:
        while True:
            try:
                sample = await asyncio.wait_for(
                    anext(stream), timeout=STALL_TIMEOUT_SECONDS
                )
            except (TimeoutError, StopAsyncIteration) as error:
                raise StreamStalled(
                    np.asarray(samples, dtype=np.float64)
                ) from error
            samples.append((sample.accel.x, sample.accel.y, sample.accel.z))
            if time.monotonic() >= deadline:
                break
    finally:
        await stream.aclose()
    return np.asarray(samples, dtype=np.float64)


async def _connect(mac: str | None, scan_timeout: float) -> tuple[WT901Device, str]:
    _echo("扫描设备……")
    found = await scan(scan_timeout)
    if mac:
        found = [item for item in found if item.address.upper().endswith(mac.upper())]
    if not found:
        raise SystemExit("没有扫到设备。确认模块已开机、在附近，且没有被别的程序占用。")
    if len(found) > 1 and not mac:
        listed = ", ".join(item.address for item in found)
        raise SystemExit(f"扫到多台设备（{listed}）。用 --mac 指定要标定哪一台。")

    discovered = found[0]
    # 把整个 `DiscoveredDevice` 传进去，**不要只传地址**：macOS 上的地址只是
    # CoreBluetooth 分配的会话内标识，跨扫描会话解析并不可靠，失败时报的是
    # 「设备未找到」，哪怕模块就在眼前（wt901 `WT901Device.connect` 的文档）。
    device = await WT901Device.connect(discovered, auto_reconnect=False)
    identity = await read_device_identity(device)
    _echo(f"已连接 {identity.value}")

    applied = await configure_streaming(device, StreamConfig(), defer_rate=True)
    applied = await start_streaming(device, StreamConfig(), applied)
    if applied.mismatches:
        _echo("⚠ 配置回读不一致：" + "；".join(applied.mismatches))
    return device, identity.value


def _closest_degrees(
    candidate: OrientationObservation, existing: list[OrientationObservation]
) -> float | None:
    if not existing:
        return None
    cosines = [float(np.clip(candidate.direction @ item.direction, -1.0, 1.0)) for item in existing]
    return float(np.degrees(np.arccos(max(cosines))))


async def _capture(args: argparse.Namespace) -> int:
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    device, mac = await _connect(args.mac, args.scan_timeout)

    collected: list[OrientationObservation] = []
    target = max(args.count, MIN_ORIENTATIONS)
    try:
        _echo("")
        _echo(f"要采 {target} 个姿态。每次把模块换一个**任意**稳定姿态（平放、侧立、")
        _echo("靠着书斜放都行），放稳、手离开，再按回车。不需要对准任何轴。")
        while len(collected) < target:
            _echo("")
            _echo(f"[{len(collected) + 1}/{target}] 换一个姿态，放稳")
            input("    好了按回车（Ctrl-C 中止）…")

            try:
                await _collect(device, SETTLE_SECONDS)
                _echo(f"    采集 {args.seconds:.1f} s，别碰它……")
                acc = await _collect(device, args.seconds)
            except StreamStalled as error:
                # 三秒的残段没有保留价值，但已采到的姿态都已逐个落盘，不会丢。
                raise SystemExit(
                    f"数据流中断（{error}）。已采到的 {len(collected)} 个姿态已落盘，"
                    f"模块重新开机后可继续采剩下的。"
                ) from error

            try:
                observation = observe_orientation(acc)
            except CalibrationError as error:
                _echo(f"    ✗ {error}")
                continue

            closest = _closest_degrees(observation, collected)
            note = ""
            if closest is not None and closest < CLOSE_HINT_DEG:
                note = f"（与之前某个只差 {closest:.0f}°，下一个换个方向更好）"
            _echo(
                f"    ✓ 第 {len(collected) + 1} 个："
                f"{observation.samples} 样本，最大标准差 "
                f"{float(np.max(observation.std)):.4f} m/s² {note}"
            )
            np.save(out / f"orientation_{len(collected):02d}.npy", acc)
            collected.append(observation)
    finally:
        problem = await close_quietly(device)
        if problem:
            _echo(f"⚠ {problem}")

    (out / "meta.json").write_text(
        json.dumps(
            {
                "device": mac,
                "method": "multi-orientation-magnitude",
                "captured_at": datetime.now(UTC).isoformat(),
                "seconds_per_orientation": args.seconds,
                "orientations": len(collected),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    _echo("")
    _echo(f"{len(collected)} 个姿态都采到了，落在 {out}")
    _echo(f"接着跑：python -m gait.cli.accelcal solve --dir {out}")
    return 0


async def _still(args: argparse.Namespace) -> int:
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    device, mac = await _connect(args.mac, args.scan_timeout)
    stalled = False
    try:
        _echo("")
        _echo(f"把模块平放不动，采 {args.minutes:g} min。")
        input("准备好后按回车开始…")
        try:
            # 静置等待也要包进来：模块一开始就没在推流时，不该甩一个裸 traceback 出去。
            await _collect(device, SETTLE_SECONDS)
            acc = await _collect(device, args.minutes * 60.0)
        except StreamStalled as error:
            # 断流也把已收到的留下：八分钟的静置段仍然有用，而重采要再花十分钟。
            acc, stalled = error.collected, True
    finally:
        problem = await close_quietly(device)
        if problem:
            _echo(f"⚠ {problem}")

    if acc.size == 0:
        raise SystemExit("一个样本都没收到，没有可留的数据。确认模块已开机后重试。")

    np.save(out / "still.npy", acc)
    (out / "still_meta.json").write_text(
        json.dumps(
            {
                "device": mac,
                "requested_minutes": args.minutes,
                "samples": int(acc.shape[0]),
                "stalled": stalled,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    if stalled:
        _echo(
            f"⚠ 中途断流，已把收到的 {acc.shape[0]} 个样本留在 "
            f"{out / 'still.npy'}（不足 {args.minutes:g} min）。"
        )
    else:
        _echo(f"静置段落在 {out / 'still.npy'}（{acc.shape[0]} 样本）")
    return 0


def _load(directory: Path) -> list[OrientationObservation]:
    paths = sorted(directory.glob("orientation_*.npy"))
    if not paths:
        raise SystemExit(f"{directory} 里没有 orientation_*.npy。先跑 capture。")
    return [observe_orientation(np.load(path)) for path in paths]


def _solve(args: argparse.Namespace) -> int:
    directory = Path(args.dir)
    meta_path = directory / "meta.json"
    device = "unknown"
    if meta_path.exists():
        device = json.loads(meta_path.read_text(encoding="utf-8")).get("device", device)

    calibration = solve_orientations(device, _load(directory))

    _echo(f"设备 {calibration.device}   姿态 {len(calibration.orientations)} 个")
    _echo("")
    _echo("解算结果：")
    _echo(f"  器件零偏      {np.array2string(calibration.bias_mg, precision=1)} mg")
    _echo(f"  零偏模长      {calibration.bias_magnitude_mg:.1f} mg   （规格书 ±20~40 mg）")
    _echo(f"  标度误差      {np.array2string(calibration.scale_error_ppt, precision=2)} ‰")
    _echo(f"  交叉轴最大    {calibration.cross_axis_ppt:.2f} ‰")
    _echo("")
    _echo("质量：")
    _echo(f"  拟合残差      {calibration.residual_mg:.2f} mg   （目标 2~5 mg）")
    _echo(f"  留一交叉验证  {calibration.loo_mg:.2f} mg   ← 对没参与拟合的姿态")
    _echo(f"  雅可比条件数  {calibration.condition_number:.1f}")

    output = directory / "calibration.json"
    output.write_text(
        json.dumps(calibration.snapshot(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    _echo("")
    _echo(f"参数写到 {output}")

    if args.still:
        still = np.load(Path(args.still))
        raw = (np.linalg.norm(still.mean(axis=0)) - STANDARD_GRAVITY) / MILLI_G
        fixed = (
            np.linalg.norm(calibration.apply(still).mean(axis=0)) - STANDARD_GRAVITY
        ) / MILLI_G
        _echo("")
        _echo("静置段（比力模长相对 1 g 的偏差）：")
        _echo(f"  标定前  {raw:+.1f} mg")
        _echo(f"  标定后  {fixed:+.1f} mg")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m gait.cli.accelcal", description="加计多姿态标定工装（服务方）"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    capture = sub.add_parser("capture", help="连设备，采多个任意稳定姿态")
    capture.add_argument("--out", required=True, help="输出目录")
    capture.add_argument("--mac", help="指定设备（地址后缀即可）")
    capture.add_argument("--count", type=int, default=MIN_ORIENTATIONS + 4)
    capture.add_argument("--seconds", type=float, default=DEFAULT_SECONDS)
    capture.add_argument("--scan-timeout", type=float, default=10.0)

    still = sub.add_parser("still", help="采一段长静置，供漂移取证")
    still.add_argument("--out", required=True)
    still.add_argument("--mac")
    still.add_argument("--minutes", type=float, default=10.0)
    still.add_argument("--scan-timeout", type=float, default=10.0)

    solve = sub.add_parser("solve", help="从采集目录解算标定参数（不需要硬件）")
    solve.add_argument("--dir", required=True)
    solve.add_argument("--still", help="可选：静置段 .npy，用于标定前后对比")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "solve":
        return _solve(args)
    runner = {"capture": _capture, "still": _still}[args.command]
    try:
        return asyncio.run(runner(args))
    except KeyboardInterrupt:
        _echo("\n已中止。")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
