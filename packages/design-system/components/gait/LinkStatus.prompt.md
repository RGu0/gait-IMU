**LinkStatus** — BLE link health during capture, as three tiers driven by **arrival rate only**.

```jsx
<LinkStatus side="left"  tier="good" />
<LinkStatus side="right" tier="fair" />
<LinkStatus side="right" tier="bad" />
```

Tiers: `good` (≥99% arrival, 3 solid bars), `fair` (95–99%, 2 bars), `bad` (<95% or dropped, 1 bar + slash). The **number of solid bars** carries the tier, so the icon survives with color removed.

At 200 Hz the module registers cannot be read — **never show battery on a capture screen**, not even a stale value. Arrival rate is the only honest signal there.

The two rows always appear as a pair, left above right, matching `SideBadge` order. A drop to `bad` during capture changes this row and nothing else: no dialog, no toast, and no error code shown to the operator — mid-walk there is nothing they can do about it without ruining the test. The code belongs in the log and the session record.
