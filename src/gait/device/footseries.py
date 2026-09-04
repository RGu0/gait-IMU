"""原始帧 → `FootSeries` 的数据桥（RAY-345 最小 MVP 的 `--replay` 路径）。

`FootSeries` 是「标定与同步之后、算法之前」的契约（§3.2），它上游封装了协议解析、
标定、同步三件事。真正完整的实现属 RAY-208（标定）+ RAY-209/210（时基/空洞）。
本模块是**最小桥**：只把已经能拿到的东西——wt901 的 SI 换算、F2.3 的固定坐标系
重排、标称采样率——接成一份能喂给 `cloud.run_basic_chain` 的 `FootSeries`。

## 这里明写哪些「精度」被暂时牺牲

1. **不做标定补偿**（F2.1 六面法 / F2.2 静止零偏）。原始码值只过 wt901 的固定
   量程换算，没有零偏/标度校正。`FootSeries` 契约说「已标定补偿」，这里违背它，
   是 MVP 有意为之的简化——详见 RAY-345 的降范围说明。
2. **时轴用标称采样率**。回放路径的 `t_host` 是「回放那一刻」的时钟，不是原始
   采集时刻（`capture.py` 明写），拿它回归会得到被压缩的假采样率。真正的时基
   （`sync/timebase.build_timebase` + 原始到达时刻）留到接真机采集时再用；MVP 用
   `t[k] = k / nominal_fs` 的均匀标称时轴，`fs` 也取标称 200 Hz。
3. **不切空洞、不做插值**。`segments` 就一整段，`quality` 只标 `SATURATED`。
   空洞切分（RAY-210）与插值不在 MVP 范围。

这三条牺牲共同保证一件事：回放数据能走通整条链、产出报告，数字**有限且量级正确**，
但精确值不作保证——这正是「暂不追求精度」的含义。

## 坐标系重排（F2.3）是本模块唯一「有物理对错」的地方

《BS-BT91 硬件适配》§2 与《MVP-v1 功能清单》F2.3 都写明：

* 模块体体系：**X 左 / Y 前 / Z 上**
* 足部解剖系：**X 前 / Y 外侧 / Z 上**

因此固定重排为 `foot = [module_y, ±module_x, module_z]`，其中 Y 的符号由脚决定：
左足外侧 = 左 = `+module_x`，右足外侧 = 右 = `−module_x`。**这个转换必须显式写成
常量并加单元测试**（硬件适配文档原话），因为一旦错，后续所有角度/方向指标的符号
都会静默地错——而那不是报错，是给出一份看着正常的错误报告。

⚠ **这个固定重排在左足上是镜射不是旋转**（行列式 −1），而角速度是伪矢量，于是
左足步长静默偏低 17.8%、步频步时全对。已立 RAY-390（Urgent），需求裁定未下之前
本模块不动它 —— 下面的标定路径 `calibrated_foot_series` 用实测旋转并显式校验
行列式，**不继承**该缺陷。

## 标定路径（RAY-360 `raw-to-series`）：把上面三条牺牲还回来两条半

`calibrated_foot_series` 与 `frames_to_foot_series` **共用同一段装配代码**，只是喂给
它的东西不同。`FootSeries` 在本仓库的生产代码里只有一个构造点（`_assemble`），
这正是 RAY-360 要立住的那件事：「谁构造 `FootSeries`」必须只有一个答案。

| MVP 桥 | 标定路径 |
| -- | -- |
| 不做标定补偿 | 陀螺零偏（`StillCalibration`）+ 实测坐标重排（`MountingCalibration`） |
| `t[k] = k / 200 Hz` | `sync.timebase.build_timebase` 解出的时轴与**实测** `fs` |
| `segments` 就一整段 | `find_gaps` + `split_segments`，段边界标 `Quality.GAP_EDGE` |
| 加计标度/零偏 —— 无 | 加计标度/零偏 —— **仍然无**（RAY-207 未交付），但必须显式传 |

### 真实到达时刻一直在磁盘上

MVP 桥上面写着「回放路径的 `t_host` 是回放那一刻的时钟，真正的时基留到接真机采集
时再用」。**前半句对，结论不对。**

录制文件每一行都带 `"t"` —— 相对第一段字节的秒数，由 `device/recorder.py` 在 **BLE
回调里**取的 `time.monotonic()`，那正是 PRD §6.1 要的「主机高精度接收时刻」。丢掉它
的是**回放路径**：`ReplayTransport` 重走一遍传输层，`t_host` 被重新打戳。

所以 `read_recorded_frames` 绕开 `ReplayTransport`，直接读 chunk，把每个 chunk 的 `t`
配给它解出的那几帧。跨 chunk 的帧算在**补齐它的那个 chunk** 上 —— 与实时路径一致
（实时的 `t_host` 也是在补齐该帧的那次通知的回调里打的）。同一次通知内的多帧共享
到达时刻，这正是 `timebase._packet_boundaries` 假设的形状。

**这一条把本层从「等真机」变成「桌面即可交付并验收」。**

### 出厂加计标定是必填参数，没有默认值

RAY-207 未交付，`AccelCalibration` 的真实实现还不存在。本模块**不造**它，但也不能
当它不存在：参数必填。要想不带它跑，调用方必须显式传 `NO_ACCEL_CALIBRATION` ——
一个名字里就写着「没有标定」的东西，而不是一个叫「标称值」、听起来像真标定的对象。
一个默认值会让「这条数据到底标没标定」变成一件要翻源码才知道的事。

### 补偿顺序：码值 → SI → 加计标定 → 陀螺零偏 → 坐标重排

零偏与标定都是**传感器**的属性，所以在传感器自己的坐标系（模块体系）里减掉，然后
才旋转。数学上 `R(g − b) = Rg − Rb` 两边等价，但只在「记得把零偏也旋转」时才等价；
先减后旋没有这个如果。

`StillCalibration` / `MountingCalibration` 因此都必须是**在模块体系下**解出来的
（`calibrate_still` / `estimate_mounting` 用的是调用方喂进去的那个系，它们自己不知道
是哪个系）。这一条写在这里，是因为喂错了不会报错。

`RawFrame` 的文档说「标定补偿要在码值上做」，而这里的接缝在 SI 之后。两者不冲突：
wt901 的换算是一次纯标量乘（`raw / 32768 * 满量程`），仿射模型 `A·m + c` 在换算前后
只差一个单位，`A` 不变、`c` 差同一个标量因子。把接缝放在 SI 之后，是为了不让量程
常数在本仓库里出现第二份。
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Final, Protocol, runtime_checkable

import numpy as np
from wt901.models import ImuSample
from wt901.protocol.frames import FrameDecoder, FrameFlag
from wt901.protocol.units import accel_to_m_s2, angular_velocity_to_rad_s
from wt901.recording import open_recording

from gait.calib.still import StillCalibration
from gait.calib.walk import MountingCalibration
from gait.config import AlgoConfig
from gait.contracts import FootLabel, FootSeries, Quality, RawFrame
from gait.device.adapter import to_raw_frame
from gait.device.capture import replay_session_foot
from gait.io.session import raw_path
from gait.sync.integrity import find_gaps, split_segments
from gait.sync.timebase import Timebase, build_timebase

__all__ = [
    "NOMINAL_FS",
    "NO_ACCEL_CALIBRATION",
    "AccelCalibration",
    "FootSeriesError",
    "NoAccelCalibration",
    "calibrated_foot_series",
    "frames_to_foot_series",
    "load_session_foot",
    "load_session_frames",
    "read_recorded_frames",
    "reorder_module_to_foot",
]

#: 器件标称采样率，Hz。真实值会由 `sync/timebase` 从到达时刻解出，MVP 直接用它。
NOMINAL_FS: Final[float] = 200.0

#: 足部系各分量取自模块体系的哪个下标（F2.3）：`foot[0]=module[1]`（前=前）、
#: `foot[1]=module[0]`（外=左）、`foot[2]=module[2]`（上=上）。
_AXIS_INDEX: Final[tuple[int, int, int]] = (1, 0, 2)

#: Y（外侧）分量的符号：左足外侧朝左（=+X），右足外侧朝右（=−X）。
_Y_SIGN: Final[dict[str, float]] = {"L": 1.0, "R": -1.0}


class FootSeriesError(ValueError):
    """原始帧不足以构成一条可用的 `FootSeries`。"""


def reorder_module_to_foot(
    acc: np.ndarray, gyr: np.ndarray, label: FootLabel
) -> tuple[np.ndarray, np.ndarray]:
    """把模块体体系（X左/Y前/Z上）的比力与角速度重排到足部系（X前/Y外/Z上）。

    `acc`/`gyr` 都是 `(n,3)` 的 SI 数组，顺序为模块体系的 (x,y,z)。返回同样形状的
    足部系数组。左足 Y 取 +X，右足 Y 取 −X —— 见模块文档。
    """
    if label not in _Y_SIGN:
        raise FootSeriesError(f"label 应为 'L' 或 'R'，收到 {label!r}")
    sign = _Y_SIGN[label]
    index = _AXIS_INDEX

    foot_acc = np.empty_like(acc, dtype=np.float64)
    foot_acc[:, 0] = acc[:, index[0]]
    foot_acc[:, 1] = sign * acc[:, index[1]]
    foot_acc[:, 2] = acc[:, index[2]]

    foot_gyr = np.empty_like(gyr, dtype=np.float64)
    foot_gyr[:, 0] = gyr[:, index[0]]
    foot_gyr[:, 1] = sign * gyr[:, index[1]]
    foot_gyr[:, 2] = gyr[:, index[2]]
    return foot_acc, foot_gyr


def _to_si(acc_raw: np.ndarray, gyr_raw: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """码值 → SI。换算公式的唯一实现是 wt901（`protocol/units`），这里逐元素调用。"""
    vector_accel = np.vectorize(accel_to_m_s2, otypes=[np.float64])
    vector_gyro = np.vectorize(angular_velocity_to_rad_s, otypes=[np.float64])
    return vector_accel(acc_raw), vector_gyro(gyr_raw)


def frames_to_foot_series(
    frames: Sequence[RawFrame],
    label: FootLabel,
    *,
    nominal_fs: float = NOMINAL_FS,
) -> FootSeries:
    """把一串原始帧拼成一条 `FootSeries`。

    纯函数（不碰 I/O），好测试。`frames` 按样本顺序排列；`t_host` 不参与时轴——
    时轴用标称采样率均匀铺开，见模块文档。
    """
    if label not in ("L", "R"):
        raise FootSeriesError(f"label 应为 'L' 或 'R'，收到 {label!r}")
    if not frames:
        raise FootSeriesError("没有原始帧，无法构成 FootSeries")
    if not nominal_fs > 0:
        raise FootSeriesError(f"nominal_fs 必须为正，收到 {nominal_fs}")

    acc_raw = np.stack([frame.acc_raw for frame in frames])
    gyr_raw = np.stack([frame.gyr_raw for frame in frames])
    acc_si, gyr_si = _to_si(acc_raw, gyr_raw)
    acc, gyr = reorder_module_to_foot(acc_si, gyr_si, label)

    n = len(frames)
    return _assemble(
        frames,
        label,
        acc=acc,
        gyr=gyr,
        t=np.arange(n, dtype=np.float64) / nominal_fs,
        fs=float(nominal_fs),
        segments=[(0, n)],
        gap_edges=(),
    )


def _assemble(
    frames: Sequence[RawFrame],
    label: FootLabel,
    *,
    acc: np.ndarray,
    gyr: np.ndarray,
    t: np.ndarray,
    fs: float,
    segments: list[tuple[int, int]],
    gap_edges: Sequence[int],
) -> FootSeries:
    """**生产代码里唯一构造 `FootSeries` 的地方。**（另一处是 `validate/synthetic`
    的合成数据生成器，它按定义就在生产路径之外。）

    两条入口（MVP 桥与标定路径）都从这里出结果，所以它们不可能在质量位或段的
    语义上分叉 —— 那种分叉正是 RAY-360 要消灭的东西。

    `gap_edges` 是空洞两侧**实际收到**的样本下标；MVP 桥不切洞，传空。
    """
    n = len(frames)
    quality = np.zeros(n, dtype=np.uint8)
    for index, frame in enumerate(frames):
        if frame.saturated:
            quality[index] |= int(Quality.SATURATED)
    for index in gap_edges:
        quality[index] |= int(Quality.GAP_EDGE)

    return FootSeries(
        label=label,
        t=t,
        acc=acc,
        gyr=gyr,
        quality=quality,
        segments=segments,
        fs=fs,
    )


async def load_session_foot(
    root: Path,
    session_id: str,
    label: FootLabel,
    *,
    nominal_fs: float = NOMINAL_FS,
) -> FootSeries:
    """按会话目录回放某一只脚，产出 `FootSeries`。

    `--replay` 路径的入口：路径由 `io.session.raw_path` 决定，解析由 `capture.py`
    的 `replay_session_foot` 完成，这里只负责「收集 → 桥接」。
    """
    frames = [frame async for frame in replay_session_foot(root, session_id, label)]
    return frames_to_foot_series(frames, label, nominal_fs=nominal_fs)


# ── 出厂加计标定：接口在这里，实现属 RAY-207 ──────────────────────────────


@runtime_checkable
class AccelCalibration(Protocol):
    """出厂加计标定的最小接口。**本模块只用到这两样。**

    写成 `Protocol` 而不是基类，是为了让 RAY-207 交付的 `AccelCalibration`
    （frozen dataclass + `snapshot()`，形状对齐 `StillCalibration`）**不必继承本仓库
    的任何东西**就能满足它 —— 那条 Issue 的接口一致性约定说得很清楚，标定类只有
    一套形状，不为某个消费者另造一个基类。
    """

    @property
    def name(self) -> str:
        """进会话元数据的标识。**它要能让人一眼看出这份数据标没标定。**"""

    def apply(self, acc: np.ndarray) -> np.ndarray:
        """把 `(n,3)` 的 SI 比力做标度/零偏补偿，返回同形状数组。"""


class NoAccelCalibration:
    """恒等：不做任何加计补偿。**名字里就写着它什么都不做。**

    存在的理由是 RAY-207 尚未交付，而本层不能因此就把标定参数变成可选的。它是
    `NO_ACCEL_CALIBRATION` 这个常量的类型；调用点写出那个名字，等于在源码里留下
    一句「这份数据没有出厂标定」，而不是让人去翻默认值。

    FR-04 说缺失出厂标定应当**阻断新会话**，那道闸在 `app.service.runPreflight`
    （`E-CAL-3001`）。本层不重复它：同一件事有两处判据时，它们迟早对不上。本层
    只保证「没标定」这件事在数据路径上是**写出来的**，不是省略掉的。
    """

    @property
    def name(self) -> str:
        return "none(未做出厂加计标定)"

    def apply(self, acc: np.ndarray) -> np.ndarray:
        return acc

    def __repr__(self) -> str:
        return "NO_ACCEL_CALIBRATION"


#: 显式的「没有出厂标定」。见 `NoAccelCalibration`。
NO_ACCEL_CALIBRATION: Final[NoAccelCalibration] = NoAccelCalibration()


def _check_proper_rotation(rotation: np.ndarray) -> None:
    """安装矩阵必须是**真旋转**（det = +1），不能是镜射。

    比力是真矢量，角速度是**伪矢量**：在 det = −1 的正交变换下，伪矢量要多带一个
    `det` 的符号。同一个改向矩阵套在两者身上，角速度会整体反号，ESKF 积出来的姿态
    朝相反方向转，重力扣不干净，**步长静默偏低约 18%，而步频步时全对** —— 报告里
    没有任何一处看着不对。

    这不是假想：本模块上方的固定重排在左足上就是 det = −1，实测步长 1.068 m 对真值
    1.300 m。那条缺陷是 RAY-390，需求裁定未下之前不动它；这道检查保证**标定路径**
    不会走进同一个坑。
    """
    det = float(np.linalg.det(rotation))
    if abs(det - 1.0) > 1e-6:
        raise FootSeriesError(
            f"安装矩阵的行列式是 {det:.6f}，不是 +1 —— 它是镜射或带缩放，不是旋转。"
            "角速度是伪矢量，用改向的矩阵变换它会让整场会话的角速度反号，"
            "而后果是步长静默偏低、步频步时全对（见 RAY-390）。"
        )


def read_recorded_frames(path: Path) -> list[RawFrame]:
    """读一份录制，产出 `RawFrame`，`t_host` 是**原始采集时的到达时刻**。

    这是它与 `capture.replay_raw_frames` 的**唯一但要命的**区别：那条路径把字节重新
    喂过 `ReplayTransport`，`t_host` 于是变成回放那一刻的时钟，凡是从它算出来的东西
    （到达率、空洞、实测采样率）在回放数据上全部作废。而录制文件每一行本来就带着
    真实到达时刻 —— 见模块文档。

    要「与实时逐帧一致」的载荷比对仍然用 `capture.replay_raw_frames`：本函数不驱动
    设备层，因此也不复现设备层的队列与丢弃行为。**两者不是替代关系**：这条路要的是
    时间，那条路要的是行为。

    末行残行（掉电）容忍：读到哪里算哪里。丢了多少无从得知，所以这里不猜，
    `sync.integrity` 会把它表现成一个空洞。
    """
    decoder = FrameDecoder()
    frames: list[RawFrame] = []
    with open_recording(Path(path), tolerate_truncated_tail=True) as reader:
        device_id = reader.header.device_id
        for chunk in reader:
            for frame in decoder.feed(chunk.data):
                if frame.flag is not FrameFlag.DATA:
                    continue  # 寄存器应答混在流里，不是样本。
                sample = ImuSample.from_frame(
                    frame, device_id=device_id, t_host=chunk.t, seq=len(frames)
                )
                frames.append(to_raw_frame(sample))
    return frames


def load_session_frames(root: Path, session_id: str, label: FootLabel) -> list[RawFrame]:
    """按会话目录读一只脚的原始帧，带真实到达时刻。路径由 `io.session.raw_path` 定。"""
    return read_recorded_frames(raw_path(root, session_id, label))


def calibrated_foot_series(
    frames: Sequence[RawFrame],
    label: FootLabel,
    *,
    still: StillCalibration,
    mounting: MountingCalibration,
    accel_calibration: AccelCalibration,
    nominal_fs: float = NOMINAL_FS,
    cfg: AlgoConfig | None = None,
) -> tuple[FootSeries, Timebase]:
    """把一串带**真实到达时刻**的原始帧，拼成一条标定过、时基正确、按空洞切段的
    `FootSeries`。

    与 `frames_to_foot_series` 的关系见模块文档：同一个装配函数，不同的输入。

    `accel_calibration` **必填，没有默认值** —— 不带出厂标定时传
    `NO_ACCEL_CALIBRATION`。

    `still` 与 `mounting` 必须是在**模块体系**下解出来的（见模块文档「补偿顺序」）。
    喂错坐标系不会报错，只会给出一份看着正常的错数。

    连 `Timebase` 一起返回而不是只返回 `FootSeries`：`SyncReport` 是 PRD §6.1 的
    强制元数据字段，而把它丢在这里、让调用方为了拿它再跑一遍回归，两次结果就有了
    分叉的可能。
    """
    if label not in ("L", "R"):
        raise FootSeriesError(f"label 应为 'L' 或 'R'，收到 {label!r}")
    if not frames:
        raise FootSeriesError("没有原始帧，无法构成 FootSeries")
    if not nominal_fs > 0:
        raise FootSeriesError(f"nominal_fs 必须为正，收到 {nominal_fs}")
    _check_proper_rotation(np.asarray(mounting.rotation, dtype=np.float64))

    acc_raw = np.stack([frame.acc_raw for frame in frames])
    gyr_raw = np.stack([frame.gyr_raw for frame in frames])
    acc_si, gyr_si = _to_si(acc_raw, gyr_raw)

    # 顺序见模块文档：标定与零偏都在传感器自己的坐标系里减，然后才旋转。
    acc_si = np.asarray(accel_calibration.apply(acc_si), dtype=np.float64)
    if acc_si.shape != acc_raw.shape:
        raise FootSeriesError(
            f"{accel_calibration.name} 的补偿把形状从 {acc_raw.shape} 改成了 "
            f"{acc_si.shape}。标定是逐样本的仿射变换，不该增删样本。"
        )
    gyr_si = gyr_si - np.asarray(still.gyro_bias, dtype=np.float64)
    acc, gyr = mounting.apply(acc_si, gyr_si)

    arrival = np.asarray([frame.t_host for frame in frames], dtype=np.float64)
    timebase = build_timebase(arrival, nominal_fs, cfg)
    gaps = find_gaps(arrival, nominal_fs, cfg)
    segments = split_segments(len(frames), gaps)
    edges = [index for gap in gaps for index in (gap.before, gap.after)]

    series = _assemble(
        frames,
        label,
        acc=acc,
        gyr=gyr,
        t=timebase.t,
        fs=timebase.report.fs,
        segments=segments,
        gap_edges=edges,
    )
    return series, timebase
