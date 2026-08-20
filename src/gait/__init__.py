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

#: Layer packages, lowest dependency first. This tuple is the single
#: declaration of what the package is made of: the layout test reads it, and
#: the layering guard will read it rather than re-listing directories.
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
