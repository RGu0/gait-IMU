import React from "react";

export type BatteryTier = "full" | "mid" | "low";

export interface BatteryPairProps {
  /** Left module charge, 0–100. */
  left: number;
  /** Right module charge, 0–100. */
  right: number;
  style?: React.CSSProperties;
}

/** ≥60 → "full" · 30–60 → "mid" · <30 → "low" (blocks a new session). */
export function batteryTier(percent: number): BatteryTier;

/**
 * BatteryPair — both modules' charge; pre-capture only, removed during capture.
 * @startingPoint section="Gait" subtitle="Charge tiers — pre-capture only" viewport="700x150"
 */
export function BatteryPair(props: BatteryPairProps): JSX.Element;
