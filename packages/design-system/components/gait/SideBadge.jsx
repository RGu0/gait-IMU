import React from "react";

/**
 * SideBadge — which foot. The side is carried by THREE channels at once: the
 * character (左/右), the shape (left = rounded square, right = circle) and the
 * color. Wearing the modules on the wrong ankles cannot be compensated for by
 * the algorithm, so redundancy here is not decoration — it is the cheapest
 * place to prevent an error that costs a whole session.
 */
export function SideBadge({ side, size = 22, style, ...rest }) {
  const isLeft = side === "left";
  const ch = isLeft ? "左" : "右";
  return (
    <span
      aria-label={ch}
      style={{
        width: size,
        height: size,
        flex: "none",
        borderRadius: isLeft ? "6px" : "999px",
        background: isLeft ? "var(--side-left)" : "var(--side-right)",
        color: "var(--text-on-brand)",
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
