import React from "react";

/**
 * Steady Health primary control. One high-emphasis (primary) button per screen.
 * Copy is always a verb phrase. Solid & flat — no gradients.
 */
export function Button({
  children,
  variant = "primary",   // "primary" | "secondary" | "danger" | "ghost"
  size = "md",           // "lg" (first-screen CTA, 64px) | "md" | "sm" (48px min)
  loading = false,
  loadingText,
  disabled = false,
  fullWidth = false,
  iconLeft = null,
  onClick,
  type = "button",
  style,
  ...rest
}) {
  const heights = { lg: 64, md: 56, sm: 48 };
  const height = heights[size] ?? 56;
  const isDisabled = disabled || loading;

  const base = {
    display: "inline-flex",
    alignItems: "center",
    justifyContent: "center",
    gap: "var(--space-2)",
    height,
    minHeight: "var(--size-touch-min)",
    padding: "0 var(--space-6)",
    borderRadius: "var(--radius-control)",
    font: "var(--text-body-lg)",
    fontWeight: 600,
    lineHeight: 1,
    cursor: isDisabled ? "not-allowed" : "pointer",
    width: fullWidth ? "100%" : undefined,
    border: "1px solid transparent",
    transition: "background var(--motion-fast), border-color var(--motion-fast), color var(--motion-fast)",
    outlineOffset: "var(--focus-offset)",
    whiteSpace: "nowrap",
    ...style,
  };

  const variants = {
    primary: {
      background: isDisabled && !loading ? "var(--bg-sunken)" : "var(--brand-primary)",
      color: isDisabled && !loading ? "var(--text-disabled)" : "var(--text-on-brand)",
      borderColor: "transparent",
    },
    secondary: {
      background: "var(--bg-surface)",
      color: isDisabled ? "var(--text-disabled)" : "var(--text-primary)",
      borderColor: "var(--border-strong)",
    },
    danger: {
      background: "var(--bg-surface)",
      color: "var(--danger-fg)",
      borderColor: "var(--danger-border)",
    },
    ghost: {
      background: "transparent",
      color: "var(--brand-primary)",
      borderColor: "transparent",
      padding: "0 var(--space-2)",
    },
  };

  const hoverBg = {
    primary: "var(--brand-primary-hover)",
    secondary: "var(--bg-sunken)",
    danger: "var(--danger-bg)",
    ghost: "var(--brand-alpha-1)",
  };

  const [hover, setHover] = React.useState(false);
  const composed = { ...base, ...variants[variant] };
  if (hover && !isDisabled) {
    if (variant === "primary") composed.background = hoverBg.primary;
    else composed.background = hoverBg[variant];
  }
  if (loading) composed.opacity = 1; // stays branded; spinner conveys busy

  return (
    <button
      type={type}
      disabled={isDisabled}
      onClick={onClick}
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
      style={composed}
      aria-busy={loading || undefined}
      {...rest}
    >
      {loading ? <Spinner /> : iconLeft}
      <span>{loading && loadingText ? loadingText : children}</span>
    </button>
  );
}

function Spinner() {
  return (
    <span
      style={{
        width: "var(--btn-spinner-size)",
        height: "var(--btn-spinner-size)",
        borderRadius: "999px",
        border: "2px solid currentColor",
        borderTopColor: "transparent",
        opacity: 0.9,
        display: "inline-block",
        animation: "steady-spin 700ms linear infinite",
      }}
    />
  );
}

if (typeof document !== "undefined" && !document.getElementById("steady-spin-kf")) {
  const s = document.createElement("style");
  s.id = "steady-spin-kf";
  s.textContent = "@keyframes steady-spin{to{transform:rotate(360deg)}}";
  document.head.appendChild(s);
}
