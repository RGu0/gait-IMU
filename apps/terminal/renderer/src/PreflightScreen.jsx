import { useEffect, useRef, useState } from "react";
import { Banner, Button, ChecklistItem } from "@gait/design-system";
import { WizardShell } from "./WizardShell.jsx";

/**
 * P-05 — safety confirmation, then device pre-check.
 *
 * Two things here are load-bearing and easy to "improve" away:
 *
 * 1. **There is no select-all** (C-11). Three ticks that can be satisfied with
 *    one click are one tick wearing three hats. Each line is a separate thing
 *    the operator must have actually looked at — a cleared walkway, a spotter
 *    beside a fall-risk subject, a subject well enough to walk three minutes.
 *    These are recorded for audit, and an audit trail of a select-all records
 *    nothing.
 *
 * 2. **The pre-check section does not exist until all three are ticked** — it is
 *    not rendered disabled. One thing at a time: a greyed-out block below still
 *    pulls attention away from the safety lines, and the safety lines are the
 *    part a person has to think about.
 *
 * When every check passes the page advances on its own after a short all-green
 * dwell. The dwell exists so the operator sees the result rather than a screen
 * that changed while they were still reading it.
 */

const SAFETY_ITEMS = [
  { id: "walkway", text: "往返通道已清空，两端转身标志已放置" },
  { id: "spotter", text: "跌倒高风险受试者已有工作人员在侧陪护" },
  { id: "fitness", text: "受试者当前状态适合进行 3 分钟步行" },
];

export const ALL_GREEN_DWELL_MS = 800;

function SafetyItem({ item, checked, onToggle }) {
  return (
    <label className="safety-item">
      <input
        type="checkbox"
        checked={checked}
        onChange={(event) => onToggle(item.id, event.target.checked)}
      />
      <span>{item.text}</span>
    </label>
  );
}

export function PreflightScreen({ runChecks, onReady, dwellMs = ALL_GREEN_DWELL_MS }) {
  const [ticked, setTicked] = useState({});
  const [checks, setChecks] = useState(null);
  const [running, setRunning] = useState(false);
  const advanceTimer = useRef(null);

  const allSafetyTicked = SAFETY_ITEMS.every((item) => ticked[item.id]);

  async function start() {
    setRunning(true);
    setChecks(null);
    try {
      setChecks(await runChecks());
    } finally {
      setRunning(false);
    }
  }

  /**
   * Ticking the third safety box starts the pre-check — the operator has
   * already said "go" by ticking it, and a second confirm would be a click that
   * means nothing.
   *
   * This lives in the handler rather than in an effect because that is what
   * actually happens: it is caused by the tick, not by a state combination that
   * happens to hold. Un-ticking discards the previous result rather than leaving
   * a stale one on screen for the next attempt to inherit.
   */
  function toggleSafety(id, next) {
    // The next state is computed here, NOT inside the setTicked updater. An
    // updater must be a pure function of its argument: React invokes it twice
    // under StrictMode, so a side effect placed there fires twice. That is not
    // theoretical — the first version of this ran the pre-check twice, and
    // because the mock fails only on its first run, the screen sailed straight
    // past the blocked state. The tests missed it because they render without
    // StrictMode; the browser caught it immediately.
    const updated = { ...ticked, [id]: next };
    setTicked(updated);

    if (SAFETY_ITEMS.every((item) => updated[item.id])) {
      start();
    } else {
      setChecks(null);
    }
  }

  const allPassed = Boolean(checks?.length) && checks.every((check) => check.status === "pass");
  const blocked = checks?.filter((check) => check.status === "fail") ?? [];

  useEffect(() => {
    if (!allPassed) return undefined;
    advanceTimer.current = setTimeout(onReady, dwellMs);
    return () => clearTimeout(advanceTimer.current);
  }, [allPassed, dwellMs, onReady]);

  return (
    <WizardShell
      step={3}
      title="安全确认与设备自检"
      lead="先与现场核对以下三项，逐条勾选。"
      actions={
        blocked.length ? (
          <Button size="lg" onClick={start} loading={running} loadingText="正在重新检查…">
            重新检查
          </Button>
        ) : null
      }
    >
      <section className="safety-section" aria-labelledby="safety-title">
        <h2 id="safety-title">安全确认</h2>
        {/* No "tick all" control exists here, by design (C-11). */}
        {SAFETY_ITEMS.map((item) => (
          <SafetyItem
            key={item.id}
            item={item}
            checked={Boolean(ticked[item.id])}
            onToggle={toggleSafety}
          />
        ))}
      </section>

      {allSafetyTicked ? (
        <section className="preflight-section" aria-labelledby="preflight-title">
          <h2 id="preflight-title">设备自检</h2>
          {running && !checks ? (
            <p className="preflight-running" role="status">正在检查设备…</p>
          ) : null}
          {checks?.map((check) => (
            <ChecklistItem
              key={check.id}
              status={check.status}
              label={check.label}
              hint={check.hint}
            />
          ))}
          {allPassed ? (
            <Banner tone="success" title="设备已就绪">
              全部通过，正在进入下一步。
            </Banner>
          ) : null}
          {blocked.length ? (
            <Banner tone="danger" title="设备还不能开始">
              请按上面每一条的说明处理后，点击「重新检查」。
            </Banner>
          ) : null}
        </section>
      ) : null}
    </WizardShell>
  );
}
