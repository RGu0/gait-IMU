/**
 * 用真的 Electron printToPDF 出一份样例报告 PDF（RAY-224 pdf-export，V-U3 电子半边的证据）。
 *
 *     pnpm --filter @gait/terminal-renderer build
 *     pnpm --filter @gait/terminal-main exec electron tools/render-sample-pdf.js /path/to/sample-report.pdf
 *
 * ## 它走的是哪条路
 *
 * 加载与应用**同一份**构建产物（renderer/dist），但不挂 preload —— 于是渲染端退回
 * mock adapter，报告内容是 `mockTerminalAdapter.js` 的 REPORT 夹具，不需要 sidecar、
 * 也不需要走完 60 秒采集。然后像操作员一样点「检测记录」→「查看」进到报告预览，
 * 再调用与 IPC 通道**同一个** `exportReportPdf`（同一组 printToPDF 参数、同一份打印
 * 样式），只把保存对话框换成直接返回目标路径。
 *
 * 所以它证明的是：模板 + 打印样式 + printToPDF 参数在真 Chromium 里出的 PDF 长什么样。
 * 它不证明 IPC 桥接得通（那是 smoke.js 验的键集合与单元测试验的通道）。
 *
 * `--platform-fonts`：只为取证。开发机上往往装了 Noto Sans SC，它排在字体栈最前，
 * 于是看不到「没装 Noto 的机器」会嵌什么。加这个参数时临时把 Noto / 思源从栈里拿掉，
 * 验证平台字体（macOS 上是 PingFang）也会被嵌入。它不改模板文件。
 */
import { app, BrowserWindow } from "electron";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { exportReportPdf } from "../reportExport.js";
import { rendererIndex } from "../runtimeConfig.js";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(HERE, "../../../..");
const target = path.resolve(process.argv.at(-1)?.endsWith(".pdf") ? process.argv.at(-1) : "sample-report.pdf");

// 不写操作员的 userData。
app.setPath("userData", fs.mkdtempSync(path.join(os.tmpdir(), "gait-sample-pdf-")));

function fail(message) {
  process.stderr.write(`render-sample-pdf FAILED: ${message}\n`);
  app.exit(1);
}

async function waitFor(webContents, expression, label, timeoutMs = 15_000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (await webContents.executeJavaScript(expression)) return;
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new Error(`等待超时：${label}`);
}

const clickButton = (text) =>
  `(() => { const b = [...document.querySelectorAll("button")].find((x) => x.textContent.trim() === ${JSON.stringify(text)} && !x.disabled); if (b) b.click(); return Boolean(b); })()`;

app.whenReady().then(async () => {
  try {
    const index = rendererIndex({ packaged: false, repoRoot: REPO_ROOT });
    if (!fs.existsSync(index)) throw new Error(`没有构建产物 ${index}，先 build 渲染端`);
    const window = new BrowserWindow({
      show: false,
      width: 1440,
      height: 900,
      webPreferences: { contextIsolation: true, nodeIntegration: false, sandbox: true },
    });
    await window.loadFile(index);
    const wc = window.webContents;

    await waitFor(wc, clickButton("检测记录"), "工作台导航「检测记录」");
    await waitFor(wc, clickButton("查看"), "记录列表「查看」");
    await waitFor(wc, "Boolean(document.querySelector('.report-preview-page .rp-page'))", "报告预览");
    if (process.argv.includes("--platform-fonts")) {
      await wc.insertCSS(
        '.rp-page { font-family: "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif !important; }',
      );
    }
    await wc.executeJavaScript("document.fonts.ready.then(() => true)");

    const meta = await wc.executeJavaScript(
      "({ reportId: [...document.querySelectorAll('.review-row')].find((r) => r.textContent.startsWith('报告编号'))?.querySelector('dd')?.textContent })",
    );
    const result = await exportReportPdf({
      webContents: wc,
      window,
      dialog: { showSaveDialog: async () => ({ canceled: false, filePath: target }) },
      documentsDir: path.dirname(target),
      meta,
    });
    if (result.status !== "saved") throw new Error(`导出结果 ${JSON.stringify(result)}`);
    process.stdout.write(`reportId: ${meta.reportId}\nsaved: ${result.path}\nbytes: ${fs.statSync(result.path).size}\n`);
    window.destroy();
    app.exit(0);
  } catch (error) {
    fail(error?.stack ?? String(error));
  }
});
