import { fireEvent, render, screen } from "@testing-library/react";
import { HubScreen } from "./HubScreen.jsx";
import { TerminalApp } from "./TerminalApp.jsx";

const readySnapshot = {
  operator: null,
  deviceSummary: {
    ready: true,
    issues: [],
    leftBattery: 82,
    rightBattery: 76,
  },
  uploadSummary: {
    pending: 2,
    uploaded: 12,
  },
  recentRecords: [
    { subjectLabel: "**1234", status: "已完成" },
    { subjectLabel: "**2345", status: "已完成" },
    { subjectLabel: "**3456", status: "待上传" },
    { subjectLabel: "**4567", status: "已完成" },
    { subjectLabel: "**5678", status: "已完成" },
  ],
};

const readyAdapter = {
  snapshot: async () => readySnapshot,
  recheckDevices: async () => readySnapshot,
};

const needsAttentionSnapshot = {
  ...readySnapshot,
  deviceSummary: {
    ...readySnapshot.deviceSummary,
    ready: false,
    issues: ["右侧模块未连接（E-BLE-1001）"],
  },
};

it("opens directly at the ready hub without a login step", async () => {
  render(<TerminalApp adapter={readyAdapter} />);
  expect(
    await screen.findByRole("button", { name: "开始新的检测" }),
  ).toBeVisible();
  // 最小 MVP 无登录（P-00 暂不考虑）：不出现任何登录表单。
  expect(screen.queryByLabelText("机构账号")).not.toBeInTheDocument();
  expect(screen.queryByLabelText("登录密码")).not.toBeInTheDocument();
});

it("rechecks attention-required devices and refreshes the ready hub", async () => {
  const snapshot = vi.fn()
    .mockResolvedValueOnce(needsAttentionSnapshot)
    .mockResolvedValueOnce(readySnapshot);
  const recheckDevices = vi.fn().mockResolvedValue(undefined);
  const adapter = { snapshot, recheckDevices };

  render(<TerminalApp adapter={adapter} />);
  await screen.findByRole("button", { name: "重新检查设备" });

  fireEvent.click(screen.getByRole("button", { name: "重新检查设备" }));

  expect(await screen.findByRole("button", { name: "开始新的检测" })).toBeVisible();
  expect(recheckDevices).toHaveBeenCalledOnce();
  expect(snapshot).toHaveBeenCalledTimes(2);
});

it("keeps the operator out of a License or registration flow", async () => {
  render(<TerminalApp adapter={readyAdapter} />);
  await screen.findByRole("button", { name: "开始新的检测" });
  expect(screen.queryByText(/License|注册|激活/i)).not.toBeInTheDocument();
});

it("shows exactly five recent records in the ready hub", async () => {
  render(<TerminalApp adapter={readyAdapter} />);
  const table = await screen.findByRole("table");
  expect(table.querySelectorAll("tbody tr")).toHaveLength(5);
});

it.each([
  ["a disconnected module", "右侧模块未连接（E-BLE-1001）"],
  ["a low battery", "右侧模块电量不足（E-BLE-1002）"],
  ["missing factory calibration", "右侧模块缺少出厂校准（E-BLE-1003）"],
])("replaces the start action for %s", (_condition, issue) => {
  const onRecheck = vi.fn();
  render(
    <HubScreen
      snapshot={{
        ...readySnapshot,
        deviceSummary: { ...readySnapshot.deviceSummary, ready: false, issues: [issue] },
      }}
      onRecheck={onRecheck}
    />,
  );

  fireEvent.click(screen.getByRole("button", { name: "重新检查设备" }));
  expect(screen.getByRole("button", { name: "重新检查设备" })).toBeEnabled();
  expect(screen.queryByRole("button", { name: "开始新的检测" })).not.toBeInTheDocument();
  expect(screen.getByText(issue)).toBeVisible();
  expect(onRecheck).toHaveBeenCalledOnce();
});

it("keeps the start action when upload backlog is the only warning", () => {
  render(<HubScreen snapshot={readySnapshot} onRecheck={vi.fn()} />);

  expect(screen.getByRole("button", { name: "开始新的检测" })).toBeEnabled();
  expect(screen.queryByRole("button", { name: "重新检查设备" })).not.toBeInTheDocument();
  expect(screen.getByText("待上传 2 条")).toBeVisible();
});
