/**
 * Electron 主进程。窗口、sidecar 看护、菜单，以及它们之间的转发 —— 没有别的。
 *
 * 判定全在别处：进程层面的事实在 `sidecarSupervisor.js`，业务判定在 Python sidecar
 * 里（红线 R-2），会算错的配置（环境变量、可执行文件路径、设置文件）在
 * `runtimeConfig.js`。这个文件只做接线，因此它短，也因此它不需要自己的测试 ——
 * 但接线必须真的跑过一次，见 `smoke.js`。
 */
import { app, BrowserWindow, dialog, ipcMain, Menu, shell } from "electron";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { SidecarSupervisor, SidecarUnavailable } from "./sidecarSupervisor.js";
import { resolveSidecarCommand } from "./sidecarCommand.js";
import {
  loadSettings,
  rendererIndex,
  saveSettings,
  sessionRoot,
  sidecarOptions,
} from "./runtimeConfig.js";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(HERE, "../../..");

let supervisor = null;
let window = null;
let settings = null;
let lastState = null;
let quitting = false;

function log(line) {
  process.stdout.write(`[main] ${line}\n`);
}

function send(channel, payload) {
  if (window && !window.isDestroyed()) window.webContents.send(channel, payload);
}

function supervisorOptions() {
  const command = resolveSidecarCommand({
    packaged: app.isPackaged,
    repoRoot: REPO_ROOT,
    // 打包态用它定位冻结的 sidecar（RAY-250 preview-installers）；开发态忽略。
    resourcesPath: process.resourcesPath,
  });
  const { executable, options } = sidecarOptions({
    command,
    packaged: app.isPackaged,
    settings,
    userDataDir: app.getPath("userData"),
    processEnv: process.env,
    platform: process.platform,
  });
  if (!executable) process.stderr.write(`找不到 ${command.command}（PATH 中没有），sidecar 将无法启动。\n`);
  return options;
}

function startSupervisor() {
  fs.mkdirSync(sessionRoot(app.getPath("userData")), { recursive: true });
  const next = new SidecarSupervisor(supervisorOptions());
  next.on("state", (payload) => {
    // 只认当前这一个监管器：切换设备来源时旧的那个还会吐出最后几条状态。
    if (next !== supervisor) return;
    lastState = payload;
    log(`sidecar state: ${payload.state}`);
    send("gait:sidecar-state", payload);
  });
  next.on("event", (payload) => {
    if (next === supervisor) send("gait:sidecar-event", payload);
  });
  next.on("diagnostic", (text) => process.stderr.write(String(text)));
  supervisor = next;
  next.start();
}

// 切换串行化：连点两下菜单时，第二次必须等第一次把旧 sidecar 停干净，
// 否则会同时起两个、其中一个成为没人管的孤儿进程。
let switching = Promise.resolve();
function switchDeviceSource(deviceSource) {
  switching = switching.then(() => doSwitchDeviceSource(deviceSource)).catch((error) => {
    process.stderr.write(`切换设备来源失败：${error?.stack ?? error}\n`);
  });
  return switching;
}

async function doSwitchDeviceSource(deviceSource) {
  if (settings.deviceSource === deviceSource || quitting) return;
  settings = saveSettings(app.getPath("userData"), { ...settings, deviceSource });
  log(`device source -> ${settings.deviceSource}`);
  const previous = supervisor;
  supervisor = null;
  lastState = null;
  if (previous) await previous.stop();
  if (quitting) return;
  startSupervisor();
  // 重载窗口：渲染端的快照、会话状态都属于旧的 sidecar，留着只会把两个数据源混在一屏上。
  if (window && !window.isDestroyed()) window.webContents.reload();
}

function buildMenu() {
  const previewMenu = {
    label: "预览",
    submenu: [
      {
        label: "演示模式（合成步行）",
        type: "radio",
        checked: settings.deviceSource === "synthetic",
        click: () => switchDeviceSource("synthetic"),
      },
      {
        label: "真实传感器（实验）",
        type: "radio",
        checked: settings.deviceSource === "ble",
        click: () => switchDeviceSource("ble"),
      },
      { type: "separator" },
      {
        label: "打开数据目录",
        click: async () => {
          const dir = sessionRoot(app.getPath("userData"));
          fs.mkdirSync(dir, { recursive: true });
          const error = await shell.openPath(dir);
          if (error) dialog.showErrorBox("无法打开数据目录", `${dir}\n${error}`);
        },
      },
    ],
  };
  const template = [
    ...(process.platform === "darwin" ? [{ role: "appMenu" }] : [{ role: "fileMenu" }]),
    { role: "editMenu" },
    previewMenu,
    { role: "viewMenu" },
    { role: "windowMenu" },
  ];
  Menu.setApplicationMenu(Menu.buildFromTemplate(template));
}

function createWindow() {
  window = new BrowserWindow({
    title: "步态评估 · 预览版",
    width: 1440,
    height: 900,
    minWidth: 1280,
    minHeight: 720,
    show: false,
    webPreferences: {
      preload: path.join(HERE, "preload.cjs"),
      // 渲染进程拿不到 Node，红线 R-1 才是结构性的而不是靠自觉。
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });
  window.once("ready-to-show", () => window.show());
  // 窗口标题以主进程为准；index.html 的 <title> 不该把预览版字样盖掉。
  window.on("page-title-updated", (event) => event.preventDefault());
  window.webContents.on("did-finish-load", () => {
    log("renderer did-finish-load");
    // sidecar 的第一条状态往往在页面加载完之前就发出了，渲染端那时还没订阅，
    // 会一直停在「正在连接采集服务…」。加载完（含每次重载）补发一次当前状态。
    if (lastState) send("gait:sidecar-state", lastState);
  });
  // 渲染端的报错转到主进程 stdout：否则一个白屏窗口在终端里什么都看不出来。
  window.webContents.on("console-message", (event) => {
    if (event.level === "error" || event.level === "warning") {
      log(`renderer ${event.level}: ${event.message} (${event.sourceId}:${event.lineNumber})`);
    }
  });
  window.webContents.on("render-process-gone", (_event, details) => {
    log(`renderer gone: ${details.reason} (exit ${details.exitCode})`);
  });
  window.webContents.on("preload-error", (_event, preloadPath, error) => {
    process.stderr.write(`preload 失败 ${preloadPath}: ${error?.stack ?? error}\n`);
  });

  const devServer = process.env.GAIT_RENDERER_URL;
  if (devServer) {
    window.loadURL(devServer);
  } else {
    window.loadFile(rendererIndex({ packaged: app.isPackaged, appPath: app.getAppPath(), repoRoot: REPO_ROOT }));
  }
}

ipcMain.handle("gait:sidecar-request", async (_event, message) => {
  try {
    if (!supervisor) {
      throw new SidecarUnavailable({
        kind: "sidecar-unavailable",
        message: "采集服务正在切换，暂时不可用。",
        action: "请稍候再试。",
        recoverable: true,
      });
    }
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

app.whenReady().then(() => {
  settings = loadSettings(app.getPath("userData"));
  log(`userData: ${app.getPath("userData")} deviceSource: ${settings.deviceSource}`);
  buildMenu();
  createWindow();
  startSupervisor();

  // 自动化验证用：到点自己退出，让「真的启动过一次」能在无人值守的命令里完成。
  const quitAfter = Number(process.env.GAIT_SMOKE_QUIT_AFTER_MS);
  if (quitAfter > 0) {
    setTimeout(async () => {
      if (window && !window.isDestroyed()) {
        const text = await window.webContents.executeJavaScript("document.body.innerText").catch(() => "");
        log(`renderer text: ${JSON.stringify(String(text).slice(0, 200))}`);
      }
      log("GAIT_SMOKE_QUIT_AFTER_MS reached, quitting");
      app.quit();
    }, quitAfter);
  }
});

app.on("window-all-closed", () => {
  // 预览版一个窗口就是整个应用：关窗即退出，macOS 上也一样，
  // 否则 sidecar 会在没有窗口的情况下继续跑着。
  app.quit();
});

app.on("before-quit", (event) => {
  // stop() 是异步的；不拦一下，Electron 会在 sidecar 还没收到 EOF 时就退出。
  if (quitting) return;
  quitting = true;
  if (!supervisor) return;
  event.preventDefault();
  const current = supervisor;
  supervisor = null;
  current.stop().finally(() => app.quit());
});
