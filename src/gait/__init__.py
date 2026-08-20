"""IMU-based gait analysis.

Layer map — see ``documents/Ref/模块划分与接口契约.md`` §1-2 and PRD v1.2 §14::

    cli/ ─────────────────────────────────┐
      │  may depend on every layer below  │
      ▼                                   │
    app/  cloud/  report/ ──▶ validate/   │
                    │            │        │
                    ▼            ▼        │
                analysis/ ──▶ quality/    │
                    │                     │
                    ▼                     │
                  core/  ◀── pure functions, numpy/scipy only
                    ▲                     │
                    │                     │
    protocolflow/  sync/ ──▶ calib/       │
                    │           │         │
                    ▼           ▼         │
                   io/  ◀───────┘         │
                    ▲                     │
                    │                     │
                device/ ◀─────────────────┘
                    │
                    ▼
                config.py  ← readable by every layer, depends on none

Two red lines carry over from the contract into CI:

* ``core/`` must not import ``io/``, ``device/`` or ``sync/`` — if ``open()``
  or ``bleak`` ever appears under ``core/``, the layering is broken.
* Quality logic is implemented exactly once, in ``quality/``.

Enforcement lands in RAY-192 scope ``layering-guard``; this scope only
establishes the structure the check will read.
"""

__version__ = "0.1.0"

#: Every layer package, in the order the contract presents them (§1). This is
#: an inventory, not a dependency order: the dependencies form a DAG with two
#: roots — ``core`` depends on nothing but numpy/scipy, ``config`` on nothing
#: at all — and a flat tuple cannot express that. The graph is drawn in the
#: module docstring above; ``layering-guard`` encodes it. The layout test
#: reads this tuple as a set, which is all it is.
LAYERS: tuple[str, ...] = (
    "device",
    "io",
    "calib",
    "sync",
    "core",
    "analysis",
    "quality",
    "protocolflow",
    "report",
    "validate",
    "app",
    "cloud",
    "cli",
)

#: Packages that may not appear in a ``core/`` import statement. Stated here
#: so the rule has one home; ``layering-guard`` turns it into a check.
CORE_FORBIDDEN_IMPORTS: tuple[str, ...] = ("io", "device", "sync")
