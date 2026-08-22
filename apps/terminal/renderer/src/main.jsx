import React from "react";
import { createRoot } from "react-dom/client";
import "@gait/design-system/styles.css";
import "./app.css";
import { TerminalApp } from "./TerminalApp.jsx";

const noopAdapter = { snapshot: () => ({}) };

createRoot(document.getElementById("root")).render(
  <React.StrictMode>
    <TerminalApp adapter={noopAdapter} />
  </React.StrictMode>,
);
