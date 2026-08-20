"""Test protocol state machines.

Scope: T-01 timed walk: timing, pause detection, effective-duration stats (RAY-214).

PRD v1.2 §7: default 180 s, configurable 60/120/180; a session is valid
when effective duration reaches 70%.

"""
