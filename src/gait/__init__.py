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

#: 本包内不得出现在 ``core/`` import 语句中的层。声明在这里，规则只有一处家。
CORE_FORBIDDEN_IMPORTS: tuple[str, ...] = ("io", "device", "sync")

#: 不得出现在 ``core/`` import 语句中的第三方包。
#:
#: 契约 §2 的原文就点名了 ``bleak``："任何时候发现 core 里出现了 open() 或
#: bleak，说明分层被破坏了"。在 wt901 进入依赖树之前这只是假想防御 —— 一个
#: 装不上的包本来就 import 不了。现在它真实存在，这条才成为能被违反的规则。
#:
#: ``wt901`` 与 ``bleak`` 分列而不是只写 ``wt901``：前者是我们选的适配对象，
#: 后者是它的传递依赖，两条路径都能把 BLE 拖进纯函数库。
CORE_FORBIDDEN_PACKAGES: tuple[str, ...] = ("bleak", "wt901")
