/**
 * RAY-503：真机模式工作台跟随蓝牙连接状态。
 *
 * 装机 RC 上，模块在后台连上之后工作台仍停在「电量读不到」，只能手点「重新检查设备」。
 * 快照只在进入工作台时拉一次；这里钉住：`deviceSummary.state === "connecting"` 时
 * 留在工作台就自动重拉，连上即停；失败不轮询；离开工作台不轮询。
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { TerminalApp } from "./TerminalApp.jsx";
import { createSidecarAdapter } from "./sidecarTerminalAdapter.js";
import { RECORDS, SNAPSHOT, fakeTransport } from "./testing/sidecarFixtures.js";

const POLL_MS = 20;
const WAIT = { timeout: 3000 };
const CONNECTING = { ready: false, issues: ["正在连接左右模块…"], state: "connecting" };
const CONNECTED = { ready: true, issues: [], state: "connected" };
const FAILED = {
  ready: false,
  issues: ["左脚电量读不到：请确认模块已连接且未处于高速流。"],
  state: "failed",
};

/** 快照依次给出 `summaries`，用完后停在最后一个。 */
function sidecar(summaries) {
  let served = 0;
  const fake = fakeTransport({
    snapshot: () => {
      const deviceSummary = summaries[Math.min(served, summaries.length - 1)];
      served += 1;
      return { ...SNAPSHOT, deviceSummary };
    },
    listRecords: RECORDS,
  });
  const adapter = createSidecarAdapter(fake.transport, { now: () => 100 });
  const snapshots = () => fake.calls.filter((call) => call.method === "snapshot").length;
  return { adapter, snapshots };
}

const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

describe("工作台跟随设备连接状态（RAY-503）", () => {
  it("连接中自动重拉，连上后显示设备已就绪并停止轮询", async () => {
    const { adapter, snapshots } = sidecar([CONNECTING, CONNECTING, CONNECTED]);
    render(<TerminalApp adapter={adapter} devicePollMs={POLL_MS} />);

    expect(await screen.findByText("正在连接左右模块…")).toBeTruthy();
    expect(await screen.findByText("设备已就绪", {}, WAIT)).toBeTruthy();
    expect(screen.queryByText("正在连接左右模块…")).toBeNull();

    const settled = snapshots();
    expect(settled).toBe(3);
    await pause(POLL_MS * 5);
    expect(snapshots()).toBe(settled);
  });

  it("连接失败不自动重试，留给「重新检查设备」", async () => {
    const { adapter, snapshots } = sidecar([FAILED]);
    render(<TerminalApp adapter={adapter} devicePollMs={POLL_MS} />);

    expect(await screen.findByText(/左脚电量读不到/)).toBeTruthy();
    await pause(POLL_MS * 5);
    expect(snapshots()).toBe(1);
  });

  it("离开工作台就不再轮询", async () => {
    const { adapter, snapshots } = sidecar([CONNECTING]);
    render(<TerminalApp adapter={adapter} devicePollMs={POLL_MS} />);

    await screen.findByText("正在连接左右模块…");
    await waitFor(() => expect(snapshots()).toBeGreaterThanOrEqual(2), WAIT);
    fireEvent.click(screen.getByRole("button", { name: "检测记录" }));
    await screen.findByRole("heading", { name: "检测记录" });

    await pause(POLL_MS * 2);
    const away = snapshots();
    await pause(POLL_MS * 5);
    expect(snapshots()).toBe(away);
  });
});
