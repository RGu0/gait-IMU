/**
 * V-U4 的「sidecar 崩溃时 UI 有明确、可执行的表现」那一半。
 *
 * 判定全在监管器里，所以这里能把它测完：真的起进程、真的杀掉、真的看它重启。
 * 不需要窗口 —— 这正是监管器不 import electron 的目的。
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { fileURLToPath } from "node:url";
import path from "node:path";

import {
  MAX_CONSECUTIVE_RESTARTS,
  SIDECAR_READY,
  SIDECAR_RESTARTING,
  SIDECAR_UNAVAILABLE,
  SidecarSupervisor,
  SidecarUnavailable,
} from "../sidecarSupervisor.js";

const FAKE = fileURLToPath(new URL("./fakeSidecar.mjs", import.meta.url));
const HERE = path.dirname(FAKE);

let live = [];

function supervise(mode = "ok", extra = {}) {
  const supervisor = new SidecarSupervisor({
    command: process.execPath,
    args: [FAKE, mode],
    cwd: HERE,
    requestTimeoutMs: 2_000,
    ...extra,
  });
  live.push(supervisor);
  return supervisor;
}

/** 等一个状态出现；超时就失败，而不是永远挂着。 */
function waitForState(supervisor, wanted, timeoutMs = 8_000) {
  return new Promise((resolve, reject) => {
    if (supervisor.state === wanted) return resolve({ state: wanted });
    const timer = setTimeout(
      () => reject(new Error(`等 ${wanted} 超时，停在 ${supervisor.state}`)),
      timeoutMs,
    );
    const listener = (payload) => {
      if (payload.state !== wanted) return;
      clearTimeout(timer);
      supervisor.off("state", listener);
      resolve(payload);
    };
    supervisor.on("state", listener);
  });
}

afterEach(async () => {
  await Promise.all(live.map((s) => s.stop()));
  live = [];
});

describe("正常生命周期", () => {
  it("起来之后进入 ready 并能往返", async () => {
    const supervisor = supervise().start();
    await waitForState(supervisor, SIDECAR_READY);
    const response = await supervisor.request({ kind: "request", method: "snapshot" });
    expect(response.status).toBe("ok");
    expect(response.result.echoed).toBe("snapshot");
  });

  it("转发 sidecar 推来的事件", async () => {
    const supervisor = supervise().start();
    await waitForState(supervisor, SIDECAR_READY);
    const seen = new Promise((resolve) => supervisor.once("event", resolve));
    supervisor.request({ kind: "request", method: "emit" }).catch(() => {});
    const event = await seen;
    expect(event.topic).toBe("session.tick");
    expect(event.payload.remainingSeconds).toBe(7);
  });
});

describe("V-U4：崩溃时 UI 拿到明确、可执行的表现", () => {
  it("在飞的请求当场失败，而不是一直等", async () => {
    // 最糟的失败方式是界面看起来只是慢：操作员会一直等下去。
    const supervisor = supervise().start();
    await waitForState(supervisor, SIDECAR_READY);
    await expect(supervisor.request({ kind: "request", method: "die" })).rejects.toBeInstanceOf(
      SidecarUnavailable,
    );
  });

  it("失败说明带现象与动作", async () => {
    const supervisor = supervise().start();
    await waitForState(supervisor, SIDECAR_READY);
    const error = await supervisor
      .request({ kind: "request", method: "die" })
      .catch((caught) => caught);
    expect(error.notice.message).toMatch(/采集服务/);
    expect(error.notice.action.trim()).not.toBe("");
  });

  it("进程级失败**不带六域错误码**", async () => {
    // 六域说的是采集现场出了什么事；sidecar 进程没了不是其中任何一种。
    // 编一个 E-BLE 会在日志里造出一个查无此事的设备故障 —— 与 sidecar 那侧
    // `_fatal` 拒绝给协议层失败编码是同一条理由。
    const supervisor = supervise().start();
    await waitForState(supervisor, SIDECAR_READY);
    const error = await supervisor
      .request({ kind: "request", method: "die" })
      .catch((caught) => caught);
    expect(error.notice).not.toHaveProperty("code");
    expect(JSON.stringify(error.notice)).not.toMatch(/E-(BLE|WEAR|CAL|SYNC|QLT|NET)/);
  });

  it("sidecar 不在时请求立刻被拒，不排队", async () => {
    const supervisor = supervise("crash-on-start").start();
    await waitForState(supervisor, SIDECAR_RESTARTING);
    await expect(supervisor.request({ kind: "request", method: "snapshot" })).rejects.toBeInstanceOf(
      SidecarUnavailable,
    );
  });

  it("崩溃后自动重启并恢复到 ready", async () => {
    const supervisor = supervise().start();
    await waitForState(supervisor, SIDECAR_READY);
    supervisor.request({ kind: "request", method: "die" }).catch(() => {});
    await waitForState(supervisor, SIDECAR_RESTARTING);
    await waitForState(supervisor, SIDECAR_READY);
    const response = await supervisor.request({ kind: "request", method: "snapshot" });
    expect(response.status).toBe("ok");
  });
});

describe("反复崩溃时停下来，而不是一直闪", () => {
  it(`连续 ${MAX_CONSECUTIVE_RESTARTS} 次之后进入 unavailable`, async () => {
    // 无限重启比不重启更糟：界面会在「正在恢复」和「失败」之间闪烁，
    // 而操作员看到的是一个一直在动、永远不好的东西。
    const supervisor = supervise("crash-on-start").start();
    const payload = await waitForState(supervisor, SIDECAR_UNAVAILABLE, 15_000);
    expect(payload.notice.recoverable).toBe(false);
    expect(payload.notice.action).toMatch(/联系服务方/);
  });

  it("活得够久的一次运行会把连续计数清零", () => {
    // 一个跑了两小时才崩的 sidecar，不该和一个起来就死的共用计数器：
    // 那会让「一整天里第三次」被当成「三秒内第三次」。
    let clock = 0;
    const supervisor = new SidecarSupervisor({
      command: process.execPath,
      args: [FAKE],
      cwd: HERE,
      now: () => clock,
    });
    supervisor.restarts = 2;
    supervisor._startedAt = 0;
    clock = 60_000; // 活了一分钟
    supervisor._died({ reason: "test", code: 9, signal: null });
    expect(supervisor.restarts).toBe(1); // 清零后这次崩溃算第一次
    expect(supervisor.state).toBe(SIDECAR_RESTARTING);
  });
});

describe("真实 Python sidecar", () => {
  it("经监管器往返一次，并在被杀后恢复", async () => {
    // 假 sidecar 验的是监管逻辑；这条验的是监管器和真的那个说得上话。
    const repoRoot = fileURLToPath(new URL("../../../../", import.meta.url));
    const supervisor = new SidecarSupervisor({
      command: "uv",
      args: ["run", "--locked", "python", "-m", "gait.app"],
      cwd: repoRoot,
      env: { ...process.env, UV_NO_CONFIG: "1", PYTHONUTF8: "1" },
      requestTimeoutMs: 60_000,
    });
    live.push(supervisor);
    supervisor.start();
    await waitForState(supervisor, SIDECAR_READY, 30_000);

    const described = await supervisor.request({ kind: "request", method: "describe", params: {} });
    expect(described.status).toBe("ok");
    expect(described.result.ipc_contract_version).toBeTruthy();

    // 杀掉它，看监管器把它接回来。
    const pid = supervisor.child.pid;
    process.kill(pid, "SIGKILL");
    await waitForState(supervisor, SIDECAR_RESTARTING, 15_000);
    await waitForState(supervisor, SIDECAR_READY, 60_000);

    const again = await supervisor.request({ kind: "request", method: "describe", params: {} });
    expect(again.status).toBe("ok");
  }, 120_000);
});
