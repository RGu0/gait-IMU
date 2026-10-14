import { useEffect, useRef, useState } from "react";
import { Button, CountdownFocus } from "@gait/design-system";
import { WizardShell } from "./WizardShell.jsx";
import { CapabilityGap } from "./CapabilityGap.jsx";

/**
 * P-07 — session calibration (RAY-208): stand still 5 s, walk 10 steps in a
 * straight line, then stand still about 3 s. Left/right confirmation
 * (`WearConfirmScreen`, RAY-287) follows this screen and stays the last step
 * before the walk test.
 *
 * Neither step has a button. The operator's hands are busy with the
 * subject; a "next" they have to reach for is a step that happens late or not
 * at all.
 *
 * ## The sidecar drives, this screen only shows
 *
 * The sidecar advances the steps from the samples it actually received (5 s of
 * data, 10 counted steps, a still settle). This screen polls and draws. A
 * client-side countdown that calls "compute now" when it reaches zero would
 * disagree with the data whenever the link runs half a second late — and the
 * operator would get a failure they can do nothing about.
 *
 * The settle step exists because looseness is "the direction of gravity moved
 * > 5° since calibration" (PRD §6.1). With only the opening stand there is
 * nothing to compare against; standing again after the walk measures exactly
 * whether walking shook the strap loose (user decision 2026-10-13).
 *
 * ## Failures speak in actions
 *
 * Phenomenon, action and code all come from the sidecar (RAY-248: the renderer
 * does not write error copy). There is no "worn swapped" failure: automatic
 * swap detection was removed in PRD v1.4 D1 (RAY-260), so that state is
 * unreachable in v1 and is not drawn here (RAY-287 acceptance 4).
 *
 * MAX_ATTEMPTS: the way out appears only after the third failure. Offering it
 * earlier invites abandoning a session that a re-tightened strap would have
 * saved; never offering it strands an operator with a subject who cannot
 * continue.
 */

const MAX_ATTEMPTS = 3;

const RUNNING = new Set(["stand", "walk", "settle"]);

const STEPS = {
  stand: {
    instruction: "请让受试者双脚与肩同宽自然站立，保持不动。",
    caption: "剩余时间（秒）",
    eyebrow: "静立校准",
    index: 1,
  },
  walk: {
    instruction: "请让受试者向前直线走 10 步，速度与平时一致。",
    caption: "已走步数",
    eyebrow: "直线校准",
    index: 2,
  },
  settle: {
    instruction: "请让受试者停下，原地站定不动。",
    caption: "剩余时间（秒）",
    eyebrow: "站定检查",
    index: 3,
  },
};

function shown(status) {
  if (status.phase === "walk") return Math.min(status.walkSteps ?? 0, status.walkTotal ?? 10);
  const total = status.phase === "stand" ? status.standTotal : status.settleTotal;
  const done = status.phase === "stand" ? status.standSeconds : status.settleSeconds;
  return Math.max(0, Math.ceil((total ?? 0) - (done ?? 0)));
}

export function CalibrationScreen({ runCalibration, onDone, onAbandon, pollMs = 250 }) {
  const [status, setStatus] = useState(null);
  const [attempts, setAttempts] = useState(0);
  // 能力缺口（旧 sidecar）与「这个数据源做不了」是两件事，分开画。
  const [gap, setGap] = useState(null);
  const [unavailable, setUnavailable] = useState(null);
  const [round, setRound] = useState(0);
  const onDoneRef = useRef(onDone);
  onDoneRef.current = onDone;
  const runningRef = useRef(false);
  const runRef = useRef(runCalibration);
  runRef.current = runCalibration;

  useEffect(() => {
    let cancelled = false;
    let timer = null;

    function show(next) {
      setStatus(next);
      if (RUNNING.has(next.phase)) {
        runningRef.current = true;
        timer = setTimeout(poll, pollMs);
        return;
      }
      runningRef.current = false;
      if (next.phase === "passed") onDoneRef.current();
      if (next.phase === "failed") setAttempts((current) => current + 1);
    }

    async function poll() {
      if (cancelled) return;
      const next = await runRef.current({ action: "status" });
      if (!cancelled) show(next);
    }

    (async () => {
      const begun = await runRef.current({ action: "begin", fresh: round === 0 });
      if (cancelled) return;
      if (begun?.unimplemented) {
        setGap(begun.unimplemented);
        return;
      }
      if (begun?.phase === "unavailable") {
        setUnavailable(begun.message);
        return;
      }
      show(begun);
    })();

    return () => {
      cancelled = true;
      if (timer !== null) clearTimeout(timer);
    };
  }, [round, pollMs]);

  // 离开这一屏时还在采：告诉 sidecar 收掉标定的流。
  useEffect(
    () => () => {
      if (runningRef.current) {
        runningRef.current = false;
        runRef.current({ action: "cancel" });
      }
    },
    [],
  );

  function retry() {
    setStatus(null);
    setRound((current) => current + 1);
  }

  function abandon() {
    if (runningRef.current) {
      runningRef.current = false;
      runRef.current({ action: "cancel" });
    }
    onAbandon();
  }

  if (gap) {
    return (
      <CapabilityGap
        gap={gap}
        step={5}
        onBack={onAbandon}
        onContinue={onDone}
        continueLabel="跳过标定，继续检测"
      />
    );
  }

  if (unavailable) {
    return (
      <WizardShell
        step={5}
        width="wide"
        title="本次不做会话标定"
        lead="当前数据源没有可用于标定的片段。"
        actions={
          <>
            <Button variant="secondary" onClick={onAbandon}>返回工作台</Button>
            <Button size="lg" onClick={onDone}>跳过标定，继续检测</Button>
          </>
        }
      >
        <div className="capability-gap" role="status" data-capability="calibration">
          <p className="capability-gap__summary">{unavailable}</p>
          <p className="capability-gap__note">
            继续下去的话，本次会话记为「未做标定」—— 界面不会用占位数据冒充它跑过。
          </p>
        </div>
      </WizardShell>
    );
  }

  if (status?.phase === "failed" && status.failure) {
    const exhausted = attempts >= MAX_ATTEMPTS;
    return (
      <WizardShell
        step={5}
        width="wide"
        title="标定未通过"
        lead={`第 ${attempts} 次 / 共 ${MAX_ATTEMPTS} 次`}
        actions={
          <>
            {/* The exit appears only after the third failure. */}
            {exhausted ? (
              <Button variant="secondary" onClick={abandon}>退出本次检测</Button>
            ) : null}
            <Button size="lg" onClick={retry}>重试</Button>
          </>
        }
      >
        <div className="calib-failure" role="alert">
          <p className="calib-failure__copy">{status.failure.message}</p>
          <p className="calib-failure__action">{status.failure.action}</p>
          <p className="calib-failure__code">{status.failure.code}</p>
        </div>
      </WizardShell>
    );
  }

  const phase = status && STEPS[status.phase] ? status.phase : "stand";
  const step = STEPS[phase];
  return (
    <WizardShell step={5} width="wide" title="标定" lead="三个步骤会自动进行，无需点击。">
      <div className="two-column">
        <div className="two-column__figure">
          <CountdownFocus
            eyebrow={step.eyebrow}
            seconds={status ? shown(status) : 5}
            caption={step.caption}
            compact
          />
        </div>
        <div className="two-column__text">
          <p className="calib-instruction">{step.instruction}</p>
          <p className="calib-substep">
            步骤 {step.index} / {Object.keys(STEPS).length}
          </p>
        </div>
      </div>
    </WizardShell>
  );
}
