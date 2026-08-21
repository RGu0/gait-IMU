import React from "react";

/**
 * Toast — bottom-right, transient success/info ONLY. Errors never use Toast.
 * 4s auto-dismiss, one at a time, aria-live polite, non-focus-stealing.
 */
export function Toast({ tone = "success", children, action, onClose, style, ...rest }) {
  const fg = tone === "info" ? "var(--info-fg)" : "var(--success-fg)";
  const icon =
    tone === "info"
      ? <><circle cx="12" cy="12" r="9" /><path d="M12 11v5M12 8h.01" /></>
      : <><circle cx="12" cy="12" r="9" /><path d="m9 12 2 2 4-4" /></>;
  return (
    <div
      role="status"
      aria-live="polite"
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: "var(--space-2)",
        maxWidth: 420,
        padding: "var(--space-3) var(--space-4)",
        background: "var(--bg-surface)",
        border: "1px solid var(--border-default)",
        borderRadius: "var(--radius-control)",
        boxShadow: "var(--shadow-dialog)",
        font: "var(--text-body)",
        color: "var(--text-primary)",
        ...style,
      }}
      {...rest}
    >
      <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke={fg} strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" style={{ flex: "none" }}>
        {icon}
      </svg>
      <span style={{ flex: 1 }}>{children}</span>
      {action}
      {onClose && (
        <button type="button" onClick={onClose} aria-label="关闭" style={{ background: "none", border: "none", cursor: "pointer", color: "var(--text-secondary)", padding: 2, display: "inline-flex" }}>
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" aria-hidden="true"><path d="M18 6 6 18M6 6l12 12" /></svg>
        </button>
      )}
    </div>
  );
}
