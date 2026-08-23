import React from "react";
import { createRoot } from "react-dom/client";
import "@gait/design-system/styles.css";
// The report's own stylesheet, loaded alongside the app's. It is the same file
// the printToPDF export will load (R-4) — the preview must not be styled by
// anything the printed page will not have.
import "@gait/report-template/report.css";
import "./app.css";
import { TerminalApp } from "./TerminalApp.jsx";
import { mockTerminalAdapter } from "./mockTerminalAdapter.js";

createRoot(document.getElementById("root")).render(
  <React.StrictMode>
    <TerminalApp adapter={mockTerminalAdapter} />
  </React.StrictMode>,
);
