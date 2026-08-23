import { fireEvent, render, screen } from "@testing-library/react";
import { HubScreen } from "./HubScreen.jsx";
import { TerminalApp } from "./TerminalApp.jsx";

const readySnapshot = {
  operator: { organization: "community-kangjian" },
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
  login: async () => readySnapshot,
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

it("goes from institutional login to the ready hub", async () => {
  render(<TerminalApp adapter={readyAdapter} />);
  fireEvent.change(screen.getByLabelText("机构账号"), {
    target: { value: "community-kangjian" },
  });
  fireEvent.change(screen.getByLabelText("登录密码"), {
    target: { value: "secret" },
  });
  fireEvent.click(screen.getByRole("button", { name: "登录" }));
  expect(
    await screen.findByRole("button", { name: "开始新的检测" }),
  ).toBeVisible();
});

it("rechecks attention-required devices and refreshes the ready hub", async () => {
  const snapshot = vi.fn()
    .mockResolvedValueOnce(needsAttentionSnapshot)
    .mockResolvedValueOnce(readySnapshot);
  const recheckDevices = vi.fn().mockResolvedValue(undefined);
  const adapter = {
    login: vi.fn().mockResolvedValue(needsAttentionSnapshot),
    recheckDevices,
    snapshot,
  };

  render(<TerminalApp adapter={adapter} />);
  fireEvent.change(screen.getByLabelText("机构账号"), {
    target: { value: "community-kangjian" },
  });
  fireEvent.change(screen.getByLabelText("登录密码"), {
    target: { value: "secret" },
  });
  fireEvent.click(screen.getByRole("button", { name: "登录" }));
  await screen.findByRole("button", { name: "重新检查设备" });

  fireEvent.click(screen.getByRole("button", { name: "重新检查设备" }));

  expect(await screen.findByRole("button", { name: "开始新的检测" })).toBeVisible();
  expect(recheckDevices).toHaveBeenCalledOnce();
  expect(snapshot).toHaveBeenCalledTimes(2);
});

it("keeps the operator out of a License or registration flow", () => {
  render(<TerminalApp adapter={readyAdapter} />);
  expect(screen.queryByText(/License|注册|激活/i)).not.toBeInTheDocument();
});

it("keeps the operator at login when authentication fails", async () => {
  const failingAdapter = {
    ...readyAdapter,
    login: async () => {
      throw new Error("账号或密码不正确");
    },
  };
  render(<TerminalApp adapter={failingAdapter} />);
  fireEvent.change(screen.getByLabelText("机构账号"), {
    target: { value: "community-kangjian" },
  });
  fireEvent.change(screen.getByLabelText("登录密码"), {
    target: { value: "incorrect" },
  });
  fireEvent.click(screen.getByRole("button", { name: "登录" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("账号或密码不正确");
  expect(screen.getByRole("button", { name: "登录" })).toBeVisible();
});

it("shows exactly five recent records in the ready hub", async () => {
  render(<TerminalApp adapter={readyAdapter} />);
  fireEvent.change(screen.getByLabelText("机构账号"), {
    target: { value: "community-kangjian" },
  });
  fireEvent.change(screen.getByLabelText("登录密码"), {
    target: { value: "secret" },
  });
  fireEvent.click(screen.getByRole("button", { name: "登录" }));
  const table = await screen.findByRole("table");
  expect(table.querySelectorAll("tbody tr")).toHaveLength(5);
});

it("opens at the institutional login", () => {
  render(<TerminalApp adapter={readyAdapter} />);
  expect(
    screen.getByRole("heading", { name: "步态健康筛查与分析平台" }),
  ).toBeVisible();
  expect(screen.getByLabelText("机构账号")).toBeVisible();
  expect(screen.getByLabelText("登录密码")).toBeVisible();
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
