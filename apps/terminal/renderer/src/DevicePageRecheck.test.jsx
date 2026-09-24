/**
 * RAY-530：设备与支持页。
 *
 * 装机 RC 上点「重新检查」毫无反应 —— 设备页复用工作台的重查，而那只重拉工作台快照；
 * 本页既拿不到新数据，也没有加载态和错误提示。另有三处与其他页面矛盾：出厂标定在
 * 预览放行下仍显示红色「缺少」、地址用平台句柄而不是自检里的 MAC 尾号、固件版本未显示。
 */
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { TerminalApp } from "./TerminalApp.jsx";
import { connectedLabel, createSidecarAdapter, toDeviceSupportView } from "./sidecarTerminalAdapter.js";
import { RECORDS, SNAPSHOT, fail, fakeTransport } from "./testing/sidecarFixtures.js";

const WAIT = { timeout: 3000 };

const module = (side, overrides = {}) => ({
  side,
  maskedAddress: side === "left" ? "…:11:47" : "…:C9:31",
  batteryPercent: null,
  factoryCalibrated: false,
  factoryCalibrationWaived: true,
  ...overrides,
});

/**
 * `recheckDevices` 挂住，直到测试调 `release()` —— 「进行中不能重复触发」必须在
 * 真的还在进行中时断言。用定时器放行在慢的 Windows runner 上会先跑完（RAY-530 CI）。
 */
function sidecar({ recheck = SNAPSHOT, before, after }) {
  let rechecked = false;
  let release = () => {};
  const held = new Promise((resolve) => {
    release = resolve;
  });
  const fake = fakeTransport({
    snapshot: SNAPSHOT,
    listRecords: RECORDS,
    recheckDevices: async () => {
      await held;
      rechecked = true;
      return recheck;
    },
    deviceSupport: () => ({ modules: rechecked ? after : before, ipcContractVersion: "1.0" }),
  });
  const adapter = createSidecarAdapter(fake.transport, { now: () => 100 });
  const methods = () => fake.calls.map((call) => call.method);
  return { adapter, methods, release: () => release() };
}

async function openDevicePage(adapter) {
  render(<TerminalApp adapter={adapter} />);
  await screen.findByRole("heading", { name: "工作台" });
  fireEvent.click(screen.getByRole("button", { name: "设备与支持" }));
  await screen.findByRole("heading", { name: "设备与支持" });
}

describe("设备页「重新检查」（RAY-530）", () => {
  it("重查后重拉本页数据，进行中有加载态且不能重复触发", async () => {
    const { adapter, methods, release } = sidecar({
      before: [module("left"), module("right")],
      after: [module("left", { batteryPercent: 100 }), module("right", { batteryPercent: 100 })],
    });
    await openDevicePage(adapter);
    expect(screen.getByText("电量未读取")).toBeVisible();

    fireEvent.click(screen.getByRole("button", { name: "重新检查" }));
    const busy = await screen.findByRole("button", { name: /正在重新检查/ });
    fireEvent.click(busy);
    release();

    await waitFor(() => expect(screen.queryByText("电量未读取")).toBeNull(), WAIT);
    expect(screen.getAllByText("100%")).toHaveLength(2);
    const calls = methods();
    expect(calls.filter((m) => m === "recheckDevices")).toHaveLength(1);
    expect(calls.lastIndexOf("deviceSupport")).toBeGreaterThan(calls.indexOf("recheckDevices"));
    expect(screen.getByRole("heading", { name: "设备与支持" })).toBeVisible();
  });

  it("数据没变也显示「已重新检查（HH:MM:SS）」；失败时不显示（RAY-537）", async () => {
    const same = [module("left", { batteryPercent: 100 }), module("right", { batteryPercent: 100 })];
    const { adapter, release } = sidecar({ before: same, after: same });
    await openDevicePage(adapter);
    expect(screen.queryByText(/已重新检查/)).toBeNull();
    release();
    fireEvent.click(screen.getByRole("button", { name: "重新检查" }));
    const status = await screen.findByText(/^已重新检查（\d{2}:\d{2}:\d{2}）$/, {}, WAIT);
    expect(status).toBeVisible();

    fireEvent.click(screen.getByRole("button", { name: "工作台" }));
    await screen.findByRole("heading", { name: "工作台" });
    fireEvent.click(screen.getByRole("button", { name: "设备与支持" }));
    await screen.findByRole("heading", { name: "设备与支持" });
    expect(screen.queryByText(/已重新检查/)).toBeNull();
  });

  it("重查失败时在本页提示，离开本页提示不跟过去", async () => {
    const { adapter, release } = sidecar({
      recheck: fail({ code: "E-BLE-1001", domain: "E-BLE", message: "模块未连接", action: "请确认模块已开机。", blocking: true }),
      before: [module("left"), module("right")],
      after: [module("left"), module("right")],
    });
    await openDevicePage(adapter);
    release();
    fireEvent.click(screen.getByRole("button", { name: "重新检查" }));
    const banner = await screen.findByLabelText("重新检查设备失败", {}, WAIT);
    expect(within(banner).getByText(/模块未连接/)).toBeVisible();
    expect(screen.queryByText(/已重新检查/)).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "工作台" }));
    await screen.findByRole("heading", { name: "工作台" });
    expect(screen.queryByLabelText("重新检查设备失败")).toBeNull();
  });
});

describe("设备页与其他页面同一口径（RAY-530）", () => {
  it("预览放行显示「未匹配（预览放行）」，真缺失仍是「缺少出厂标定」", async () => {
    const { adapter } = sidecar({
      before: [module("left"), module("right", { factoryCalibrationWaived: false })],
      after: [],
    });
    await openDevicePage(adapter);
    expect(screen.getByText("未匹配（预览放行）")).toBeVisible();
    expect(screen.getByText("缺少出厂标定")).toBeVisible();
  });

  it("地址用 MAC 尾号，固件与连接时间有值；未连接时如实写未连接", () => {
    const at = "2026-09-23T12:34:00+00:00";
    const view = toDeviceSupportView({
      modules: [
        module("left", { firmware: "1.4.2", connectedAt: at }),
        { side: "right", maskedAddress: null, batteryPercent: null },
      ],
    });
    const [left, right] = view.devices.modules;
    expect(left.maskedAddress).toBe("…:11:47");
    expect(left.firmware).toBe("1.4.2");
    expect(left.lastConnected).toBe(connectedLabel(at));
    expect(left.lastConnected).toMatch(/^本次 \d{2}:\d{2} 连接$/);
    expect(right.maskedAddress).toBe("未连接");
    expect(right.firmware).toBe("未记录");
    expect(right.lastConnected).toBe("未记录");
  });
});
