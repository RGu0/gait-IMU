import React from "react";

export interface CountdownFocusProps {
  /** Small tracked eyebrow above the number. */
  eyebrow?: string;
  /** Whole seconds remaining. Plain seconds, not mm:ss — a moving reader scans a bare number fastest. */
  seconds: React.ReactNode;
  caption?: string;
  /** One sentence, read from ~3 m. */
  instruction?: string;
  /** 1280×720 fallback: 160px → 120px. */
  compact?: boolean;
  tone?: "brand" | "success";
  style?: React.CSSProperties;
}

/**
 * CountdownFocus — the subject-facing half of the capture screen, sized for ~3 m.
 * @startingPoint section="Gait" subtitle="Subject-facing countdown, read while walking" viewport="900x520"
 */
export function CountdownFocus(props: CountdownFocusProps): JSX.Element;
