**RhythmStrip** — the last few seconds of footfalls, at their real timestamps. Up = left, down = right.

```jsx
<RhythmStrip left={[18, 66, 112, 161, 207]} right={[42, 90, 137, 184, 233]} />
```

Ticks are placed at **actual** footfall times, so the spacing is uneven — that unevenness is the information. Render at ~30 FPS, decoupled from the 200 Hz data rate.

**It is not a metronome.** Never tick at a fixed cadence, never animate, never pulse. A subject who can see a regular beat will walk to it, and the measurement becomes the instrument's rhythm rather than their own. For the same reason the strip belongs in the operator column, out of the subject's line of sight.

Pair it with a caption naming the two directions (「刻痕向上为左足、向下为右足」) — inside such a short strip there is no room for legible 14px labels, and the direction alone is not a text channel.
