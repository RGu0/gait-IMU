import React from "react";

export type Side = "left" | "right";

export interface SideBadgeProps {
  side: Side;
  /**
   * 22 for compact rows, chips and legends; 24 for card titles. Nothing
   * smaller: the label sits at the 14px screen floor and has to fit.
   */
  size?: 22 | 24;
  style?: React.CSSProperties;
}

/**
 * SideBadge — left/right identity in three channels at once (text + shape + color).
 * @startingPoint section="Gait" subtitle="Left/right identity — text + shape + color" viewport="700x150"
 */
export function SideBadge(props: SideBadgeProps): JSX.Element;
