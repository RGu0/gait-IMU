"""`cli.mvp --replay` —— 回放路径的端到端验收（RAY-345）。

把一个合成双足行走**量化回 int16 码值**写进录制，再经 `--replay` 走完整条闭环
（读录制 → FootSeries → 基础链 → report.html）。这条守的是「回放路径真的通」——
它是 Issue 验收第二条的可执行版。
"""

import json
import struct

import numpy as np
from wt901.protocol.units import (
    ACCEL_FULL_SCALE_G,
    GYRO_FULL_SCALE_DPS,
    INT16_FULL_SCALE,
    STANDARD_GRAVITY,
)
from wt901.recording import RecordedChunk, Recording, write_recording

from gait.cli.mvp import main
from gait.contracts import SessionMeta
from gait.io.session import create_session, new_session_id, raw_path
from gait.report.wording import reason_text
from gait.validate.synthetic import WalkSpec, generate_dual_walk


def _to_module_raw(acc: np.ndarray, gyr: np.ndarray, label: str) -> tuple[np.ndarray, np.ndarray]:
    """足部系 → 模块体系（重排的逆），再 SI → int16 码值。

    **重排现在是恒等**（RAY-390：实测确认模块系与足部系是同一个系），所以它的逆也是
    恒等，这里不再换轴。本函数原先写的是 `[sign*acc[:,1], acc[:,0], acc[:,2]]` ——
    那是旧的轴交换的逆，RAY-390 之后它不再是任何东西的逆，只是在**引入**一次错乱。

    没有测试因此变红，因为本文件唯一那条用例只断言 `report.html` 生成得出来、里面
    没有 `NaN` —— 一份轴错乱的数据照样满足它。所以顺手把这条也补上：下面新增的
    两条用例会读 `report.json` 里的指标。
    """
    del label  # 两只脚同一个映射（RAY-390）；参数留着是为了调用点仍然读得懂。
    acc_raw = np.rint(acc * INT16_FULL_SCALE / (ACCEL_FULL_SCALE_G * STANDARD_GRAVITY))
    gyr_raw = np.rint(np.degrees(gyr) * INT16_FULL_SCALE / GYRO_FULL_SCALE_DPS)
    return np.clip(acc_raw, -32768, 32767).astype(int), np.clip(gyr_raw, -32768, 32767).astype(int)


def _frame(ax: int, ay: int, az: int, wx: int, wy: int, wz: int) -> bytes:
    return b"\x55\x61" + struct.pack("<9h", ax, ay, az, wx, wy, wz, 0, 0, 0)


def _record(path, series, label: str) -> None:
    acc_raw, gyr_raw = _to_module_raw(series.acc, series.gyr, label)
    chunks = tuple(
        RecordedChunk(
            t=float(i) / series.fs,
            data=_frame(
                int(acc_raw[i, 0]), int(acc_raw[i, 1]), int(acc_raw[i, 2]),
                int(gyr_raw[i, 0]), int(gyr_raw[i, 1]), int(gyr_raw[i, 2]),
            ),
        )
        for i in range(len(series.t))
    )
    write_recording(
        path,
        Recording(
            device_id=f"dev-{label}",
            created_utc="2026-09-01T00:00:00Z",
            note="synthetic",
            chunks=chunks,
        ),
    )


def _make_session(root) -> None:
    session_id = new_session_id()
    meta = SessionMeta(
        session_id=session_id,
        created_at="2026-09-01T00:00:00Z",
        subject_uuid="3f2a6d0e-0000-4000-8000-000000000001",
        scenario="walk",
        devices={"L": {"mac": "AA"}, "R": {"mac": "BB"}},
        config_snapshot={"rate_hz": 200},
        calib_snapshot={"L": {}, "R": {}},
        algo_version="basic-0.0.0",
        algo_params={"preset": "default"},
        sync_report={"anchors": 0},
        integrity_report={"loss_rate": 0.0},
        protocol_config={"duration_s": 20, "version": "2.3"},
    )
    create_session(root, meta)
    dual = generate_dual_walk(WalkSpec(duration_s=20.0))
    for label, (series, _truth) in dual.items():
        _record(raw_path(root, session_id, label), series, label)
    return session_id


def test_replay_produces_html_and_json(tmp_path):
    session_id = _make_session(tmp_path)
    session_dir = tmp_path / session_id

    code = main(["--replay", str(session_dir), "--out", str(tmp_path / "out")])
    assert code == 0

    html_path = tmp_path / "out" / "report.html"
    json_path = tmp_path / "out" / "report.json"
    assert html_path.is_file()
    assert json_path.is_file()

    markup = html_path.read_text(encoding="utf-8")
    for heading in ("步态检测报告", "核心指标", "左右对比", "测试条件", "报告编号"):
        assert heading in markup
    assert "NaN" not in markup and "Infinity" not in markup


def _metrics(json_path) -> dict:
    return {
        item["key"]: item
        for item in json.loads(json_path.read_text(encoding="utf-8"))["metrics"]
    }


def test_replay_does_not_claim_a_sync_quality_it_never_computed(tmp_path):
    """回放路径**没算过**同步质量，就不能让跨足指标带着一个干净的等级出场。

    回放路径的时轴是标称 200 Hz（MVP 桥），跨足同步从未被计算。第一版这里填了
    `{"determinate": True, "flagged": False}`，等于告诉定级层「这次同步是好的」——
    实测（RAY-395）那份占位把双支撑期从 `low` + `missing_sync_quality` 抬成了
    `normal` + 无理由：**连「这项没有同步依据」都看不见了**。

    留空之后它带上 `missing_sync_quality` 这条理由，等级落到 `low`。**这条断言钉的
    是那条理由与那个等级**，把占位填回去，理由消失、等级变 `normal`，本条变红。

    ## RAY-395 改了这条断言的写法，但没有改它守的东西

    本条原先钉的是「**不出数**」（`assert "value" not in ds`）。那不是本 scope 的决定，
    是当时的一个**下游后果**：`cloud/chain.py` 在 `sync_quality is None` 时根本不算
    双支撑期，于是标注拿到 `computable=False`，报告印「本次不适用」。

    `one-report-builder` 把两条报告路径合成一个装配层之后，这个后果变了：采集端那条路
    （`app/service.py` 的 `reportFor`）从来不传 `sync_quality`，而它一直是**出数**的 ——
    走站立相恒等式（`2 × 平均站立相 − 100`，足内量，不碰跨足时序）。同一个指标在两条路
    上一条出数一条不出数，正是 RAY-395 要消掉的那种分岔；用户裁定「冲突以产品路径为准，
    是否可算按产品路径现在的做法定」，所以留下来的是出数的那条。

    **本 scope 的决定原样保留**：回放路径依旧不填它没算过的同步质量，报告依旧说得出
    「这一项没有同步依据」。变的是缺同步依据时印什么 —— 从「本次不适用」变成一个
    带 `low` 与 `missing_sync_quality` 的读数。占位一旦填回来，读数会换成相位重叠口径、
    等级抬到 `normal`、理由清空，下面三条断言会一起红。
    """
    session_id = _make_session(tmp_path)
    code = main(["--replay", str(tmp_path / session_id), "--out", str(tmp_path / "out")])
    assert code == 0

    ds = _metrics(tmp_path / "out" / "report.json")["double-support"]
    assert ds["grade"] != "normal", "没算过同步质量的双支撑期不该是「良好」"
    # 「这一项没有同步依据」必须**看得见**——那是本 scope 的全部要点。
    assert "missing_sync_quality" in ds["quality"]["reasons"], (
        f"回放路径的双支撑期该说出它没有同步依据，实际 {ds['quality']['reasons']}"
    )
    assert ds["quality"]["sync_quality"] is None, "回放路径没算过同步质量，不该带一份"
    # ── 已知缺陷，钉住现状（不在本 scope 修） ────────────────────────────────
    #
    # 给读者的那句说明**是错的**：`wording.metric_note` 对任何 `low` 都回同一句
    # 「本次有效步数较少」，而这一项落 `low` 的真实原因是**没有同步依据**，与步数
    # 无关。读的人会去补步数 —— 那正是 `wording.py` 模块文档自己写的那种「具体而
    # 错误的解释比不解释更糟」。
    #
    # 这不是 `one-report-builder` 引进来的：拉齐前这条路上印的是「本次协议不产出
    # 该项」，同样具体而错误（协议当然产出双支撑期）。拉齐只是把机器可读的那一条
    # 从 `not_computable` 换成了更准的 `missing_sync_quality`，人读的那句没跟上。
    #
    # 属 RAY-398（一个 reason 盖两种来路）的范围。这里钉住现状，等它动翻译时本条
    # 变红，好让有人回来看一眼这句话该怎么写。
    #
    # RAY-437 起 `note` 前面多了一段口径标注（这条路走站立相恒等式），所以这里改钉
    # **等级说明那一半**。缺陷本身一个字没动：那句话依旧在说步数，而真实原因是缺
    # 同步依据。口径标注是另一件事，它由下面一条单独钉住。
    assert ds["note"].endswith("本次有效步数较少，此项仅供参考。"), (
        f"已知缺陷的现状变了，请回看 RAY-398：实际 {ds.get('note')!r}"
    )
    # 回放路径走的是恒等式那一支，**不能**被标成「ZUPT 边界口径」（RAY-437）。
    assert ds["caliber"] == "stance-identity", (
        f"回放路径没有同步依据，只可能走恒等式，实际 {ds.get('caliber')!r}"
    )
    assert "非 ZUPT 边界口径" in ds["note"]
    assert "reference" not in ds, "恒等式那一支不给参考值（RAY-288 R2）"
    assert reason_text(["missing_sync_quality"]) == "本次没有两侧同步质量的依据。", (
        "翻译表里那句对的话仍在，只是 `metric_note` 在 `low` 时没有用它"
    )


def test_synthetic_keeps_its_sync_quality_because_there_it_is_true(tmp_path):
    """合成路径**保留**那份同步质量 —— 在那条路上它不是占位，是真的。

    `generate_dual_walk` 的两只脚共用同一个精确时轴、无抖动、无丢包。两条路径从此
    在这一点上不同，而**这条断言就是那个「不同」的锚**：若哪天有人图省事把两条路径
    又合并成同一个常量，两条测试里必有一条变红。
    """
    code = main(["--synthetic", "--seconds", "20", "--out", str(tmp_path / "out")])
    assert code == 0

    ds = _metrics(tmp_path / "out" / "report.json")["double-support"]
    assert "value" in ds, f"合成数据的同步质量是确定的，双支撑期该出数，实际 {ds}"
    # 两条路径的「不同」现在锚在**这里**：合成路径带着那份真实的同步质量，于是
    # 双支撑期没有 `missing_sync_quality`；回放路径有。两条断言合起来才是那个锚。
    assert ds["quality"]["sync_quality"] == {"determinate": True, "flagged": False}
    assert "missing_sync_quality" not in ds["quality"]["reasons"]
