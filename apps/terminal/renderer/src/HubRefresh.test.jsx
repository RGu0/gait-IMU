/**
 * RAY-493 端到端验收发现的两处工作台缺陷，跑在真 sidecar 形状上：
 *
 * - B1：回到工作台不重拉快照，待上传数与最近记录停在旧值（证据
 *   `.project-context/evidence/ray-493/parent-acceptance/2026-09-15-e2e-macos-dev.md` A2a.3）。
 * - B2：「重新检查设备」失败时界面什么也不说，重复点击还会叠加。
 */
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { REPORT } from "./mockTerminalAdapter.js";
import { TerminalApp, recheckFailureMessage } from "./TerminalApp.jsx";
import { SidecarDown, createSidecarAdapter } from "./sidecarTerminalAdapter.js";
import {
  CREATE_SUBJECT,
  PREFLIGHT_WAIVED,
  SESSION_RESULT_READY,
  SNAPSHOT,
  START_SESSION,
  STOP_SESSION,
  fail,
  fakeTransport,
} from "./testing/sidecarFixtures.js";

const WAIT = { timeout: 5000 };

/** 一个会记账的假 sidecar：每次 stopSession 落一条会话，快照与记录都从这本账里读。 */
function countingSidecar(overrides = {}) {
  const state = { sessions: 0 };
  const recordAt = (i) => ({
    id: `20260915T08${String(i).padStart(4, "0")}Z-0000000${i}`,
    subjectUuid: `d107${i}a74-0000-0000-0000-000000000000`,
    protocolSeconds: 60,
    algoVersion: "gait-contract-1.1",
    complete: true,
    source: "synthetic",
  });
  const fake = fakeTransport({
    snapshot: () => ({
      ...SNAPSHOT,
      source: "synthetic",
      uploadSummary: { ...SNAPSHOT.uploadSummary, sessions: state.sessions },
    }),
    listRecords: () => Array.from({ length: state.sessions }, (_, i) => recordAt(i + 1)),
    recheckDevices: SNAPSHOT,
    createSubject: CREATE_SUBJECT,
    runPreflight: PREFLIGHT_WAIVED,
    startSession: START_SESSION,
    stopSession: () => {
      state.sessions += 1;
      return STOP_SESSION;
    },
    sessionResult: SESSION_RESULT_READY,
    reportFor: REPORT,
    ...overrides,
  });
  const adapter = createSidecarAdapter(fake.transport, { now: () => 100 });
  return { adapter, calls: fake.calls, state };
}

const click = (name) => fireEvent.click(screen.getByRole("button", { name }));
const tickAll = () => screen.getAllByRole("checkbox").forEach((box) => fireEvent.click(box));

async function expectHubCounts(count) {
  await screen.findByRole("heading", { name: "工作台" }, WAIT);
  await waitFor(() => expect(screen.getByText(`待上传 ${count} 条`)).toBeVisible(), WAIT);
  const recent = screen.getByRole("region", { name: "最近检测记录" });
  expect(within(recent).getByText(`${count} 条`)).toBeVisible();
}

async function walkToRun() {
  fireEvent.click(await screen.findByRole("button", { name: "开始新的检测" }, WAIT));
  fireEvent.click(await screen.findByRole("button", { name: "无编号，快速建档" }, WAIT));
  fireEvent.click(await screen.findByRole("button", { name: "跳过" }, WAIT));
  await screen.findByRole("heading", { name: "数据授权" }, WAIT);
  tickAll();
  click("同意并继续");
  await screen.findByRole("heading", { name: "安全确认与设备自检" }, WAIT);
  tickAll();
  await screen.findByRole("heading", { name: "佩戴引导" }, WAIT);
  tickAll();
  click("佩戴完成，开始标定");
  await screen.findByRole("heading", { name: "确认左右" }, WAIT);
  tickAll();
  click("确认无误，开始检测");
  return screen.findByLabelText("操作员侧栏", {}, WAIT);
}

describe("B1：回到工作台时重拉快照", () => {
  it("走完一次、看完报告，从顶栏回工作台后待上传数与最近记录更新", async () => {
    const { adapter } = countingSidecar({ startSession: { ...START_SESSION, totalSeconds: 0, remainingSeconds: 0 } });
    render(<TerminalApp adapter={adapter} preview />);
    await expectHubCounts(0);

    await walkToRun();
    // 倒计时 0 → 站定 → 收尾 → 报告预览
    await screen.findByRole("button", { name: "返回检测记录" }, { timeout: 10000 });

    click("工作台");
    await expectHubCounts(1);
  }, 30000);

  it("操作员中途停止后回到的工作台显示新落的会话", async () => {
    const { adapter } = countingSidecar();
    render(<TerminalApp adapter={adapter} />);
    await expectHubCounts(0);

    await walkToRun();
    click("停止检测");
    const dialog = screen.getByRole("alertdialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "停止检测" }));
    await expectHubCounts(1);
  }, 30000);

  it("顶栏回工作台时先显示上一份快照，拉到新快照后再更新，不回占位屏", async () => {
    const { adapter, state } = countingSidecar();
    render(<TerminalApp adapter={adapter} />);
    await expectHubCounts(0);

    click("检测记录");
    await screen.findByRole("heading", { name: "检测记录" }, WAIT);

    // 别处（例如另一场检测）落了两条会话；这次快照先挂起，看工作台在等待期间画什么。
    state.sessions = 2;
    let release;
    const gate = new Promise((resolve) => { release = resolve; });
    const originalSnapshot = adapter.snapshot;
    adapter.snapshot = async () => {
      await gate;
      return originalSnapshot();
    };

    click("工作台");
    expect(await screen.findByRole("heading", { name: "工作台" }, WAIT)).toBeVisible();
    expect(screen.getByText("待上传 0 条")).toBeVisible();
    expect(screen.queryByText("正在连接采集服务…")).not.toBeInTheDocument();

    release();
    await expectHubCounts(2);
  }, 15000);

  it("已在工作台时再点「工作台」也会重拉", async () => {
    const { adapter, state, calls } = countingSidecar();
    render(<TerminalApp adapter={adapter} />);
    await expectHubCounts(0);
    const before = calls.filter((c) => c.method === "snapshot").length;
    state.sessions = 3;
    click("工作台");
    await expectHubCounts(3);
    expect(calls.filter((c) => c.method === "snapshot").length).toBe(before + 1);
  });
});

const ATTENTION_SNAPSHOT = {
  ...SNAPSHOT,
  source: "ble",
  deviceSummary: { ready: false, issues: ["左脚/右脚电量读不到"] },
};

describe("B2：重新检查设备失败要说出来", () => {
  it("失败时在工作台给出原因与可操作的建议，下一次成功后清掉", async () => {
    let attempts = 0;
    const { adapter, calls } = countingSidecar({
      snapshot: ATTENTION_SNAPSHOT,
      recheckDevices: () => {
        attempts += 1;
        if (attempts === 1) {
          return fail({ code: "E-BLE-1001", domain: "E-BLE", message: "没有找到左脚模块。", action: "请确认模块已开机", blocking: true });
        }
        return SNAPSHOT;
      },
    });
    render(<TerminalApp adapter={adapter} preview />);
    fireEvent.click(await screen.findByRole("button", { name: "重新检查设备" }, WAIT));

    const banner = await screen.findByRole("status", { name: "重新检查设备失败" }, WAIT);
    expect(banner).toHaveTextContent(
      "重新检查设备失败：没有找到左脚模块。请确认模块已开机。请确认两个模块已开机并在附近；也可在菜单「预览」中切换到演示模式。",
    );
    expect(banner).not.toHaveTextContent("。。");
    // 失败不挡工作台：按钮还在，可以再点
    const again = screen.getByRole("button", { name: "重新检查设备" });
    expect(again).toBeEnabled();

    // 第二次成功：快照变为就绪，提示消失
    adapter.snapshot = async () => ({ ...SNAPSHOT, deviceSummary: { ready: true, issues: [] }, recentRecords: [], uploadSummary: { pending: 0 } });
    fireEvent.click(again);
    expect(await screen.findByRole("button", { name: "开始新的检测" }, WAIT)).toBeVisible();
    expect(screen.queryByRole("status", { name: "重新检查设备失败" })).not.toBeInTheDocument();
    expect(calls.filter((c) => c.method === "recheckDevices")).toHaveLength(2);
  }, 15000);

  it("重查进行中按钮显示忙碌且禁用，连点不叠加请求", async () => {
    let finish;
    const { adapter, calls } = countingSidecar({
      snapshot: ATTENTION_SNAPSHOT,
      recheckDevices: () => new Promise((resolve) => { finish = () => resolve(fail({ code: "E-BLE-1001", domain: "E-BLE", message: "没有找到左脚模块。", action: "请把左脚模块靠近终端。", blocking: true })); }),
    });
    render(<TerminalApp adapter={adapter} />);
    fireEvent.click(await screen.findByRole("button", { name: "重新检查设备" }, WAIT));

    const busy = await screen.findByRole("button", { name: "正在重新检查…" }, WAIT);
    expect(busy).toBeDisabled();
    expect(busy).toHaveAttribute("aria-busy", "true");
    fireEvent.click(busy);
    fireEvent.click(busy);
    expect(calls.filter((c) => c.method === "recheckDevices")).toHaveLength(1);

    finish();
    const banner = await screen.findByRole("status", { name: "重新检查设备失败" }, WAIT);
    // 非预览（mock / 无外壳）时不提并不存在的「预览」菜单
    expect(banner).toHaveTextContent("重新检查设备失败：没有找到左脚模块。请把左脚模块靠近终端。请确认两个模块已开机并在附近。");
    expect(banner).not.toHaveTextContent("预览");
    expect(screen.getByRole("button", { name: "重新检查设备" })).toBeEnabled();
  }, 15000);

  it("提示可以手动关闭", async () => {
    const { adapter } = countingSidecar({
      snapshot: ATTENTION_SNAPSHOT,
      recheckDevices: () => { throw new Error("传输断开"); },
    });
    render(<TerminalApp adapter={adapter} />);
    fireEvent.click(await screen.findByRole("button", { name: "重新检查设备" }, WAIT));
    const banner = await screen.findByRole("status", { name: "重新检查设备失败" }, WAIT);
    fireEvent.click(within(banner).getByRole("button", { name: "关闭" }));
    expect(screen.queryByRole("status", { name: "重新检查设备失败" })).not.toBeInTheDocument();
  });

  it("sidecar 进程不在时，说明用主进程给的原话且不重复句号", () => {
    const down = new SidecarDown({ message: "采集服务已停止，正在自动重新启动。", action: "请稍候。", recoverable: true });
    expect(recheckFailureMessage(down, true)).toBe(
      "重新检查设备失败：采集服务已停止，正在自动重新启动。请确认两个模块已开机并在附近；也可在菜单「预览」中切换到演示模式。",
    );
    expect(recheckFailureMessage(undefined)).toBe("重新检查设备失败：未知原因。请确认两个模块已开机并在附近。");
  });
});
