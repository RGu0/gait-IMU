/**
 * RAY-542 `side-identity-screens` — the module is pictured where the operator
 * has it in hand, and P-07 shows each side on one row.
 *
 * Before RAY-542 the operator read 「橙色模块戴右脚」 next to a cyan circle; no
 * screen showed any orange. The source-level guards (no old colours, the P-07
 * row CSS) are in test/sideIdentitySources.test.js — reading files is not
 * allowed under src/.
 */
import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { BindingWizardScreen } from "./BindingWizardScreen.jsx";
import { WearConfirmScreen } from "./WearConfirmScreen.jsx";
import { WearGuideScreen } from "./WearGuideScreen.jsx";

const figures = () => screen.getAllByRole("img").map((node) => node.getAttribute("aria-label"));

describe("the module is pictured where the operator has it in hand", () => {
  it("P-06 wear guide shows both modules", () => {
    render(<WearGuideScreen onContinue={vi.fn()} />);
    expect(figures()).toEqual(expect.arrayContaining(["蓝色模块（左脚）", "橙色模块（右脚）"]));
  });

  it("P-07 left/right confirmation shows both modules", () => {
    render(<WearConfirmScreen onDone={vi.fn()} onBack={vi.fn()} />);
    expect(figures()).toEqual(expect.arrayContaining(["蓝色模块（左脚）", "橙色模块（右脚）"]));
  });

  it("the pairing wizard shows the one module this step switches on", () => {
    render(<BindingWizardScreen bindFoot={vi.fn(() => new Promise(() => {}))} onDone={vi.fn()} onCancel={vi.fn()} />);
    expect(figures()).toContain("蓝色模块（左脚）");
    expect(figures()).not.toContain("橙色模块（右脚）");
  });
});

describe("P-07 rows are their own one-line list, not P-06's two-cell grid", () => {
  it("renders one .side-rows item per side and no .wear-points grid", () => {
    const { container } = render(<WearConfirmScreen onDone={vi.fn()} onBack={vi.fn()} />);
    expect(container.querySelectorAll(".side-rows li")).toHaveLength(2);
    expect(container.querySelector(".wear-points")).toBeNull();
  });
});
