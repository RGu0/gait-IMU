import React from "react";

/**
 * Steady Health text field. Label is ALWAYS visible above the input
 * (never placeholder-as-label). Optional fields tagged "(选填)".
 * Unit (cm/kg) sits inside on the right. Validate on blur/submit.
 */
export function Field({
  label,
  value,
  onChange,
  optional = false,
  unit,
  placeholder,
  error,
  hint,
  type = "text",
  disabled = false,
  id,
  style,
  ...rest
}) {
  const inputId = id || React.useId();
  const describedBy = error ? `${inputId}-err` : hint ? `${inputId}-hint` : undefined;
  const [focus, setFocus] = React.useState(false);

  const borderColor = error
    ? "var(--danger-fg)"
    : focus
    ? "var(--brand-primary)"
    : "var(--border-strong)";

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "var(--space-2)", ...style }}>
      <label htmlFor={inputId} style={{ font: "var(--text-secondary-size)", fontWeight: 500, color: "var(--text-primary)" }}>
        {label}
        {optional && <span style={{ color: "var(--text-disabled)", fontWeight: 400 }}>(选填)</span>}
      </label>
      <div style={{ position: "relative", display: "flex", alignItems: "center" }}>
        <input
          id={inputId}
          type={type}
          value={value}
          onChange={onChange}
          placeholder={placeholder}
          disabled={disabled}
          onFocus={() => setFocus(true)}
          onBlur={() => setFocus(false)}
          aria-invalid={!!error}
          aria-describedby={describedBy}
          style={{
            width: "100%",
            height: "var(--size-input)",
            padding: unit ? "0 44px 0 var(--space-3)" : "0 var(--space-3)",
            font: "var(--text-body)",
            color: disabled ? "var(--text-disabled)" : "var(--text-primary)",
            background: disabled ? "var(--bg-sunken)" : "var(--bg-surface)",
            border: `1px solid ${borderColor}`,
            borderRadius: "var(--radius-control)",
            outline: focus ? "var(--focus-ring)" : "none",
            outlineOffset: "var(--focus-offset)",
            transition: "border-color var(--motion-fast)",
            boxSizing: "border-box",
          }}
          {...rest}
        />
        {unit && (
          <span style={{ position: "absolute", right: "var(--space-3)", font: "var(--text-body)", color: "var(--text-secondary)", pointerEvents: "none" }}>
            {unit}
          </span>
        )}
      </div>
      {error ? (
        <span id={`${inputId}-err`} role="alert" style={{ font: "var(--text-secondary-size)", color: "var(--danger-fg)" }}>
          {error}
        </span>
      ) : hint ? (
        <span id={`${inputId}-hint`} style={{ font: "var(--text-secondary-size)", color: "var(--text-secondary)" }}>
          {hint}
        </span>
      ) : null}
    </div>
  );
}
