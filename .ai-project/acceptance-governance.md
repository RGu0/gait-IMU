# Acceptance governance

Scope-local acceptance is not enough. A scope that touches a **shared algorithm path** —
period estimation, timebase, stance detection, the dual-foot planning entry — changes the
readings that **other Issues' criteria** were verified against, including Issues that are
already Done.

## The rule

Before requesting review, such a scope runs the full suite and records the result:

```
python tools/run_acceptance.py <RAY-230 trial dirs>...
```

Every script must pass, or the failure must be explained in the scope's `review` record.
A script that **crashes** is reported separately from one that fails its criteria — the
two mean different things, and the runner keeps them apart.

## Why the rule exists

RAY-328 and RAY-339 each ran only the criteria their own scopes carried. The layering
red line and the regression suite are checked on every `lint` / `test`, but the
quantitative criteria were not. So RAY-328's three acceptance scripts went red on `main`
and **it took two Issues to notice** (RAY-343). Three different causes, only one of them
benign:

| Script | What had happened |
| -- | -- |
| `alternation_acceptance` | a hardcoded per-cell table went stale; the guarded properties still held |
| `dual_foot_qc_acceptance` | its only true positive had been **fixed** by a later scope |
| `xcorr_prior_acceptance` | **a real regression** — the cross-correlation period prior had gone from useful to net-negative |

The third one is the reason this rule is not optional.

## Writing acceptance scripts

They live in `tools/acceptance/` because **they are code, not evidence**: versioned,
reviewed, linted, and pinned by `tests/test_acceptance_suite.py`. The numbers they produce
are the evidence and belong in the shared library.

Two rules, both learned the hard way:

1. **Pin properties, not absolute numbers.** In one RAY-328 script the accounting identity
   (`slots = detected − merged + inferred`) still holds today, while the `CYCLES_AFTER_L1`
   lookup table in the same file rotted completely — 12 of its 12 failures came from that
   table. If a baseline is unavoidable, **recompute it in the same run**. The one constant
   that may be pinned is the controlled ground truth (`TRUTH_CYCLES = 38`), because it was
   counted in the field and does not move when the code does.
2. **Every script carries a positive control.** Inject a defect it is supposed to catch and
   assert it goes red. Without one, "pinned to properties" quietly becomes "pinned to
   always green" — which is worse than stale, because it looks like it is guarding.

## What this rule does not do

The runner is **not** in `./dev` and not in CI. It reads the shared cloud library, so
gating on it would turn an offline-capable gate into one that needs the cloud, and each
script runs the full pipeline over 24 cells.

So it rests on a convention, and conventions get forgotten. That is a known, unfixed gap —
recorded here rather than papered over.
