import React from "react";

export type BannerTone = "warning" | "info" | "success" | "danger";

export interface BannerProps {
  tone?: BannerTone;
  /** Optional bold lead line in the tone color. */
  title?: string;
  children: React.ReactNode;
  /** Optional trailing action node (e.g. a ghost Button). */
  action?: React.ReactNode;
  /** Show a close button; wire dismissal here. */
  onClose?: () => void;
  style?: React.CSSProperties;
}

/**
 * Banner — full-width, non-blocking, top-of-page notice for persistent states.
 * @startingPoint section="Feedback" subtitle="Persistent degraded-state banner" viewport="700x110"
 */
export function Banner(props: BannerProps): JSX.Element;
