import React from "react";

/**
 * StepBar — thin linear wizard stepper. Current = brand; done = gray-green
 * check; not-reached = gray. Shown atop the wizard; no global nav during flow.
 */
export function StepBar({ steps, current = 0, style, ...rest }) {
  return (
    <ol style={{ display: "flex", alignItems: "center", gap: "var(--space-2)", listStyle: "none", margin: 0, padding: 0, ...style }} {...rest}>
      {steps.map((label, i) => {
        const done = i < current;
        const active = i === current;
        return (
          <React.Fragment key={i}>
            <li style={{ display: "flex", alignItems: "center", gap: "var(--space-2)" }} aria-current={active ? "step" : undefined}>
              <span style={{
                width: 24, height: 24, borderRadius: "999px", flex: "none",
                display: "inline-flex", alignItems: "center", justifyContent: "center",
                font: "var(--text-secondary-size)", fontWeight: 600,
                background: done ? "var(--success-bg)" : active ? "var(--brand-primary)" : "var(--bg-sunken)",
                color: done ? "var(--success-fg)" : active ? "var(--text-on-brand)" : "var(--text-disabled)",
                border: done ? "1px solid var(--success-border)" : "none",
              }}>
                {done
                  ? <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="M20 6 9 17l-5-5" /></svg>
                  : i + 1}
              </span>
              <span style={{ font: "var(--text-secondary-size)", fontWeight: active ? 600 : 400, color: active ? "var(--text-primary)" : done ? "var(--text-secondary)" : "var(--text-disabled)", whiteSpace: "nowrap" }}>
                {label}
              </span>
            </li>
            {i < steps.length - 1 && (
              <span aria-hidden="true" style={{ flex: 1, minWidth: 16, height: 1, background: i < current ? "var(--success-border)" : "var(--border-default)" }} />
            )}
          </React.Fragment>
        );
      })}
    </ol>
  );
}
