import React from "react";

export interface DialogProps {
  open?: boolean;
  /** h3 title stating a fact, e.g. "停止本次检测?" */
  title?: string;
  /** Body: one consequence + one recoverability sentence. */
  children: React.ReactNode;
  confirmLabel?: string;
  cancelLabel?: string;
  onConfirm?: () => void;
  onCancel?: () => void;
  /** Danger confirm — the ONLY filled-red button in the system. */
  danger?: boolean;
  style?: React.CSSProperties;
}

/**
 * Dialog — the only blocking surface; ≤2 buttons, default focus on cancel, Esc cancels.
 * @startingPoint section="Feedback" subtitle="Blocking confirm dialog (danger variant)" viewport="700x420"
 */
export function Dialog(props: DialogProps): JSX.Element;
