import { useEffect, useState } from "react";
import { Banner, Button, Toast } from "@gait/design-system";
import { ReportDocument } from "@gait/report-template";
import { AppBar } from "./AppBar.jsx";

// 设计系统约定 Toast 4 秒自动消失；错误不走 Toast，留在 Banner 里等操作员关。
const TOAST_MS = 4000;

/**
 * P-10b — report preview.
 *
 * The preview renders `ReportDocument`, the same component the export will
 * render (R-4). It is scaled to fit the pane, never re-laid-out: scaling keeps
 * the proportions the printer will produce, whereas a "screen-friendly" variant
 * would quietly become a second template and drift.
 *
 * 导出与打印（RAY-224）：两者都是 Electron 外壳经 `window.gaitShell` 提供的能力，
 * 由主进程对**当前这个页面**调用 printToPDF / print —— 打印出来的就是这张预览，
 * 打印样式在 app.css 的 `@media print` 里把预览外框剥掉。
 *
 * 没有外壳（浏览器、mock、测试）时按钮保持禁用并说明原因：把按钮画成可用，
 * 是一个应用兑现不了的承诺。
 *
 * `shell` 可注入，缺省读 `globalThis.gaitShell`；只把 `reportId` 交给它 ——
 * 文件名由主进程构造，受检者标识不出渲染端。
 */
export function ReportPreviewScreen({ report, onNavigate, onBack, shell = globalThis.gaitShell }) {
  const available = Boolean(shell?.exportReportPdf && shell?.printReport);
  const [busy, setBusy] = useState(null);
  const [toast, setToast] = useState(null);
  const [failure, setFailure] = useState(null);

  useEffect(() => {
    if (!toast) return undefined;
    const timer = setTimeout(() => setToast(null), TOAST_MS);
    return () => clearTimeout(timer);
  }, [toast]);

  async function run(kind) {
    setBusy(kind);
    setToast(null);
    setFailure(null);
    let result;
    try {
      result =
        kind === "pdf" ? await shell.exportReportPdf({ reportId: report.reportId }) : await shell.printReport();
    } catch (error) {
      // IPC 本身断了（主进程抛错）也要说出来，不能让按钮转一圈后什么都没发生。
      result = { status: "error", message: String(error?.message ?? error) };
    } finally {
      setBusy(null);
    }
    const title = kind === "pdf" ? "导出 PDF 失败" : "打印失败";
    if (result?.status === "saved") setToast(`已导出 PDF：${result.path}`);
    else if (result?.status === "printed") setToast("已发送到打印机。");
    else if (result?.status === "error") setFailure({ title, message: result.message || "未知错误" });
    // canceled：操作员自己取消的，不提示。
  }

  return (
    <div className="page report-preview-page">
      <AppBar current="检测记录" onNavigate={onNavigate} />
      <main className="preview-body">
        <div className="preview-pane">
          <div className="preview-sheet">
            <ReportDocument report={report} />
          </div>
        </div>

        <aside className="preview-meta" aria-label="报告信息">
          <h1>报告预览</h1>
          <dl className="review-list">
            <div className="review-row"><dt>报告编号</dt><dd className="review-row__value">{report.reportId}</dd></div>
            <div className="review-row"><dt>版本</dt><dd className="review-row__value">{report.edition}</dd></div>
            <div className="review-row"><dt>算法版本</dt><dd className="review-row__value">{report.algoVersion}</dd></div>
            <div className="review-row"><dt>协议配置</dt><dd className="review-row__value">{report.protocolVersion}</dd></div>
            <div className="review-row"><dt>受检者编号</dt><dd className="review-row__value">{report.subjectLabel}</dd></div>
          </dl>

          {available ? null : (
            <Banner tone="info" title="导出与打印尚不可用">
              导出与打印由应用外壳提供，当前版本尚未包含。预览内容与将来导出的
              PDF 出自同一份模板。
            </Banner>
          )}

          {failure ? (
            <Banner tone="danger" title={failure.title} onClose={() => setFailure(null)}>
              {failure.message}
            </Banner>
          ) : null}

          <div className="preview-actions">
            <Button
              disabled={!available || busy !== null}
              loading={busy === "pdf"}
              loadingText="正在导出…"
              onClick={() => run("pdf")}
            >
              导出 PDF
            </Button>
            <Button
              variant="secondary"
              disabled={!available || busy !== null}
              loading={busy === "print"}
              loadingText="正在打开打印…"
              onClick={() => run("print")}
            >
              打印
            </Button>
          </div>

          {toast ? (
            <Toast tone="success" onClose={() => setToast(null)}>
              {toast}
            </Toast>
          ) : null}

          <Button variant="ghost" onClick={onBack}>返回检测记录</Button>
        </aside>
      </main>
    </div>
  );
}
