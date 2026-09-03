"""加计六面法标定工装。cli 的 `sixface`（RAY-207，服务方流程）。

**不进机构产品流程**（FR-04：出厂标定由服务方完成并下发，机构侧不做）。

    # 服务方桌面：模块连本机，按提示摆六个面
    python -m gait.cli.sixface capture --out out/AA-BB-CC

    # 解算（不需要硬件，可在别处跑）
    python -m gait.cli.sixface solve --dir out/AA-BB-CC

    # 静置漂移取证（可选，10 min）
    python -m gait.cli.sixface still --out out/AA-BB-CC --minutes 10

## 面别由**数据**判定，不由提示顺序判定

工装会依次提示摆哪个面，但收下这一段之后是拿 `identify_face` 从数据里读出它到底是
哪个面，而不是把提示的那个面记上去。

十分钟里摆六次，摆错顺序或漏摆是最常见的操作失误，而**自报面别会让这个错静默地进到
拟合里** —— 最小二乘照单全收，给出一组解，只是解是错的，且没有任何迹象。让数据自己
说它是哪个面，漏摆与重复才暴露得出来（`solve_six_face` 那两道闸靠的就是它）。

## 每一面当场验，不等到最后

收完一面立刻跑 `observe_face`。不合格（没静置好、立在棱上、摆成了已采过的面）当场
提示重采。把校验推到最后意味着操作员要为第一面的失误重跑整个流程 —— 而这个流程要
十分钟，重跑的代价高到会诱使人跳过校验。

## 采集与解算分开，是因为采集跑不到解算里

macOS 的 TCC 会在非交互进程里直接中止蓝牙访问，采集只能在操作员自己的终端里跑。
`solve` 因此是独立子命令、只读文件，任何地方都能跑，也便于事后复算。

## 不碰模块的标定寄存器

本工装全程只**读**数据流。加计补偿在上位机完成，模块保持出厂原始态（FR-03）。
wt901 的 `device.calibration` 通道由 `tools/check_calibration_channel.py` 守着，
理由见 `gait/calib/accel.py` 的模块文档。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
from wt901 import WT901Device, scan
from wt901.transport.ble import BleTransport

from gait.calib.accel import FACES, MILLI_G, observe_face, solve_six_face
from gait.calib.still import CalibrationError
from gait.device.ble import StreamConfig, configure_streaming, start_streaming
from gait.device.identity import read_device_identity

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")

#: 每个面默认采多久。200 Hz 下 4 s ≈ 800 个样本，是 `MIN_SAMPLES_PER_FACE` 的两倍，
#: 留了余量给开头那几个还没稳下来的样本。
DEFAULT_FACE_SECONDS: float = 4.0

#: 操作员摆好之后、开始收数之前的静置等待。手离开模块时会带一下，这段丢掉。
SETTLE_SECONDS: float = 1.5

FACE_HINTS: dict[str, str] = {
    "+Z": "模块正面朝上平放（Z 轴朝上）",
    "-Z": "模块翻过来，背面朝上平放（Z 轴朝下）",
    "+X": "模块立起来，X 轴朝上",
    "-X": "模块立起来，X 轴朝下",
    "+Y": "模块立起来，Y 轴朝上",
    "-Y": "模块立起来，Y 轴朝下",
}


def _echo(message: str) -> None:
    print(message, flush=True)


async def _collect(device: WT901Device, seconds: float) -> np.ndarray:
    """收 `seconds` 秒的比力（标称 SI），返回 `(n,3)`。"""
    samples: list[tuple[float, float, float]] = []
    deadline = time.monotonic() + seconds
    async for sample in device.samples():
        samples.append((sample.accel.x, sample.accel.y, sample.accel.z))
        if time.monotonic() >= deadline:
            break
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
    device = WT901Device(BleTransport(discovered.address), auto_reconnect=False)
    await device.connect()
    identity = await read_device_identity(device)
    _echo(f"已连接 {identity.value}")

    applied = await configure_streaming(device, StreamConfig(), defer_rate=True)
    applied = await start_streaming(device, StreamConfig(), applied)
    if applied.mismatches:
        _echo("⚠ 配置回读不一致：" + "；".join(applied.mismatches))
    return device, identity.value


async def _capture(args: argparse.Namespace) -> int:
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    device, mac = await _connect(args.mac, args.scan_timeout)

    collected: dict[str, np.ndarray] = {}
    try:
        for face in FACES:
            while True:
                remaining = [name for name in FACES if name not in collected]
                _echo("")
                _echo(f"[{len(collected) + 1}/6] {FACE_HINTS[face]}")
                _echo(f"    还差：{' '.join(remaining)}")
                input("    摆好后按回车开始采（Ctrl-C 中止）…")

                _echo(f"    静置 {SETTLE_SECONDS:.1f} s……")
                await _collect(device, SETTLE_SECONDS)
                _echo(f"    采集 {args.seconds:.1f} s，别碰它……")
                acc = await _collect(device, args.seconds)

                try:
                    observation = observe_face(acc)
                except CalibrationError as error:
                    _echo(f"    ✗ {error}")
                    _echo("    请重摆这一面。")
                    continue

                if observation.face in collected:
                    _echo(
                        f"    ✗ 这一段读出来是 {observation.face} 面，而它已经采过了。"
                        "请按提示换一个面。"
                    )
                    continue

                tilt = np.degrees(np.arcsin(min(1.0, observation.tilt_ratio)))
                _echo(
                    f"    ✓ 收下 {observation.face} 面："
                    f"{observation.samples} 样本，倾斜 {tilt:.1f}°，"
                    f"最大标准差 {float(np.max(observation.std)):.4f} m/s²"
                )
                collected[observation.face] = acc
                np.save(out / f"face_{observation.face.replace('+', 'p').replace('-', 'm')}.npy", acc)
                break
    finally:
        await device.disconnect()

    meta = {
        "device": mac,
        "captured_at": datetime.now(UTC).isoformat(),
        "seconds_per_face": args.seconds,
        "faces": sorted(collected),
    }
    (out / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    _echo("")
    _echo(f"六个面都采到了，落在 {out}")
    _echo(f"接着跑：python -m gait.cli.sixface solve --dir {out}")
    return 0


async def _still(args: argparse.Namespace) -> int:
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    device, mac = await _connect(args.mac, args.scan_timeout)
    seconds = args.minutes * 60.0
    try:
        _echo("")
        _echo(f"把模块平放不动，采 {args.minutes:g} min。")
        input("准备好后按回车开始…")
        await _collect(device, SETTLE_SECONDS)
        acc = await _collect(device, seconds)
    finally:
        await device.disconnect()

    np.save(out / "still.npy", acc)
    (out / "still_meta.json").write_text(
        json.dumps(
            {"device": mac, "minutes": args.minutes, "samples": int(acc.shape[0])},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    _echo(f"静置段落在 {out / 'still.npy'}（{acc.shape[0]} 样本）")
    return 0


def _load_faces(directory: Path) -> list[Any]:
    observations = []
    for face in FACES:
        path = directory / f"face_{face.replace('+', 'p').replace('-', 'm')}.npy"
        if not path.exists():
            raise SystemExit(f"缺少 {face} 面的数据（{path}）。先跑 capture。")
        observations.append(observe_face(np.load(path)))
    return observations


def _solve(args: argparse.Namespace) -> int:
    directory = Path(args.dir)
    meta_path = directory / "meta.json"
    device = "unknown"
    if meta_path.exists():
        device = json.loads(meta_path.read_text(encoding="utf-8")).get("device", device)

    calibration = solve_six_face(device, _load_faces(directory))

    _echo(f"设备 {calibration.device}")
    _echo("")
    _echo("各面观测：")
    for face in calibration.faces:
        tilt = np.degrees(np.arcsin(min(1.0, face.tilt_ratio)))
        _echo(
            f"  {face.face}  样本 {face.samples:5d}  倾斜 {tilt:5.1f}°  "
            f"最大标准差 {float(np.max(face.std)):.4f} m/s²"
        )
    _echo("")
    _echo("解算结果：")
    _echo(f"  器件零偏      {np.array2string(calibration.bias_mg, precision=1)} mg")
    _echo(f"  零偏模长      {calibration.bias_magnitude_mg:.1f} mg   （规格书 ±20~40 mg）")
    _echo(f"  标度误差      {np.array2string(calibration.scale_error_ppt, precision=2)} ‰")
    _echo(f"  标定后残差    {calibration.residual_mg:.2f} mg   （目标 2~5 mg）")
    _echo(f"  条件数        {calibration.condition_number:.2f}")

    output = directory / "calibration.json"
    output.write_text(
        json.dumps(calibration.snapshot(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    _echo("")
    _echo(f"参数写到 {output}")

    if args.still:
        still = np.load(Path(args.still))
        raw_mg = float(np.linalg.norm(still.mean(axis=0)) - 9.80665) / MILLI_G
        fixed = calibration.apply(still)
        fixed_mg = float(np.linalg.norm(fixed.mean(axis=0)) - 9.80665) / MILLI_G
        _echo("")
        _echo("静置段（比力模长相对 1 g 的偏差）：")
        _echo(f"  标定前  {raw_mg:+.1f} mg")
        _echo(f"  标定后  {fixed_mg:+.1f} mg")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m gait.cli.sixface", description="加计六面法标定工装（服务方）"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    capture = sub.add_parser("capture", help="连设备，按提示摆六个面")
    capture.add_argument("--out", required=True, help="输出目录")
    capture.add_argument("--mac", help="指定设备（地址后缀即可）")
    capture.add_argument("--seconds", type=float, default=DEFAULT_FACE_SECONDS)
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
