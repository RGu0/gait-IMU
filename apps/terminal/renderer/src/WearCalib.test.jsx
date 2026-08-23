import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { CalibrationScreen } from "./CalibrationScreen.jsx";
import { WearGuideScreen } from "./WearGuideScreen.jsx";

describe("P-06 — the wearing diagram is the last human check on left/right", () => {
  const renderGuide = (props = {}) => render(<WearGuideScreen onContinue={vi.fn()} {...props} />);

  it("spells out that a front view mirrors the subject", () => {
    renderGuide();
    // Without this sentence an operator following the picture will mirror the
    // modules every time. RAY-260 removed the automatic safety net behind it.
    expect(screen.getByText("受试者的左侧在图中的右边")).toBeVisible();
    expect(screen.getByText("面向受试者的视角")).toBeVisible();
  });

  it("labels each ankle by the subject's side, not the viewer's", () => {
    renderGuide();
    expect(screen.getAllByText("受试者左踝").length).toBeGreaterThan(0);
    expect(screen.getAllByText("受试者右踝").length).toBeGreaterThan(0);
  });

  it("carries the side as shape and character, not colour alone", () => {
    const { container } = renderGuide();
    const svg = container.querySelector(".ankle-diagram");
    // rounded square for left, circle for right
    expect(svg.querySelector('rect[rx="12"]')).not.toBeNull();
    expect(svg.querySelector("circle")).not.toBeNull();
    expect(svg.textContent).toContain("左");
    expect(svg.textContent).toContain("右");
  });

  it("names the three things that can be got wrong", () => {
    renderGuide();
    for (const point of ["位置", "朝向", "松紧"]) {
      expect(screen.getByText(point)).toBeVisible();
    }
  });

  it("will not proceed until a person confirms the fit", () => {
    const onContinue = vi.fn();
    renderGuide({ onContinue });
    const proceed = screen.getByRole("button", { name: "佩戴完成，开始标定" });
    expect(proceed).toBeDisabled();

    fireEvent.click(screen.getByRole("checkbox"));
    expect(proceed).toBeEnabled();
    fireEvent.click(proceed);
    expect(onContinue).toHaveBeenCalledOnce();
  });

  it("asks about the swap explicitly in the confirmation", () => {
    renderGuide();
    expect(screen.getByText(/确认左右没有戴反/)).toBeVisible();
  });

  it("uses no video and fetches nothing", () => {
    const { container } = renderGuide();
    // PRD P-06: this has to work on a terminal that has been offline all day.
    expect(container.querySelector("video")).toBeNull();
    expect(container.querySelector("img[src^='http']")).toBeNull();
    expect(container.querySelector("iframe")).toBeNull();
  });

  it("shows the same drawing when enlarged, not a second one", () => {
    const { container } = renderGuide();
    fireEvent.click(screen.getByRole("button", { name: "查看佩戴示范图（放大）" }));
    const dialog = screen.getByRole("alertdialog");
    // A second illustration would be a second thing to keep correct.
    expect(dialog.querySelector(".ankle-diagram")).not.toBeNull();
    expect(container.querySelectorAll(".ankle-diagram").length).toBe(2);
  });
});

describe("P-07 — two sub-steps that advance on their own", () => {
  const renderCalib = (props = {}) =>
    render(
      <CalibrationScreen
        runCalibration={vi.fn().mockResolvedValue({ ok: true })}
        onDone={vi.fn()}
        onAbandon={vi.fn()}
        tickMs={5}
        {...props}
      />,
    );

  it("offers no button to advance", () => {
    renderCalib();
    // The operator's hands are on the subject; a "next" they must reach for is
    // a step that happens late or not at all.
    expect(screen.queryByRole("button", { name: /下一步|继续|开始/ })).not.toBeInTheDocument();
  });

  it("moves from standing to walking without being told", async () => {
    renderCalib();
    expect(screen.getByText(/双脚与肩同宽自然站立/)).toBeVisible();
    await waitFor(() => expect(screen.getByText(/向前直线走 10 步/)).toBeVisible());
  });

  it("finishes on its own when calibration passes", async () => {
    const onDone = vi.fn();
    renderCalib({ onDone });
    await waitFor(() => expect(onDone).toHaveBeenCalledOnce(), { timeout: 3000 });
  });
});

describe("P-07 failures speak in actions, not algorithms", () => {
  const failing = (reason) => vi.fn().mockResolvedValue({ ok: false, reason });

  const renderFailing = (reason, props = {}) =>
    render(
      <CalibrationScreen
        runCalibration={failing(reason)}
        onDone={vi.fn()}
        onAbandon={vi.fn()}
        tickMs={5}
        {...props}
      />,
    );

  it.each([
    ["swapped", "两个模块戴反了，请交换左右后重试。"],
    ["loose", "模块有些松动，请绑紧后重试。"],
    ["drift", "这一段没有采到有效数据，请重新走 10 步。"],
  ])("states what to do for %s", async (reason, copy) => {
    renderFailing(reason);
    expect(await screen.findByText(copy, {}, { timeout: 3000 })).toBeVisible();
  });

  it("never uses an algorithm word", async () => {
    const { container } = renderFailing("drift");
    await screen.findByRole("alert", {}, { timeout: 3000 });
    expect(container.textContent).not.toMatch(
      /安装角|收敛|滤波|标定参数|零偏|四元数|协方差|算法/,
    );
  });

  it("offers no exit until the third failure", async () => {
    renderFailing("loose");
    await screen.findByRole("alert", {}, { timeout: 3000 });

    // Offering it earlier invites abandoning a session a re-tightened strap
    // would have saved.
    expect(screen.getByText("第 1 次 / 共 3 次")).toBeVisible();
    expect(screen.queryByRole("button", { name: "退出本次检测" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "重试" })).toBeEnabled();
  });

  it("offers the exit once the third has failed", async () => {
    renderFailing("loose");
    await screen.findByRole("alert", {}, { timeout: 3000 });
    for (const attempt of [1, 2]) {
      fireEvent.click(screen.getByRole("button", { name: "重试" }));
      await screen.findByText(`第 ${attempt + 1} 次 / 共 3 次`, {}, { timeout: 3000 });
    }
    expect(screen.getByRole("button", { name: "退出本次检测" })).toBeEnabled();
    // Retry stays the primary action even then.
    expect(screen.getByRole("button", { name: "重试" })).toBeEnabled();
  });

  it("restarts from the first sub-step on retry", async () => {
    renderFailing("loose");
    await screen.findByRole("alert", {}, { timeout: 3000 });
    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    // Re-running only the walk would calibrate against a stale standing
    // baseline — the two sub-steps are one measurement.
    expect(screen.getByText(/双脚与肩同宽自然站立/)).toBeVisible();
  });
});

describe("both screens tell the operator where they are", () => {
  it.each([
    ["P-06", <WearGuideScreen onContinue={vi.fn()} />],
    [
      "P-07",
      <CalibrationScreen
        runCalibration={vi.fn().mockResolvedValue({ ok: true })}
        onDone={vi.fn()}
        onAbandon={vi.fn()}
        tickMs={5000}
      />,
    ],
  ])("%s carries the full seven-step wizard header", (_name, element) => {
    const { container } = render(element);
    const header = container.querySelector(".wizard-header");
    expect(header).not.toBeNull();
    // The operator uses this to answer "how much longer"; a screen that drops
    // it leaves them guessing mid-procedure.
    for (const label of ["识别", "档案", "授权", "自检", "佩戴", "标定", "测试"]) {
      expect(within(header).getByText(label)).toBeVisible();
    }
  });
});
