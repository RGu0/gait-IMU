import React from "react";

/**
 * CountdownFocus — the subject-facing half of the timed-walk screen.
 *
 * The subject reads this while walking a ~4 m shuttle, 0–3 m from the terminal;
 * the operator reads their own numbers at arm's length in a separate column.
 * The two viewing distances differ by an order of magnitude, so one type scale
 * cannot serve both — this component owns the far half and holds four elements
 * and nothing else. Never put step counts, metrics or upload state here.
 *
 * `compact` is the 1280×720 fallback (160px → 120px).
 */
export function CountdownFocus({ eyebrow = "正在测试", seconds, caption = "剩余时间（秒）", instruction, compact = false, tone = "brand", style, ...rest }) {
  const color = tone === "success" ? "var(--success-fg)" : "var(--brand-primary)";
  return (
    <div style={{ display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", textAlign: "center", padding: "var(--space-8)", ...style }} {...rest}>
      <div style={{ font: "600 14px/1 var(--font-ui)", letterSpacing: 2, color }}>{eyebrow}</div>
      <div
        style={{
          font: (compact ? "700 120px/1 " : "700 160px/1 ") + "var(--font-num)",
          fontVariantNumeric: "tabular-nums",
          color: "var(--brand-primary)",
          margin: "26px 0 4px",
        }}
      >
        {seconds}
      </div>
      <div style={{ font: "400 20px/28px var(--font-ui)", color: "var(--text-secondary)" }}>{caption}</div>
      {instruction && (
        <p style={{ margin: "52px 0 0", font: "400 32px/1.5 var(--font-ui)", color: "var(--text-primary)", maxWidth: 620, textWrap: "pretty" }}>{instruction}</p>
      )}
    </div>
  );
}
