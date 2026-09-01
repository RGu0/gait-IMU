/**
 * 真的把主进程跑起来一次，然后退出。
 *
 * ## 为什么这个文件存在
 *
 * `main.js` 只是接线，接线没有单元测试 —— 于是它可以在结构上完全正确、却从来没有
 * 被执行过。这个项目在 RAY-258 上吃过这个亏：`dev.ps1` 经三个 scope 改动仍是零执行，
 * 教训写在那里，「结构正确不等于执行得通」。
 *
 * 所以本文件用真实的 Electron 运行时，走一遍与 `main.js` 完全相同的路：起窗口
 * （不显示）、拉起真实的 Python sidecar、经 IPC 发一条真请求、拿到回应、收工退出。
 * 它验的是这条链**接得通**，不是它的逻辑对不对（逻辑在监管器的单元测试里）。
 *
 * ## 它还没有被执行过 —— 这是本 scope 交付的一个已知缺口
 *
 * 运行它需要 Electron 运行时二进制，而**开发环境取不到它**（首次执行时才下载，
 * 网络受限）。所以 `main.js` / `preload.js` / 本文件是**从未被执行过的接线**，
 * 与监管器不同 —— 那个有 10 条测试，包括对真实 Python sidecar 的 SIGKILL 恢复。
 *
 * `electron` 依赖因此由 `packaging` scope 声明（它本来就拥有 Electron 运行时与打包
 * 链）。在那之前，本文件是给 RAY-247（计划中首个真机活动）用的现成工具：
 *
 *     pnpm add -D electron --filter @gait/terminal-main
 *     pnpm exec electron apps/terminal/main/smoke.js
 *
 * 退出码 0 即通过。**在有人真的跑过它之前，不要声称主进程能启动** —— 这个项目在
 * RAY-258 上吃过这个亏（`dev.ps1` 经三个 scope 改动仍零执行）。
 */
import { app, BrowserWindow } from "electron";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { SidecarSupervisor, SIDECAR_READY } from "./sidecarSupervisor.js";
import { resolveSidecarCommand } from "./sidecarCommand.js";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(HERE, "../../..");

function fail(message) {
  process.stderr.write(`smoke FAILED: ${message}\n`);
  app.exit(1);
}

app.whenReady().then(async () => {
  const started = Date.now();
  const window = new BrowserWindow({
    show: false,
    webPreferences: { preload: path.join(HERE, "preload.js"), contextIsolation: true, sandbox: true },
  });

  const supervisor = new SidecarSupervisor(
    resolveSidecarCommand({ packaged: false, repoRoot: REPO_ROOT }),
  );
  supervisor.on("diagnostic", (text) => process.stderr.write(`[sidecar] ${text}`));

  const ready = new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error("sidecar 30 秒内没有 ready")), 30_000);
    supervisor.on("state", (payload) => {
      process.stdout.write(`state: ${payload.state}\n`);
      if (payload.state === SIDECAR_READY) {
        clearTimeout(timer);
        resolve();
      }
    });
  });

  try {
    supervisor.start();
    await ready;
    const response = await supervisor.request({ kind: "request", method: "describe", params: {} });
    if (response?.status !== "ok") throw new Error(`describe 回应异常：${JSON.stringify(response)}`);
    process.stdout.write(
      `ipc_contract_version: ${response.result.ipc_contract_version}\n` +
        `capabilities: ${Object.keys(response.result.capabilities).join(", ")}\n` +
        `elapsed_ms: ${Date.now() - started}\n` +
        "smoke OK\n",
    );
    await supervisor.stop();
    window.destroy();
    app.exit(0);
  } catch (error) {
    await supervisor.stop().catch(() => {});
    fail(error.message);
  }
});
