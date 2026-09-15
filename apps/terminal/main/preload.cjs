/**
 * 渲染进程与主进程之间唯一的门。
 *
 * ## 为什么只暴露这三样
 *
 * 红线 R-1：渲染进程不得直接访问 BLE、文件系统、网络上传，一切经 sidecar IPC。
 * `contextBridge` 暴露的东西就是这条红线的实际宽度 —— 多暴露一个 `require` 或一个
 * `fs`，红线就没了，而且不会有任何东西报错。
 *
 * `request` 送契约信封；`onSidecarState` 订阅进程生死；`onEvent` 收 sidecar 推来的
 * 采集事件。没有第四样。
 *
 * ## 第二扇门：gaitShell（RAY-224）
 *
 * 导出 PDF 与打印是**外壳**的能力，不是 sidecar 的，所以单开一个 `gaitShell`，
 * 不塞进 `gaitSidecar`。它同样窄：只有 `exportReportPdf(meta)` 与 `printReport()`，
 * 存到哪、叫什么由主进程决定 —— 渲染端既拿不到路径 API，也拿不到写盘函数。
 *
 * ## 为什么是 .cjs
 *
 * 本包是 `"type": "module"`，而 `sandbox: true` 的 preload 只能是 CommonJS ——
 * 扩展名是 `.js` 时 Electron 按 ESM 解析，`require` 不存在，preload 静默失败，
 * 渲染端看到的就是 `window.gaitSidecar` 为 undefined。
 */
const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("gaitSidecar", {
  request: (message) => ipcRenderer.invoke("gait:sidecar-request", message),
  onSidecarState: (handler) => {
    const listener = (_event, payload) => handler(payload);
    ipcRenderer.on("gait:sidecar-state", listener);
    return () => ipcRenderer.removeListener("gait:sidecar-state", listener);
  },
  onEvent: (handler) => {
    const listener = (_event, payload) => handler(payload);
    ipcRenderer.on("gait:sidecar-event", listener);
    return () => ipcRenderer.removeListener("gait:sidecar-event", listener);
  },
});

contextBridge.exposeInMainWorld("gaitShell", {
  // 只把 reportId 递过去：文件名由主进程构造，受检者标识不进文件名。
  exportReportPdf: (meta) => ipcRenderer.invoke("gait:report-export-pdf", { reportId: meta?.reportId }),
  printReport: () => ipcRenderer.invoke("gait:report-print"),
});
