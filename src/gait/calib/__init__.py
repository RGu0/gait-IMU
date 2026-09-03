"""会话级标定（RAY-208）。

`still.py` —— 静立 5 s：陀螺零偏、标定基准、松动检测。
坐标系重排与安装误差角属 `walk-calibration` scope，尚未实现。
"""

from gait.calib.still import (
    LOOSENESS_LIMIT_DEG,
    MIN_STILL_SECONDS,
    CalibrationError,
    CalibrationVerdict,
    LoosenessCheck,
    StillCalibration,
    calibrate_still,
    check_looseness,
    verdict,
)

__all__ = [
    "LOOSENESS_LIMIT_DEG",
    "MIN_STILL_SECONDS",
    "CalibrationError",
    "CalibrationVerdict",
    "LoosenessCheck",
    "StillCalibration",
    "calibrate_still",
    "check_looseness",
    "verdict",
]
