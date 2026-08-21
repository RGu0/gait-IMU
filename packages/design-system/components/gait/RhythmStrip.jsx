import React from "react";

/**
 * RhythmStrip — the last few seconds of footfalls, drawn at their ACTUAL
 * timestamps: up = left, down = right.
 *
 * Rendered at ~30 FPS and fully decoupled from the 200 Hz data rate. It exists
 * so the operator can see that the subject is still walking normally. It is
 * NOT a metronome: it must never tick at a fixed cadence and never animate,
 * or the subject will fall into step with it and the measurement becomes the
 * instrument's rhythm instead of their own. That is also why it belongs in the
 * operator column, away from the subject's line of sight.
 */
export function RhythmStrip({ left = [], right = [], width = 483, height = 66, style, ...rest }) {
  const mid = Math.round(height / 2);
  return (
    <svg viewBox={"0 0 " + width + " " + height} width="100%" height={height} style={{ display: "block", ...style }} aria-label="近 8 秒落步刻痕" {...rest}>
      <rect x="0.5" y="0.5" width={width - 1} height={height - 1} rx="8" fill="var(--viz-canvas)" stroke="var(--viz-canvas-border)" />
      <path d={"M 8 " + mid + " L " + (width - 8) + " " + mid} stroke="var(--viz-grid)" strokeWidth="1.5" />
      {left.map((x, i) => (
        <path key={"l" + i} d={"M " + x + " " + mid + " L " + x + " " + (mid - 19)} stroke="var(--viz-gait-left)" strokeWidth="2.5" strokeLinecap="round" />
      ))}
      {right.map((x, i) => (
        <path key={"r" + i} d={"M " + x + " " + mid + " L " + x + " " + (mid + 19)} stroke="var(--viz-gait-right)" strokeWidth="2.5" strokeLinecap="round" />
      ))}
    </svg>
  );
}
