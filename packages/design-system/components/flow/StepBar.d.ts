import React from "react";

export interface StepBarProps {
  /** Ordered step labels. */
  steps: string[];
  /** Zero-based index of the current (active) step. */
  current?: number;
  style?: React.CSSProperties;
}

/**
 * StepBar — thin linear wizard stepper (current brand / done check / upcoming gray).
 * @startingPoint section="Flow" subtitle="Linear wizard step indicator" viewport="700x80"
 */
export function StepBar(props: StepBarProps): JSX.Element;
