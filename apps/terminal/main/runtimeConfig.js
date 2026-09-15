/**
 * 主进程的运行时配置：预览设置、sidecar 环境、可执行文件解析。
 *
 * ## 为什么单独一个文件、且不 import electron
 *
 * 与 `sidecarSupervisor.js` 同一条理由：`main.js` 是没有单元测试的接线，凡是会算错
 * 的东西都不该长在那里。这里每个函数都只吃参数（`userData` 目录、`process.env`、
 * 平台名），于是 vitest 能在任何系统上把 win32 分支也跑一遍。
 *
 * ## sidecar 的环境是「替换」而不是「继承」
 *
 * `SidecarSupervisor` 把 `env` 原样交给 `spawn`，它**替换**整个环境。这是有意的：
 * 主进程的环境里有什么（开发机上的 `GAIT_ACCESS_ROOT`、代理、别的项目的虚拟环境）
 * 不该悄悄决定 sidecar 的行为。所以这里逐项列出 sidecar 需要的变量 —— 多一项都要
 * 在这里写出理由。
 */
import fs from "node:fs";
import path from "node:path";

/** 预览版可选的设备来源。第一项是默认值。 */
export const DEVICE_SOURCES = ["synthetic", "ble"];
export const DEFAULT_DEVICE_SOURCE = DEVICE_SOURCES[0];

const SETTINGS_FILE = "preview-settings.json";

/** 协议段时长（秒）。预览版缩短到一分钟，演示时不必走满完整协议。 */
const PREVIEW_PROTOCOL_SECONDS = "60";

function normalizeSettings(raw) {
  const source = raw && DEVICE_SOURCES.includes(raw.deviceSource) ? raw.deviceSource : DEFAULT_DEVICE_SOURCE;
  return { deviceSource: source };
}

/**
 * 读预览设置。文件不存在或损坏都退回默认值 —— 一个坏掉的设置文件不该让应用
 * 起不来，操作员也没有办法去手工修它。
 */
export function loadSettings(userDataDir) {
  try {
    return normalizeSettings(JSON.parse(fs.readFileSync(path.join(userDataDir, SETTINGS_FILE), "utf8")));
  } catch {
    return normalizeSettings(null);
  }
}

export function saveSettings(userDataDir, settings) {
  const normalized = normalizeSettings(settings);
  fs.mkdirSync(userDataDir, { recursive: true });
  fs.writeFileSync(path.join(userDataDir, SETTINGS_FILE), `${JSON.stringify(normalized, null, 2)}\n`, "utf8");
  return normalized;
}

/** 会话落盘目录。放在 userData 下，而不是仓库里 —— 预览数据不进版本库。 */
export function sessionRoot(userDataDir) {
  return path.join(userDataDir, "sessions");
}

/**
 * sidecar 的业务环境变量。
 *
 * **刻意没有 `GAIT_ACCESS_ROOT`**：预览版不预配置云端，sidecar 因此不建上传线程、
 * 不设登录闸（见 `gait.app.__main__.service_from_environment`）。有测试守着这一条。
 */
export function sidecarEnv({ settings, userDataDir }) {
  return {
    GAIT_SESSION_ROOT: sessionRoot(userDataDir),
    GAIT_DEVICE_SOURCE: normalizeSettings(settings).deviceSource,
    GAIT_PREVIEW: "1",
    GAIT_PROTOCOL_SECONDS: PREVIEW_PROTOCOL_SECONDS,
    // sidecar 的文案是中文，Windows 默认代码页会把它变成乱码。
    PYTHONUTF8: "1",
  };
}

/**
 * 子进程在操作系统层面真正离不开的变量 —— 只挑这几项，**不带 PATH**。
 *
 * - `HOME` / `USERPROFILE`：uv 靠它找缓存与自己管理的解释器。
 * - win32 的 `SYSTEMROOT`：没有它，Windows 上的 Python 连随机数源都初始化不了。
 * - `TEMP` / `TMP` / `APPDATA` / `LOCALAPPDATA`：uv 与 Python 的临时目录和缓存位置。
 *
 * 不带 PATH 是因为 uv 已在主进程里解析成绝对路径（见 `resolveExecutable`），
 * 把整个 PATH 交出去等于把「替换环境」退化成「继承环境」。
 */
export function osEnv(processEnv, platform) {
  const names = platform === "win32"
    ? ["USERPROFILE", "SYSTEMROOT", "TEMP", "TMP", "APPDATA", "LOCALAPPDATA"]
    : ["HOME"];
  const picked = {};
  for (const name of names) {
    if (processEnv[name] !== undefined) picked[name] = processEnv[name];
  }
  return picked;
}

/**
 * 在 PATH 里找可执行文件，返回绝对路径；找不到返回 null。
 *
 * 为什么不直接 `spawn("uv")`：sidecar 的环境里没有 PATH（见 `osEnv`），裸命令名在
 * 子进程里解析不到，只会得到一个 `ENOENT`。于是在主进程 —— 它有 PATH —— 先解析好。
 */
export function resolveExecutable(name, pathVar, platform, exists = isExecutableFile) {
  const flavor = platform === "win32" ? path.win32 : path.posix;
  if (flavor.isAbsolute(name)) return exists(name) ? name : null;
  const separator = platform === "win32" ? ";" : ":";
  // win32 只补 `.exe`：`.cmd` / `.bat` 不经 shell 是 spawn 不起来的，找到它等于没找到。
  const candidates = platform === "win32" && !/\.exe$/i.test(name) ? [`${name}.exe`] : [name];
  for (const dir of String(pathVar ?? "").split(separator)) {
    if (!dir) continue;
    for (const candidate of candidates) {
      const full = flavor.join(dir, candidate);
      if (exists(full)) return full;
    }
  }
  return null;
}

function isExecutableFile(file) {
  try {
    const stat = fs.statSync(file);
    if (!stat.isFile()) return false;
    if (process.platform === "win32") return true;
    fs.accessSync(file, fs.constants.X_OK);
    return true;
  } catch {
    return false;
  }
}

/** 单条请求的超时。监管器默认 30 秒是给探活类请求的；预览版一次采集段是 60 秒，
 * `stopSession` 要等分析跑完才回，30 秒会把一次正常的收尾判成「服务没回应」。 */
export const REQUEST_TIMEOUT_MS = 120_000;

/**
 * 拼出 `SidecarSupervisor` 的构造参数。`main.js` 与 `smoke.js` 共用它 —— 冒烟验的
 * 必须是应用真正用的那一套环境，而不是一份看起来一样的抄本。
 *
 * 环境是**替换**式的：命令自带的变量、OS 必需的几项、业务变量，三者之外什么都没有。
 *
 * 只有开发态才在 PATH 里解析命令：开发态的命令是裸名 `uv`；打包态的命令已经是
 * `resourcesPath` 下冻结产物的绝对路径（`sidecarCommand.packagedCommand`），再去
 * PATH 里找它只会把一个正确的路径换成 null。
 *
 * `executable` 为 null 表示开发态 PATH 里找不到命令；此时照原样交给 spawn，由监管器
 * 走它自己的「服务不可用」路径，界面上有一屏说得清楚的话，而不是主进程直接崩掉。
 */
export function sidecarOptions({ command, packaged = false, settings, userDataDir, processEnv, platform, exists }) {
  const executable = packaged
    ? command.command
    : resolveExecutable(command.command, processEnv.PATH, platform, exists);
  return {
    executable,
    options: {
      ...command,
      command: executable ?? command.command,
      env: { ...command.env, ...osEnv(processEnv, platform), ...sidecarEnv({ settings, userDataDir }) },
      requestTimeoutMs: REQUEST_TIMEOUT_MS,
    },
  };
}

/**
 * 渲染端入口。
 *
 * - 打包态：electron-builder 把 `renderer/dist` 拷进应用包的 `renderer/`（见 RAY-250
 *   preview-installers 的 `electron-builder.yml`），从 `app.getAppPath()` 找。
 * - 开发态：仓库里的 Vite 构建产物。
 */
export function rendererIndex({ packaged = false, appPath, repoRoot }) {
  if (packaged) return path.join(appPath, "renderer", "index.html");
  return path.join(repoRoot, "apps", "terminal", "renderer", "dist", "index.html");
}
