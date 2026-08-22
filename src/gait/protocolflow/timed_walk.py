"""T-01 定时步行测试状态机。契约 §1 的 `protocolflow/`（F5.1）。

PRD §7、§13：定时测试的流程状态机，**UI 只消费其状态**。

## 一、状态机在这里，不在 UI 里

"UI 只消费其状态"是一条设计约束，不是措辞。把计时与停顿判定放进 React 组件，会得到
三个后果，每一个都在真实产品里发生过：

1. **窗口失焦时计时会漂**（浏览器与 Electron 都会节流后台定时器），而受试者正在走。
2. **状态不可回放** —— 一次采集出了问题，没法拿着数据把当时的流程重演一遍。
3. **同一个流程有两份实现**（采集端一份、云端重算一份），于是"这次测试有效吗"有两个
   答案。这与 RAY-218 的端云同构是同一类问题。

所以本模块**不持有时钟**：每个方法都接收 `now`。调用方（sidecar）负责喂时间，测试
则可以喂任意时间序列 —— 那也是"停顿/中断/提前停止路径都要覆盖"这条验收标准能被落实
的前提。

## 二、停顿是被"跳过"的，不是让测试作废的

PRD §7：中途停顿超过 `pause_threshold_s`（默认 5 s）即标记该时段并跳过，**不作废
测试**。

短于阈值的停顿**照常计入有效时长** —— 那是正常的犹豫、避让、转身减速，把它们剔掉会
让每一次真实行走都损失几秒。只有长停顿才被扣掉。

这条规则的方向值得说清楚：扣的是**有效时长**，触发的是"要不要提示重测"，而不是"这次
数据能不能用"。数据照常留着。

## 三、三条底线里有一条现在评不了

PRD §13 的会话级有效性有三条底线：**佩戴、链路、有效时长**。

后两条本模块能判：链路由 `sync/integrity.py` 的分级给出，有效时长由这里算。

**佩戴那条现在评不了。** RAY-260 证明了左右戴反在位置法下数学上不可判定，而它是佩戴
底线的一部分。所以 `SessionVerdict` 里佩戴的取值有三种而不是两种：`pass` / `fail` /
**`unknown`**，且 `unknown` **不等于 pass**。

这一点必须做在类型上而不是写在注释里 —— 一个 `bool` 只能表示前两种，而把"评不了"
默认成"通过"，正是 PRD §13 唯一硬拦截被悄悄架空的方式。`overall` 在任何一条为
`unknown` 时给出 `indeterminate`，报告层因此不得不显式处理它。

## 四、会话组装的边界要记下来

自检基线段 → 标定段 → 测试段。三段的边界进 `protocol_config` 元数据（验收标准要求），
因为下游算指标时只该用**测试段**：基线段是静止的，标定段里受试者在做规定动作，两者
混进步态统计都会污染结果，而且是那种"数值看起来正常、只是偏了一点"的污染。
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any, Final

from gait.config import ProtocolConfig

#: 状态机的结构版本。
FLOW_VERSION: Final[str] = "1.0"

STATE_IDLE: Final[str] = "idle"
STATE_BASELINE: Final[str] = "baseline"
STATE_CALIBRATION: Final[str] = "calibration"
STATE_WALKING: Final[str] = "walking"
STATE_PAUSED: Final[str] = "paused"
STATE_FINISHED: Final[str] = "finished"
STATE_ABORTED: Final[str] = "aborted"
STATES: Final[tuple[str, ...]] = (
    STATE_IDLE,
    STATE_BASELINE,
    STATE_CALIBRATION,
    STATE_WALKING,
    STATE_PAUSED,
    STATE_FINISHED,
    STATE_ABORTED,
)

#: 终态。进了就不能再动。
TERMINAL: Final[frozenset[str]] = frozenset({STATE_FINISHED, STATE_ABORTED})

#: 底线的三种取值。**`UNKNOWN` 不等于 `PASS`**，见模块文档 §3。
CHECK_PASS: Final[str] = "pass"
CHECK_FAIL: Final[str] = "fail"
CHECK_UNKNOWN: Final[str] = "unknown"

VERDICT_VALID: Final[str] = "valid"
VERDICT_INVALID: Final[str] = "invalid"
VERDICT_INDETERMINATE: Final[str] = "indeterminate"

SEGMENT_BASELINE: Final[str] = "baseline"
SEGMENT_CALIBRATION: Final[str] = "calibration"
SEGMENT_WALKING: Final[str] = "walking"


class ProtocolError(RuntimeError):
    """状态机被要求做一次它当前做不了的转移。"""


@dataclass(frozen=True)
class Segment:
    """会话里的一段。边界进 `protocol_config` 元数据（验收标准要求）。"""

    kind: str
    start: float
    stop: float

    @property
    def duration(self) -> float:
        return self.stop - self.start

    def snapshot(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "start": self.start,
            "stop": self.stop,
            "duration": self.duration,
        }


@dataclass(frozen=True)
class Pause:
    """一次停顿。`skipped` 为真表示它超过阈值、被扣出有效时长。"""

    start: float
    stop: float
    skipped: bool

    @property
    def duration(self) -> float:
        return self.stop - self.start

    def snapshot(self) -> dict[str, Any]:
        return {
            "start": self.start,
            "stop": self.stop,
            "duration": self.duration,
            "skipped": self.skipped,
        }


@dataclass(frozen=True)
class SessionVerdict:
    """会话级有效性。PRD §13 的三条底线。"""

    wearing: str
    link: str
    duration: str
    valid_seconds: float
    required_seconds: float
    overall: str
    reasons: list[str] = field(default_factory=list)

    def snapshot(self) -> dict[str, Any]:
        return {
            "wearing": self.wearing,
            "link": self.link,
            "duration": self.duration,
            "valid_seconds": self.valid_seconds,
            "required_seconds": self.required_seconds,
            "overall": self.overall,
            "reasons": list(self.reasons),
        }


class TimedWalk:
    """T-01 的流程状态机。

    **不持有时钟** —— 每个方法接收 `now`。理由见模块文档 §1：UI 里的定时器在窗口失焦
    时会被节流，而受试者正在走；而且外部时钟让整个流程可以被回放与测试。
    """

    def __init__(self, config: ProtocolConfig | None = None) -> None:
        self.config = config or ProtocolConfig()
        self._state = STATE_IDLE
        self._segments: list[Segment] = []
        self._pauses: list[Pause] = []
        self._segment_start: float | None = None
        self._pause_start: float | None = None
        self._walking_started: float | None = None
        self._walking_stopped: float | None = None
        self._abort_reason: str = ""

    # ── 状态 ──────────────────────────────────────────────────────────────

    @property
    def state(self) -> str:
        return self._state

    @property
    def segments(self) -> list[Segment]:
        return list(self._segments)

    @property
    def pauses(self) -> list[Pause]:
        return list(self._pauses)

    def _require(self, *allowed: str) -> None:
        if self._state not in allowed:
            raise ProtocolError(
                f"当前状态是 {self._state!r}，这一步只能从 {allowed} 进入。"
                "状态机拒绝非法转移而不是静默忽略 —— 静默忽略会让 UI 与实际流程"
                "悄悄分叉，而分叉之后没有任何一方是权威。"
            )

    def _close_segment(self, kind: str, now: float) -> None:
        if self._segment_start is None:
            return
        if now < self._segment_start:
            raise ProtocolError(
                f"时间倒流：段起始 {self._segment_start}，收到 {now}。"
                "状态机不持有时钟，所以单调性只能由调用方保证 —— 这里把它变成显式失败。"
            )
        self._segments.append(Segment(kind=kind, start=self._segment_start, stop=now))
        self._segment_start = None

    # ── 转移 ──────────────────────────────────────────────────────────────

    def start_baseline(self, now: float) -> str:
        """开始自检基线段。"""
        self._require(STATE_IDLE)
        self._segment_start = now
        self._state = STATE_BASELINE
        return self._state

    def start_calibration(self, now: float) -> str:
        """结束基线段，开始标定段。"""
        self._require(STATE_BASELINE)
        self._close_segment(SEGMENT_BASELINE, now)
        self._segment_start = now
        self._state = STATE_CALIBRATION
        return self._state

    def start_walking(self, now: float) -> str:
        """结束标定段，开始测试段。计时从这里起算。

        允许从 `baseline` 直接进来 —— 有些场景不做标定（沿用上次的标定参数），那时
        标定段的时长为零而不是不存在。段的存在与否比它的时长更重要：下游按段名取数据，
        缺一个段会变成 `KeyError`，而一个零长度的段是明确的"这次没做"。
        """
        self._require(STATE_BASELINE, STATE_CALIBRATION)
        self._close_segment(
            SEGMENT_CALIBRATION if self._state == STATE_CALIBRATION else SEGMENT_BASELINE, now
        )
        if self._state == STATE_BASELINE:
            self._segments.append(Segment(kind=SEGMENT_CALIBRATION, start=now, stop=now))
        self._segment_start = now
        self._walking_started = now
        self._state = STATE_WALKING
        return self._state

    def pause(self, now: float) -> str:
        """受试者停下了。**不作废测试**（PRD §7）。"""
        self._require(STATE_WALKING)
        self._pause_start = now
        self._state = STATE_PAUSED
        return self._state

    def resume(self, now: float) -> str:
        """继续。超过阈值的停顿会被标记并扣出有效时长。"""
        self._require(STATE_PAUSED)
        if self._pause_start is None:  # pragma: no cover - 由 _require 保证
            raise ProtocolError("没有正在进行的停顿")
        duration = now - self._pause_start
        if duration < 0:
            raise ProtocolError(f"时间倒流：停顿起始 {self._pause_start}，收到 {now}")
        self._pauses.append(
            Pause(
                start=self._pause_start,
                stop=now,
                skipped=duration > self.config.pause_threshold_s,
            )
        )
        self._pause_start = None
        self._state = STATE_WALKING
        return self._state

    def stop(self, now: float) -> str:
        """正常结束测试段。"""
        self._require(STATE_WALKING, STATE_PAUSED)
        if self._state == STATE_PAUSED:
            # 停顿中直接结束：把这次停顿按现在收尾，否则它的时长会消失。
            self.resume(now)
        self._close_segment(SEGMENT_WALKING, now)
        self._walking_stopped = now
        self._state = STATE_FINISHED
        return self._state

    def abort(self, now: float, reason: str) -> str:
        """中断。**数据照常留着** —— 中断的是流程，不是数据。

        必须给理由：一个没有理由的中断在事后与"程序崩了"无法区分。
        """
        if self._state in TERMINAL:
            raise ProtocolError(f"已经处于终态 {self._state!r}，不能再中断")
        if not reason:
            raise ProtocolError(
                "中断必须给出理由 —— 没有理由的中断在事后与'程序崩了'无法区分。"
            )
        if self._state == STATE_PAUSED:
            self.resume(now)
        if self._state == STATE_WALKING:
            self._close_segment(SEGMENT_WALKING, now)
            self._walking_stopped = now
        elif self._segment_start is not None:
            self._close_segment(self._state, now)
        self._abort_reason = reason
        self._state = STATE_ABORTED
        return self._state

    # ── 有效时长 ──────────────────────────────────────────────────────────

    @property
    def elapsed_seconds(self) -> float:
        """测试段的墙上时长（含停顿）。"""
        if self._walking_started is None:
            return 0.0
        stop = self._walking_stopped
        if stop is None:
            return 0.0
        return max(stop - self._walking_started, 0.0)

    @property
    def skipped_seconds(self) -> float:
        """被跳过的停顿总时长。短于阈值的停顿不算在内。"""
        return sum(item.duration for item in self._pauses if item.skipped)

    @property
    def valid_seconds(self) -> float:
        """有效时长 = 墙上时长 − 被跳过的停顿。

        短停顿**照常计入** —— 那是正常的犹豫与转身减速，剔掉它们会让每次真实行走都
        损失几秒（见模块文档 §2）。
        """
        return max(self.elapsed_seconds - self.skipped_seconds, 0.0)

    @property
    def measured_valid_fraction(self) -> float:
        """有效时长占配置时长的**实测**比例。

        名字里的 `measured` 不是修饰：`ProtocolConfig.valid_fraction` 是**判定阈值**
        （0.70），两者同名会在元数据里撞车，而撞车的结果是阈值被实测值悄悄覆盖。
        """
        if self.config.duration_s <= 0:  # pragma: no cover - 配置层已保证为正
            return 0.0
        return self.valid_seconds / self.config.duration_s

    # ── 会话级有效性 ──────────────────────────────────────────────────────

    def verdict(self, *, wearing: str = CHECK_UNKNOWN, link: str = CHECK_UNKNOWN) -> SessionVerdict:
        """PRD §13 的三条底线：佩戴、链路、有效时长。

        **`wearing` 默认是 `unknown` 而不是 `pass`。** RAY-260 证明了左右戴反在位置法
        下数学上不可判定，而它是佩戴底线的一部分 —— 在有一个可用的判据之前，这条底线
        的诚实答案是"评不了"。

        把"评不了"默认成"通过"正是 PRD §13 唯一硬拦截被悄悄架空的方式，所以这里让
        `overall` 变成 `indeterminate`，报告层不得不显式处理它。
        """
        for name, value in (("wearing", wearing), ("link", link)):
            if value not in (CHECK_PASS, CHECK_FAIL, CHECK_UNKNOWN):
                raise ProtocolError(
                    f"{name} 应为 pass/fail/unknown 之一，收到 {value!r}"
                )

        required = self.config.minimum_valid_seconds
        duration_check = CHECK_PASS if self.valid_seconds >= required else CHECK_FAIL

        reasons: list[str] = []
        if duration_check == CHECK_FAIL:
            reasons.append(
                f"valid_seconds:{self.valid_seconds:.1f}<{required:.1f}"
            )
        if wearing == CHECK_FAIL:
            reasons.append("wearing_failed")
        if link == CHECK_FAIL:
            reasons.append("link_failed")
        if wearing == CHECK_UNKNOWN:
            reasons.append("wearing_unknown")
        if link == CHECK_UNKNOWN:
            reasons.append("link_unknown")

        checks = (wearing, link, duration_check)
        if CHECK_FAIL in checks:
            overall = VERDICT_INVALID
        elif CHECK_UNKNOWN in checks:
            overall = VERDICT_INDETERMINATE
        else:
            overall = VERDICT_VALID

        return SessionVerdict(
            wearing=wearing,
            link=link,
            duration=duration_check,
            valid_seconds=self.valid_seconds,
            required_seconds=required,
            overall=overall,
            reasons=reasons,
        )

    # ── 元数据 ────────────────────────────────────────────────────────────

    def protocol_snapshot(self) -> dict[str, Any]:
        """进 `SessionMeta.protocol_config`（验收标准要求）。

        除了配置本身，还带上**三段的边界**与停顿清单 —— 下游算指标时只该用测试段，
        而基线段（静止）与标定段（规定动作）混进步态统计都会污染结果，且是那种
        "数值看起来正常、只是偏了一点"的污染。
        """
        return {
            **self.config.snapshot(),
            "state": self._state,
            "segments": [item.snapshot() for item in self._segments],
            "pauses": [item.snapshot() for item in self._pauses],
            "elapsed_seconds": self.elapsed_seconds,
            "skipped_seconds": self.skipped_seconds,
            "valid_seconds": self.valid_seconds,
            # **不叫 `valid_fraction`** —— `ProtocolConfig.snapshot()` 已经用那个键存
            # 「判定阈值」（0.70）。同名会让实测值把阈值覆盖掉，而读回来的元数据看起来
            # 就像"这次的阈值是 100%"。测试撞出过这个。
            "measured_valid_fraction": self.measured_valid_fraction,
            "abort_reason": self._abort_reason,
            "flow_version": FLOW_VERSION,
        }

    def walking_segment(self) -> Segment:
        """测试段。下游算指标只该用它。"""
        for item in self._segments:
            if item.kind == SEGMENT_WALKING:
                return item
        raise ProtocolError("还没有测试段 —— 测试尚未开始或尚未结束")

    def walking_intervals(self) -> Iterator[tuple[float, float]]:
        """测试段里**扣掉被跳过的停顿**之后的连续区间。

        指标应当只在这些区间上算。返回区间而不是"总时长"，是因为跨停顿的步态周期
        本身也该被排除，而那需要知道停顿在哪里，不只是停了多久。
        """
        segment = self.walking_segment()
        skipped = [item for item in self._pauses if item.skipped]
        edges = [segment.start]
        for item in skipped:
            edges.extend((item.start, item.stop))
        edges.append(segment.stop)
        for index in range(0, len(edges) - 1, 2):
            start, stop = edges[index], edges[index + 1]
            if stop > start:
                yield (start, stop)
