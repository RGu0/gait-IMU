"""会话级标定（RAY-208）。

`still.py` —— 静立 5 s：陀螺零偏、标定基准、松动检测。
`walk.py` —— 直线走 10 步：安装误差角与坐标系重排。
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
from gait.calib.walk import (
    MIN_PEAK_ASYMMETRY,
    MIN_PRINCIPAL_RATIO,
    MountingCalibration,
    estimate_mounting,
)

__all__ = [
    "LOOSENESS_LIMIT_DEG",
    "MIN_PEAK_ASYMMETRY",
    "MIN_PRINCIPAL_RATIO",
    "MIN_STILL_SECONDS",
    "CalibrationError",
    "CalibrationVerdict",
    "LoosenessCheck",
    "MountingCalibration",
    "StillCalibration",
    "calibrate_still",
    "check_looseness",
    "estimate_mounting",
    "verdict",
]
