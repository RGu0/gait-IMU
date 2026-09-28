/**
 * 装机驱动认 sidecar 进程的纯函数（RAY-547）。
 *
 * 只看进程的**可执行文件**（`exe`：macOS 取 `ps -o comm`，Windows 取 `Win32_Process.ExecutablePath`；
 * 缺省时退回 `name`），从不看命令行参数 —— 否则一条 `zsh -c "…/sidecar/gait-sidecar …"` 这样只是
 * 在参数里提到路径的 shell 也会被当成 sidecar，Q.1 就把它判成残留（2026-09-28 RAY-545 #173 实测）。
 *
 * 进程对象形状：`{ pid, ppid, name, exe, command }`；`command` 只留作证据，不参与判定。
 */

const SIDECAR_BASENAME = /^gait-sidecar(\.exe)?$/i;

// 两个平台的路径统一成小写、正斜杠、无结尾分隔符，再比较。
const norm = (p) => p.replaceAll("\\", "/").replace(/\/+$/, "").toLowerCase();
const basename = (p) => p.replace(/[\\/]+$/, "").split(/[\\/]/).pop();

export function isSidecar(p) {
  const exe = p.exe ?? "";
  if (exe) return SIDECAR_BASENAME.test(basename(exe));
  return SIDECAR_BASENAME.test(p.name ?? "");
}

/** 可执行文件位于安装目录之下。取不到可执行路径时为 false。 */
export function fromInstall(p, installRoot) {
  if (!p.exe) return false;
  return norm(p.exe).startsWith(`${norm(installRoot)}/`);
}

/**
 * Q.1 的残留判定：属于本安装的 sidecar，以及取不到可执行路径的同名进程 ——
 * 后者证明不了不是我们的，宁可报出来，也不让残留检查因为读不到路径而静默通过。
 */
export function leftoverSidecars(procs, installRoot) {
  return procs.filter((p) => isSidecar(p) && (fromInstall(p, installRoot) || !p.exe));
}
