import { render, screen } from "@testing-library/react";
import { TerminalApp } from "./TerminalApp.jsx";

it("opens at the institutional login", () => {
  render(<TerminalApp adapter={{ snapshot: () => ({}) }} />);
  expect(
    screen.getByRole("heading", { name: "步态健康筛查与分析平台" }),
  ).toBeVisible();
});
