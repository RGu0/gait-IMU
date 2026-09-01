/**
 * 跨语言、跨进程的真实往返。
 *
 * 上面那份 contract.test.js 验的是「渲染端这一侧按契约行事」；这一份验的是**两侧
 * 真的能对上话** —— 它起一个真实的 Python sidecar 进程，用真实的 adapter 驱动它，
 * 断言的是从 `device/orchestration.py`、`protocolflow/timed_walk.py` 里推出来的结论，
 * 全程不经 mockTerminalAdapter。
 *
 * 这是 RAY-248 验收里「至少一条真实数据通路端到端可跑」的那条通路。它之所以值得
 * 单独存在，是因为前一份测试即使全绿，两侧也可能各自自洽而互相说不通 —— 而那种
 * 不一致只会在打包之后第一次被发现（RAY-319：不需要硬件就能验的东西，不该靠上机
 * 来发现）。
 */
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";

import { createSidecarAdapter } from "../../../apps/terminal/renderer/src/sidecarTerminalAdapter.js";

const REPO_ROOT = fileURLToPath(new URL("../../../", import.meta.url));

/** 起一个真进程，并把它包成 adapter 要的 transport。 */
function startSidecar() {
  const child = spawn("uv", ["run", "--locked", "python", "-m", "gait.app"], {
    cwd: REPO_ROOT,
    // UV_NO_CONFIG：本机的 uv 镜像配置会让 uv 报一个假的 lockfile 陈旧错误。
    env: { ...process.env, UV_NO_CONFIG: "1", PYTHONUTF8: "1" },
    stdio: ["pipe", "pipe", "pipe"],
  });

  let buffer = "";
  const waiting = [];
  let stderr = "";

  child.stderr.setEncoding("utf8");
  child.stderr.on("data", (chunk) => {
    stderr += chunk;
  });

  child.stdout.setEncoding("utf8");
  child.stdout.on("data", (chunk) => {
    buffer += chunk;
    let index;
    while ((index = buffer.indexOf("\n")) >= 0) {
      const line = buffer.slice(0, index).trim();
      buffer = buffer.slice(index + 1);
      if (!line) continue;
      const resolve = waiting.shift();
      if (resolve) resolve(JSON.parse(line));
    }
  });

  const transport = (request) =>
    new Promise((resolve, reject) => {
      const timer = setTimeout(
        () => reject(new Error(`sidecar 超时未回应 ${request.method}；stderr=${stderr}`)),
        30_000,
      );
      waiting.push((message) => {
        clearTimeout(timer);
        resolve(message);
      });
      child.stdin.write(`${JSON.stringify(request)}\n`);
    });

  return { child, transport, stderrText: () => stderr };
}

describe("真实 sidecar 往返（不经 mock）", () => {
  let sidecar;
  let adapter;

  beforeEach(() => {
    sidecar = startSidecar();
    adapter = createSidecarAdapter(sidecar.transport);
  });

  afterEach(() => {
    sidecar.child.stdin.end();
    sidecar.child.kill();
  });

  it("两侧报出同一个 IPC 契约版本", async () => {
    const described = await adapter.describe();
    const { IPC_CONTRACT_VERSION } = await import("../index.js");
    expect(described.ipc_contract_version).toBe(IPC_CONTRACT_VERSION);
  });

  it("自检的结论来自真实的三态电量准入", async () => {
    const items = await adapter.runPreflight();
    const battery = items.find((item) => item.id === "battery");
    expect(battery.status).toBe("pass");
    // 到达率、磁盘、出厂标定也都是推出来的，不是写死的
    expect(items.map((item) => item.id)).toEqual(
      expect.arrayContaining(["link-l", "link-r", "factory-cal", "disk", "battery", "arrival"]),
    );
  });

  it("计时与会话判定走真实的 TimedWalk", async () => {
    await adapter.call("startSession", { now: 0 });
    await adapter.call("stopSession", { now: 200 });
    const result = await adapter.sessionResult({ wearing: "pass" });
    expect(result.overall).toBe("valid");
    expect(result.verdict.duration).toBe("pass");
  });

  it("佩戴未裁定时会话判定是「评不了」，不是「通过」", async () => {
    await adapter.call("startSession", { now: 0 });
    await adapter.call("stopSession", { now: 200 });
    const result = await adapter.sessionResult();
    expect(result.overall).toBe("indeterminate");
  });

  it("标定与报告以缺口出境，adapter 不把它们当结果", async () => {
    const calibration = await adapter.runCalibration();
    expect(adapter.gapOf(calibration)).toMatchObject({ capability: "calibration", issue: "RAY-208" });

    const report = await adapter.reportFor({ id: "whatever" });
    expect(adapter.gapOf(report)).toMatchObject({ capability: "report", issue: "RAY-224" });
  });

  it("错误带着 sidecar 给的码与动作到达渲染端", async () => {
    // 走一条真实的短步行：180 s 配置下只走 30 s，有效时长不到 70%，
    // 于是 TimedWalk.verdict() 判 invalid，服务端附上 E-QLT-5002。
    // 这条路的错误码与文案全部由 sidecar 推出，渲染端一个字也没写。
    await adapter.call("startSession", { now: 0 });
    await adapter.call("stopSession", { now: 30 });
    const result = await adapter.sessionResult({ wearing: "pass" });

    expect(result.overall).toBe("invalid");
    expect(result.error.code).toBe("E-QLT-5002");
    expect(result.error.action).toMatch(/重新检测/);
    expect(result.error.message).toMatch(/70%/);
  });

  it("登录没有后端，因此以缺口出境而不是一个假的通过", async () => {
    // 「账号密码非空就放行」等于没有认证 —— 那是在假装一个后端存在。
    const outcome = await adapter.login({ organization: "康健社区卫生服务中心", password: "x" });
    expect(adapter.gapOf(outcome)).toMatchObject({ capability: "operator-auth", issue: null });
  });
});
