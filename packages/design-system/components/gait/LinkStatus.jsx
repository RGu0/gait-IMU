import React from "react";
import { SideBadge } from "./SideBadge.jsx";

/**
 * LinkStatus — BLE link health as three tiers, expressed by ARRIVAL RATE and
 * nothing else. At 200 Hz the module registers cannot be read, so battery must
 * never appear during capture. The number of SOLID bars carries the tier, so
 * the icon still reads with color removed.
 *
 * During capture a drop to "bad" changes this row and nothing else: no dialog,
 * no toast. The operator cannot act on it mid-walk without ruining the test.
 */
const TIERS = {
  good: { color: "var(--success-fg)", bars: 3, label: "链路良好" },
  fair: { color: "var(--warning-fg)", bars: 2, label: "链路波动" },
  bad: { color: "var(--danger-fg)", bars: 1, label: "链路异常" },
};

export function LinkStatus({ side, tier = "good", label, style, ...rest }) {
  const t = TIERS[tier] || TIERS.good;
  return (
    <div style={{ display: "flex", alignItems: "center", gap: "var(--space-3)", height: 40, ...style }} {...rest}>
      {side && <SideBadge side={side} />}
      <LinkBars tier={tier} />
      <span style={{ flex: 1, font: "var(--text-body)", fontWeight: 500, color: t.color }}>{label || t.label}</span>
    </div>
  );
}

function LinkBars({ tier }) {
  const t = TIERS[tier] || TIERS.good;
  const geom = [
    [2, 12, 6],
    [8, 8, 10],
    [14, 4, 14],
  ];
  return (
    <svg width="20" height="20" viewBox="0 0 20 20" aria-hidden="true" style={{ flex: "none" }}>
      {geom.map(([x, y, h], i) =>
        i < t.bars ? (
          <rect key={i} x={x} y={y} width="4" height={h} rx="1" fill={t.color} />
        ) : (
          <rect key={i} x={x + 0.6} y={y + 0.6} width="2.8" height={h - 1.2} rx="1" fill="none" stroke={t.color} strokeWidth="1.2" opacity="0.4" />
        )
      )}
      {tier === "bad" && <path d="M3 4 L17 17" stroke={t.color} strokeWidth="1.8" strokeLinecap="round" />}
    </svg>
  );
}
