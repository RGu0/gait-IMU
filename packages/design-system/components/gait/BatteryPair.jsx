import React from "react";
import { SideBadge } from "./SideBadge.jsx";

/**
 * BatteryPair — left and right module charge, read BEFORE the 200 Hz stream
 * starts. Below 30% blocks a new session.
 *
 * Once the high-rate stream is running the registers cannot be read. This
 * component must then be REMOVED from the screen — not greyed out, not left
 * showing the last known value. A stale battery reading is worse than none.
 */
export function BatteryPair({ left, right, style, ...rest }) {
  return (
    <div style={{ display: "inline-flex", alignItems: "center", gap: "var(--space-2)", ...style }} {...rest}>
      <BatteryChip side="left" percent={left} />
      <BatteryChip side="right" percent={right} />
    </div>
  );
}

/** ≥60% adequate · 30–60% moderate · <30% blocks a new session. */
export function batteryTier(percent) {
  if (percent >= 60) return "full";
  if (percent >= 30) return "mid";
  return "low";
}

function BatteryChip({ side, percent }) {
  const tier = batteryTier(percent);
  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: 7, height: 28, padding: "0 10px 0 6px", borderRadius: "var(--radius-pill)", background: "var(--bg-sunken)", border: "1px solid var(--border-default)", font: "var(--text-secondary-size)", color: "var(--text-secondary)" }}>
      <SideBadge side={side} />
      <BatteryIcon tier={tier} />
      <span style={{ fontFamily: "var(--font-num)", fontVariantNumeric: "tabular-nums", fontWeight: 600, color: "var(--text-primary)" }}>{percent}%</span>
    </span>
  );
}

function BatteryIcon({ tier }) {
  const color = tier === "full" ? "var(--success-fg)" : tier === "mid" ? "var(--warning-fg)" : "var(--danger-fg)";
  const n = tier === "full" ? 3 : tier === "mid" ? 2 : 1;
  return (
    <svg width="26" height="14" viewBox="0 0 26 14" aria-hidden="true" style={{ flex: "none" }}>
      <rect x="0.8" y="0.8" width="21.4" height="12.4" rx="3" fill="none" stroke={color} strokeWidth="1.4" />
      <rect x="23.4" y="4.6" width="2" height="4.8" rx="1" fill={color} />
      {[0, 1, 2].slice(0, n).map((i) => (
        <rect key={i} x={3.2 + i * 5.6} y="3.6" width="4.6" height="6.8" rx="1" fill={color} />
      ))}
    </svg>
  );
}
