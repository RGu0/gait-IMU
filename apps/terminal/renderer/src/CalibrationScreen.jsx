import { useEffect, useRef, useState } from "react";
import { Button, CountdownFocus } from "@gait/design-system";
import { WizardShell } from "./WizardShell.jsx";
import { CapabilityGap } from "./CapabilityGap.jsx";

/**
 * P-07 — session calibration, in two sub-steps that advance on their own:
 * stand still for 5 s, then walk 10 steps in a straight line.
 *
 * Neither sub-step has a button. The operator's hands are busy with the
 * subject; a "next" they have to reach for is a step that happens late or not
 * at all.
 *
 * Failures speak in actions, never in algorithm terms: 「戴反了，请交换」 not
 * 「安装角估计不收敛」. The operator can act on the first and can do nothing
 * with the second.
 *
 * MAX_ATTEMPTS: the way out appears only after the third failure. Offering it
 * earlier invites abandoning a session that a re-tightened strap would have
 * saved; never offering it strands an operator with a subject who cannot
 * continue.
 *
 * ── If RAY-260 resolves against the automatic swap check ──
 * The 「戴反」 entry in FAILURE_COPY becomes unreachable and should be removed,
 * along with its route from E-WEAR. The other two failures do not depend on it.
 * Everything else on this screen stands.
 */

const MAX_ATTEMPTS = 3;

const STEPS = [
  {
    id: "stand",
    instruction: "请让受试者双脚与肩同宽自然站立，保持不动。",
    caption: "剩余时间（秒）",
    eyebrow: "静立校准",
    total: 5,
  },
  {
    id: "walk",
    instruction: "请让受试者向前直线走 10 步，速度与平时一致。",
    caption: "已走步数",
    eyebrow: "直线校准",
    total: 10,
  },
];

/** Action language only. No algorithm words reach the operator. */
const FAILURE_COPY = {
  swapped: "两个模块戴反了，请交换左右后重试。",
  loose: "模块有些松动，请绑紧后重试。",
  drift: "这一段没有采到有效数据，请重新走 10 步。",
};

export function CalibrationScreen({ runCalibration, onDone, onAbandon, tickMs = 1000 }) {
  const [stepIndex, setStepIndex] = useState(0);
  const [progress, setProgress] = useState(STEPS[0].total);
  const [failure, setFailure] = useState(null);
  const [attempts, setAttempts] = useState(0);
  // 会话标定（RAY-208）还不存在。sidecar 因此返回 unimplemented 而不是一个
  // 看起来通过了的 verdict —— 这一步必须停下来说清楚，不能悄悄放行。
  const [gap, setGap] = useState(null);
  const onDoneRef = useRef(onDone);
  onDoneRef.current = onDone;

  const step = STEPS[stepIndex];
  const running = failure === null;

  useEffect(() => {
    if (!running) return undefined;
    const id = setInterval(() => setProgress((current) => Math.max(0, current - 1)), tickMs);
    return () => clearInterval(id);
  }, [tickMs, running, stepIndex]);

  useEffect(() => {
    if (!running || progress > 0) return undefined;
    let cancelled = false;

    (async () => {
      if (stepIndex === 0) {
        setStepIndex(1);
        setProgress(STEPS[1].total);
        return;
      }
      const verdict = await runCalibration();
      if (cancelled) return;
      if (verdict?.unimplemented) {
        setGap(verdict.unimplemented);
      } else if (verdict.ok) {
        onDoneRef.current();
      } else {
        setFailure(verdict.reason);
        setAttempts((current) => current + 1);
      }
    })();

    return () => { cancelled = true; };
  }, [progress, running, stepIndex, runCalibration]);

  function retry() {
    setFailure(null);
    setStepIndex(0);
    setProgress(STEPS[0].total);
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

  if (failure) {
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
              <Button variant="secondary" onClick={onAbandon}>退出本次检测</Button>
            ) : null}
            <Button size="lg" onClick={retry}>重试</Button>
          </>
        }
      >
        <div className="calib-failure" role="alert">
          <p className="calib-failure__copy">{FAILURE_COPY[failure]}</p>
        </div>
      </WizardShell>
    );
  }

  return (
    <WizardShell step={5} width="wide" title="标定" lead="两个步骤会自动进行，无需点击。">
      <div className="two-column">
        <div className="two-column__figure">
          <CountdownFocus
            eyebrow={step.eyebrow}
            seconds={stepIndex === 0 ? progress : step.total - progress}
            caption={step.caption}
            compact
          />
        </div>
        <div className="two-column__text">
          <p className="calib-instruction">{step.instruction}</p>
          <p className="calib-substep">
            步骤 {stepIndex + 1} / {STEPS.length}
          </p>
        </div>
      </div>
    </WizardShell>
  );
}
