/**
 * 真的把主进程跑起来一次，然后退出。
 *
 * ## 为什么这个文件存在
 *
 * `main.js` 只是接线，接线没有单元测试 —— 于是它可以在结构上完全正确、却从来没有
 * 被执行过。这个项目在 RAY-258 上吃过这个亏：`dev.ps1` 经三个 scope 改动仍是零执行，
 * 教训写在那里，「结构正确不等于执行得通」。
 *
 * 所以本文件用真实的 Electron 运行时，走一遍与 `main.js` 相同的路：起窗口（不显示）、
 * 加载 preload 并确认桥接真的出现在页面里、用与应用**同一个** `sidecarOptions` 拉起
 * 真实的 Python sidecar、发一条 `describe`、拿到回应、收工退出。它验的是这条链
 * **接得通**，不是它的逻辑对不对（逻辑在监管器与 runtimeConfig 的单元测试里）。
 *
 *     pnpm --filter @gait/terminal-main smoke
 *
 * 退出码 0 即通过。RAY-493 第一次真的跑通了它（macOS，Electron 44）；Electron 44
 * 起二进制在第一次运行时才下载，网络不通时可设 `ELECTRON_MIRROR`。
 *
 * userData 指向一个临时目录：冒烟不该改写操作员的预览设置，也不该在真实数据目录
 * 里留下会话。
 */
import { app, BrowserWindow } from "electron";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { SidecarSupervisor, SIDECAR_READY } from "./sidecarSupervisor.js";
import { resolveSidecarCommand } from "./sidecarCommand.js";
import { loadSettings, sessionRoot, sidecarOptions } from "./runtimeConfig.js";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(HERE, "../../..");

const userDataDir = fs.mkdtempSync(path.join(os.tmpdir(), "gait-smoke-"));
app.setPath("userData", userDataDir);

function fail(message) {
  process.stderr.write(`smoke FAILED: ${message}\n`);
  app.exit(1);
}

app.whenReady().then(async () => {
  const started = Date.now();
  let supervisor = null;
  try {
    const window = new BrowserWindow({
      show: false,
      webPreferences: {
        preload: path.join(HERE, "preload.cjs"),
        contextIsolation: true,
        nodeIntegration: false,
        sandbox: true,
      },
    });
    let preloadError = null;
    window.webContents.on("preload-error", (_event, _path, error) => {
      preloadError = error;
    });
    await window.loadURL("data:text/html,<title>smoke</title>");
    const bridge = await window.webContents.executeJavaScript(
      "Object.keys(window.gaitSidecar ?? {}).sort().join(',')",
    );
    if (preloadError) throw new Error(`preload 加载失败：${preloadError.message ?? preloadError}`);
    if (bridge !== "onEvent,onSidecarState,request") throw new Error(`桥接不完整：「${bridge}」`);
    process.stdout.write(`bridge: ${bridge}\n`);

    fs.mkdirSync(sessionRoot(userDataDir), { recursive: true });
    const command = resolveSidecarCommand({ packaged: false, repoRoot: REPO_ROOT });
    const { executable, options } = sidecarOptions({
      command,
      settings: loadSettings(userDataDir),
      userDataDir,
      processEnv: process.env,
      platform: process.platform,
    });
    if (!executable) throw new Error(`PATH 中找不到 ${command.command}`);
    process.stdout.write(`sidecar: ${executable} ${options.args.join(" ")}\n`);
    process.stdout.write(`sidecar env keys: ${Object.keys(options.env).sort().join(", ")}\n`);

    supervisor = new SidecarSupervisor(options);
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
    fs.rmSync(userDataDir, { recursive: true, force: true });
    app.exit(0);
  } catch (error) {
    if (supervisor) await supervisor.stop().catch(() => {});
    fail(error.message);
  }
});
