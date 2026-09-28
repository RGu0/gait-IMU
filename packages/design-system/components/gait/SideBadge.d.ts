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
 * SideBadge — left/right identity as the physical module: character + module color
 * (left = blue shell, right = orange shell), same rounded rectangle on both sides.
 * @startingPoint section="Gait" subtitle="Left/right identity — character + module color" viewport="700x150"
 */
export function SideBadge(props: SideBadgeProps): JSX.Element;
