import React from "react";

const TONES = {
  warning: { fg: "var(--warning-fg)", bg: "var(--warning-bg)", border: "var(--warning-border)" },
  info: { fg: "var(--info-fg)", bg: "var(--info-bg)", border: "var(--info-border)" },
  success: { fg: "var(--success-fg)", bg: "var(--success-bg)", border: "var(--success-border)" },
  danger: { fg: "var(--danger-fg)", bg: "var(--danger-bg)", border: "var(--danger-border)" },
};

const ICONS = {
  warning: <><path d="M12 9v4" /><path d="M12 17h.01" /><path d="M10.3 3.9 2 18a2 2 0 0 0 1.7 3h16.6a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z" /></>,
  info: <><circle cx="12" cy="12" r="9" /><path d="M12 11v5M12 8h.01" /></>,
  success: <><circle cx="12" cy="12" r="9" /><path d="m9 12 2 2 4-4" /></>,
  danger: <><circle cx="12" cy="12" r="9" /><path d="M12 8v4M12 16h.01" /></>,
};

/**
 * Banner — full-width, top-of-page, non-blocking notice. Use for persistent
 * degraded states (e.g. grace-period network loss). Never interrupts a scan.
 */
export function Banner({ tone = "info", title, children, action, onClose, style, ...rest }) {
  const t = TONES[tone] || TONES.info;
  return (
    <div
      role="status"
      style={{
        display: "flex",
        alignItems: "flex-start",
        gap: "var(--space-3)",
        width: "100%",
        padding: "var(--space-3) var(--space-4)",
        background: t.bg,
        borderTop: `1px solid ${t.border}`,
        borderBottom: `1px solid ${t.border}`,
        color: t.fg,
        boxSizing: "border-box",
        ...style,
      }}
      {...rest}
    >
      <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" style={{ flex: "none", marginTop: 2 }}>
        {ICONS[tone]}
      </svg>
      <div style={{ flex: 1, color: "var(--text-primary)" }}>
        {title && <div style={{ font: "var(--text-body)", fontWeight: 600, color: t.fg }}>{title}</div>}
        <div style={{ font: "var(--text-body)" }}>{children}</div>
      </div>
      {action}
      {onClose && (
        <button type="button" onClick={onClose} aria-label="关闭" style={{ flex: "none", background: "none", border: "none", cursor: "pointer", color: t.fg, padding: 4, borderRadius: 6, display: "inline-flex" }}>
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" aria-hidden="true"><path d="M18 6 6 18M6 6l12 12" /></svg>
        </button>
      )}
    </div>
  );
}
