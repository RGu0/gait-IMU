import React from "react";
import { Side } from "./SideBadge";

export type LinkTier = "good" | "fair" | "bad";

export interface LinkStatusProps {
  /** Renders a leading SideBadge. Omit for a single unattributed row. */
  side?: Side;
  /** Trailing-5s loss rate: good < 8% · fair 8–16% · bad ≥ 16% or dropped. */
  tier?: LinkTier;
  /** Overrides the default Chinese label. */
  label?: string;
  style?: React.CSSProperties;
}

/**
 * LinkStatus — BLE link health in three tiers, carried by solid-bar count.
 * @startingPoint section="Gait" subtitle="Trailing-window loss tiers — never battery during capture" viewport="700x180"
 */
export function LinkStatus(props: LinkStatusProps): JSX.Element;
