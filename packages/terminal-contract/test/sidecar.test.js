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
  const child = spawn("uv", ["run", "--no-sync", "python", "-m", "gait.app"], {
    cwd: REPO_ROOT,
    // UV_NO_CONFIG：本机的 uv 镜像配置会让 uv 报一个假的 lockfile 陈旧错误。
    env: { ...process.env, UV_NO_CONFIG: "1", PYTHONUTF8: "1" },
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
      const pending = waiting.shift();
      if (pending) pending.resolve(JSON.parse(line));
    }
  });

  child.on("error", (error) => fail(new Error(`sidecar 起不来：${error.message}`)));
  child.on("exit", (code, signal) => {
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

  it("标定仍以缺口出境，报告则已实现", async () => {
    const calibration = await adapter.runCalibration();
    expect(adapter.gapOf(calibration)).toMatchObject({ capability: "calibration", issue: "RAY-208" });

    // report 已实现（RAY-224 `basic-report`）：没有周期时它给的是一个**错误**
    // 而不是缺口 —— 两者在契约上是不同的结局，这条断言把它们分开。
    await expect(adapter.reportFor({ id: "whatever" })).rejects.toMatchObject({
      name: "TerminalFailure",
      code: "E-QLT-5003",
    });
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
