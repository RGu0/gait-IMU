import React from "react";
import { createRoot } from "react-dom/client";
import "@gait/design-system/styles.css";
// The report's own stylesheet, loaded alongside the app's. It is the same file
// the printToPDF export will load (R-4) — the preview must not be styled by
// anything the printed page will not have.
import "@gait/report-template/report.css";
import "./app.css";
import { TerminalApp } from "./TerminalApp.jsx";
import { mockTerminalAdapter } from "./mockTerminalAdapter.js";
import { createSidecarAdapter } from "./sidecarTerminalAdapter.js";

/**
 * 选哪个 adapter（RAY-248）。
 *
 * 真实通路要经 sidecar，而**拉起 sidecar 并把桥接暴露到 `window` 是 RAY-250 的事**
 * （Electron 主进程 + 打包）。所以这里的规则是：桥接在就用真的，不在就退回 mock，
 * 并且**把退回这件事显示出来**。
 *
 * 为什么不在这里自己造一个传输：那等于在本 scope 里替 RAY-250 决定进程形态，而那个
 * 决定要连着打包、生命周期看护、崩溃恢复一起做（V-U4/V-U5）。
 *
 * 为什么退回时必须显示：mock 不再是生产路径的唯一来源，但它仍然存在；一个悄悄用着
 * 假数据的窗口与一个连着真后端的窗口长得一模一样，而这两者的结论完全不能互换。
 */
export function selectAdapter(scope = globalThis) {
  const bridge = scope?.gaitSidecar;
  if (bridge?.request) {
    return { adapter: createSidecarAdapter((request) => bridge.request(request)), mocked: false };
  }
  return { adapter: mockTerminalAdapter, mocked: true };
}

/* c8 ignore start -- 浏览器入口，由 vite 加载，不在 jsdom 测试里跑 */
if (typeof document !== "undefined" && document.getElementById("root")) {
  const { adapter, mocked } = selectAdapter(globalThis.window);
  createRoot(document.getElementById("root")).render(
    <React.StrictMode>
      {mocked ? (
        <div className="mock-banner" role="status">
          演示数据：未连接 sidecar，界面显示的不是真实采集结果。
        </div>
      ) : null}
      <TerminalApp adapter={adapter} />
    </React.StrictMode>,
  );
}
/* c8 ignore stop */
