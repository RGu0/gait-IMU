"""L5 algorithm core — a pure function library over numpy/scipy.

Modules, and the Issue that delivers each::

    quaternion.py  quaternion and rotation helpers
                   -> RAY-201
    ins.py         F4.1 strapdown inertial mechanisation
                   -> RAY-201
    alignment.py   F4.5 initial alignment (gravity, yaw=0 relative heading)
                   -> RAY-202
    zupt.py        F4.2/4.6 adaptive zero-velocity detection, slow/pathological presets
                   -> RAY-203
    eskf.py        F4.3 15-state error-state Kalman filter
                   -> RAY-204
    dualfoot.py    F4.4 dual-foot distance constraint, cross-foot period check
                   -> RAY-205, RAY-328

RED LINE: this package must not import gait.io, gait.device or
gait.sync. An ``open()`` or a ``bleak`` import appearing here means the
layering has been broken. Enforced by RAY-192 scope ``layering-guard``.

"""
