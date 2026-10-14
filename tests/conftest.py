"""跨测试文件共用的夹具。"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from gait.app.replay import calibration_clip
from gait.app.service import TerminalService
from gait.app.sessioncalib import CalibrationRun


def _passed_run() -> CalibrationRun:
    """把合成标定片段离线推一遍，得到一次**真的通过了**的标定。

    不是伪造一个 `phase = "passed"` 的对象：判据照旧走 `gait/calib`，只是不等真实的秒数。
    """
    now = [0.0]
    run = CalibrationRun(clock=lambda: now[0], step_total=lambda: int(now[0] * 1.8))
    run.begin()
    timeline = sorted(
        (
            (t, label, data)
            for label, items in calibration_clip().items()
            for t, data in items
        ),
        key=lambda item: (item[0], item[1]),
    )
    index = 0
    while run.running:
        now[0] += 0.25
        while index < len(timeline) and timeline[index][0] <= now[0]:
            run.buffers[timeline[index][1]].receive(timeline[index][2])
            index += 1
        run.poll()
        if now[0] > 60:  # pragma: no cover - 片段本身坏了才会走到
            raise AssertionError("合成标定片段没能在 60 s 内通过")
    if not run.passed:  # pragma: no cover - 同上
        raise AssertionError(f"合成标定片段没有通过：{run.failure}")
    return run


@pytest.fixture
def calibrated() -> Callable[[TerminalService], TerminalService]:
    """给一个 service 装上一次已通过的会话标定（RAY-754：真实传感器下没标定不能开检测）。

    走真实传感器路径、却不是在测标定本身的测试用它，免得每个都去驱动一遍 P-07。
    """

    def install(service: TerminalService) -> TerminalService:
        service._session_calib = _passed_run()
        service._calib_unavailable = None
        service._calib_first_session = None
        service._calib_subject = None
        return service

    return install
