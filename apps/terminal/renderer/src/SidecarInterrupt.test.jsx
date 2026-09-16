/**
 * RAY-493 preview-rc2-fixes（装机 A5 §6-1）：采集中 sidecar 进程没了。
 *
 * 装机 app 上走到第 8 秒 `kill -9` sidecar：主进程 309 ms 就重启好了，界面回到采集页
 * 接着倒计时（步数冻结），走满后向**新**进程 stopSession，拿回「会话尚未开始」。
 * 这里钉住：进程在检测中（或收尾中）离开过 ready，这场检测就结束了 —— 倒计时停，
 * 不向新进程收尾，给一屏说实话的中断，并能回工作台或回自检重测。
 */
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { TerminalApp, WALK_INTERRUPTED } from "./TerminalApp.jsx";
import { createSidecarAdapter } from "./sidecarTerminalAdapter.js";
import {
  CREATE_SUBJECT,
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

const RESTARTING = {
  state: "restarting",
  notice: { message: "采集服务已停止，正在自动重新启动。", action: "请稍候。", recoverable: true },
};
const NOT_STARTED = { code: "E-QLT-5000", domain: "E-QLT", message: "协议层失败：会话尚未开始", action: "请重新检测。", blocking: true };

/** 真 sidecar 形状的假进程 + 一个可以手动推状态的生命周期。 */
function rig(overrides = {}, { intercept } = {}) {
  const fake = fakeTransport({
    snapshot: SNAPSHOT,
    listRecords: RECORDS,
    createSubject: CREATE_SUBJECT,
    runPreflight: PREFLIGHT_WAIVED,
    startSession: START_SESSION,
    stopSession: STOP_SESSION,
    sessionResult: SESSION_RESULT_READY,
    // 报告本身不是这里要测的；被拒最省事，也让「收尾走完」在屏上看得见。
    reportFor: fail(REPORT_NO_CYCLES),
    ...overrides,
  });
  // 自己记账：被 intercept 挂住或替换的请求也要算「发出去了」。
  const sent = [];
  const transport = async (request) => {
    sent.push(request.method);
    const replaced = intercept ? await intercept(request, fake) : undefined;
    return replaced ?? fake.transport(request);
  };
  const adapter = createSidecarAdapter(transport, { now: () => 100 });
  let push = () => {};
  const lifecycle = {
    subscribe: (handler) => {
      push = handler;
      handler({ state: "ready" });
      return () => {};
    },
  };
  const setState = (next) => act(() => push(next));
  const methods = () => [...sent];
  return { adapter, lifecycle, setState, methods };
}

const click = (name) => fireEvent.click(screen.getByRole("button", { name }));
const tickAll = () => screen.getAllByRole("checkbox").forEach((box) => fireEvent.click(box));

async function walkToRun() {
  fireEvent.click(await screen.findByRole("button", { name: "开始新的检测" }));
  fireEvent.click(await screen.findByRole("button", { name: "无编号，快速建档" }));
  fireEvent.click(await screen.findByRole("button", { name: "跳过" }));
  await screen.findByRole("heading", { name: "数据授权" });
  tickAll();
  click("同意并继续");
  await screen.findByRole("heading", { name: "安全确认与设备自检" });
  tickAll();
  await screen.findByRole("heading", { name: "佩戴引导" }, { timeout: 5000 });
  tickAll();
  click("佩戴完成，开始标定");
  await screen.findByRole("heading", { name: "确认左右" });
  tickAll();
  click("确认无误，开始检测");
  return screen.findByLabelText("操作员侧栏", {}, { timeout: 5000 });
}

async function expectInterruptionScreen() {
  const alert = await screen.findByRole("alert", { name: WALK_INTERRUPTED.title }, { timeout: 5000 });
  expect(alert).toHaveTextContent("采集服务中断，本次检测已中断");
  expect(alert).toHaveTextContent("数据已尽可能保存；请回到工作台重新检测");
  // 不是那句误导的协议错误，也不是「报告未能生成」
  expect(alert).not.toHaveTextContent("会话尚未开始");
  expect(screen.queryByRole("alert", { name: "报告未能生成" })).toBeNull();
  expect(screen.getByRole("button", { name: "返回工作台" })).toBeVisible();
  expect(screen.getByRole("button", { name: "重新检测" })).toBeVisible();
  return alert;
}

describe("采集中 sidecar 重启：本次检测已中断", () => {
  it(
    "重启中接管，回到 ready 后是中断屏；倒计时停了，不向新进程收尾",
    async () => {
      // 1 秒的会话：若倒计时还在走，1 s + 3 s 站定后就会触发 onFinish → stopSession。
      const { adapter, lifecycle, setState, methods } = rig({
        startSession: { ...START_SESSION, totalSeconds: 1, remainingSeconds: 1 },
      });
      render(<TerminalApp adapter={adapter} lifecycle={lifecycle} />);
      await walkToRun();
      expect(screen.getByText(/剩余 00:0/)).toBeInTheDocument();

      setState(RESTARTING);
      expect(screen.getByRole("alert")).toHaveTextContent("正在自动重新启动");
      setState({ state: "ready" });

      await expectInterruptionScreen();
      expect(screen.queryByText(/剩余/)).toBeNull();
      expect(screen.queryByText("可以停下了")).toBeNull();

      const before = methods().length;
      await new Promise((resolve) => setTimeout(resolve, 4500));
      const after = methods().slice(before);
      expect(after).not.toContain("stopSession");
      expect(after).not.toContain("sessionResult");
      expect(after).not.toContain("reportFor");
      expect(methods()).not.toContain("stopSession");
      // 仍停在中断屏，没有被迟到的收尾改写
      expect(screen.getByRole("alert", { name: WALK_INTERRUPTED.title })).toBeVisible();
    },
    20000,
  );

  it(
    "「返回工作台」回到工作台并重拉快照（记录里那场是「未正常结束」）",
    async () => {
      const { adapter, lifecycle, setState, methods } = rig();
      render(<TerminalApp adapter={adapter} lifecycle={lifecycle} />);
      await walkToRun();
      setState(RESTARTING);
      setState({ state: "ready" });
      await expectInterruptionScreen();
      const snapshotsBefore = methods().filter((m) => m === "snapshot").length;
      click("返回工作台");
      expect(await screen.findByRole("heading", { name: "工作台" }, { timeout: 5000 })).toBeVisible();
      await waitFor(() => expect(methods().filter((m) => m === "snapshot").length).toBeGreaterThan(snapshotsBefore));
      expect(methods()).not.toContain("stopSession");
    },
    20000,
  );

  it(
    "「重新检测」回到自检，受检者保留",
    async () => {
      const { adapter, lifecycle, setState } = rig();
      render(<TerminalApp adapter={adapter} lifecycle={lifecycle} />);
      await walkToRun();
      setState(RESTARTING);
      setState({ state: "ready" });
      await expectInterruptionScreen();
      click("重新检测");
      expect(await screen.findByRole("heading", { name: "安全确认与设备自检" }, { timeout: 5000 })).toBeVisible();
    },
    20000,
  );

  it(
    "进程彻底起不来（unavailable）时先接管，回工作台不留下采集页",
    async () => {
      const { adapter, lifecycle, setState, methods } = rig();
      render(<TerminalApp adapter={adapter} lifecycle={lifecycle} />);
      await walkToRun();
      setState({ state: "unavailable", notice: { message: "采集服务连续 3 次未能启动。", action: "请退出并重新打开应用。", recoverable: false } });
      click("返回工作台");
      expect(screen.queryByLabelText("操作员侧栏")).toBeNull();
      expect(methods()).not.toContain("stopSession");
    },
    20000,
  );
});

describe("收尾中 sidecar 重启：同样是中断，而不是协议错误", () => {
  /** 让某个方法挂住，直到测试放行；放行时给出指定应答。 */
  function gate() {
    let release;
    const opened = new Promise((resolve) => { release = resolve; });
    return { opened, release };
  }

  it(
    "stopSession 在路上时进程重启，新进程回「会话尚未开始」→ 中断屏，不再取结果与报告",
    async () => {
      const g = gate();
      const { adapter, lifecycle, setState, methods } = rig(
        { startSession: { ...START_SESSION, totalSeconds: 0, remainingSeconds: 0 }, stopSession: fail(NOT_STARTED) },
        {
          intercept: async (request, fake) => {
            if (request.method !== "stopSession") return undefined;
            await g.opened;
            return fake.transport(request);
          },
        },
      );
      render(<TerminalApp adapter={adapter} lifecycle={lifecycle} />);
      await walkToRun();
      // 倒计时 0 → 站定 3 s → onFinish → stopSession 挂住
      await waitFor(() => expect(methods()).toContain("stopSession"), { timeout: 6000 });
      setState(RESTARTING);
      setState({ state: "ready" });
      await act(async () => g.release());
      await expectInterruptionScreen();
      expect(methods()).not.toContain("sessionResult");
      expect(methods()).not.toContain("reportFor");
    },
    20000,
  );

  it(
    "sessionResult 以 SidecarDown 失败（生命周期事件还没到）→ 中断屏，不取报告",
    async () => {
      const { adapter, lifecycle, methods } = rig(
        { startSession: { ...START_SESSION, totalSeconds: 0, remainingSeconds: 0 } },
        {
          intercept: async (request) =>
            request.method === "sessionResult"
              ? { kind: "response", id: request.id, status: "error", sidecarUnavailable: RESTARTING.notice }
              : undefined,
        },
      );
      render(<TerminalApp adapter={adapter} lifecycle={lifecycle} />);
      await walkToRun();
      await expectInterruptionScreen();
      expect(methods()).toContain("stopSession");
      expect(methods()).not.toContain("reportFor");
    },
    20000,
  );

  it(
    "没有进程出事时收尾照常：结果与报告都取（回归）",
    async () => {
      const { adapter, lifecycle, methods } = rig({ startSession: { ...START_SESSION, totalSeconds: 0, remainingSeconds: 0 } });
      render(<TerminalApp adapter={adapter} lifecycle={lifecycle} />);
      await walkToRun();
      await screen.findByRole("alert", { name: "报告未能生成" }, { timeout: 8000 });
      expect(methods()).toEqual(expect.arrayContaining(["stopSession", "sessionResult", "reportFor"]));
      expect(screen.queryByRole("alert", { name: WALK_INTERRUPTED.title })).toBeNull();
    },
    20000,
  );
});
