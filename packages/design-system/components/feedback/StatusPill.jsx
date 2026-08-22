import React from "react";

const TONES = {
  success: { fg: "var(--success-fg)", bg: "var(--success-bg)", border: "var(--success-border)" },
  warning: { fg: "var(--warning-fg)", bg: "var(--warning-bg)", border: "var(--warning-border)" },
  danger: { fg: "var(--danger-fg)", bg: "var(--danger-bg)", border: "var(--danger-border)" },
  info: { fg: "var(--info-fg)", bg: "var(--info-bg)", border: "var(--info-border)" },
  neutral: { fg: "var(--text-secondary)", bg: "var(--bg-sunken)", border: "var(--border-default)" },
};

/**
 * StatusPill — dot/icon + TEXT, pill shape, light bg. Text is NEVER omitted;
 * status = icon + text + color together. info can slowly spin its icon.
 */
export function StatusPill({ tone = "neutral", children, icon = "dot", spin = false, style, ...rest }) {
  const t = TONES[tone] || TONES.neutral;
  return (
    <span
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: "var(--space-2)",
        height: 28,
        padding: "0 var(--space-3)",
        borderRadius: "var(--radius-pill)",
        font: "var(--text-secondary-size)",
        fontWeight: 500,
        color: t.fg,
        background: t.bg,
        border: `1px solid ${t.border}`,
        whiteSpace: "nowrap",
        ...style,
      }}
      {...rest}
    >
      <PillMark icon={icon} tone={tone} spin={spin} />
      {children}
    </span>
  );
}

function PillMark({ icon, spin }) {
  if (icon === "dot") {
    return <span style={{ width: 8, height: 8, borderRadius: "999px", background: "currentColor", flex: "none" }} />;
  }
  const paths = {
    check: <path d="M20 6 9 17l-5-5" />,
    x: <path d="M18 6 6 18M6 6l12 12" />,
    warning: <><path d="M12 9v4" /><path d="M12 17h.01" /><path d="M10.3 3.9 2 18a2 2 0 0 0 1.7 3h16.6a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z" /></>,
    spinner: <><path d="M21 12a9 9 0 1 1-6.2-8.6" /></>,
  };
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.25" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" style={spin ? { animation: "steady-spin 900ms linear infinite" } : undefined}>
      {paths[icon] || paths.check}
    </svg>
  );
}

if (typeof document !== "undefined" && !document.getElementById("steady-spin-kf")) {
  const s = document.createElement("style");
  s.id = "steady-spin-kf";
  s.textContent = "@keyframes steady-spin{to{transform:rotate(360deg)}}";
  document.head.appendChild(s);
}
