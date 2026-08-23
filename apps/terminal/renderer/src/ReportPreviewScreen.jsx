import { Banner, Button } from "@gait/design-system";
import { ReportDocument } from "@gait/report-template";
import { AppBar } from "./AppBar.jsx";

/**
 * P-10b — report preview.
 *
 * The preview renders `ReportDocument`, the same component the export will
 * render (R-4). It is scaled to fit the pane, never re-laid-out: scaling keeps
 * the proportions the printer will produce, whereas a "screen-friendly" variant
 * would quietly become a second template and drift.
 *
 * Export and print are deliberately disabled here. Both go through Chromium's
 * printToPDF, which lives in the Electron main process — and that process does
 * not exist yet (RAY-250). Rendering the buttons as available would be a
 * promise the app cannot keep; disabling them with the reason attached is the
 * honest state.
 */
export function ReportPreviewScreen({ report, onNavigate, onBack }) {
  return (
    <div className="page">
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

          <Banner tone="info" title="导出与打印尚不可用">
            导出与打印由应用外壳提供，当前版本尚未包含。预览内容与将来导出的
            PDF 出自同一份模板。
          </Banner>

          <div className="preview-actions">
            <Button disabled>导出 PDF</Button>
            <Button variant="secondary" disabled>打印</Button>
          </div>

          <Button variant="ghost" onClick={onBack}>返回检测记录</Button>
        </aside>
      </main>
    </div>
  );
}
