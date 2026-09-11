**LinkStatus** — BLE link health during capture, as three tiers driven by the **trailing-window
loss rate**.

```jsx
<LinkStatus side="left"  tier="good" />
<LinkStatus side="right" tier="fair" />
<LinkStatus side="right" tier="bad" />
```

Tiers, from the loss rate over the **trailing 5 s**: `good` (< 8%, 3 solid bars),
`fair` (8–16%, 2 bars), `bad` (≥ 16% **or dropped**, 1 bar + slash). The **number of solid
bars** carries the tier, so the icon survives with color removed. `dropped` is its own
state, not a loss rate — a disconnected module is `bad` regardless of what the last window
read.

**Not arrival rate.** An earlier version of this spec drove the tiers off per-second arrival
rate at 99% / 95%. That was measured wrong on real hardware: in a 30-minute round that lost
**zero samples**, 534 of 1799 seconds read below 0.99 — BLE notifications arrive in bursts, so
the baseline sits right on 0.99 and jitters across it. Implemented as written, a perfectly
healthy capture would show "链路波动" about **30% of the time**. Changing the three numbers does
not fix it; the quantity was wrong. Loss carries no jitter — a sample either arrived or it did
not (RAY-274).

The same loss rate backs the session-level criterion, so there is **one definition and one set
of thresholds**; the two differ only in aggregation — this row shows the current window, while
the session verdict takes the worst window. Measured on five real rounds, this row leaves `good`
for **0.0–0.3%** of a healthy capture and 8.8–37% of a failing one.

Both numerator and denominator must match the session side exactly: losses come from the **gap
detector**, and the denominator is the **device's emitted rate**, not the nominal 200 Hz. The
8% / 16% thresholds are **provisional** — they are borrowed from the V2 link criterion, and what
they still lack is a measured "loss rate → stride-length error" curve.

At 200 Hz the module registers cannot be read — **never show battery on a capture screen**, not
even a stale value.

The two rows always appear as a pair, left above right, matching `SideBadge` order. A drop to
`bad` during capture changes this row and nothing else: no dialog, no toast, and no error code
shown to the operator — mid-walk there is nothing they can do about it without ruining the test.
The code belongs in the log and the session record.
