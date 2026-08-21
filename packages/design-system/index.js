// Steady Health design system — barrel export.
// Consumers also need the stylesheet:  import "@gait/design-system/styles.css";

export { Button } from "./components/forms/Button.jsx";
export { Field } from "./components/forms/Field.jsx";
export { ChipGroup } from "./components/forms/ChipGroup.jsx";

export { StatusPill } from "./components/feedback/StatusPill.jsx";
export { Banner } from "./components/feedback/Banner.jsx";
export { Dialog } from "./components/feedback/Dialog.jsx";
export { Toast } from "./components/feedback/Toast.jsx";

export { StepBar } from "./components/flow/StepBar.jsx";
export { ChecklistItem } from "./components/flow/ChecklistItem.jsx";

export { DataTable } from "./components/data/DataTable.jsx";

// Dual-ankle IMU only — see components/gait/*.prompt.md for the rule each enforces.
export { SideBadge } from "./components/gait/SideBadge.jsx";
export { LinkStatus } from "./components/gait/LinkStatus.jsx";
export { BatteryPair, batteryTier } from "./components/gait/BatteryPair.jsx";
export { MetricTile } from "./components/gait/MetricTile.jsx";
export { CountdownFocus } from "./components/gait/CountdownFocus.jsx";
export { RhythmStrip } from "./components/gait/RhythmStrip.jsx";
