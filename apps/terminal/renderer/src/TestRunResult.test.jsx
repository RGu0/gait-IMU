import { useEffect, useState } from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { ResultScreen } from "./ResultScreen.jsx";
import { TestRunScreen } from "./TestRunScreen.jsx";
import { INVALID_RESULT, VALID_RESULT } from "./mockTerminalAdapter.js";

const live = {
  totalSeconds: 3,
  instruction: "请按平时走路的速度，在两个标志之间来回走",
  steps: { left: 87, right: 86 },
  link: { left: "good", right: "good" },
  footfalls: { left: [18, 66, 112], right: [42, 90, 137] },
  notices: [],
  aborted: null,
};

/** sidecar 给出的完整错误：码 + 现象 + 动作。渲染端只排版这三段。 */
const ABORTED = {
  code: "E-BLE-1020",
  domain: "E-BLE",
  message: "原始数据写盘失败，测试已安全停止。",
  action: "请检查磁盘剩余空间后重新检测。本次数据已尽可能保留，但不完整，不会生成报告。",
  blocking: true,
};

const renderRun = (overrides = {}, props = {}) =>
  render(
    <TestRunScreen
      live={{ ...live, ...overrides }}
      onFinish={vi.fn()}
      onAbort={vi.fn()}
      tickMs={5}
      holdMs={20}
      {...props}
    />,
  );

describe("C-1 / C-2 — during capture the screen shows three things only", () => {
  it("carries no clinical metric anywhere", () => {
    const { container } = renderRun();
    // A number the subject reads mid-walk changes how they walk, which corrupts
    // the very measurement being taken.
    expect(container.textContent).not.toMatch(
      /步速|步长|步频|双支撑|对称性|变异|m\/s|CV/,
    );
  });

  it("carries no upload state", () => {
    const { container } = renderRun();
    expect(container.textContent).not.toMatch(/上传|同步中|队列/);
  });

  it("has no progress bar", () => {
    const { container } = renderRun();
    expect(container.querySelector("progress")).toBeNull();
    expect(container.querySelector('[role="progressbar"]')).toBeNull();
  });

  it("does show the three that belong: time, steps, link", () => {
    renderRun();
    expect(screen.getByText("87")).toBeVisible();
    expect(screen.getByText("86")).toBeVisible();
    expect(screen.getAllByText("链路良好")).toHaveLength(2);
  });
});

describe("C-14 — the countdown steps down on a 720-high terminal", () => {
  const digits = () => screen.getByText(String(live.totalSeconds));

  it("uses the large size when there is room", () => {
    renderRun({}, { compact: false });
    expect(digits().style.font).toContain("160px");
  });

  it("steps down to 120px when there is not", () => {
    // 1280×720 is the floor the product must run on (C-14). The size lives in
    // an inline style, so no media query can reach it — the breakpoint has to
    // be evaluated in JS, and this asserts the wiring exists at all. It did not
    // in the first version: the prop was accepted and never passed.
    renderRun({}, { compact: true });
    expect(digits().style.font).toContain("120px");
  });
});

describe("C-5 — capture cannot be navigated away from", () => {
  it("offers no navigation and no back control", () => {
    renderRun();
    expect(screen.queryByRole("navigation")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /返回|工作台|检测记录/ })).not.toBeInTheDocument();
  });

  it("shows a link drop as a sidebar change, never a dialog", () => {
    renderRun({ link: { left: "good", right: "bad" } });
    expect(screen.getByText("链路异常")).toBeVisible();
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
  });
});

describe("a pause is recorded, not punished", () => {
  it("states it neutrally and does not hurry the operator", () => {
    const { container } = renderRun({ notices: ["已记录一次停顿，测试继续。"] });
    expect(screen.getByText("已记录一次停顿，测试继续。")).toBeVisible();
    expect(container.textContent).not.toMatch(/请尽快|超时|失败|警告|错误/);
  });

  it("does not stop the countdown", async () => {
    renderRun({ notices: ["已记录一次停顿，测试继续。"] });
    // Stopping the clock for a pause would silently change the protocol length
    // and make this session incomparable with every other one.
    await waitFor(() => expect(screen.getByText("可以停下了")).toBeVisible());
  });
});

describe("C-10 — stopping needs one confirmation, and one red button", () => {
  it("does not stop on the first click", () => {
    const onAbort = vi.fn();
    renderRun({}, { onAbort });
    fireEvent.click(screen.getByRole("button", { name: "停止检测" }));
    expect(onAbort).not.toHaveBeenCalled();
    expect(screen.getByRole("alertdialog")).toBeVisible();
  });

  it("puts the only filled red button inside that dialog", () => {
    renderRun();
    // Before the dialog: nothing filled-red anywhere on the page.
    const filledDangerBefore = screen
      .getAllByRole("button")
      .filter((b) => b.style.background.includes("--danger-fg"));
    expect(filledDangerBefore).toHaveLength(0);

    fireEvent.click(screen.getByRole("button", { name: "停止检测" }));
    const dialog = screen.getByRole("alertdialog");
    const filled = [...dialog.querySelectorAll("button")].filter((b) =>
      b.style.background.includes("--danger-fg"),
    );
    expect(filled).toHaveLength(1);
    expect(filled[0]).toHaveTextContent("停止检测");
  });

  it("says how much was captured, so the choice is informed", () => {
    renderRun();
    fireEvent.click(screen.getByRole("button", { name: "停止检测" }));
    expect(screen.getByRole("alertdialog")).toHaveTextContent(/已采集/);
  });

  it("carries on when the operator backs out", () => {
    const onAbort = vi.fn();
    renderRun({}, { onAbort });
    fireEvent.click(screen.getByRole("button", { name: "停止检测" }));
    fireEvent.click(screen.getByRole("button", { name: "继续测试" }));
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    expect(onAbort).not.toHaveBeenCalled();
  });
});

describe("the countdown reaching zero", () => {
  it("takes over the subject area rather than adding a line", async () => {
    renderRun();
    await waitFor(() => expect(screen.getByText("可以停下了")).toBeVisible());
    expect(screen.getByText("请站定 3 秒")).toBeVisible();
    // The countdown itself is gone, not pushed down.
    expect(screen.queryByText("剩余时间（秒）")).not.toBeInTheDocument();
  });

  it("holds before advancing", async () => {
    const onFinish = vi.fn();
    renderRun({}, { onFinish, holdMs: 60 });
    await waitFor(() => expect(screen.getByText("可以停下了")).toBeVisible());
    expect(onFinish).not.toHaveBeenCalled();
    await waitFor(() => expect(onFinish).toHaveBeenCalledOnce());
  });

  it("still advances while the live sidebar keeps updating", async () => {
    // The real parent passes a freshly-created onFinish on every render, and
    // re-renders several times a second as steps and footfalls arrive. An
    // earlier version depended on that callback identity, so each update tore
    // down the hold timer and never re-armed it — the screen sat on
    // 「可以停下了」 forever. A single stable vi.fn() cannot show that; this
    // harness reproduces the churn.
    const advanced = vi.fn();
    function Churning() {
      const [n, setN] = useState(0);
      useEffect(() => {
        const id = setInterval(() => setN((c) => c + 1), 5);
        return () => clearInterval(id);
      }, []);
      return (
        <TestRunScreen
          live={{ ...live, steps: { left: n, right: n } }}
          onFinish={() => advanced()} // new identity every render, as in the app
          onAbort={() => {}}
          tickMs={5}
          holdMs={40}
        />
      );
    }

    render(<Churning />);
    await waitFor(() => expect(screen.getByText("可以停下了")).toBeVisible());
    await waitFor(() => expect(advanced).toHaveBeenCalled(), { timeout: 2000 });
  });
});

describe("a mid-session abort takes over the whole page", () => {
  it("says data was kept, that it is incomplete, and offers one way out", () => {
    // 错误对象是 sidecar 给的完整三段（码 + 现象 + 动作），mock 也照这个形状 ——
    // 一个只带 code 的 fixture 会让界面「自造文案」看起来是必要的。
    renderRun({ aborted: ABORTED });
    expect(screen.getByRole("alert")).toHaveTextContent("原始数据写盘失败，测试已安全停止。");
    expect(screen.getByRole("alert")).toHaveTextContent("E-BLE-1020");
    // 动作那一段同样来自 sidecar：它说数据保住了、不完整、不会出报告。
    expect(screen.getByText(ABORTED.action)).toBeVisible();
    expect(screen.getAllByRole("button")).toHaveLength(1);
    expect(screen.getByRole("button", { name: "返回工作台" })).toBeEnabled();
  });

  it("不自造文案：换一条错误，屏上跟着换", () => {
    // 这条是 RAY-248 验收第二条的可执行形式。写死的文案在上面那条测试里同样能过 ——
    // 只有换一条错误才能把「界面只是排版」和「界面自己写了一句」分开。
    renderRun({
      aborted: {
        code: "E-BLE-1002",
        domain: "E-BLE",
        message: "右模块电量耗尽，测试已安全停止。",
        action: "请更换电池后重新检测。",
        blocking: true,
      },
    });
    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent("右模块电量耗尽，测试已安全停止。");
    expect(alert).toHaveTextContent("请更换电池后重新检测。");
    expect(alert).not.toHaveTextContent("写盘");
    expect(alert).not.toHaveTextContent("磁盘");
  });

  it("shows no countdown and no sidebar once aborted", () => {
    renderRun({ aborted: ABORTED });
    expect(screen.queryByLabelText("操作员侧栏")).not.toBeInTheDocument();
    expect(screen.queryByText("剩余时间（秒）")).not.toBeInTheDocument();
  });
});

describe("P-09 finished — C-7 and C-8", () => {
  const renderResult = (result = VALID_RESULT) =>
    render(
      <ResultScreen
        result={result}
        onNextSubject={vi.fn()}
        onOpenReport={vi.fn()}
        onRetry={vi.fn()}
        onBackToHub={vi.fn()}
      />,
    );

  it("gives an uncomputable metric its own words, not a blank", () => {
    renderResult();
    expect(screen.getByText("本次不适用")).toBeVisible();
    expect(screen.getByText("有效步数不足以估计步周期变异。")).toBeVisible();
  });

  it("never renders a bare 0, dash or N/A as a metric value", () => {
    const { container } = renderResult();
    expect(container.textContent).not.toMatch(/N\/A|暂无数据/);
  });

  it("ships the valid step count together with variability (PRD §13)", () => {
    renderResult();
    // A variability figure without the step count behind it is a number with
    // no error bar.
    expect(screen.getAllByText(`基于 ${VALID_RESULT.validSteps} 个有效步`)).toHaveLength(2);
  });

  it("draws no reference band, normal range or cohort line (C-8)", () => {
    const { container } = renderResult();
    expect(container.textContent).not.toMatch(/正常范围|参考区间|常模|健康人|同龄人/);
  });

  it("does not block the next subject on the cloud report", () => {
    renderResult();
    expect(screen.getByRole("button", { name: "开始下一位" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "查看完整报告" })).toBeDisabled();
  });

  it("flags an assistive device for interpretation", () => {
    renderResult();
    expect(screen.getByText(/受试者使用了拄拐/)).toBeVisible();
  });
});

describe("P-09 invalid — C-6, a different layout and not the word 完成", () => {
  const renderInvalid = () =>
    render(
      <ResultScreen
        result={INVALID_RESULT}
        onNextSubject={vi.fn()}
        onOpenReport={vi.fn()}
        onRetry={vi.fn()}
        onBackToHub={vi.fn()}
      />,
    );

  it("never says 完成 anywhere on the page", () => {
    const { container } = renderInvalid();
    expect(container.textContent).not.toContain("完成");
  });

  it("shows no metric card and no chart", () => {
    const { container } = renderInvalid();
    // Structural, not textual: an operator recognises the shape of the page
    // before reading it, and a page shaped like a result reads as a result.
    expect(screen.queryByLabelText("核心指标")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("左右对比")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("变异性")).not.toBeInTheDocument();

    // "No chart" has to be judged by what a chart is made of, not by "no <svg>"
    // — a status icon is an svg too, and asserting against those would make the
    // test fail for the wrong reason. Charts in this product are drawn with the
    // gait palette and with the comparison bars; neither may appear here.
    expect(container.querySelector(".compare-bar")).toBeNull();
    expect(container.innerHTML).not.toMatch(/--viz-gait/);
    const big = [...container.querySelectorAll("svg")].filter(
      (svg) => Number(svg.getAttribute("width")) > 48,
    );
    expect(big).toHaveLength(0);
  });

  it("gives one concrete reason and one thing to do", () => {
    renderInvalid();
    expect(screen.getByText(/有效步行时长 96 秒/)).toBeVisible();
    expect(screen.getByText(/请确认通道长度与转身标志位置/)).toBeVisible();
  });

  it("offers retry and return, and nothing that looks like a report", () => {
    renderInvalid();
    expect(screen.getByRole("button", { name: "重新检测" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "返回工作台" })).toBeEnabled();
    expect(screen.queryByRole("button", { name: /报告|导出|下一位/ })).not.toBeInTheDocument();
  });
});
