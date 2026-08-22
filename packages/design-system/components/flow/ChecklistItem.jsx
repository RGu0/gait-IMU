import React from "react";

/**
 * ChecklistItem — one pre-check row: 24px status icon + item name + right-side
 * hint. Failure shows an ACTIONABLE fix ("请检查设备连接线"), never tech detail.
 */
export function ChecklistItem({ status = "pending", label, hint, style, ...rest }) {
  return (
    <div style={{ display: "flex", alignItems: "center", gap: "var(--space-3)", minHeight: 56, padding: "var(--space-2) 0", borderBottom: "1px solid var(--border-default)", ...style }} {...rest}>
      <StatusIcon status={status} />
      <span style={{ flex: 1, font: "var(--text-body)", color: "var(--text-primary)" }}>{label}</span>
      {hint && (
        <span style={{ font: "var(--text-secondary-size)", color: status === "fail" ? "var(--danger-fg)" : "var(--text-secondary)", textAlign: "right" }}>
          {hint}
        </span>
      )}
    </div>
  );
}

function StatusIcon({ status }) {
  const common = { width: 24, height: 24, flex: "none" };
  if (status === "running") {
    return (
      <svg {...common} viewBox="0 0 24 24" fill="none" stroke="var(--brand-primary)" strokeWidth="2" strokeLinecap="round" aria-label="进行中" style={{ animation: "steady-spin 900ms linear infinite" }}>
        <path d="M21 12a9 9 0 1 1-6.2-8.6" />
      </svg>
    );
  }
  if (status === "pass") {
    return (
      <span {...common} style={{ ...common, borderRadius: "999px", background: "var(--success-bg)", display: "inline-flex", alignItems: "center", justifyContent: "center" }} aria-label="通过">
        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="var(--success-fg)" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><path d="M20 6 9 17l-5-5" /></svg>
      </span>
    );
  }
  if (status === "fail") {
    return (
      <span {...common} style={{ ...common, borderRadius: "999px", background: "var(--danger-bg)", display: "inline-flex", alignItems: "center", justifyContent: "center" }} aria-label="失败">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="var(--danger-fg)" strokeWidth="2.5" strokeLinecap="round"><path d="M18 6 6 18M6 6l12 12" /></svg>
      </span>
    );
  }
  return <span {...common} style={{ ...common, borderRadius: "999px", border: "2px solid var(--border-strong)" }} aria-label="待检" />;
}

if (typeof document !== "undefined" && !document.getElementById("steady-spin-kf")) {
  const s = document.createElement("style");
  s.id = "steady-spin-kf";
  s.textContent = "@keyframes steady-spin{to{transform:rotate(360deg)}}";
  document.head.appendChild(s);
}
