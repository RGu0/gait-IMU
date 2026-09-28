import React from "react";
import type { Side } from "./SideBadge";

export interface ModuleFigureProps {
  side: Side;
  /** Rendered width in px; height follows the module's 100:142 shell. Default 72. */
  width?: number;
  /**
   * Show the SideBadge + color-name caption (default true). Turn it off only when
   * the same row already carries a SideBadge for this side — the figure alone
   * tells the feet apart by color only.
   */
  caption?: boolean;
  style?: React.CSSProperties;
}

/**
 * ModuleFigure — the TF9BT50 module as it looks on the table (shell color, inset
 * outline, model name, center ring, indicator light), for wear and pairing steps.
 * @startingPoint section="Gait" subtitle="The physical module — blue left, orange right" viewport="700x260"
 */
export function ModuleFigure(props: ModuleFigureProps): JSX.Element;
