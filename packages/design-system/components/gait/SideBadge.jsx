import React from "react";

/**
 * SideBadge — which foot. It looks like the module the operator is holding:
 * the left badge is the sky-blue shell, the right one the orange shell, both the
 * module's rounded rectangle. The side is carried by TWO channels at once — the
 * character (左/右) and the color — so a grayscale print still reads. Wearing
 * the modules on the wrong ankles cannot be compensated for by the algorithm, so
 * redundancy here is not decoration — it is the cheapest place to prevent an
 * error that costs a whole session.
 */
export function SideBadge({ side, size = 22, style, ...rest }) {
  const isLeft = side === "left";
  const ch = isLeft ? "左" : "右";
  const key = isLeft ? "left" : "right";
  return (
    <span
      aria-label={ch}
      style={{
        width: size,
        height: size,
        flex: "none",
        boxSizing: "border-box",
        borderRadius: "6px",
        background: `var(--side-${key})`,
        border: `1px solid var(--side-${key}-edge)`,
        color: `var(--side-${key}-fg)`,
        display: "inline-flex",
        alignItems: "center",
        justifyContent: "center",
        font: "600 14px/1 var(--font-ui)",
        ...style,
      }}
      {...rest}
    >
      {ch}
    </span>
  );
}
