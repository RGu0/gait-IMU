import { StrictMode } from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { PreflightScreen } from "./PreflightScreen.jsx";

const SAFETY_LINES = [
  "往返通道已清空，两端转身标志已放置",
  "跌倒高风险受试者已有工作人员在侧陪护",
  "受试者当前状态适合进行 3 分钟步行",
];

const passing = [
  { id: "link-left", label: "左模块连接", status: "pass", hint: "已连接" },
  { id: "battery", label: "左右模块电量", status: "pass", hint: "左 82% · 右 76%" },
  { id: "arrival", label: "链路到达率", status: "pass", hint: "观察 5 秒通过" },
];

const blocked = [
  { id: "link-left", label: "左模块连接", status: "pass", hint: "已连接" },
  {
    id: "battery",
    label: "左右模块电量",
    status: "fail",
    hint: "左模块电量不足 22%（E-BLE-1005）。请更换或充电后重新检查。",
  },
  { id: "arrival", label: "链路到达率", status: "pass", hint: "观察 5 秒通过" },
];

function tickAllSafety() {
  for (const box of screen.getAllByRole("checkbox")) {
    fireEvent.click(box);
  }
}

describe("C-11 — three separate confirmations, and no shortcut past them", () => {
  it("offers exactly three, each its own control", () => {
    render(<PreflightScreen runChecks={vi.fn()} onReady={vi.fn()} />);
    expect(screen.getAllByRole("checkbox")).toHaveLength(3);
    for (const line of SAFETY_LINES) {
      expect(screen.getByText(line)).toBeVisible();
    }
  });

  it("has no tick-all control", () => {
    const { container } = render(<PreflightScreen runChecks={vi.fn()} onReady={vi.fn()} />);
    // Three ticks satisfiable with one click would be one tick wearing three
    // hats, and the audit record would then mean nothing.
    expect(container.textContent).not.toMatch(/全选|全部确认|一键|全部同意/);
  });
});

describe("the pre-check does not exist until all three are ticked", () => {
  it("is absent, not merely disabled, at first", () => {
    render(<PreflightScreen runChecks={vi.fn()} onReady={vi.fn()} />);
    // Asserting absence rather than `toBeDisabled` is the point: a greyed-out
    // block still competes for attention with the safety lines.
    expect(screen.queryByRole("region", { name: "设备自检" })).not.toBeInTheDocument();
  });

  it("stays absent after only two of three", () => {
    render(<PreflightScreen runChecks={vi.fn()} onReady={vi.fn()} />);
    const boxes = screen.getAllByRole("checkbox");
    fireEvent.click(boxes[0]);
    fireEvent.click(boxes[1]);
    expect(screen.queryByRole("region", { name: "设备自检" })).not.toBeInTheDocument();
  });

  it("does not run any device check before the third tick", () => {
    const runChecks = vi.fn().mockResolvedValue(passing);
    render(<PreflightScreen runChecks={runChecks} onReady={vi.fn()} />);
    fireEvent.click(screen.getAllByRole("checkbox")[0]);
    expect(runChecks).not.toHaveBeenCalled();
  });

  it("appears and starts checking once the third is ticked", async () => {
    const runChecks = vi.fn().mockResolvedValue(passing);
    render(<PreflightScreen runChecks={runChecks} onReady={vi.fn()} />);
    tickAllSafety();

    expect(await screen.findByRole("region", { name: "设备自检" })).toBeVisible();
    expect(runChecks).toHaveBeenCalledOnce();
  });

  it("runs the pre-check exactly once under StrictMode", async () => {
    // The app renders inside <React.StrictMode>, which invokes state updaters
    // twice on purpose to surface side effects hiding in them. An earlier
    // version of this screen called runChecks from inside a setState updater;
    // it ran twice, and since a retry can succeed where the first attempt
    // failed, the blocked state was skipped entirely. Rendering the plain
    // component could not see that — this case can.
    const runChecks = vi.fn().mockResolvedValue(passing);
    render(
      <StrictMode>
        <PreflightScreen runChecks={runChecks} onReady={vi.fn()} dwellMs={5000} />
      </StrictMode>,
    );
    tickAllSafety();

    await screen.findByRole("region", { name: "设备自检" });
    expect(runChecks).toHaveBeenCalledOnce();
  });

  it("shows the first result, even when a retry would pass", async () => {
    // Guards the same defect from the user's side rather than the call count's:
    // whatever the first run says is what the operator must see.
    const runChecks = vi.fn()
      .mockResolvedValueOnce(blocked)
      .mockResolvedValue(passing);
    render(
      <StrictMode>
        <PreflightScreen runChecks={runChecks} onReady={vi.fn()} dwellMs={5000} />
      </StrictMode>,
    );
    tickAllSafety();

    expect(await screen.findByText("设备还不能开始")).toBeVisible();
  });

  it("hides the pre-check again if a safety line is un-ticked", async () => {
    render(<PreflightScreen runChecks={vi.fn().mockResolvedValue(passing)} onReady={vi.fn()} />);
    tickAllSafety();
    await screen.findByRole("region", { name: "设备自检" });

    fireEvent.click(screen.getAllByRole("checkbox")[1]);
    expect(screen.queryByRole("region", { name: "设备自检" })).not.toBeInTheDocument();
  });
});

describe("all green advances on its own, after a dwell", () => {
  it("does not advance instantly — the operator has to see the result", async () => {
    const onReady = vi.fn();
    render(
      <PreflightScreen
        runChecks={vi.fn().mockResolvedValue(passing)}
        onReady={onReady}
        dwellMs={40}
      />,
    );
    tickAllSafety();

    await screen.findByText("设备已就绪");
    expect(onReady).not.toHaveBeenCalled(); // still inside the dwell
    await waitFor(() => expect(onReady).toHaveBeenCalledOnce());
  });

  it("offers no button to press when everything passed", async () => {
    render(
      <PreflightScreen
        runChecks={vi.fn().mockResolvedValue(passing)}
        onReady={vi.fn()}
        dwellMs={40}
      />,
    );
    tickAllSafety();
    await screen.findByText("设备已就绪");

    expect(screen.queryByRole("button", { name: "重新检查" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /继续|下一步/ })).not.toBeInTheDocument();
  });
});

describe("a failed check blocks, and says what to do", () => {
  it("never advances", async () => {
    const onReady = vi.fn();
    render(
      <PreflightScreen
        runChecks={vi.fn().mockResolvedValue(blocked)}
        onReady={onReady}
        dwellMs={10}
      />,
    );
    tickAllSafety();
    await screen.findByText("设备还不能开始");

    await new Promise((resolve) => setTimeout(resolve, 40));
    expect(onReady).not.toHaveBeenCalled();
  });

  it("shows the fix, not the fault", async () => {
    const { container } = render(
      <PreflightScreen runChecks={vi.fn().mockResolvedValue(blocked)} onReady={vi.fn()} />,
    );
    tickAllSafety();

    expect(await screen.findByText(/请更换或充电后重新检查/)).toBeVisible();
    // No stack traces, no log lines, no internal identifiers.
    expect(container.textContent).not.toMatch(
      /Error|Traceback|at \w+\.|null|undefined|0x[0-9a-f]|stack|exception/i,
    );
  });

  it("turns the primary action into a retry that actually re-runs", async () => {
    const runChecks = vi.fn()
      .mockResolvedValueOnce(blocked)
      .mockResolvedValueOnce(passing);
    render(<PreflightScreen runChecks={runChecks} onReady={vi.fn()} dwellMs={40} />);
    tickAllSafety();
    await screen.findByText("设备还不能开始");

    fireEvent.click(screen.getByRole("button", { name: "重新检查" }));

    expect(await screen.findByText("设备已就绪")).toBeVisible();
    expect(runChecks).toHaveBeenCalledTimes(2);
  });

  it("keeps the passing rows visible alongside the failing one", async () => {
    render(<PreflightScreen runChecks={vi.fn().mockResolvedValue(blocked)} onReady={vi.fn()} />);
    tickAllSafety();
    await screen.findByText("设备还不能开始");

    // Hiding what passed would leave the operator guessing how much is wrong.
    expect(screen.getByText("左模块连接")).toBeVisible();
    expect(screen.getByText("链路到达率")).toBeVisible();
    expect(screen.getByText("左右模块电量")).toBeVisible();
  });
});
