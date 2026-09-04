"""会话级标定（RAY-208）。

`still.py` —— 静立 5 s：陀螺零偏、标定基准、松动检测。
`walk.py` —— 直线走 10 步：安装误差角与坐标系重排。

出厂标定（RAY-207 R2，服务方工装）：
`accel.py` —— 加计多姿态标定：模长判据解对称 3×3 标度/非正交矩阵与零偏向量。
"""

from gait.calib.accel import (
    MILLI_G,
    MIN_ORIENTATIONS,
    AccelCalibration,
    OrientationObservation,
    observe_orientation,
    solve_orientations,
)
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
    "MILLI_G",
    "MIN_ORIENTATIONS",
    "MIN_PEAK_ASYMMETRY",
    "MIN_PRINCIPAL_RATIO",
    "MIN_STILL_SECONDS",
    "AccelCalibration",
    "CalibrationError",
    "CalibrationVerdict",
    "LoosenessCheck",
    "MountingCalibration",
    "OrientationObservation",
    "StillCalibration",
    "calibrate_still",
    "check_looseness",
    "estimate_mounting",
    "observe_orientation",
    "solve_orientations",
    "verdict",
]
