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
