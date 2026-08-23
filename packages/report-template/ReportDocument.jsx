import React from "react";

/**
 * The client report. There is exactly one of these (R-4).
 *
 * The same component renders the on-screen preview and, once the Electron main
 * process exists (RAY-250), the printToPDF export. That is not a convenience —
 * it is the only way the basic and the full version of the *same report ID* can
 * be guaranteed not to drift apart in layout. A second template would let the
 * two diverge silently, and the person comparing a printout against the screen
 * would have no way to tell which one to believe.
 *
 * Consequently: **every difference between versions must come from `report`,
 * never from a branch in here.** If something needs to look different, it needs
 * a different value, not a different code path.
 *
 * Section order is fixed by PRD §12 and must not be rearranged.
 */

const GRADE_NOTE = {
  low: "本次有效步数较少，此项仅供参考。",
};

/**
 * Grade → presentation, in one table.
 *
 * Kept out of the JSX for two reasons. It reads better in one place, and
 * `tools/check_quality_single_source.py` flags any line holding both a
 * relational operator and a grade literal — its operator pattern matches the
 * `<` that opens a JSX tag, so `<p className={g === "low" ? … }>` trips it
 * (RAY-265). Nothing here derives a grade; every value is a lookup on the grade
 * the sidecar already decided.
 */
const VALUE_CLASS = {
  normal: "rp-metric__value",
  low: "rp-metric__value rp-metric__value--low",
};

const UNCOMPUTABLE = "uncomputable";

/** Professional-parameter rows print the words, never a blank cell. */
function parameterValue(row) {
  return row.grade === UNCOMPUTABLE ? "本次不适用" : row.value;
}

/**
 * A metric never renders empty. Blank, 0, "N/A" and an em dash all read as a
 * measured value of nothing, which is a different claim from "we could not
 * measure this".
 */
function MetricValue({ metric }) {
  if (metric.grade === UNCOMPUTABLE) {
    return (
      <p className="rp-metric__none">
        本次不适用
        <span className="rp-metric__reason">{metric.reason}</span>
      </p>
    );
  }
  return (
    <p className={VALUE_CLASS[metric.grade] ?? VALUE_CLASS.normal}>
      {metric.value}
      {metric.unit ? <span className="rp-metric__unit">{metric.unit}</span> : null}
    </p>
  );
}

function SideBar({ side, value, max }) {
  const width = `${Math.round((value / (max || 1)) * 100)}%`;
  return (
    <div className="rp-sidebar">
      {/* Shape + text carry the side as well as colour: this page is printed in
          black and white (C-9). */}
      <span className={`rp-sidemark rp-sidemark--${side}`}>{side === "left" ? "左" : "右"}</span>
      <span className="rp-sidebar__track">
        <span className={`rp-sidebar__fill rp-sidebar__fill--${side}`} style={{ width }} />
      </span>
      <span className="rp-sidebar__value">{value}</span>
    </div>
  );
}

export function ReportDocument({ report }) {
  return (
    <article className="rp-page" lang="zh">
      {/* ① 封面 / 摘要 */}
      <header className="rp-cover">
        <div className="rp-cover__org">{report.organization}</div>
        <h1 className="rp-cover__title">步态检测报告</h1>
        <dl className="rp-cover__meta">
          <div><dt>受试者编号</dt><dd>{report.subjectLabel}</dd></div>
          <div><dt>检测日期</dt><dd>{report.assessedAt}</dd></div>
          <div><dt>测试项目</dt><dd>{report.protocolName}</dd></div>
          <div><dt>时长配置</dt><dd>{report.protocolSeconds} 秒</dd></div>
        </dl>
        <span className="rp-cover__edition">{report.edition}</span>
      </header>

      {/* 顶部标注条 — 只在需要时插入，位置固定在①之后 */}
      {report.annotations.length ? (
        <p className="rp-annotation">{report.annotations.join("；")}</p>
      ) : null}

      {/* ② 筛查摘要 —— 措辞受限，不得出现诊断语 */}
      <section className="rp-section">
        <h2>筛查摘要</h2>
        <p className="rp-summary">{report.summary}</p>
        <p className="rp-advice">{report.advice}</p>
      </section>

      {/* ③ 核心指标 */}
      <section className="rp-section">
        <h2>核心指标</h2>
        <div className="rp-metrics">
          {report.metrics.map((metric) => (
            <div
              key={metric.key}
              className={metric.grade === "normal" ? "rp-metric" : `rp-metric rp-metric--${metric.grade}`}
            >
              <p className="rp-metric__title">{metric.title}</p>
              <MetricValue metric={metric} />
              {metric.grade === "low" ? (
                <p className="rp-metric__note">{metric.note || GRADE_NOTE.low}</p>
              ) : null}
            </div>
          ))}
        </div>
      </section>

      {/* ④ 左右对比 */}
      <section className="rp-section">
        <h2>左右对比</h2>
        {report.comparison.map((row) => {
          const max = Math.max(row.left, row.right);
          return (
            <div className="rp-compare" key={row.label}>
              <span className="rp-compare__name">{row.label}</span>
              <div className="rp-compare__bars">
                <SideBar side="left" value={row.left} max={max} />
                <SideBar side="right" value={row.right} max={max} />
              </div>
              <span className="rp-compare__unit">{row.unit}</span>
            </div>
          );
        })}
      </section>

      {/* ⑤ 专业参数 —— 每行末列一个质量标注 */}
      <section className="rp-section">
        <h2>专业参数</h2>
        <table className="rp-table">
          <thead>
            <tr><th>参数</th><th>数值</th><th>单位</th><th>质量</th></tr>
          </thead>
          <tbody>
            {report.parameters.map((row) => (
              <tr key={row.label}>
                <td>{row.label}</td>
                <td className="rp-num">{parameterValue(row)}</td>
                <td>{row.unit}</td>
                <td><span className={`rp-tag rp-tag--${row.grade}`}>{row.qualityLabel}</span></td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>

      {/* ⑥ 图表 —— 不画常模带、正常区间或健康人对照线（C-8） */}
      <section className="rp-section">
        <h2>步态时序</h2>
        <svg className="rp-chart" viewBox="0 0 480 90" role="img" aria-label="步态周期时序条">
          <rect x="0.5" y="0.5" width="479" height="89" rx="6" fill="#F6FAFD" stroke="#DCE7F2" />
          <line x1="10" y1="45" x2="470" y2="45" stroke="rgba(37,105,188,0.12)" strokeWidth="1.5" />
          {report.timeline.left.map((x) => (
            <line key={`l${x}`} x1={x} y1="45" x2={x} y2="20" stroke="#2569BC" strokeWidth="2.5" strokeLinecap="round" />
          ))}
          {report.timeline.right.map((x) => (
            <line key={`r${x}`} x1={x} y1="45" x2={x} y2="70" stroke="#17A2C4" strokeWidth="2.5" strokeDasharray="6 4" strokeLinecap="round" />
          ))}
        </svg>
        <p className="rp-chart__legend">上方实线为左足落步，下方虚线为右足落步。</p>
      </section>

      {/* ⑦ 测试条件 */}
      <section className="rp-section">
        <h2>测试条件</h2>
        <dl className="rp-conditions">
          {report.conditions.map((row) => (
            <div key={row.label}><dt>{row.label}</dt><dd>{row.value}</dd></div>
          ))}
        </dl>
      </section>

      {/* ⑧ 页脚 —— 规格里唯一允许 12px 的位置 */}
      <footer className="rp-footer">
        报告编号 {report.reportId} · {report.edition} · 算法版本 {report.algoVersion} · 协议配置 {report.protocolVersion}
      </footer>
    </article>
  );
}
