import React from "react";
import { Button } from "../forms/Button.jsx";

/**
 * Dialog — the ONLY blocking surface, for must-decide moments (stop scan,
 * profile conflict, network gate). ≤2 buttons. Default focus on the SAFE
 * action (cancel). Danger confirm is the one place a filled-red button lives.
 */
export function Dialog({
  open = true,
  title,
  children,
  confirmLabel = "确定",
  cancelLabel = "取消",
  onConfirm,
  onCancel,
  danger = false,
  style,
}) {
  const cancelRef = React.useRef(null);
  React.useEffect(() => {
    if (!open) return;
    const onKey = (e) => { if (e.key === "Escape") onCancel && onCancel(); };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open, onCancel]);

  if (!open) return null;
  return (
    <div style={{ position: "fixed", inset: 0, background: "var(--overlay)", display: "flex", alignItems: "center", justifyContent: "center", zIndex: 1000, padding: "var(--space-6)" }} onMouseDown={(e) => { if (e.target === e.currentTarget) onCancel && onCancel(); }}>
      <div role="alertdialog" aria-modal="true" aria-label={title}
        style={{
          width: "100%",
          maxWidth: "var(--dialog-max-width)",
          background: "var(--bg-surface)",
          borderRadius: "var(--radius-card)",
          boxShadow: "var(--shadow-dialog)",
          padding: "var(--space-6)",
          ...style,
        }}>
        {title && <h3 style={{ margin: 0, font: "var(--text-h3)", color: "var(--text-primary)" }}>{title}</h3>}
        <div style={{ margin: "var(--space-3) 0 var(--space-6)", font: "var(--text-body)", color: "var(--text-secondary)" }}>{children}</div>
        <div style={{ display: "flex", justifyContent: "flex-end", gap: "var(--space-3)" }}>
          <Button variant="secondary" size="sm" autoFocus onClick={onCancel}>{cancelLabel}</Button>
          {danger ? (
            <FilledDanger onClick={onConfirm}>{confirmLabel}</FilledDanger>
          ) : (
            <Button variant="primary" size="sm" onClick={onConfirm}>{confirmLabel}</Button>
          )}
        </div>
      </div>
    </div>
  );
}

/* The single place in the whole system where a filled-red button is allowed. */
function FilledDanger({ children, onClick }) {
  const [hover, setHover] = React.useState(false);
  return (
    <button type="button" onClick={onClick} onMouseEnter={() => setHover(true)} onMouseLeave={() => setHover(false)}
      style={{
        display: "inline-flex", alignItems: "center", justifyContent: "center",
        height: 48, minHeight: "var(--size-touch-min)", padding: "0 var(--space-6)",
        borderRadius: "var(--radius-control)", font: "var(--text-body-lg)", fontWeight: 600, lineHeight: 1,
        color: "var(--text-on-brand)", background: hover ? "#A82F2F" : "var(--danger-fg)",
        border: "1px solid transparent", cursor: "pointer", transition: "background var(--motion-fast)",
      }}>
      {children}
    </button>
  );
}
