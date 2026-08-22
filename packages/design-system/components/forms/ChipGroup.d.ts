export type ChipOption = string | { value: string; label: string };

export interface ChipGroupProps {
  /** Chip options; strings or {value,label}. */
  options: ChipOption[];
  /** Selected values (controlled). */
  value?: string[];
  onChange?: (next: string[]) => void;
  style?: React.CSSProperties;
}

/**
 * Multi-select tag chips. Selected = brand subtle fill + border + check.
 * @startingPoint section="Forms" subtitle="Multi-select medical-history chips" viewport="700x140"
 */
export function ChipGroup(props: ChipGroupProps): JSX.Element;
