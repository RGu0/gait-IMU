import React from "react";

export type CheckStatus = "pending" | "running" | "pass" | "fail";

export interface ChecklistItemProps {
  status?: CheckStatus;
  /** Item name (body size). */
  label: string;
  /** Right-side hint; on fail, an actionable fix (not a tech detail). */
  hint?: string;
  style?: React.CSSProperties;
}

/**
 * ChecklistItem — a pre-check row: status icon + name + actionable hint.
 * @startingPoint section="Flow" subtitle="Device pre-check row states" viewport="700x220"
 */
export function ChecklistItem(props: ChecklistItemProps): JSX.Element;
