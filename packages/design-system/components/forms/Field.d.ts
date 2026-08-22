import React from "react";

export interface FieldProps {
  /** Always-visible label above the input. Required — never use placeholder as label. */
  label: string;
  value?: string;
  onChange?: (e: React.ChangeEvent<HTMLInputElement>) => void;
  /** Appends a muted "(选填)" marker to the label. */
  optional?: boolean;
  /** Unit shown inside the field on the right, e.g. "cm" / "kg". */
  unit?: string;
  placeholder?: string;
  /** Error text (red, role=alert). Tells the user how to fix it. */
  error?: string;
  /** Neutral helper text below the field (hidden when error is present). */
  hint?: string;
  type?: string;
  disabled?: boolean;
  id?: string;
  style?: React.CSSProperties;
}

/**
 * Steady Health labelled text input. 48px tall, validate on blur/submit.
 * @startingPoint section="Forms" subtitle="Label-above input with unit + error" viewport="700x160"
 */
export function Field(props: FieldProps): JSX.Element;
