import React from "react";

/**
 * Multi-select tag chips (e.g. medical history: 高血压 / 糖尿病 / 既往下肢损伤).
 * Selected = brand subtle fill + border + check. Solid & flat.
 */
export function ChipGroup({ options, value = [], onChange, style, ...rest }) {
  const set = new Set(value);
  const toggle = (v) => {
    const next = new Set(set);
    next.has(v) ? next.delete(v) : next.add(v);
    onChange && onChange([...next]);
  };
  return (
    <div style={{ display: "flex", flexWrap: "wrap", gap: "var(--space-2)", ...style }} role="group" {...rest}>
      {options.map((opt) => {
        const val = typeof opt === "string" ? opt : opt.value;
        const label = typeof opt === "string" ? opt : opt.label;
        const selected = set.has(val);
        return <Chip key={val} label={label} selected={selected} onClick={() => toggle(val)} />;
      })}
    </div>
  );
}

function Chip({ label, selected, onClick }) {
  const [hover, setHover] = React.useState(false);
  return (
    <button
      type="button"
      aria-pressed={selected}
      onClick={onClick}
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: "var(--space-2)",
        minHeight: "var(--size-touch-min)",
        padding: "0 var(--space-4)",
        borderRadius: "var(--radius-pill)",
        font: "var(--text-body)",
        cursor: "pointer",
        transition: "background var(--motion-fast), border-color var(--motion-fast)",
        background: selected ? "var(--brand-primary-subtle)" : hover ? "var(--bg-sunken)" : "var(--bg-surface)",
        color: selected ? "var(--brand-primary)" : "var(--text-primary)",
        border: `1px solid ${selected ? "var(--brand-primary-border)" : "var(--border-strong)"}`,
        outlineOffset: "var(--focus-offset)",
      }}
    >
      {selected && (
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.25" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <path d="M20 6 9 17l-5-5" />
        </svg>
      )}
      {label}
    </button>
  );
}
