**BatteryPair** — both modules' charge, shown **only before** the 200 Hz stream starts.

```jsx
<BatteryPair left={82} right={55} />
```

Tiers come from `batteryTier(percent)`: `full` ≥60% (3 segments, green), `mid` 30–60% (2, amber), `low` <30% (1, red — **blocks a new session**). Segment count carries the tier, so it reads without color.

Once capture begins the registers are unreadable at 200 Hz. **Remove this component from the screen** — do not grey it out and do not leave the last value on screen. Use `LinkStatus` instead: arrival rate is the only signal that stays honest during capture.

When a module is not connected, show no battery at all rather than a placeholder percentage.
