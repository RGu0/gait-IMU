"""出厂标定参数库（RAY-207 R3）：按设备身份存取，缺失或失效即阻断新会话。

PRD **FR-04**：出厂标定由服务方完成、按 MAC 下发；**缺失即阻断新会话**；机构侧不做。
本模块是「下发到机构侧之后」的那一半 —— 存下来、取回来、以及回答一个问题：

    这台设备现在能不能开一次正式会话？

## 阻断的物理依据不是精度，是「会话跑不跑得起来」

本 Issue 的标定价值研究（`acceptance/calibration-value-study.txt`）实测：ESKF 的 15 维
状态里有加计零偏，ZUPT 每步抑制它，所以**零偏在约 19.8 mg 以下时标定净改善是 0.000
个百分点**。越过那个门槛则是 `find_still_window` 判不出静止段、初始对准直接失败 ——
不是精度变差，是**没有结果**。而规格书允许的 ±20~40 mg 正落在门槛之上。

所以「缺失即阻断」不是保守，是必要：一台没标定过的模块可能根本开不了会话，而那件事
要在开始采集**之前**说出来，不是让操作员采完十分钟再看到一份空报告。

## 「失效」按身份/固件变化判，不设时间期限（R3）

R3 之前本 Issue 一直写着「缺失/过期」，但 **PRD FR-04 原文里没有「过期」**，全仓与 PRD
也找不到任何有效期定义。拍一个天数会有两个毛病：到期就阻断，会在没有任何证据表明参数
变差时把能用的模块拦在外面；未到期就放行，又拦不住身份已经静默变了的真实失效。两头
都不准。

改判**可检查的事实**：

1. **身份推导（**`provenance`**）变了。** `device/binding.py` 记录并警告过：
   `wt901.Telemetry.read_mac()` 的字节排布是**推出来的**，若哪天被推翻，同一台设备读出
   来的 `value` 就变了 —— **而 **`kind`** 还是 **`mac`。一份挂在静默变了的键上的标定，
   正是该被作废的东西。
2. **固件版本变了。** 器件输出链路改了之后，旧参数不再描述它。

若今后真有实测表明参数随时间漂移（例如 `accel-field-trial` 重复标定同一台模块时量到
了），再拿那个数据加时间期限 —— 那时它才有依据。

## 只收字符串，不认识 `DeviceIdentity`

`gait.calib` 目前不依赖 `gait.device`（`still.py` / `walk.py` / `accel.py` 都只用
`gait.config` 与 `gait.core`），本模块保持这一点：键与推导都以**字符串**传入，由设备层
把 `DeviceIdentity` 映射过来。

这不是洁癖。参数库要能在**服务方工装**那一侧跑 —— 那里生成参数、写库、再下发，未必有
BLE 栈可用；把设备层拖进来会让一个纯粹的读写库需要一整套蓝牙依赖才 import 得动。

## 判据只有一处

「缺失即阻断」这件事在 `app.service.runPreflight` 有一道面向操作员的闸（`E-CAL-3001`），
本模块提供的是它背后的**事实来源**，不是第二套判据。`device/footseries.py` 的
`NoAccelCalibration` 同理 —— 那一层只保证「没标定」在数据路径上是写出来的。三处各司其
职：本模块答「有没有、有效否」，service 决定拦不拦，footseries 保证记录不丢。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final
from urllib.parse import quote

from gait.calib.still import CalibrationError

#: 参数库的目录名，落在会话根下。
STORE_DIRNAME: Final[str] = "calibrations"

#: 记录的 schema 版本。参数库要跨版本读，格式变了得能认出来。
SCHEMA_VERSION: Final[int] = 1

#: 固件版本未知时的占位。**不是空字符串** —— 空值会与「读到了一个空版本」混淆，
#: 而前者应当阻断（下面 `admit` 里明写）。
FIRMWARE_UNKNOWN: Final[str] = "unknown"

__all__ = [
    "FIRMWARE_UNKNOWN",
    "SCHEMA_VERSION",
    "STORE_DIRNAME",
    "CalibrationRecord",
    "CalibrationStore",
    "StoreVerdict",
    "admit_devices",
    "normalize_key",
    "record_from_calibration",
]


def _require_text(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CalibrationError(f"{field} 不能为空")
    return value.strip()


def normalize_key(kind: str, value: str) -> tuple[str, str]:
    """把身份规范化成入库用的键。**唯一的一处** —— 记录、路径、读回校验都用它。

    与 `DeviceIdentity.__post_init__` 同一套规则：同一个 MAC 写成 `aa:bb…` 与
    `AA-BB…` 是同一台设备。两处对不上的话，写进去的键取不出来，而那种失败在日志里
    看起来和「这台模块没标定过」一模一样。

    第一版把这套规则抄在了三个地方（记录的 `__post_init__`、`path_for`、读回校验），
    抄第三遍时就写歪了 —— 同一件事有三处实现，它们迟早对不上。
    """
    kind = _require_text(kind, "kind")
    value = _require_text(value, "value")
    if kind == "mac":
        value = value.upper().replace("-", ":")
    return kind, value


@dataclass(frozen=True, slots=True)
class CalibrationRecord:
    """库里的一条：一台设备的出厂标定参数，连同它当时的身份与固件。

    **身份与固件必须和参数存在一起**，否则「这份参数还算不算数」无从判断 —— 那正是
    R3 定义的失效判据要读的两个字段。
    """

    kind: str
    value: str
    provenance: str
    firmware: str
    recorded_at: str
    #: `AccelCalibration.snapshot()` 的原样内容。
    #:
    #: 名字取 `calib_snapshot` 而不是 `calibration`，有两个理由。一是它进的就是
    #: `SessionMeta.calib_snapshot`，同名省得读者在两处之间做映射。二是本仓库的
    #: wt901 标定通道红线禁止光秃秃的 `.calibration` 属性访问（那是
    #: `device.calibration` 这条通道的门）—— 第一版就叫 `calibration`，被那道红线
    #: 当场拦下。红线把命名约定推到了更一致的那个选择上。
    calib_snapshot: dict[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "provenance", _require_text(self.provenance, "provenance"))
        object.__setattr__(self, "firmware", _require_text(self.firmware, "firmware"))
        object.__setattr__(self, "recorded_at", _require_text(self.recorded_at, "recorded_at"))
        kind, value = normalize_key(self.kind, self.value)
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "value", value)
        if not isinstance(self.calib_snapshot, dict) or not self.calib_snapshot:
            raise CalibrationError(
                "calib_snapshot 不能为空 —— 一条没有参数的记录不是记录"
            )

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.value}"

    def snapshot(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "kind": self.kind,
            "value": self.value,
            "provenance": self.provenance,
            "firmware": self.firmware,
            "recorded_at": self.recorded_at,
            "calib_snapshot": self.calib_snapshot,
        }

    @classmethod
    def from_snapshot(cls, data: dict[str, Any]) -> CalibrationRecord:
        version = data.get("schema_version")
        if version != SCHEMA_VERSION:
            raise CalibrationError(
                f"标定记录的 schema_version 是 {version!r}，本版认得的是 {SCHEMA_VERSION}。"
                "格式不认得时**不猜**：按缺失处理并重新下发，比按错误的字段名读出一组"
                "看起来正常的参数安全。"
            )
        try:
            return cls(
                kind=data["kind"],
                value=data["value"],
                provenance=data["provenance"],
                firmware=data["firmware"],
                recorded_at=data["recorded_at"],
                calib_snapshot=data["calib_snapshot"],
            )
        except KeyError as error:
            raise CalibrationError(f"标定记录缺字段：{error.args[0]}") from error


@dataclass(frozen=True, slots=True)
class StoreVerdict:
    """这台设备能不能开一次正式会话，以及为什么不能。

    形状对齐 `device/binding.admit_for_session` 的 `AdmissionVerdict`：布尔 + 动作语言
    的原因列表。**不另造一套判定形状** —— P-05 自检把两者并排显示给同一个操作员。

    `reason` 另给一个机器可读的枚举值，因为「缺失」与「失效」在运营上是两件事：
    前者是这台模块还没标定过，后者是标定过但那份参数不再适用。UI 文案可以一样，
    但服务方要能把它们分开统计。
    """

    admitted: bool
    reason: str  # 'ok' | 'missing' | 'stale-provenance' | 'stale-firmware' | 'unreadable'
    problems: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.admitted and self.problems:
            raise CalibrationError("通过时不应带 problems —— 那会让调用方两头猜")
        if not self.admitted and not self.problems:
            raise CalibrationError("不通过必须给出原因 —— 操作员要知道下一步做什么")

    def snapshot(self) -> dict[str, Any]:
        return {
            "admitted": self.admitted,
            "reason": self.reason,
            "problems": list(self.problems),
        }


def _filename(kind: str, value: str) -> str:
    """键 → 文件名。冒号在 Windows 上不是合法文件名字符，必须转义。

    用**百分号转义**而不是把 `:` 换成 `-`。第一版就是后者，而它**不是单射**：
    `serial:AB:CD` 与 `serial:AB-CD` 会落到同一个文件名上，后写的那台设备静默覆盖
    前一台。MAC 因为先被规范化成冒号形式而侥幸不受影响，但 `serial` 同样是
    `binding._PORTABLE_KINDS` 里的可移植身份，这条路是走得通的。

    百分号转义可逆，因此不同的值一定落到不同的文件；而 `F9%3AB3%3A…` 仍然一眼能认
    出是哪台设备，服务方在文件管理器里找得到 —— 这是当初选可读文件名的理由，没有丢。
    """
    return f"{quote(kind, safe='')}-{quote(value, safe='')}.json"


class CalibrationStore:
    """按设备身份存取出厂标定参数。

    一台设备一个文件（而不是一个大字典），理由与 `device/recorder.py` 选择分文件相同：
    服务方按 MAC 下发时是逐台操作的，一台的写入不该有机会损坏另一台的记录。
    """

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root) / STORE_DIRNAME

    def path_for(self, kind: str, value: str) -> Path:
        return self.root / _filename(*normalize_key(kind, value))

    def put(self, record: CalibrationRecord) -> Path:
        """写入一条记录，覆盖同一台设备的旧记录。"""
        self.root.mkdir(parents=True, exist_ok=True)
        target = self.path_for(record.kind, record.value)
        # 先写临时文件再改名：写到一半断电会留下半个 JSON，而半个 JSON 读出来是
        # `unreadable`，那会把一台标定过的设备报成「参数损坏」。
        temporary = target.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(record.snapshot(), ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        temporary.replace(target)
        return target

    def get(self, kind: str, value: str) -> CalibrationRecord | None:
        """取回一条记录。**不存在返回 None，不抛异常** —— 缺失是正常状态之一。

        文件在但读不出来（损坏、schema 不认得）则**抛错**：那不是缺失，是有东西坏了，
        两者对服务方是不同的行动。
        """
        target = self.path_for(kind, value)
        if not target.exists():
            return None
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            raise CalibrationError(
                f"标定记录 {target.name} 读不出来（JSON 损坏）。这不是「没有标定」，"
                "是文件坏了 —— 请重新下发而不是重新标定。"
            ) from error
        loaded = CalibrationRecord.from_snapshot(data)

        # 文件**内容**必须与它被找到的那个键一致。这不是重复上面的文件名规则：
        # 文件名管的是「本程序写出去的键不会撞」，这一条管的是「这个文件是不是被
        # 放错了地方」—— 而下发是服务方**手工拷贝**文件的操作，拷到隔壁设备的名字
        # 下完全可能发生，且拷错之后一切看起来都正常：读得出、schema 对、参数也像
        # 真的，只是那是另一台模块的参数。
        expected_kind, expected_value = normalize_key(kind, value)
        if (loaded.kind, loaded.value) != (expected_kind, expected_value):
            raise CalibrationError(
                f"标定记录 {target.name} 里记的是 {loaded.key}，与请求的 "
                f"{expected_kind}:{expected_value} 对不上。这份文件多半是被放错了位置"
                "（下发时拷错），拿它去补偿等于用另一台模块的参数。"
            )
        return loaded

    def admit(
        self,
        kind: str,
        value: str,
        *,
        current_provenance: str,
        current_firmware: str,
    ) -> StoreVerdict:
        """这台设备能不能开一次正式会话（FR-04）。

        三种不通过，理由分开报，因为操作员的下一步不同：**缺失**要联系服务方下发；
        **失效**同样要重新下发，但服务方那边得知道是身份推导还是固件变了；**读不出来**
        是文件坏了，重新下发即可，不必重新标定。
        """
        current_provenance = _require_text(current_provenance, "current_provenance")
        current_firmware = _require_text(current_firmware, "current_firmware")

        try:
            record = self.get(kind, value)
        except CalibrationError as error:
            return StoreVerdict(
                admitted=False, reason="unreadable", problems=(str(error),)
            )

        if record is None:
            return StoreVerdict(
                admitted=False,
                reason="missing",
                problems=(
                    (
                        f"这台模块（{kind}:{value}）没有出厂标定参数。"
                        "请联系服务方按模块 MAC 下发；机构侧不做标定。"
                    ),
                ),
            )

        if record.provenance != current_provenance:
            return StoreVerdict(
                admitted=False,
                reason="stale-provenance",
                problems=(
                    (
                        f"这台模块的标定参数是用 {record.provenance} 推导出的键存的，"
                        f"当前推导是 {current_provenance}。同一台设备在两套推导下得到"
                        "不同的键，所以这不是设备换了一台 —— 请联系服务方重新下发。"
                    ),
                ),
            )

        # 固件未知时阻断而不是放行：FR-04 要求参数与模块匹配，而「匹配不上」与
        # 「不知道匹不匹配」对下游是同一件事 —— 都不能保证这份参数描述的是这台设备。
        # 这与 `device/orchestration.preflight_battery` 的三路判定同一口径：
        # 够电放行、低电阻断、**读不到也阻断**。
        if current_firmware == FIRMWARE_UNKNOWN or record.firmware == FIRMWARE_UNKNOWN:
            return StoreVerdict(
                admitted=False,
                reason="stale-firmware",
                problems=(
                    (
                        "读不到模块固件版本，无法确认标定参数与这台模块匹配。"
                        "请重连模块后重试。"
                    ),
                ),
            )

        if record.firmware != current_firmware:
            return StoreVerdict(
                admitted=False,
                reason="stale-firmware",
                problems=(
                    (
                        f"这台模块的标定参数是在固件 {record.firmware} 上做的，"
                        f"当前固件是 {current_firmware}。请联系服务方重新标定并下发。"
                    ),
                ),
            )

        return StoreVerdict(admitted=True, reason="ok")


def record_from_calibration(
    calibration: Any,
    *,
    kind: str,
    value: str,
    provenance: str,
    firmware: str,
    recorded_at: str | None = None,
) -> CalibrationRecord:
    """把一个 `AccelCalibration` 连同它的身份包成可入库的记录。

    `calibration` 只要求有 `snapshot()` —— 与 `device/footseries.AccelCalibration`
    那个 Protocol 同样的理由：标定类只有一套形状，不为某个消费者另造基类。
    """
    if not hasattr(calibration, "snapshot"):
        raise CalibrationError(
            f"{type(calibration).__name__} 没有 snapshot()，无法入库"
        )
    return CalibrationRecord(
        kind=kind,
        value=value,
        provenance=provenance,
        firmware=firmware,
        recorded_at=recorded_at or datetime.now(UTC).isoformat(),
        calib_snapshot=calibration.snapshot(),
    )


def admit_devices(
    store: CalibrationStore,
    readings: dict[str, dict[str, str]],
    *,
    current_provenance: str,
) -> dict[str, StoreVerdict]:
    """双足各判一次。`readings` 是**读数**：每只脚一份 `{kind, value, firmware}`。

    形状刻意与 `device/orchestration.preflight_battery(readings)` 一致：**入参是读数，
    出参是判定**。`app/sources.py` 的模块文档把这条写成了原则 —— stub「只被允许提供
    读数，不能决定准入是否通过」。

    两只脚都必须有读数。少一只不是「那只脚没问题」，是这次自检没覆盖它 —— 与
    `calib.still.verdict` 和 `device/orchestration.preflight_battery` 同一口径。
    """
    missing = sorted({"L", "R"} - set(readings))
    if missing:
        raise CalibrationError(
            f"缺少这些脚的设备读数：{missing}。少一只不是「那只脚没问题」，"
            "是这次自检没覆盖它。"
        )
    verdicts: dict[str, StoreVerdict] = {}
    for label in ("L", "R"):
        reading = readings[label]
        try:
            kind, value = reading["kind"], reading["value"]
            firmware = reading.get("firmware", FIRMWARE_UNKNOWN)
        except KeyError as error:
            raise CalibrationError(
                f"{label} 脚的读数缺字段：{error.args[0]}"
            ) from error
        verdicts[label] = store.admit(
            kind,
            value,
            current_provenance=current_provenance,
            current_firmware=firmware or FIRMWARE_UNKNOWN,
        )
    return verdicts
