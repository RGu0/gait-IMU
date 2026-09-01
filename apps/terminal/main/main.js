/**
 * Electron 主进程。窗口、sidecar 看护、以及两者之间的转发 —— 没有别的。
 *
 * 判定全在别处：进程层面的事实在 `sidecarSupervisor.js`，业务判定在 Python sidecar
 * 里（红线 R-2）。这个文件只做接线，因此它短，也因此它不需要自己的测试 ——
 * 真正会出错的逻辑都在能被测的那两个模块里。
 */
import { app, BrowserWindow, ipcMain } from "electron";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { SidecarSupervisor, SidecarUnavailable } from "./sidecarSupervisor.js";
import { resolveSidecarCommand } from "./sidecarCommand.js";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(HERE, "../../..");

let supervisor = null;
let window = null;

function send(channel, payload) {
  if (window && !window.isDestroyed()) window.webContents.send(channel, payload);
}

function createWindow() {
  window = new BrowserWindow({
    width: 1440,
    height: 900,
    show: false,
    webPreferences: {
      preload: path.join(HERE, "preload.js"),
      // 渲染进程拿不到 Node，红线 R-1 才是结构性的而不是靠自觉。
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });
  window.once("ready-to-show", () => window.show());

  const devServer = process.env.GAIT_RENDERER_URL;
  if (devServer) {
    window.loadURL(devServer);
  } else {
    window.loadFile(path.join(REPO_ROOT, "apps/terminal/renderer/dist/index.html"));
  }
}

app.whenReady().then(() => {
  supervisor = new SidecarSupervisor(
    resolveSidecarCommand({ packaged: app.isPackaged, repoRoot: REPO_ROOT }),
  );
  // 状态先转发给渲染端再建窗口没有意义（窗口还不存在），所以顺序是先建窗口。
  createWindow();
  supervisor.on("state", (payload) => send("gait:sidecar-state", payload));
  supervisor.on("event", (payload) => send("gait:sidecar-event", payload));
  supervisor.on("diagnostic", (text) => process.stderr.write(String(text)));
  supervisor.start();

  ipcMain.handle("gait:sidecar-request", async (_event, message) => {
    try {
      return await supervisor.request(message);
    } catch (error) {
      if (error instanceof SidecarUnavailable) {
        // 把进程级失败以**响应**的形状送回去，渲染端不必区分「抛了」和「回了错」。
        // 注意它没有六域错误码 —— 见 sidecarSupervisor 的模块文档。
        return { kind: "response", id: message?.id ?? "", status: "error", sidecarUnavailable: error.notice };
      }
      throw error;
    }
  });
});

app.on("window-all-closed", async () => {
  if (supervisor) await supervisor.stop();
  if (process.platform !== "darwin") app.quit();
});

app.on("before-quit", async () => {
  if (supervisor) await supervisor.stop();
});
