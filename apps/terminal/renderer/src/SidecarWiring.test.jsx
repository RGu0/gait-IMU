/**
 * RAY-493：整条操作员流程跑在**真 sidecar 形状**上。
 *
 * 其他 TerminalApp 测试用的是 mock 形状（视图形状、同步 startSession），这正是流程在
 * 浏览器里走得通、接上真 sidecar 就一步一崩的原因。这里用 `createSidecarAdapter`
 * 包一个回放真实应答的假传输，让测试看见的就是真 sidecar 会给的东西。
 */
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { ErrorBoundary } from "./ErrorBoundary.jsx";
import { TerminalApp } from "./TerminalApp.jsx";
import { createSidecarAdapter } from "./sidecarTerminalAdapter.js";
import {
  CREATE_SUBJECT,
  PREFLIGHT,
  PREFLIGHT_WAIVED,
  RECORDS,
  REPORT_NO_CYCLES,
  SESSION_RESULT_READY,
  SNAPSHOT,
  START_SESSION,
  STOP_SESSION,
  fail,
  fakeTransport,
} from "./testing/sidecarFixtures.js";

function sidecar(overrides = {}, { events } = {}) {
  const fake = fakeTransport({
    snapshot: SNAPSHOT,
    listRecords: RECORDS,
    recheckDevices: SNAPSHOT,
    createSubject: CREATE_SUBJECT,
    runPreflight: PREFLIGHT_WAIVED,
    startSession: START_SESSION,
    stopSession: STOP_SESSION,
    sessionResult: SESSION_RESULT_READY,
    reportFor: fail(REPORT_NO_CYCLES),
    ...overrides,
  });
  const adapter = createSidecarAdapter(fake.transport, { now: () => 100, events });
  return { adapter, calls: fake.calls };
}

const click = (name) => fireEvent.click(screen.getByRole("button", { name }));
const tickAll = () => screen.getAllByRole("checkbox").forEach((box) => fireEvent.click(box));

/** 工作台 → 快速建档 → 跳过档案 → 授权 → 自检（等它自己前进）→ 佩戴 → 确认左右 → 开始。 */
async function walkToRun() {
  fireEvent.click(await screen.findByRole("button", { name: "开始新的检测" }));
  fireEvent.click(await screen.findByRole("button", { name: "无编号，快速建档" }));
  fireEvent.click(await screen.findByRole("button", { name: "跳过" }));
  await screen.findByRole("heading", { name: "数据授权" });
  expect(screen.getByText(/临时-E8E0/)).toBeInTheDocument();
  tickAll();
  click("同意并继续");
  await screen.findByRole("heading", { name: "安全确认与设备自检" });
  tickAll();
  await screen.findByRole("heading", { name: "佩戴引导" }, { timeout: 3000 });
  tickAll();
  click("佩戴完成，开始标定");
  await screen.findByRole("heading", { name: "确认左右" });
  tickAll();
  click("确认无误，开始检测");
  return screen.findByLabelText("操作员侧栏");
}

describe("真 sidecar 形状上的工作台", () => {
  it("快照没有 recentRecords / pending 也能画出工作台，并显示预览条", async () => {
    const { adapter } = sidecar({ snapshot: { ...SNAPSHOT, source: "stub" } });
    render(<TerminalApp adapter={adapter} preview />);
    expect(await screen.findByRole("heading", { name: "工作台" })).toBeVisible();
    expect(screen.getByText("待上传 2 条")).toBeVisible();
    const table = screen.getByRole("table");
    expect(within(table).getByText("临时-E12B")).toBeVisible();
    const banner = screen.getByRole("note", { name: "预览版提示" });
    expect(banner).toHaveTextContent("预览版");
    await waitFor(() => expect(banner).toHaveTextContent("演示数据，非实测"));
  });

  it("数据来源是真机时只说预览版", async () => {
    const { adapter } = sidecar({ snapshot: { ...SNAPSHOT, source: "ble" } });
    render(<TerminalApp adapter={adapter} preview />);
    await screen.findByRole("heading", { name: "工作台" });
    const banner = screen.getByRole("note", { name: "预览版提示" });
    expect(banner).toHaveTextContent("预览版");
    expect(banner).not.toHaveTextContent("非实测");
  });

  it("快照失败会每隔一段时间重试，直到拿到为止", async () => {
    let attempts = 0;
    const { adapter } = sidecar({
      snapshot: () => {
        attempts += 1;
        if (attempts < 3) throw new Error("sidecar 还没起来");
        return SNAPSHOT;
      },
    });
    render(<TerminalApp adapter={adapter} snapshotRetryMs={20} />);
    expect(screen.getByRole("status")).toHaveTextContent("正在连接采集服务");
    expect(await screen.findByRole("heading", { name: "工作台" })).toBeVisible();
    expect(attempts).toBe(3);
  });

  it("sidecar 生命周期回到 ready 时重拉快照", async () => {
    let handler;
    const lifecycle = { subscribe: (h) => { handler = h; return () => {}; } };
    const snapshot = vi.fn().mockResolvedValue({ ...SNAPSHOT, recentRecords: [] });
    render(<TerminalApp adapter={{ snapshot }} lifecycle={lifecycle} />);
    await screen.findByRole("heading", { name: "工作台" });
    expect(snapshot).toHaveBeenCalledTimes(1);
    act(() => handler({ state: "restarting" }));
    act(() => handler({ state: "ready" }));
    await waitFor(() => expect(snapshot).toHaveBeenCalledTimes(2));
  });

  it("记录页与设备页读得了 sidecar 的形状", async () => {
    const { adapter } = sidecar({
      deviceSupport: { modules: [{ side: "left", maskedAddress: "…:9A:4C", batteryPercent: null, factoryCalibrated: true }, { side: "right", maskedAddress: "…:9A:51", batteryPercent: 76, factoryCalibrated: false }], ipcContractVersion: "1.0" },
    });
    render(<TerminalApp adapter={adapter} />);
    await screen.findByRole("heading", { name: "工作台" });
    click("检测记录");
    await screen.findByRole("heading", { name: "检测记录" });
    const table = screen.getByRole("table");
    expect(within(table).getByText("不完整")).toBeVisible();
    expect(within(table).getAllByText("未统计")).toHaveLength(2);
    click("设备与支持");
    expect(await screen.findByRole("heading", { name: "设备与支持" })).toBeVisible();
    expect(screen.getByText("电量未读取")).toBeVisible();
  });
});

describe("真 sidecar 形状上的检测流程", () => {
  it("自检里失败项的说明来自 error，不是空白", async () => {
    const { adapter } = sidecar({ runPreflight: PREFLIGHT });
    render(<TerminalApp adapter={adapter} />);
    fireEvent.click(await screen.findByRole("button", { name: "开始新的检测" }));
    fireEvent.click(await screen.findByRole("button", { name: "无编号，快速建档" }));
    fireEvent.click(await screen.findByRole("button", { name: "跳过" }));
    await screen.findByRole("heading", { name: "数据授权" });
    tickAll();
    click("同意并继续");
    await screen.findByRole("heading", { name: "安全确认与设备自检" });
    tickAll();
    expect(await screen.findByText(/有模块没有匹配到出厂标定参数。（E-CAL-3001）/)).toBeVisible();
    expect(screen.getByRole("button", { name: "重新检查" })).toBeVisible();
  });

  it(
    "异步 startSession 进入采集页并带上受检者；报告被拒时给出错误屏，可回工作台",
    async () => {
      let emit = () => {};
      let seq = 2;
      const events = (handler) => { emit = handler; return () => { emit = () => {}; }; };
      // 真实的 180 秒会话：结束靠 sidecar 的 tick 说剩余 0，而不是本地计时器走完。
      const { adapter, calls } = sidecar({}, { events });
      render(<TerminalApp adapter={adapter} />);
      const sidebar = await walkToRun();

      const start = calls.find((c) => c.method === "startSession");
      expect(start.params.subjectUuid).toBe(CREATE_SUBJECT.subjectUuid);

      // 事件接到采集页上
      act(() => emit({ kind: "event", v: "1.0", topic: "session.tick", seq: 1, payload: { remainingSeconds: 90.4, steps: { left: 7, right: 6 }, link: { left: "good", right: "fair" } } }));
      expect(within(sidebar).getByText("7")).toBeInTheDocument();
      expect(screen.getByText(/剩余 01:/)).toBeInTheDocument();

      // sidecar 不会自己停：倒计时到 0 之后它继续推 remainingSeconds: 0，直到渲染端 stopSession
      act(() => emit({ kind: "event", v: "1.0", topic: "session.tick", seq: 2, payload: { remainingSeconds: 0, steps: { left: 8, right: 8 }, link: { left: "good", right: "good" } } }));
      expect(await screen.findByText("可以停下了")).toBeVisible();
      const keepTicking = setInterval(() => {
        seq += 1;
        act(() => emit({ kind: "event", v: "1.0", topic: "session.tick", seq, payload: { remainingSeconds: 0, steps: { left: 8, right: 8 }, link: { left: "good", right: "good" } } }));
      }, 200);

      // 倒计时 0 → 站定 3 秒 → onFinish → stop → result → reportFor 被拒
      const alert = await screen.findByRole("alert", { name: "报告未能生成" }, { timeout: 6000 });
      expect(alert).toHaveTextContent(REPORT_NO_CYCLES.message);
      expect(alert).toHaveTextContent(REPORT_NO_CYCLES.action);
      expect(alert).toHaveTextContent("E-QLT-5003");
      clearInterval(keepTicking);
      const methods = calls.map((c) => c.method);
      expect(methods.filter((m) => m === "stopSession")).toHaveLength(1);
      expect(methods.filter((m) => m === "reportFor")).toHaveLength(1);
      expect(methods).toEqual(expect.arrayContaining(["sessionResult"]));
      expect(screen.getByRole("button", { name: "重新检测" })).toBeVisible();

      click("返回工作台");
      expect(await screen.findByRole("heading", { name: "工作台" })).toBeVisible();
    },
    15000,
  );

  it("报告被拒后「重新检测」回到自检", async () => {
    const { adapter } = sidecar({ startSession: { ...START_SESSION, totalSeconds: 0, remainingSeconds: 0 } });
    render(<TerminalApp adapter={adapter} />);
    await walkToRun();
    await screen.findByRole("alert", { name: "报告未能生成" }, { timeout: 6000 });
    click("重新检测");
    expect(await screen.findByRole("heading", { name: "安全确认与设备自检" })).toBeVisible();
  }, 15000);

  it("操作员停止时让 sidecar 收尾，再回工作台", async () => {
    const stopSession = vi.fn(() => STOP_SESSION);
    const { adapter } = sidecar({ stopSession });
    render(<TerminalApp adapter={adapter} />);
    await walkToRun();
    click("停止检测");
    const dialog = screen.getByRole("alertdialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "停止检测" }));
    expect(await screen.findByRole("heading", { name: "工作台" })).toBeVisible();
    expect(stopSession).toHaveBeenCalledOnce();
  }, 10000);

  it("sidecar 中止后收尾被拒也照样回工作台", async () => {
    let emit = () => {};
    const events = (handler) => { emit = handler; return () => {}; };
    const { adapter } = sidecar({ stopSession: fail(REPORT_NO_CYCLES) }, { events });
    render(<TerminalApp adapter={adapter} />);
    await walkToRun();
    const aborted = { code: "E-BLE-1020", domain: "E-BLE", message: "原始数据写盘失败，测试已安全停止。", action: "请检查磁盘剩余空间后重新检测。", blocking: true };
    act(() => emit({ kind: "event", v: "1.0", topic: "session.aborted", seq: 5, payload: { error: aborted } }));
    // Windows runner 上默认 1 s 等不到（中止 → stopSession 被拒 → 重渲染），放宽等待。
    expect(await screen.findByText(aborted.action, {}, { timeout: 5000 })).toBeVisible();
    click("返回工作台");
    expect(await screen.findByRole("heading", { name: "工作台" }, { timeout: 5000 })).toBeVisible();
  }, 15000);
});

describe("兜底", () => {
  it("渲染抛错时给出信息与回工作台的按钮", () => {
    const onReset = vi.fn();
    const Boom = () => { throw new Error("炸了"); };
    const spy = vi.spyOn(console, "error").mockImplementation(() => {});
    render(<ErrorBoundary onReset={onReset}><Boom /></ErrorBoundary>);
    spy.mockRestore();
    expect(screen.getByRole("alert")).toHaveTextContent("炸了");
    click("返回工作台");
    expect(onReset).toHaveBeenCalledOnce();
  });
});
