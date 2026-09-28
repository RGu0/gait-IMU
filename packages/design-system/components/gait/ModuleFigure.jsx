import React from "react";
import { SideBadge } from "./SideBadge.jsx";

/**
 * ModuleFigure — the TF9BT50 module as the operator sees it on the table: the
 * shell in its color (left = blue, right = orange), the white inset outline, the
 * model name, the center ring and the green indicator light. Drawn from the
 * hardware so that "put the blue one on the left ankle" can be checked against
 * the picture, not against a color word.
 *
 * The figure alone would tell the feet apart by color only, so a caption carries
 * the second channel: SideBadge (左/右) + the color name. Keep the caption unless
 * the same row already shows a SideBadge for this side.
 */
export function ModuleFigure({ side, width = 72, caption = true, style, ...rest }) {
  const key = side === "left" ? "left" : "right";
  const colorName = key === "left" ? "蓝色模块" : "橙色模块";
  const footName = key === "left" ? "左脚" : "右脚";
  const height = Math.round((width * 142) / 100);
  return (
    <figure
      role="img"
      aria-label={`${colorName}（${footName}）`}
      style={{ margin: 0, display: "inline-flex", flexDirection: "column", alignItems: "center", gap: "var(--space-2)", ...style }}
      {...rest}
    >
      <svg width={width} height={height} viewBox="0 0 100 142" aria-hidden="true" style={{ flex: "none" }}>
        <rect x="1" y="1" width="98" height="140" rx="13" fill={`var(--side-${key})`} stroke={`var(--side-${key}-edge)`} strokeWidth="1" />
        <rect x="6" y="6" width="88" height="130" rx="9" fill="none" stroke="#FFFFFF" strokeOpacity="0.85" strokeWidth="1" />
        <text x="50" y="58" textAnchor="middle" fill="#FFFFFF" style={{ font: "600 13px/1 var(--font-num)" }}>TF9BT50</text>
        <circle cx="50" cy="86" r="14" fill={`var(--side-${key}-edge)`} stroke="#FFFFFF" strokeWidth="1" />
        <circle cx="50" cy="86" r="6" fill="#FFFFFF" />
        <circle cx="87" cy="108" r="4" fill="#7BC043" />
      </svg>
      {caption ? (
        <figcaption style={{ display: "inline-flex", alignItems: "center", gap: "var(--space-2)", font: "var(--text-secondary-size)", color: "var(--text-primary)" }}>
          <SideBadge side={key} />
          {colorName}
        </figcaption>
      ) : null}
    </figure>
  );
}
