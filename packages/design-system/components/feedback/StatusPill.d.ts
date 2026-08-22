import React from "react";

export type StatusTone = "success" | "warning" | "danger" | "info" | "neutral";
export type StatusIcon = "dot" | "check" | "x" | "warning" | "spinner";

export interface StatusPillProps {
  tone?: StatusTone;
  children: React.ReactNode;
  /** Leading mark. "dot" is the default; "spinner" pairs with spin. */
  icon?: StatusIcon;
  /** Slowly rotate the icon (use with icon="spinner", info tone). */
  spin?: boolean;
  style?: React.CSSProperties;
}

/**
 * StatusPill — icon + text + color together; text is never omitted.
 * @startingPoint section="Feedback" subtitle="Ready / processing / warning / failed pills" viewport="700x120"
 */
export function StatusPill(props: StatusPillProps): JSX.Element;
