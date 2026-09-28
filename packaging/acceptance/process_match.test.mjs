// 装机驱动认 sidecar 的纯函数（RAY-547）。不需要真实安装包：进程对象按 ps / Win32_Process 的形状手造。
import assert from "node:assert/strict";
import { test } from "node:test";

import { fromInstall, isSidecar, leftoverSidecars } from "./process_match.mjs";

const MAC_ROOT = "/Applications/GaitIMU Preview.app";
const MAC_SIDECAR = `${MAC_ROOT}/Contents/Resources/sidecar/gait-sidecar`;
const WIN_ROOT = "C:\\Users\\runneradmin\\AppData\\Local\\Programs\\GaitIMU Preview";
const WIN_SIDECAR = `${WIN_ROOT}\\resources\\sidecar\\gait-sidecar.exe`;

// 2026-09-28 RAY-545 #173 验证时被 Q.1 判成残留的那一类进程：参数里同时提到 sidecar 与 .app 路径的 shell。
const zshMentioningPaths = {
  pid: 101, ppid: 1, name: "zsh", exe: "/bin/zsh",
  command: `/bin/zsh -c GAIT_SELFTEST=clock "${MAC_SIDECAR}" --selftest; open "${MAC_ROOT}"`,
};
const macSidecar = { pid: 102, ppid: 100, name: "gait-sidecar", exe: MAC_SIDECAR, command: `${MAC_SIDECAR} --stdio` };

test("命令行里提到安装内 sidecar 路径的 zsh 不是 sidecar、也不算来自安装", () => {
  assert.equal(isSidecar(zshMentioningPaths), false);
  assert.equal(fromInstall(zshMentioningPaths, MAC_ROOT), false);
  assert.deepEqual(leftoverSidecars([zshMentioningPaths], MAC_ROOT), []);
});

test("安装内真实 sidecar（macOS）判 sidecar 且来自安装", () => {
  assert.equal(isSidecar(macSidecar), true);
  assert.equal(fromInstall(macSidecar, MAC_ROOT), true);
  assert.deepEqual(leftoverSidecars([zshMentioningPaths, macSidecar], MAC_ROOT), [macSidecar]);
});

test("别处的同名 gait-sidecar 是 sidecar，但不是本安装的", () => {
  const elsewhere = { pid: 103, ppid: 1, name: "gait-sidecar", exe: "/Users/me/build/dist/gait-sidecar/gait-sidecar", command: "" };
  assert.equal(isSidecar(elsewhere), true);
  assert.equal(fromInstall(elsewhere, MAC_ROOT), false);
  assert.deepEqual(leftoverSidecars([elsewhere], MAC_ROOT), []);
});

test("安装目录名只是前缀相同的兄弟目录不算来自安装", () => {
  const sibling = { pid: 104, ppid: 1, name: "gait-sidecar", exe: "/Applications/GaitIMU Preview.app.bak/Contents/Resources/sidecar/gait-sidecar" };
  assert.equal(fromInstall(sibling, MAC_ROOT), false);
});

test("可执行文件名只是以 gait-sidecar 开头的不是 sidecar", () => {
  assert.equal(isSidecar({ pid: 105, ppid: 1, name: "gait-sidecar-helper", exe: `${MAC_ROOT}/Contents/Resources/sidecar/gait-sidecar-helper` }), false);
});

test("Windows：按 ExecutablePath 认，大小写与分隔符无关", () => {
  const sidecar = { pid: 201, ppid: 200, name: "gait-sidecar.exe", exe: WIN_SIDECAR.toUpperCase(), command: `"${WIN_SIDECAR}" --stdio` };
  assert.equal(isSidecar(sidecar), true);
  assert.equal(fromInstall(sidecar, WIN_ROOT.toLowerCase().replaceAll("\\", "/")), true);
  assert.deepEqual(leftoverSidecars([sidecar], WIN_ROOT), [sidecar]);
});

test("Windows：命令行里提到 sidecar 路径的 powershell 不是 sidecar", () => {
  const ps = {
    pid: 202, ppid: 1, name: "powershell.exe",
    exe: "C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe",
    command: `powershell.exe -Command "& '${WIN_SIDECAR}' --selftest"`,
  };
  assert.equal(isSidecar(ps), false);
  assert.equal(fromInstall(ps, WIN_ROOT), false);
  assert.deepEqual(leftoverSidecars([ps], WIN_ROOT), []);
});

test("取不到可执行路径时按 Name 认 sidecar，并且宁可报为残留", () => {
  const unknown = { pid: 203, ppid: 1, name: "gait-sidecar.exe", exe: null, command: null };
  assert.equal(isSidecar(unknown), true);
  assert.equal(fromInstall(unknown, WIN_ROOT), false);
  assert.deepEqual(leftoverSidecars([unknown], WIN_ROOT), [unknown]);
  assert.deepEqual(leftoverSidecars([{ pid: 204, ppid: 1, name: "cmd.exe", exe: null, command: `cmd /c "${WIN_SIDECAR}"` }], WIN_ROOT), []);
});
