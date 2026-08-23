import { useEffect, useRef, useState } from "react";
import { Button, Dialog, LinkStatus, RhythmStrip, SideBadge, CountdownFocus } from "@gait/design-system";

/**
 * P-08 — the timed walk. The most consequential screen in the product, and the
 * one where the layout is not an aesthetic choice.
 *
 * The subject walks a ~4 m shuttle 0–3 m from the terminal, in motion. The
 * operator stands beside it at arm's length. Those viewing distances differ by
 * an order of magnitude, so a single type scale cannot serve both — one of them
 * would be unable to read their own information. Hence the split: the left 62%
 * belongs to the subject and holds four elements at 3 m legibility; the right
 * 38% belongs to the operator.
 *
 * What must never appear on this screen:
 *   · any clinical metric (C-1) — a number the subject reads mid-walk changes
 *     how they walk, which corrupts the measurement being taken
 *   · upload state (C-2) — nothing the operator can act on until the walk ends
 *   · a progress bar of any kind
 *   · anything that pulses, breathes or beats (C-4) — a rhythmic animation is
 *     a metronome whether or not it was meant as one, and a subject who paces
 *     to it is no longer walking at their own speed
 *
 * The rhythm strip lives in the operator column and draws footfalls at their
 * real timestamps. It is a readout, never a pacer.
 */

export const END_HOLD_MS = 3000;

function StepCount({ side, steps }) {
  return (
    <div className="step-tile">
      <SideBadge side={side} size={24} />
      <span className="step-tile__value">{steps}</span>
      <span className="step-tile__unit">步</span>
    </div>
  );
}

export function TestRunScreen({
  live,
  onFinish,
  onAbort,
  tickMs = 1000,
  holdMs = END_HOLD_MS,
  compact = false,
}) {
  const [remaining, setRemaining] = useState(live.totalSeconds);
  const [confirmingStop, setConfirmingStop] = useState(false);

  // The live sidebar values update every few hundred ms during the walk, and
  // each update re-renders this screen with a freshly-created onFinish. Holding
  // the callback in a ref keeps the hold-then-advance effect out of that churn.
  //
  // The first version depended on `onFinish` directly, and every live update
  // tore the timer down and re-created the effect — which then bailed out on a
  // "already finished" guard and never re-armed. The screen sat on 「可以停下了」
  // forever. Tests did not see it because a test passes one stable vi.fn() and
  // no live stream; the browser showed it on the first run.
  const onFinishRef = useRef(onFinish);
  onFinishRef.current = onFinish;

  // The countdown never pauses. A pause in the walk is recorded and skipped by
  // the analysis (PRD §7); stopping the clock for it would silently change the
  // protocol length and make this session incomparable with every other one.
  useEffect(() => {
    if (live.aborted) return undefined;
    const id = setInterval(() => {
      setRemaining((current) => (current > 0 ? current - 1 : 0));
    }, tickMs);
    return () => clearInterval(id);
  }, [tickMs, live.aborted]);

  const ended = remaining === 0;

  // Deps are stable values only: `ended` flips once, `holdMs` is a number, and
  // `aborted` is a boolean here rather than the object itself.
  const isAborted = Boolean(live.aborted);
  useEffect(() => {
    if (!ended || isAborted) return undefined;
    const id = setTimeout(() => onFinishRef.current(), holdMs);
    return () => clearTimeout(id);
  }, [ended, holdMs, isAborted]);

  // A link that drops across the whole session, or a write failure, takes over
  // the entire page. This is the one interruption worth making: continuing to
  // count down would produce a session the operator believes in and the
  // analysis cannot use.
  if (live.aborted) {
    return (
      <div className="abort-page">
        <div className="abort-card" role="alert">
          <h1>测试已安全停止（{live.aborted.code}）</h1>
          <p>数据已保存但不完整，本次不生成报告。</p>
          <Button size="lg" onClick={onAbort}>返回工作台</Button>
        </div>
      </div>
    );
  }

  return (
    <div className="run-page">
      {/* No nav, no back control: switching subject mid-capture is impossible
          by construction rather than by discipline (C-5). */}
      <header className="run-bar">
        <span>测试进行中</span>
        <span className="run-bar__clock">
          剩余 {String(Math.floor(remaining / 60)).padStart(2, "0")}:
          {String(remaining % 60).padStart(2, "0")}
        </span>
      </header>

      <div className="run-body">
        <section className="run-subject" aria-label="受试者提示区">
          {ended ? (
            // The whole subject area is replaced — not a line added underneath.
            // Someone walking a shuttle at 3 m reads position before text.
            <div className="run-endprompt">
              <p className="run-endprompt__title">可以停下了</p>
              <p className="run-endprompt__sub">请站定 3 秒</p>
            </div>
          ) : (
            <CountdownFocus
              seconds={remaining}
              instruction={live.instruction}
              compact={compact}
            />
          )}
        </section>

        <aside className="run-operator" aria-label="操作员侧栏">
          <div className="run-steps">
            <StepCount side="left" steps={live.steps.left} />
            <StepCount side="right" steps={live.steps.right} />
          </div>

          <div className="run-links">
            <LinkStatus side="left" tier={live.link.left} />
            <LinkStatus side="right" tier={live.link.right} />
          </div>

          <div className="run-rhythm">
            <RhythmStrip left={live.footfalls.left} right={live.footfalls.right} />
          </div>

          {/* Neutral, never hurrying: the analysis skips a pause, it does not
              fail because of one. */}
          {live.notices.map((notice) => (
            <p key={notice} className="run-notice" role="status">{notice}</p>
          ))}

          <div className="run-operator__spacer" />

          <div className="run-stop">
            <Button variant="danger" size="sm" onClick={() => setConfirmingStop(true)}>
              停止检测
            </Button>
          </div>
        </aside>
      </div>

      <Dialog
        open={confirmingStop}
        danger
        title="确定要停止本次测试吗？"
        confirmLabel="停止检测"
        cancelLabel="继续测试"
        onConfirm={() => onAbort({ reason: "operator" })}
        onCancel={() => setConfirmingStop(false)}
      >
        已采集 {formatElapsed(live.totalSeconds - remaining)}，停止后本次数据可能不足以生成报告。
      </Dialog>
    </div>
  );
}

function formatElapsed(seconds) {
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return m ? `${m} 分 ${s} 秒` : `${s} 秒`;
}
