import { describe, expect, it } from "vitest";

import { packagedCommand, resolveSidecarCommand } from "../sidecarCommand.js";

// 这里钉住的形状同时受两处约束：electron-builder.yml 的 extraResources.to（"sidecar"）
// 与 packaging/gait-sidecar.spec 的 name（"gait-sidecar"）。改任何一处都应当让这里红。
describe("packagedCommand", () => {
  it("macOS：<resources>/sidecar/gait-sidecar，cwd 为产物目录", () => {
    const resourcesPath = "/Applications/GaitIMU Preview.app/Contents/Resources";
    expect(packagedCommand({ resourcesPath, platform: "darwin" })).toEqual({
      command: `${resourcesPath}/sidecar/gait-sidecar`,
      args: [],
      cwd: `${resourcesPath}/sidecar`,
      env: { PYTHONUTF8: "1" },
    });
  });

  it("Windows：可执行文件带 .exe，路径用反斜杠", () => {
    const resourcesPath = "C:\\Users\\op\\AppData\\Local\\Programs\\GaitIMU Preview\\resources";
    expect(packagedCommand({ resourcesPath, platform: "win32" })).toEqual({
      command: `${resourcesPath}\\sidecar\\gait-sidecar.exe`,
      args: [],
      cwd: `${resourcesPath}\\sidecar`,
      env: { PYTHONUTF8: "1" },
    });
  });

  it("打包态没有 uv：env 不带 UV_NO_CONFIG", () => {
    const { env } = packagedCommand({ resourcesPath: "/r", platform: "linux" });
    expect(env).not.toHaveProperty("UV_NO_CONFIG");
  });

  it("缺 resourcesPath 时抛错，而不是拼出一个相对路径", () => {
    expect(() => packagedCommand({ platform: "darwin" })).toThrow(/resourcesPath/);
    expect(() => packagedCommand()).toThrow(/resourcesPath/);
  });
});

describe("resolveSidecarCommand", () => {
  it("packaged=true 走冻结产物", () => {
    const resolved = resolveSidecarCommand({
      packaged: true,
      repoRoot: "/repo",
      resourcesPath: "/res",
      platform: "darwin",
    });
    expect(resolved.command).toBe("/res/sidecar/gait-sidecar");
  });

  it("packaged=false 仍走 uv 开发态", () => {
    const resolved = resolveSidecarCommand({ packaged: false, repoRoot: "/repo", resourcesPath: "/res" });
    expect(resolved.command).toBe("uv");
    expect(resolved.cwd).toBe("/repo");
  });
});
