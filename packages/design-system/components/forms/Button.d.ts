import React from "react";

export type ButtonVariant = "primary" | "secondary" | "danger" | "ghost";
export type ButtonSize = "lg" | "md" | "sm";

export interface ButtonProps {
  children: React.ReactNode;
  /** primary = the single high-emphasis action per screen. */
  variant?: ButtonVariant;
  /** lg = first-screen CTA (64px), md = 56px, sm = 48px min. */
  size?: ButtonSize;
  /** Shows a spinner + locks width; keeps brand color (not gray). */
  loading?: boolean;
  /** Present-tense text swapped in while loading, e.g. "正在提交…". */
  loadingText?: string;
  disabled?: boolean;
  fullWidth?: boolean;
  iconLeft?: React.ReactNode;
  onClick?: (e: React.MouseEvent<HTMLButtonElement>) => void;
  type?: "button" | "submit" | "reset";
  style?: React.CSSProperties;
}

/**
 * Steady Health button. One primary per screen; copy is a verb phrase.
 * @startingPoint section="Forms" subtitle="Primary / secondary / danger / ghost, with loading" viewport="700x220"
 */
export function Button(props: ButtonProps): JSX.Element;
