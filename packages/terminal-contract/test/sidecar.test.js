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

import os from "node:os";
import fs from "node:fs";
import path from "node:path";
import { createSidecarAdapter } from "../../../apps/terminal/renderer/src/sidecarTerminalAdapter.js";
import { developmentCommand, UV_CONFIG_FILE_NULL } from "../../../apps/terminal/main/sidecarCommand.js";
import { sidecarOptions } from "../../../apps/terminal/main/runtimeConfig.js";

const REPO_ROOT = fileURLToPath(new URL("../../../", import.meta.url));

/**
 * RAY-750 R1：命令与环境经产品的 sidecarOptions(developmentCommand()) 拼出，
 * 不再抄一份 process.env。调用方环境里显式塞进 GAIT_ACCESS_ROOT /
 * GAIT_SESSION_ROOT，并断言它们没有渗进 product env（产品环境是替换式的，
 * PATH 不继承 —— spawn 用解析后的绝对 uv 路径）；developmentCommand 的显式
 * 业务变量（PYTHONUTF8、UV_CONFIG_FILE，见 RAY-483）原样保留。
 */
const DEV_COMMAND = developmentCommand(REPO_ROOT);
// 保留集成测试的 --no-sync（不动 developmentCommand 本身）：--locked 会让每次 spawn
// 都重新解析依赖，把每条用例拖慢约 4 s 且网络一抖就间歇超时。锁文件新鲜度由
// ./dev test / ./dev lint 的 uv run --locked 与 CI 的 uv sync --locked 守，不在这里。
const COMMAND = {
  ...DEV_COMMAND,
  args: DEV_COMMAND.args.map((arg) => (arg === "--locked" ? "--no-sync" : arg)),
};

/** 起一个真进程，并把它包成 adapter 要的 transport。 */
function startSidecar(userDataDir) {
  const { executable: SIDECAR_EXECUTABLE, options: SIDECAR_OPTIONS } = sidecarOptions({
    command: COMMAND,
    settings: {},
    userDataDir,
    processEnv: {
      ...process.env,
      GAIT_ACCESS_ROOT: "/ray-750-must-not-leak",
      GAIT_SESSION_ROOT: "/ray-750-must-not-leak",
    },
    platform: process.platform,
  });
  if (!SIDECAR_EXECUTABLE) {
    throw new Error("PATH 里解析不到 uv；请先在仓库里跑 ./dev setup 再跑本测试。");
  }
  if (
    SIDECAR_OPTIONS.env.GAIT_ACCESS_ROOT !== undefined ||
    SIDECAR_OPTIONS.env.GAIT_SESSION_ROOT === "/ray-750-must-not-leak"
  ) {
    throw new Error("调用方的 GAIT_* 变量渗进了 product env —— 产品环境应当是替换式的。");
  }
  if (SIDECAR_OPTIONS.env.PYTHONUTF8 !== "1" || SIDECAR_OPTIONS.env.UV_CONFIG_FILE !== UV_CONFIG_FILE_NULL) {
    throw new Error("developmentCommand 的显式业务变量没有保留下来。");
  }
  expect(path.isAbsolute(SIDECAR_EXECUTABLE)).toBe(true);
  expect(SIDECAR_OPTIONS.command).toBe(SIDECAR_EXECUTABLE);
  expect(SIDECAR_OPTIONS.env).not.toHaveProperty("PATH");
  expect(SIDECAR_OPTIONS.env.GAIT_SESSION_ROOT).toBe(path.join(userDataDir, "sessions"));

  // `--no-sync` 而不是 `--locked`。`--locked` 会让 uv 在**每一次** `uv run` 上重新
  // 解析依赖，而依赖里的 techflex-cloud-foundation 是一个 GitHub release 资产直链：
  // 它的 HTTP 缓存项每次都判过期，于是每次 spawn 都要向 github.com 发一轮
  // revalidate（302 → release-assets → 304）。实测约 3.8 s，**冷热缓存一模一样**
  // （新建 venv 4.8 s / 复用 venv 5.0 s），`--offline` 则是 0.03 s —— 所以这笔开销
  // 是网络往返，不是建 venv、装包或起解释器。
  //
  // 本文件每条用例都各起一个进程，7 条就是 ~28 s；而单条用例的预算是 5 s，扣掉这
  // 4 s 只剩不到 1 s 余量。网络一抖就超时，且超时落在哪一条纯属随机 —— 这正是它
  // 「间歇失败」的成因。改用 --no-sync 后单次 spawn 到应答约 0.19 s。
  //
  // 锁文件的新鲜度不靠这里守：`./dev test` 与 `./dev lint` 里的 `uv run --locked`
  // 已经守了，CI 还先跑 `./dev setup`（`uv sync --locked`）。这条用例要验的是 IPC
  // 两侧能不能对上话，不是依赖解析。
  // 命令、cwd、env 全部来自产品路径（见文件顶部）；UV_CONFIG_FILE 仍由
  // developmentCommand 带来（RAY-483），不用 UV_NO_CONFIG，它会连 .python-version 一起关掉。
  const child = spawn(SIDECAR_EXECUTABLE, SIDECAR_OPTIONS.args, {
    cwd: SIDECAR_OPTIONS.cwd,
    env: SIDECAR_OPTIONS.env,
    stdio: ["pipe", "pipe", "pipe"],
  });

  let buffer = "";
  const waiting = [];
  let stderr = "";
  let dead = null;

  // 子进程没能应答就死了的时候，立刻把等待中的（和之后来的）请求全部拒绝，并把
  // stderr 带上。否则「sidecar 起不来」只会表现为一次超时 —— 而超时读不出原因，
  // 正是这条用例此前教人重跑而不是读日志的地方。--no-sync 让这条更必要：环境没跑
  // 过 `./dev setup` 时 uv 会给出一个空 venv，`python -m gait.app` 随即以
  // "No module named gait" 退出，现在这句话会直接出现在断言失败里。
  const fail = (error) => {
    dead = error;
    while (waiting.length) waiting.shift().reject(error);
  };

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
      const message = JSON.parse(line);
      // 采集中 sidecar 会主动推事件（RAY-493 事件泵）。本 transport 按「一问一答」
      // 配对，事件不是任何请求的应答 —— 让它占掉一个等待位，后面每条应答都会错位。
      if (message.kind === "event") continue;
      const pending = waiting.shift();
      if (pending) pending.resolve(message);
    }
  });

  child.on("error", (error) => fail(new Error(`sidecar 起不来：${error.message}`)));
  // 用 close 而不是 exit：exit 在进程终止时就触发，此时 stdout 里可能还有没送达的
  // 数据，于是「应答已写出、进程随即退出」会被误判成「没应答就死了」。close 等到
  // stdio 全部关闭才触发，那一刻该收的行都已经收完。今天的 sidecar 是长驻的，不会
  // 边应答边退出，所以这条竞态还够不着；但它只值一个词，而将来若有人让它改成应答
  // 一次就退出，差别就是间歇性假失败。
  child.on("close", (code, signal) => {
    // afterEach 主动 kill 时 signal 非空，那是正常收尾，不是失败。
    if (signal) return;
    fail(new Error(`sidecar 未及应答就退出（code=${code}）；stderr=${stderr}`));
  });

  const transport = (request) =>
    new Promise((resolve, reject) => {
      if (dead) {
        reject(dead);
        return;
      }
      const timer = setTimeout(
        () => reject(new Error(`sidecar 超时未回应 ${request.method}；stderr=${stderr}`)),
        30_000,
      );
      waiting.push({
        resolve: (message) => {
          clearTimeout(timer);
          resolve(message);
        },
        reject: (error) => {
          clearTimeout(timer);
          reject(error);
        },
      });
      child.stdin.write(`${JSON.stringify(request)}\n`);
    });

  return { child, transport, stderrText: () => stderr };
}

describe("真实 sidecar 往返（不经 mock）", () => {
  let sidecar;
  let adapter;
  let userDataDir;

  beforeEach(() => {
    userDataDir = fs.mkdtempSync(path.join(os.tmpdir(), "gait-ray-750-contract-"));
    sidecar = startSidecar(userDataDir);
    adapter = createSidecarAdapter(sidecar.transport);
  });

  afterEach(async () => {
    if (sidecar) {
      const stopped = new Promise((resolve) => {
        if (sidecar.child.exitCode !== null || sidecar.child.signalCode !== null) resolve();
        else sidecar.child.once("close", resolve);
      });
      sidecar.child.stdin.end();
      sidecar.child.kill();
      await stopped;
    }
    if (userDataDir) fs.rmSync(userDataDir, { recursive: true, force: true });
    sidecar = undefined;
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

  it("标定仍以缺口出境，报告则已实现", async () => {
    const calibration = await adapter.runCalibration();
    expect(adapter.gapOf(calibration)).toMatchObject({ capability: "calibration", issue: "RAY-208" });

    // 产品环境配置了真实会话目录；请求一个不存在的会话时，实际读取路径
    // 返回 E-BLE-1021（RAY-345），不是无持久化环境的 E-QLT-5003，也不是能力缺口。
    await expect(adapter.reportFor({ id: "whatever" })).rejects.toMatchObject({
      name: "TerminalFailure",
      code: "E-BLE-1021",
    });
  });

  it("错误带着 sidecar 给的码与动作到达渲染端", async () => {
    // 走一条真实的短步行：产品预览 60 s 配置下只走 30 s，有效时长不到 70%，
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

  it("未预配置的终端登录不了，但给的是原因而不是一个假的通过", async () => {
    // 「账号密码非空就放行」等于没有认证 —— 那是在假装一个后端存在。**罪名不随
    // 实现改变**：RAY-323 接通后这条换了形态，守的东西一个字没变。
    //
    // 这个 sidecar 没有 GAIT_ACCESS_ROOT，所以造不出认证客户端。它必须给一个
    // 说得出原因的错误（E-NET-6043：做了，但这台机器还没配），既不是缺口
    // （那表示能力还没做），也不是一次成功。
    await expect(
      adapter.login({ organization: "康健社区卫生服务中心", password: "x" }),
    ).rejects.toMatchObject({ code: "E-NET-6043" });
  });
});
