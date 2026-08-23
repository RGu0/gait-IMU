import { useState } from "react";
import { Banner, Button, MetricTile, SideBadge, StatusPill } from "@gait/design-system";
import { AppBar } from "./AppBar.jsx";

/**
 * P-09 — what the operator sees after the walk.
 *
 * There are two layouts here, not two states of one layout. C-6 requires the
 * invalid session to look *structurally* different from a finished one: no
 * metric cards, no charts, one centred column, and the word 「完成」 nowhere on
 * screen. Sharing a layout and swapping a banner is exactly the failure this
 * guards against — an operator scanning the room recognises the shape of the
 * page long before they read it, and a page shaped like a result reads as a
 * result no matter what the text says.
 *
 * Two more rules shape the finished layout:
 *   · C-7 — a metric that could not be computed says 「本次不适用」 and why.
 *     Never a blank, a 0, a dash or an N/A: each of those reads as a measured
 *     value of nothing.
 *   · C-8 — no normal ranges, no reference bands, no healthy-cohort lines.
 *     This product screens; it does not diagnose, and a band on a chart is a
 *     diagnosis drawn in a way that looks like decoration.
 */

function ComparisonRow({ metric }) {
  const max = Math.max(metric.left, metric.right) || 1;
  const pct = (value) => `${Math.round((value / max) * 100)}%`;
  return (
    <div className="compare-row">
      <span className="compare-row__name">{metric.label}</span>
      <div className="compare-row__bars">
        <div className="compare-bar">
          <SideBadge side="left" size={20} />
          {/* Solid fill for left, hatched for right: the pair has to survive a
              grayscale A4 print, so colour alone cannot carry the side (C-9). */}
          <span className="compare-bar__track">
            <span className="compare-bar__fill compare-bar__fill--left" style={{ width: pct(metric.left) }} />
          </span>
          <span className="compare-bar__value">{metric.left}</span>
        </div>
        <div className="compare-bar">
          <SideBadge side="right" size={20} />
          <span className="compare-bar__track">
            <span className="compare-bar__fill compare-bar__fill--right" style={{ width: pct(metric.right) }} />
          </span>
          <span className="compare-bar__value">{metric.right}</span>
        </div>
      </div>
      <span className="compare-row__unit">{metric.unit}</span>
    </div>
  );
}

function InvalidResult({ result, onRetry, onBackToHub }) {
  return (
    <div className="invalid-page">
      <AppBar />
      {/* Single centred column. No cards, no grid, no chart — the shape itself
          has to say "this is not a result". */}
      <main className="invalid-body" role="alert">
        <StatusPill tone="warning" icon="warning">未通过质量检查</StatusPill>
        <h1>本次检测未生成报告</h1>
        <p className="invalid-reason">{result.reason}</p>
        <p className="invalid-advice">{result.advice}</p>
        <div className="invalid-actions">
          <Button size="lg" onClick={onRetry}>重新检测</Button>
          <Button variant="secondary" onClick={onBackToHub}>返回工作台</Button>
        </div>
      </main>
    </div>
  );
}

export function ResultScreen({ result, onNextSubject, onOpenReport, onRetry, onBackToHub }) {
  const [showConditions, setShowConditions] = useState(false);

  if (!result.valid) {
    return <InvalidResult result={result} onRetry={onRetry} onBackToHub={onBackToHub} />;
  }

  return (
    <div className="result-page">
      <AppBar />
      <main className="result-body">
        <section className="result-status">
          <StatusPill tone="success" icon="check">检测完成</StatusPill>
          <p>{result.fullReportNote}</p>
        </section>

        {result.annotations.length ? (
          <Banner tone="warning" title="解读时请注意">
            {result.annotations.join("；")}
          </Banner>
        ) : null}

        <section className="result-metrics" aria-label="核心指标">
          {result.metrics.map((metric) => (
            <MetricTile
              key={metric.key}
              title={metric.title}
              value={metric.value}
              unit={metric.unit}
              // grade comes from the sidecar. The renderer never derives it —
              // that is RAY-218's red line, and a second implementation of the
              // grading rules is how the terminal and the report start
              // disagreeing about the same walk.
              grade={metric.grade}
              note={metric.note}
            />
          ))}
        </section>

        <section className="result-compare" aria-label="左右对比">
          <h2>左右对比</h2>
          {result.comparison.map((metric) => (
            <ComparisonRow key={metric.label} metric={metric} />
          ))}
          <p className="result-symmetry">
            对称性指数 <strong>{result.symmetryIndex}</strong>
          </p>
        </section>

        <section className="result-variability" aria-label="变异性">
          <h2>变异性</h2>
          <div className="result-variability__cards">
            {result.variability.map((metric) => (
              <MetricTile
                key={metric.key}
                title={metric.title}
                value={metric.value}
                unit={metric.unit}
                grade={metric.grade}
                // PRD §13: variability without the step count it was computed
                // over is a number with no error bar. It always ships together.
                sub={`基于 ${result.validSteps} 个有效步`}
                note={metric.note}
              />
            ))}
          </div>
        </section>

        <section className="result-conditions">
          <button
            type="button"
            className="result-conditions__toggle"
            aria-expanded={showConditions}
            onClick={() => setShowConditions((current) => !current)}
          >
            测试条件{showConditions ? "（收起）" : "（展开）"}
          </button>
          {showConditions ? (
            <dl className="review-list">
              {result.conditions.map((row) => (
                <div className="review-row" key={row.label}>
                  <dt>{row.label}</dt>
                  <dd className="review-row__value">{row.value}</dd>
                </div>
              ))}
            </dl>
          ) : null}
        </section>
      </main>

      <footer className="result-actions">
        {/* The next subject is never blocked on cloud analysis (PRD): the
            terminal's throughput does not depend on a network round trip. */}
        <Button size="lg" onClick={onNextSubject}>开始下一位</Button>
        <Button
          variant="secondary"
          disabled={!result.fullReportReady}
          onClick={onOpenReport}
        >
          查看完整报告
        </Button>
        {!result.fullReportReady ? (
          <span className="result-actions__note">完整报告生成后可查看。</span>
        ) : null}
      </footer>
    </div>
  );
}
