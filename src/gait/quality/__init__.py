"""Quality annotation — the single implementation point.

Scope: quality labelling rules and the gating configuration carrier (RAY-218).

PRD v1.2 §13/§14 red line: quality logic is implemented exactly once,
here. Host and cloud must call this same code rather than reimplement it.

"""
