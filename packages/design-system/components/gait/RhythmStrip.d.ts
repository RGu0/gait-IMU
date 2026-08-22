import React from "react";

export interface RhythmStripProps {
  /** X positions of left footfalls, in viewBox units, at their real timestamps. */
  left?: number[];
  /** X positions of right footfalls. */
  right?: number[];
  width?: number;
  height?: number;
  style?: React.CSSProperties;
}

/**
 * RhythmStrip — recent footfalls at their real timestamps; up = left, down = right.
 * @startingPoint section="Gait" subtitle="Operator-side cadence view — never a metronome" viewport="700x160"
 */
export function RhythmStrip(props: RhythmStripProps): JSX.Element;
