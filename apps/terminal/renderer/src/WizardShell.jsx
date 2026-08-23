import { StepBar } from "@gait/design-system";

/**
 * Skeleton B — the wizard frame shared by P-02…P-07.
 *
 * The seven steps are named once, here, because a stepper that disagrees with
 * itself between screens is worse than no stepper: the operator uses it to
 * answer "how much longer", and a wrong answer costs their trust in the whole
 * flow.
 */
export const WIZARD_STEPS = ["识别", "档案", "授权", "自检", "佩戴", "标定", "测试"];

/**
 * `width` is "narrow" (720) for form pages and "wide" (1040) for the
 * illustration+text pages. Both are content widths, not breakpoints.
 */
export function WizardShell({ step, title, lead, width = "narrow", children, actions }) {
  return (
    <div className="wizard-page">
      <header className="wizard-header">
        <StepBar steps={WIZARD_STEPS} current={step} />
      </header>
      <main className={`wizard-body wizard-body--${width}`}>
        <div className="wizard-heading">
          <h1>{title}</h1>
          {lead ? <p className="wizard-lead">{lead}</p> : null}
        </div>
        {children}
      </main>
      {actions ? <footer className="wizard-actions">{actions}</footer> : null}
    </div>
  );
}
