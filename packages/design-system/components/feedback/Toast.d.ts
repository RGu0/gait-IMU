import React from "react";

export type ToastTone = "success" | "info";

export interface ToastProps {
  /** ONLY success/info. Errors must never be a Toast. */
  tone?: ToastTone;
  children: React.ReactNode;
  /** At most one text action (e.g. "查看"). */
  action?: React.ReactNode;
  onClose?: () => void;
  style?: React.CSSProperties;
}

/**
 * Toast — bottom-right transient success/info, 4s auto-dismiss, one at a time.
 * @startingPoint section="Feedback" subtitle="Transient success / info toast" viewport="700x110"
 */
export function Toast(props: ToastProps): JSX.Element;
