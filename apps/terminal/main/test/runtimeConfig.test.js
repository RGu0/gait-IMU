/**
 * 主进程运行时配置。`main.js` 没有单元测试，所以凡是会算错的都在这里被钉住 ——
 * 尤其是「sidecar 的环境里没有什么」：没有 PATH、没有 GAIT_ACCESS_ROOT。
 */
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { afterEach, describe, expect, it } from "vitest";
import { UV_CONFIG_FILE_NULL } from "../sidecarCommand.js";

import {
  configRoot,
  DEFAULT_DEVICE_SOURCE,
  DEVICE_SOURCES,
  loadSettings,
  osEnv,
  rendererIndex,
  resolveExecutable,
  REQUEST_TIMEOUT_MS,
  saveSettings,
  sidecarEnv,
  sidecarOptions,
} from "../runtimeConfig.js";

let dirs = [];
function tempDir() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "gait-runtime-"));
  dirs.push(dir);
  return dir;
}
afterEach(() => {
  for (const dir of dirs) fs.rmSync(dir, { recursive: true, force: true });
  dirs = [];
});

describe("预览设置", () => {
  it("默认是演示模式（合成步行）", () => {
    expect(DEVICE_SOURCES).toEqual(["synthetic", "ble"]);
    expect(DEFAULT_DEVICE_SOURCE).toBe("synthetic");
  });

  it("文件不存在时退回默认值", () => {
    expect(loadSettings(tempDir())).toEqual({ deviceSource: "synthetic" });
  });

  it("文件损坏时退回默认值，而不是让应用起不来", () => {
    const dir = tempDir();
    fs.writeFileSync(path.join(dir, "preview-settings.json"), "{ 不是 json");
    expect(loadSettings(dir)).toEqual({ deviceSource: "synthetic" });
  });

  it("未知的设备来源退回默认值", () => {
    const dir = tempDir();
    fs.writeFileSync(path.join(dir, "preview-settings.json"), JSON.stringify({ deviceSource: "usb" }));
    expect(loadSettings(dir)).toEqual({ deviceSource: "synthetic" });
  });

  it("存了再读回来是同一个值", () => {
    const dir = path.join(tempDir(), "nested");
    saveSettings(dir, { deviceSource: "ble" });
    expect(loadSettings(dir)).toEqual({ deviceSource: "ble" });
  });
});

describe("sidecarEnv", () => {
  const userDataDir = path.join(os.tmpdir(), "gait-user-data");

  it("给出会话目录、设备来源与预览开关", () => {
    expect(sidecarEnv({ settings: { deviceSource: "ble" }, userDataDir })).toEqual({
      GAIT_SESSION_ROOT: path.join(userDataDir, "sessions"),
      GAIT_CONFIG_ROOT: path.join(userDataDir, "config"),
      GAIT_DEVICE_SOURCE: "ble",
      GAIT_PREVIEW: "1",
      GAIT_PROTOCOL_SECONDS: "60",
      PYTHONUTF8: "1",
    });
  });

  it("绑定目录是 userData 下独立的 config，不在会话目录里，两种设备来源都带", () => {
    for (const deviceSource of DEVICE_SOURCES) {
      const env = sidecarEnv({ settings: { deviceSource }, userDataDir });
      expect(env.GAIT_CONFIG_ROOT).toBe(configRoot(userDataDir));
      expect(env.GAIT_CONFIG_ROOT.startsWith(env.GAIT_SESSION_ROOT)).toBe(false);
    }
  });

  it("永远不带 GAIT_ACCESS_ROOT —— 预览版不预配置云端", () => {
    for (const deviceSource of [...DEVICE_SOURCES, undefined]) {
      expect(sidecarEnv({ settings: { deviceSource }, userDataDir })).not.toHaveProperty("GAIT_ACCESS_ROOT");
    }
  });
});

describe("osEnv", () => {
  const processEnv = {
    PATH: "/usr/bin",
    HOME: "/Users/op",
    USERPROFILE: "C:\\Users\\op",
    SYSTEMROOT: "C:\\Windows",
    TEMP: "C:\\Temp",
    TMP: "C:\\Tmp",
    APPDATA: "C:\\AppData\\Roaming",
    LOCALAPPDATA: "C:\\AppData\\Local",
    GAIT_ACCESS_ROOT: "/secret",
  };

  it("macOS / Linux 只带 HOME", () => {
    expect(osEnv(processEnv, "darwin")).toEqual({ HOME: "/Users/op" });
  });

  it("Windows 带解释器与 uv 离不开的那几项", () => {
    expect(osEnv(processEnv, "win32")).toEqual({
      USERPROFILE: "C:\\Users\\op",
      SYSTEMROOT: "C:\\Windows",
      TEMP: "C:\\Temp",
      TMP: "C:\\Tmp",
      APPDATA: "C:\\AppData\\Roaming",
      LOCALAPPDATA: "C:\\AppData\\Local",
    });
  });

  it("不把 PATH 与 GAIT_ACCESS_ROOT 传下去", () => {
    for (const platform of ["darwin", "linux", "win32"]) {
      const env = osEnv(processEnv, platform);
      expect(env).not.toHaveProperty("PATH");
      expect(env).not.toHaveProperty("GAIT_ACCESS_ROOT");
    }
  });

  it("缺的变量就不出现，而不是写成 undefined", () => {
    expect(osEnv({}, "win32")).toEqual({});
  });
});

describe("resolveExecutable", () => {
  it("按 PATH 顺序返回第一个命中的绝对路径", () => {
    const seen = new Set(["/opt/b/uv", "/opt/c/uv"]);
    expect(resolveExecutable("uv", "/opt/a:/opt/b:/opt/c", "darwin", (f) => seen.has(f))).toBe("/opt/b/uv");
  });

  it("Windows 补 .exe，并用分号切 PATH", () => {
    const seen = new Set(["C:\\tools\\uv.exe"]);
    expect(resolveExecutable("uv", "C:\\bin;C:\\tools", "win32", (f) => seen.has(f))).toBe("C:\\tools\\uv.exe");
  });

  it("找不到返回 null", () => {
    expect(resolveExecutable("uv", "/opt/a", "darwin", () => false)).toBeNull();
    expect(resolveExecutable("uv", undefined, "darwin", () => false)).toBeNull();
  });

  it("真文件系统上能找到 node 自己", () => {
    const dir = path.dirname(process.execPath);
    const name = path.basename(process.execPath).replace(/\.exe$/i, "");
    expect(resolveExecutable(name, dir, process.platform)).toBe(path.join(dir, path.basename(process.execPath)));
  });
});

describe("sidecarOptions", () => {
  const command = {
    command: "uv",
    args: ["run", "--locked", "python", "-m", "gait.app"],
    cwd: "/repo",
    // 与 developmentCommand() 同形。取值引用产品的常量，别再手写一份会漂开的 env（RAY-483 / RAY-494）。
    env: { UV_CONFIG_FILE: UV_CONFIG_FILE_NULL, PYTHONUTF8: "1" },
  };
  const processEnv = { PATH: "/bin:/opt/uv", HOME: "/Users/op", GAIT_ACCESS_ROOT: "/secret", SHELL: "/bin/zsh" };

  it("命令解析成绝对路径，环境是命令变量 + OS 必需项 + 业务变量，别无其他", () => {
    const { executable, options } = sidecarOptions({
      command,
      settings: { deviceSource: "synthetic" },
      userDataDir: "/data",
      processEnv,
      platform: "darwin",
      exists: (file) => file === "/opt/uv/uv",
    });
    expect(executable).toBe("/opt/uv/uv");
    expect(options.command).toBe("/opt/uv/uv");
    expect(options.args).toEqual(command.args);
    expect(options.cwd).toBe("/repo");
    expect(options.requestTimeoutMs).toBe(REQUEST_TIMEOUT_MS);
    expect(Object.keys(options.env).sort()).toEqual([
      "GAIT_CONFIG_ROOT",
      "GAIT_DEVICE_SOURCE",
      "GAIT_PREVIEW",
      "GAIT_PROTOCOL_SECONDS",
      "GAIT_SESSION_ROOT",
      "HOME",
      "PYTHONUTF8",
      "UV_CONFIG_FILE",
    ]);
  });

  it("打包态不在 PATH 里找：冻结产物的绝对路径原样使用", () => {
    const frozen = { command: "/App/Resources/sidecar/gait-sidecar", args: [], cwd: "/App/Resources/sidecar", env: { PYTHONUTF8: "1" } };
    const { executable, options } = sidecarOptions({
      command: frozen,
      packaged: true,
      settings: {},
      userDataDir: "/data",
      processEnv,
      platform: "darwin",
      exists: () => {
        throw new Error("打包态不该查 PATH");
      },
    });
    expect(executable).toBe(frozen.command);
    expect(options.command).toBe(frozen.command);
    // 冻结产物不经 uv，env 里不该有任何 uv 变量。
    expect(options.env).not.toHaveProperty("UV_CONFIG_FILE");
    expect(options.env).not.toHaveProperty("GAIT_ACCESS_ROOT");
    expect(options.env.GAIT_SESSION_ROOT).toBe(path.join("/data", "sessions"));
  });

  it("PATH 里找不到时照原样交出命令名，由监管器走「服务不可用」", () => {
    const { executable, options } = sidecarOptions({
      command,
      settings: {},
      userDataDir: "/data",
      processEnv,
      platform: "darwin",
      exists: () => false,
    });
    expect(executable).toBeNull();
    expect(options.command).toBe("uv");
  });
});

describe("rendererIndex", () => {
  it("开发态指向仓库里渲染端的 Vite 构建产物", () => {
    expect(rendererIndex({ packaged: false, appPath: "/ignored", repoRoot: "/repo" })).toBe(
      path.join("/repo", "apps", "terminal", "renderer", "dist", "index.html"),
    );
  });

  it("打包态指向应用包内的 renderer/index.html", () => {
    expect(rendererIndex({ packaged: true, appPath: "/App/resources/app.asar", repoRoot: "/repo" })).toBe(
      path.join("/App/resources/app.asar", "renderer", "index.html"),
    );
  });
});
