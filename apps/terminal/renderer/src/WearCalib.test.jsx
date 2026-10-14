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

  it("carries the side as character and module colour, not colour alone", () => {
    const { container } = renderGuide();
    const svg = container.querySelector(".ankle-diagram");
    expect(svg.textContent).toContain("左");
    expect(svg.textContent).toContain("右");
    // The marks are the modules: same rounded rectangle, shell colours (RAY-542).
    const left = svg.querySelector('rect[fill="var(--side-left)"]');
    const right = svg.querySelector('rect[fill="var(--side-right)"]');
    expect(left).not.toBeNull();
    expect(right).not.toBeNull();
    for (const attr of ["width", "height", "rx"]) expect(left.getAttribute(attr)).toBe(right.getAttribute(attr));
    expect(svg.querySelector("circle")).toBeNull();
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

/**
 * A scripted sidecar: `begin` returns the first status, every `status` poll the
 * next one, and the last one repeats. Each `begin` restarts the script, the way
 * a retry restarts the sidecar's steps.
 */
function scripted(...statuses) {
  let index = 0;
  const calls = [];
  const runCalibration = vi.fn(async (params) => {
    calls.push(params);
    if (params.action === "cancel") return { phase: "cancelled" };
    if (params.action === "begin") index = 0;
    const status = statuses[Math.min(index, statuses.length - 1)];
    index += 1;
    return status;
  });
  return { runCalibration, calls };
}

const TOTALS = { standTotal: 5, walkTotal: 10, settleTotal: 3, attempt: 1 };
const stand = (standSeconds) => ({ phase: "stand", standSeconds, ...TOTALS });
const walk = (walkSteps) => ({ phase: "walk", walkSteps, ...TOTALS });
const settle = (settleSeconds) => ({ phase: "settle", settleSeconds, ...TOTALS });
const PASSED = { phase: "passed", chainSeconds: 31, ...TOTALS };
const failed = (failure) => ({ phase: "failed", failure, ...TOTALS });
const LOOSE = {
  reason: "loose",
  code: "E-WEAR-2001",
  message: "右脚的模块有些松动，请绑紧后重试。",
  action: "绑紧后点「重试」，静立与行走会重新做一遍。",
};

const renderCalib = (runCalibration, props = {}) =>
  render(
    <CalibrationScreen
      runCalibration={runCalibration}
      onDone={vi.fn()}
      onAbandon={vi.fn()}
      pollMs={5}
      {...props}
    />,
  );

describe("P-07 — three steps the sidecar drives", () => {
  it("offers no button to advance", async () => {
    renderCalib(scripted(stand(0)).runCalibration);
    // The operator's hands are on the subject; a "next" they must reach for is
    // a step that happens late or not at all.
    await screen.findByText(/双脚与肩同宽自然站立/);
    expect(screen.queryByRole("button", { name: /下一步|继续|开始/ })).not.toBeInTheDocument();
  });

  it("starts a fresh calibration, then only polls", async () => {
    const { runCalibration, calls } = scripted(stand(0), stand(2), stand(4));
    renderCalib(runCalibration);
    await waitFor(() => expect(calls.length).toBeGreaterThan(2));
    expect(calls[0]).toEqual({ action: "begin", fresh: true });
    expect(calls.slice(1).every((call) => call.action === "status")).toBe(true);
  });

  it("shows what the sidecar reports, not a clock of its own", async () => {
    // 2 s of samples received → 3 s left; the screen does not count down on
    // its own while the data is late.
    renderCalib(scripted(stand(0), stand(2)).runCalibration);
    const focus = await screen.findByText("3");
    expect(focus).toBeVisible();
  });

  it("walks through stand, walk and settle in the sidecar's order", async () => {
    renderCalib(scripted(stand(0), stand(5), walk(4), walk(10), settle(1), settle(3)).runCalibration);
    await screen.findByText(/向前直线走 10 步/);
    await screen.findByText(/原地站定不动/);
    expect(screen.getByText("步骤 3 / 3")).toBeVisible();
  });

  it("finishes on its own when the sidecar says passed", async () => {
    const onDone = vi.fn();
    renderCalib(scripted(stand(0), walk(10), settle(3), PASSED).runCalibration, { onDone });
    await waitFor(() => expect(onDone).toHaveBeenCalledOnce());
  });

  it("tells the sidecar to stop collecting when the operator leaves mid-step", async () => {
    const { runCalibration, calls } = scripted(stand(0), stand(1));
    const { unmount } = renderCalib(runCalibration);
    await waitFor(() => expect(calls.length).toBeGreaterThan(1));
    unmount();
    expect(calls.at(-1)).toEqual({ action: "cancel" });
  });

  it("hands a rejected request to the caller instead of freezing on the countdown", async () => {
    const onError = vi.fn();
    const failure = Object.assign(new Error("检测进行中不能做会话标定"), { name: "ContractViolation" });
    renderCalib(vi.fn().mockRejectedValue(failure), { onError });
    await waitFor(() => expect(onError).toHaveBeenCalledWith(failure));
  });

  it("cancels even when the operator leaves before begin has answered", async () => {
    let answer;
    const calls = [];
    const runCalibration = vi.fn((params) => {
      calls.push(params);
      if (params.action === "begin") return new Promise((resolve) => { answer = resolve; });
      return Promise.resolve({ phase: "cancelled" });
    });
    const { unmount } = renderCalib(runCalibration);
    await waitFor(() => expect(calls).toHaveLength(1));
    unmount();
    expect(calls.at(-1)).toEqual({ action: "cancel" });
    answer(stand(0));
  });

  it("says plainly when the data source cannot calibrate, and lets the operator skip", async () => {
    const onDone = vi.fn();
    renderCalib(
      vi.fn().mockResolvedValue({ phase: "unavailable", message: "「replay」数据源没有标定片段。" }),
      { onDone },
    );
    expect(await screen.findByText(/没有标定片段/)).toBeVisible();
    // Not an alert: nothing went wrong with the modules.
    expect(screen.queryByRole("alert")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "跳过标定，继续检测" }));
    expect(onDone).toHaveBeenCalledOnce();
  });
});

describe("P-07 failures speak in actions, not algorithms", () => {
  const renderFailing = (failure = LOOSE, props = {}) => {
    const rig = scripted(stand(0), failed(failure));
    return { ...rig, ...renderCalib(rig.runCalibration, props) };
  };

  it("shows the sidecar's phenomenon, action and code — it writes none of its own", async () => {
    renderFailing();
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(LOOSE.message);
    expect(alert).toHaveTextContent(LOOSE.action);
    expect(alert).toHaveTextContent("E-WEAR-2001");
  });

  it("has no wrong-way-worn failure: that state is unreachable in v1 (PRD v1.4 D1)", () => {
    // RAY-287 acceptance 4: automatic swap detection was removed (RAY-260), so
    // the screen must not carry copy for it — a reader would think it works.
    const source = CalibrationScreen.toString();
    expect(source).not.toMatch(/戴反|swapped/);
  });

  it("never uses an algorithm word of its own", async () => {
    const { container } = renderFailing();
    await screen.findByRole("alert");
    expect(container.textContent).not.toMatch(
      /安装角|收敛|滤波|标定参数|零偏|四元数|协方差|算法/,
    );
  });

  it("offers no exit until the third failure", async () => {
    renderFailing();
    await screen.findByRole("alert");

    // Offering it earlier invites abandoning a session a re-tightened strap
    // would have saved.
    expect(screen.getByText("第 1 次 / 共 3 次")).toBeVisible();
    expect(screen.queryByRole("button", { name: "退出本次检测" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "重试" })).toBeEnabled();
  });

  it("offers the exit once the third has failed", async () => {
    const onAbandon = vi.fn();
    renderFailing(LOOSE, { onAbandon });
    await screen.findByRole("alert");
    for (const attempt of [1, 2]) {
      fireEvent.click(screen.getByRole("button", { name: "重试" }));
      await screen.findByText(`第 ${attempt + 1} 次 / 共 3 次`);
    }
    // Retry stays the primary action even then.
    expect(screen.getByRole("button", { name: "重试" })).toBeEnabled();
    fireEvent.click(screen.getByRole("button", { name: "退出本次检测" }));
    expect(onAbandon).toHaveBeenCalledOnce();
  });

  it("retries from the first step, and only the first entry is fresh", async () => {
    const { calls } = renderFailing();
    await screen.findByRole("alert");
    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    // Re-running only the walk would calibrate against a stale standing
    // baseline — the steps are one measurement.
    expect(await screen.findByText(/双脚与肩同宽自然站立/)).toBeVisible();
    const begins = calls.filter((call) => call.action === "begin");
    // A retry keeps the chain timer running: retries are the operator's time too.
    expect(begins).toEqual([
      { action: "begin", fresh: true },
      { action: "begin", fresh: false },
    ]);
  });
});

describe("both screens tell the operator where they are", () => {
  it.each([
    ["P-06", <WearGuideScreen onContinue={vi.fn()} />],
    [
      "P-07",
      <CalibrationScreen
        runCalibration={vi.fn().mockResolvedValue({ phase: "stand", standSeconds: 0, standTotal: 5 })}
        onDone={vi.fn()}
        onAbandon={vi.fn()}
        pollMs={5000}
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
