/**
 * 报告导出 PDF 与打印（RAY-224 pdf-export）。
 *
 * ## 为什么在主进程
 *
 * `printToPDF` / `print` 只有 webContents 有，而渲染进程按红线 R-1 拿不到文件系统。
 * 所以渲染端只说「导出这一份」，存到哪、叫什么、怎么写盘，全在这里决定。
 *
 * ## 为什么 electron 对象全部由调用方注入
 *
 * 这里真正会出错的是：文件名有没有漏出受检者标识、取消与失败有没有被混成一种、
 * 写盘失败有没有被吞掉。这些都能用假的 dialog / webContents 在 vitest 里验，
 * 不需要起 Electron。`main.js` 只负责把真的 ipcMain、dialog 递进来。
 *
 * ## 不做什么
 *
 * 不排版（版式唯一来源是 `@gait/report-template`，红线 R-4），不做业务判定（R-2），
 * 不做「不可变版本」归档 —— 那是 RAY-224 里另一件事，本 scope 没有实现它。
 */
import fs from "node:fs";
import path from "node:path";

export const EXPORT_PDF_CHANNEL = "gait:report-export-pdf";
export const PRINT_CHANNEL = "gait:report-print";

// A4 由报告模板的 `@page` 决定（preferCSSPageSize）；这里的 pageSize 只是模板
// 万一没声明时的兜底。margins 置零，是为了让页边距也只听模板的 `@page margin`，
// 不叠加 Chromium 自己的默认边距。
export const PDF_OPTIONS = Object.freeze({
  pageSize: "A4",
  printBackground: true,
  margins: Object.freeze({ marginType: "none" }),
  preferCSSPageSize: true,
});

export const PRINT_OPTIONS = Object.freeze({ printBackground: true });

const pad = (n) => String(n).padStart(2, "0");

/**
 * 默认文件名：`步态报告-<YYYYMMDD-HHmm>-<报告编号末 8 位>.pdf`。
 *
 * 为什么只用这两样：导出的 PDF 会被拷走、发邮件、落在共享盘里，文件名是最先被人
 * 看到、也最不受控的地方。受检者编号、姓名一律不进文件名 —— 即使渲染端传了也不用，
 * 所以这个函数根本不接受受检者字段。
 *
 * 为什么取报告编号的**末** 8 位：sidecar 的编号形如 `20260915T010203Z-a1b2c3d4`，
 * 开头是时间戳（与前面的导出时间重复），能区分两份报告的是结尾的随机段。
 * 只保留 ASCII 字母数字：编号来自外部，不能让它带进路径分隔符或 `..`。
 */
export function defaultReportFileName({ reportId, now = new Date() } = {}) {
  const stamp = `${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}-${pad(now.getHours())}${pad(now.getMinutes())}`;
  const compact = String(reportId ?? "").replace(/[^A-Za-z0-9]/g, "");
  const short = compact.slice(-8) || "unknown";
  return `步态报告-${stamp}-${short}.pdf`;
}

function errorResult(error) {
  return { status: "error", message: String(error?.message ?? error ?? "未知错误") };
}

/**
 * 导出一份 PDF。先生成、后问存哪：生成失败时就不必让操作员先挑一遍目录再看到报错。
 */
export async function exportReportPdf({
  webContents,
  window,
  dialog,
  documentsDir,
  meta,
  writeFile = fs.promises.writeFile,
  now = new Date(),
}) {
  let data;
  try {
    data = await webContents.printToPDF(PDF_OPTIONS);
  } catch (error) {
    return errorResult(error);
  }

  // meta 里只认 reportId；subjectLabel 之类即使传来也不读（见 defaultReportFileName）。
  const name = defaultReportFileName({ reportId: meta?.reportId, now });
  let choice;
  try {
    choice = await dialog.showSaveDialog(window, {
      title: "导出报告 PDF",
      defaultPath: documentsDir ? path.join(documentsDir, name) : name,
      filters: [{ name: "PDF", extensions: ["pdf"] }],
    });
  } catch (error) {
    return errorResult(error);
  }
  if (!choice || choice.canceled || !choice.filePath) return { status: "canceled" };

  // 操作员手动删掉扩展名时补上：没有 .pdf 的文件在 Windows 上双击打不开。
  const target = /\.pdf$/i.test(choice.filePath) ? choice.filePath : `${choice.filePath}.pdf`;
  try {
    await writeFile(target, data);
  } catch (error) {
    return errorResult(error);
  }
  return { status: "saved", path: target };
}

/**
 * 系统打印对话框。Electron 以回调报告结果；取消与失败要分开，
 * 因为取消不该在界面上弹任何提示，失败必须弹。
 */
export function printReport({ webContents }) {
  return new Promise((resolve) => {
    try {
      webContents.print(PRINT_OPTIONS, (success, failureReason) => {
        if (success) resolve({ status: "printed" });
        else if (/cancel/i.test(String(failureReason ?? ""))) resolve({ status: "canceled" });
        else resolve({ status: "error", message: String(failureReason || "打印失败") });
      });
    } catch (error) {
      resolve(errorResult(error));
    }
  });
}

/**
 * 在 ipcMain 上登记两个通道。操作的 webContents 取发起请求的那一个（event.sender），
 * 对话框挂在 getWindow() 上，好让它是模态的、不会跑到窗口后面去。
 */
export function registerReportExport({
  ipcMain,
  dialog,
  getWindow,
  getDocumentsDir,
  writeFile = fs.promises.writeFile,
  clock = () => new Date(),
}) {
  ipcMain.handle(EXPORT_PDF_CHANNEL, (event, meta) =>
    exportReportPdf({
      webContents: event.sender,
      window: getWindow(),
      dialog,
      documentsDir: getDocumentsDir?.(),
      meta,
      writeFile,
      now: clock(),
    }),
  );
  ipcMain.handle(PRINT_CHANNEL, (event) => printReport({ webContents: event.sender }));
}
