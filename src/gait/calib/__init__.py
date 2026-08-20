"""L3 calibration layer.

Modules, and the Issue that delivers each::

    accel.py       F2.1 six-face accelerometer calibration (service-side jig)
                   -> RAY-207
    store.py       F2.5 calibration parameter store
                   -> RAY-207
    static.py      F2.2 static bias refresh
                   -> RAY-208
    frames.py      F2.3 coordinate frame re-ordering
                   -> RAY-208
    mounting.py    F2.4 mounting misalignment angle
                   -> RAY-208

PRD v1.2 §14: six-face calibration becomes a service-side workflow;
the institution side only performs session-level calibration.

"""
