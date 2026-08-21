**CountdownFocus** — the subject-facing half of the timed-walk screen.

```jsx
<CountdownFocus seconds={71} instruction="请按平时走路的速度，在两个标志之间来回走" />
<CountdownFocus compact seconds={71} instruction="请按平时走路的速度，在两个标志之间来回走" />
```

The subject reads this while walking, 0–3 m away; the operator reads their own numbers at arm's length in a separate column. Those two viewing distances differ by an order of magnitude — one type scale cannot serve both, which is why the capture screen is split and this component owns the far half.

It holds exactly four things: eyebrow, number, caption, instruction. **No step counts, no metrics, no upload state, no progress bar** — those are either the operator's or nobody's.

Seconds are shown as a bare number, not `mm:ss`: a moving reader scans one integer faster than a formatted clock. Keep `mm:ss` in the top bar for the operator.

The number changes once per second as a value swap. Never animate it — and never let anything on this half pulse at a fixed cadence, or the subject will start walking to it.
