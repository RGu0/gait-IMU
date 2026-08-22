import React from "react";

/**
 * MetricTile — one metric with its quality annotation.
 *
 * v1 deliberately does NOT gate metrics: every metric is computed and every
 * value ships with a grade.
 *   · normal       — plain value
 *   · low          — muted value + warning rail + 「参考」tag + a plain-language note
 *   · uncomputable — 「本次不适用」 + the reason, in plain language
 *
 * Never render a blank, a 0, an "N/A" or a dash for a missing metric: each of
 * those reads as a measurement rather than as an absence.
 */
export function MetricTile({ title, value, unit, grade = "normal", sub, note, style, ...rest }) {
  const low = grade === "low";
  const none = grade === "uncomputable";
  return (
    <div
      style={{
        background: "var(--bg-surface)",
        border: "1px solid var(--border-default)",
        borderRadius: "var(--radius-card)",
        boxShadow: "var(--shadow-card)",
        borderLeft: low ? "3px solid var(--warning-border)" : none ? "3px solid var(--border-strong)" : undefined,
        padding: "var(--space-4) 18px",
        display: "flex",
        flexDirection: "column",
        justifyContent: "center",
        ...style,
      }}
      {...rest}
    >
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: "var(--space-1)" }}>
        <span style={{ font: "var(--text-secondary-size)", color: "var(--text-secondary)" }}>{title}</span>
        {low && (
          <span style={{ display: "inline-flex", alignItems: "center", height: 24, padding: "0 9px", borderRadius: "var(--radius-pill)", font: "500 14px/1 var(--font-ui)", color: "var(--warning-fg)", background: "var(--bg-surface)", border: "1px solid var(--warning-border)" }}>
            参考
          </span>
        )}
      </div>

      {none ? (
        <div style={{ font: "var(--text-body)", color: "var(--text-disabled)", padding: "6px 0 4px" }}>本次不适用</div>
      ) : (
        <div>
          <span style={{ font: "600 30px/1.1 var(--font-num)", fontVariantNumeric: "tabular-nums", color: low ? "var(--text-secondary)" : "var(--text-primary)" }}>{value}</span>
          {unit && <span style={{ font: "var(--text-body)", color: low ? "var(--text-disabled)" : "var(--text-secondary)", marginLeft: 6 }}>{unit}</span>}
        </div>
      )}

      {sub && <div style={{ font: "var(--text-secondary-size)", color: "var(--text-secondary)", marginTop: 2 }}>{sub}</div>}
      {note && <div style={{ font: "var(--text-secondary-size)", color: "var(--text-secondary)", marginTop: 6 }}>{note}</div>}
    </div>
  );
}
