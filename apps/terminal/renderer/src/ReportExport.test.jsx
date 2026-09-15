import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { ReportPreviewScreen } from "./ReportPreviewScreen.jsx";
import { REPORT } from "./mockTerminalAdapter.js";

/**
 * RAY-224 pdf-export：报告预览上的导出 PDF / 打印，经 `window.gaitShell` 走主进程。
 * 这里用假的外壳桥验渲染端的三件事：有桥才可用、只交 reportId、结果按种类反馈。
 */
function fakeShell({ pdf = { status: "saved", path: "/Users/op/Documents/步态报告.pdf" }, print = { status: "printed" } } = {}) {
  return {
    exportReportPdf: vi.fn(async () => (pdf instanceof Error ? Promise.reject(pdf) : pdf)),
    printReport: vi.fn(async () => print),
  };
}

const renderWith = (shell) =>
  render(<ReportPreviewScreen report={REPORT} onNavigate={vi.fn()} onBack={vi.fn()} shell={shell} />);

afterEach(() => {
  delete globalThis.gaitShell;
  vi.useRealTimers();
});

describe("with the shell bridge", () => {
  it("enables both actions and drops the not-available banner", () => {
    renderWith(fakeShell());
    expect(screen.getByRole("button", { name: "导出 PDF" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "打印" })).toBeEnabled();
    expect(screen.queryByText("导出与打印尚不可用")).not.toBeInTheDocument();
  });

  it("picks the bridge up from globalThis when none is injected", () => {
    globalThis.gaitShell = fakeShell();
    render(<ReportPreviewScreen report={REPORT} onNavigate={vi.fn()} onBack={vi.fn()} />);
    expect(screen.getByRole("button", { name: "导出 PDF" })).toBeEnabled();
  });

  it("passes the report id only — never the subject label", async () => {
    const shell = fakeShell();
    renderWith(shell);
    fireEvent.click(screen.getByRole("button", { name: "导出 PDF" }));
    await screen.findByText(/已导出 PDF：/);
    expect(shell.exportReportPdf).toHaveBeenCalledWith({ reportId: REPORT.reportId });
    expect(JSON.stringify(shell.exportReportPdf.mock.calls)).not.toContain(REPORT.subjectLabel);
  });

  it("shows where the PDF was saved, then lets the toast go", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    renderWith(fakeShell());
    fireEvent.click(screen.getByRole("button", { name: "导出 PDF" }));
    expect(await screen.findByText("已导出 PDF：/Users/op/Documents/步态报告.pdf")).toBeVisible();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(4100);
    });
    expect(screen.queryByText(/已导出 PDF/)).not.toBeInTheDocument();
  });

  it("stays silent when the operator cancels", async () => {
    const shell = fakeShell({ pdf: { status: "canceled" }, print: { status: "canceled" } });
    renderWith(shell);
    fireEvent.click(screen.getByRole("button", { name: "导出 PDF" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "导出 PDF" })).toBeEnabled());
    fireEvent.click(screen.getByRole("button", { name: "打印" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "打印" })).toBeEnabled());
    expect(shell.printReport).toHaveBeenCalledTimes(1);
    expect(screen.queryByText(/已导出|已发送|失败/)).not.toBeInTheDocument();
  });

  it("says the export failed, with the reason", async () => {
    renderWith(fakeShell({ pdf: { status: "error", message: "EACCES: permission denied" } }));
    fireEvent.click(screen.getByRole("button", { name: "导出 PDF" }));
    expect(await screen.findByText("导出 PDF 失败")).toBeVisible();
    expect(screen.getByText("EACCES: permission denied")).toBeVisible();
  });

  it("treats a broken bridge call as a failure, not as nothing", async () => {
    renderWith(fakeShell({ pdf: new Error("ipc gone") }));
    fireEvent.click(screen.getByRole("button", { name: "导出 PDF" }));
    expect(await screen.findByText("ipc gone")).toBeVisible();
  });

  it("confirms a print and reports a print failure", async () => {
    const shell = fakeShell();
    const { unmount } = renderWith(shell);
    fireEvent.click(screen.getByRole("button", { name: "打印" }));
    expect(await screen.findByText("已发送到打印机。")).toBeVisible();
    unmount();

    renderWith(fakeShell({ print: { status: "error", message: "no printer" } }));
    fireEvent.click(screen.getByRole("button", { name: "打印" }));
    expect(await screen.findByText("打印失败")).toBeVisible();
    expect(screen.getByText("no printer")).toBeVisible();
  });
});

describe("without the shell bridge", () => {
  it("keeps both actions disabled with the reason", () => {
    renderWith(undefined);
    expect(screen.getByRole("button", { name: "导出 PDF" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "打印" })).toBeDisabled();
    expect(screen.getByText("导出与打印尚不可用")).toBeVisible();
  });
});
