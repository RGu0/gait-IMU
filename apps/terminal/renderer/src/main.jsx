import React from "react";
import { createRoot } from "react-dom/client";
import "@gait/design-system/styles.css";
import "./app.css";
import { TerminalApp } from "./TerminalApp.jsx";
import { mockTerminalAdapter } from "./mockTerminalAdapter.js";

createRoot(document.getElementById("root")).render(
  <React.StrictMode>
    <TerminalApp adapter={mockTerminalAdapter} />
  </React.StrictMode>,
);
