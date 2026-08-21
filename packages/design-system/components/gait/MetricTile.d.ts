import React from "react";

export type MetricGrade = "normal" | "low" | "uncomputable";

export interface MetricTileProps {
  title: string;
  /** Omitted when grade is "uncomputable". */
  value?: React.ReactNode;
  unit?: string;
  grade?: MetricGrade;
  /** Provenance line, e.g. "有效步数 126" or "完整链 · 直行段中段步". */
  sub?: string;
  /**
   * Plain-language reason. Required for "low" and "uncomputable" — the grade
   * alone tells the reader nothing they can act on.
   */
  note?: string;
  style?: React.CSSProperties;
}

/**
 * MetricTile — a metric plus its quality annotation; three grades, never blank.
 * @startingPoint section="Gait" subtitle="normal / low / 本次不适用" viewport="700x200"
 */
export function MetricTile(props: MetricTileProps): JSX.Element;
