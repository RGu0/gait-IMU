import { fireEvent, render, screen, within } from "@testing-library/react";
import { DeviceSupportScreen } from "./DeviceSupportScreen.jsx";
import { RecordsScreen } from "./RecordsScreen.jsx";
import { ReportPreviewScreen } from "./ReportPreviewScreen.jsx";
import { REPORT } from "./mockTerminalAdapter.js";

const records = [
  { id: "r1", assessedAt: "2026-08-23 14:32", subjectLabel: "**2781", protocol: "120 秒", validSteps: 118, status: "已完成（基础版）", reportVersion: "R-1" },
  { id: "r3", assessedAt: "2026-08-23 11:20", subjectLabel: "**9007", protocol: "120 秒", validSteps: 62, status: "未通过质检", reportVersion: "—" },
  { id: "r4", assessedAt: "2026-08-23 10:44", subjectLabel: "临时002", protocol: "60 秒", validSteps: 51, status: "处理中", reportVersion: "—" },
];

const devices = {
  leftBattery: 82,
  rightBattery: 76,
  modules: [
    { side: "left", maskedAddress: "…:9A:4C", firmware: "1.4.2", lastConnected: "2026-08-23 14:31", factoryCalibrated: true },
    { side: "right", maskedAddress: "…:9A:51", firmware: "1.4.2", lastConnected: "2026-08-23 14:31", factoryCalibrated: true },
  ],
};

const support = { phone: "400-000-0000", terminalId: "T-KJ-0042", appVersion: "0.1.0", algoVersion: "gait-core 0.4.1" };

describe("P-10a — the records list", () => {
  const renderList = (props = {}) =>
    render(<RecordsScreen records={records} onOpenRecord={vi.fn()} onNavigate={vi.fn()} {...props} />);

  it("shows a status with words and an icon, not colour alone", () => {
    renderList();
    const table = screen.getByRole("table");
    for (const status of ["已完成（基础版）", "未通过质检", "处理中"]) {
      const cell = within(table).getByText(status);
      expect(cell).toBeVisible();
      // The pill wraps an icon alongside the label.
      expect(cell.closest("span, td").innerHTML).toMatch(/<svg|<path|<circle/);
    }
  });

  it("filters without hiding that it filtered", () => {
    renderList();
    fireEvent.change(screen.getByLabelText("状态"), { target: { value: "未通过质检" } });
    expect(screen.getByRole("table").querySelectorAll("tbody tr")).toHaveLength(1);
    // A count that shrinks silently is how someone concludes the records are gone.
    expect(screen.getByText("1 / 3 条")).toBeVisible();
  });

  it("says so when a filter matches nothing", () => {
    render(<RecordsScreen records={[]} onOpenRecord={vi.fn()} onNavigate={vi.fn()} />);
    expect(screen.getByText("当前筛选条件下没有记录。")).toBeVisible();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  it("opens the record it was asked to open", () => {
    const onOpenRecord = vi.fn();
    renderList({ onOpenRecord });
    fireEvent.click(within(screen.getByRole("table")).getAllByRole("button", { name: "查看" })[0]);
    expect(onOpenRecord).toHaveBeenCalledWith(expect.objectContaining({ subjectLabel: "**2781" }));
  });
});

describe("P-10b — the preview renders the one template", () => {
  const renderPreview = () =>
    render(<ReportPreviewScreen report={REPORT} onNavigate={vi.fn()} onBack={vi.fn()} />);

  it("renders the report itself, not a screen-only summary", () => {
    const { container } = renderPreview();
    // .rp-page is the template's own root: if the preview ever grew its own
    // layout, this would be the first thing to disappear (R-4).
    expect(container.querySelector(".rp-page")).not.toBeNull();
    expect(screen.getByText("筛查摘要")).toBeVisible();
    expect(screen.getByText("专业参数")).toBeVisible();
  });

  it("keeps A4 proportions rather than reflowing for the screen", () => {
    const { container } = renderPreview();
    const sheet = container.querySelector(".preview-sheet");
    expect(sheet).not.toBeNull();
  });

  it("disables export and print, and says why", () => {
    renderPreview();
    // Both go through printToPDF, which lives in an Electron main process that
    // does not exist yet. Offering them would be a promise the app cannot keep.
    expect(screen.getByRole("button", { name: "导出 PDF" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "打印" })).toBeDisabled();
    expect(screen.getByText(/导出与打印由应用外壳提供/)).toBeVisible();
  });

  it("shows the report identity an operator would read out on the phone", () => {
    renderPreview();
    expect(screen.getAllByText(REPORT.reportId).length).toBeGreaterThan(0);
    expect(screen.getAllByText(REPORT.algoVersion).length).toBeGreaterThan(0);
  });
});

describe("P-10c — devices and support", () => {
  const renderDevices = (props = {}) =>
    render(
      <DeviceSupportScreen
        devices={devices}
        support={support}
        onRecheck={vi.fn()}
        onRepair={vi.fn()}
        onNavigate={vi.fn()}
        {...props}
      />,
    );

  it("masks the module addresses", () => {
    renderDevices();
    const { container } = renderDevices();
    // A full MAC is device-identifying detail the operator has no use for.
    expect(container.textContent).not.toMatch(/([0-9A-F]{2}:){5}[0-9A-F]{2}/i);
    expect(screen.getAllByText("…:9A:4C").length).toBeGreaterThan(0);
  });

  it("asks twice before re-pairing", () => {
    const onRepair = vi.fn();
    renderDevices({ onRepair });
    fireEvent.click(screen.getByRole("button", { name: "重新配对模块" }));
    // Re-pairing silently rebinds which module is "left"; every later session
    // would be mirrored and each metric would still look plausible.
    expect(onRepair).not.toHaveBeenCalled();
    expect(screen.getByRole("alertdialog")).toBeVisible();

    fireEvent.click(within(screen.getByRole("alertdialog")).getByRole("button", { name: "重新配对" }));
    expect(onRepair).toHaveBeenCalledOnce();
  });

  it("says the re-pair will be recorded", () => {
    renderDevices();
    fireEvent.click(screen.getByRole("button", { name: "重新配对模块" }));
    expect(screen.getByRole("alertdialog")).toHaveTextContent(/操作将被记录/);
  });

  it("exposes no engineering entry point at all", () => {
    const { container } = renderDevices();
    // Absent, not disabled: a control on screen eventually gets pressed by
    // someone who was told to "try things".
    expect(container.textContent).not.toMatch(/工程|调试|诊断模式|开发者|日志导出|高级设置/);
  });

  it("gives the operator what a support call needs", () => {
    renderDevices();
    expect(screen.getByText(support.phone)).toBeVisible();
    expect(screen.getByText(support.terminalId)).toBeVisible();
    expect(screen.getByText(support.algoVersion)).toBeVisible();
  });
});

describe("the top nav actually navigates", () => {
  it("routes by label", () => {
    const onNavigate = vi.fn();
    render(<RecordsScreen records={records} onOpenRecord={vi.fn()} onNavigate={onNavigate} />);
    fireEvent.click(screen.getByRole("button", { name: "设备与支持" }));
    expect(onNavigate).toHaveBeenCalledWith("设备与支持");
  });

  it("marks the page the operator is on", () => {
    render(<RecordsScreen records={records} onOpenRecord={vi.fn()} onNavigate={vi.fn()} />);
    expect(screen.getByRole("button", { name: "检测记录" })).toHaveAttribute("aria-current", "page");
  });

  it("renders the nav inert rather than fake when routing is not wired", () => {
    render(<RecordsScreen records={records} onOpenRecord={vi.fn()} />);
    // A nav item that looks pressable and does nothing is worse than one that
    // is plainly not offered.
    expect(screen.getByRole("button", { name: "设备与支持" })).toBeDisabled();
  });
});
