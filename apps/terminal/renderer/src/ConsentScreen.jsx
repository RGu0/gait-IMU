import { useState } from "react";
import { Button } from "@gait/design-system";
import { WizardShell } from "./WizardShell.jsx";

/**
 * P-04 — consent.
 *
 * Nothing is ticked when this page opens, so the primary action starts
 * disabled. That is the design, not an oversight: a pre-ticked consent box is
 * not consent, it is a record of the operator not having read the page. The
 * disabled button is the honest state — it says "this page is waiting for a
 * person", which is true.
 *
 * Required and optional purposes are separate sections with separate ticks, and
 * declining is described in plain terms with no warning tone. A refusal that has
 * to be talked out of was never voluntary.
 */

const REQUIRED_PURPOSES = [
  {
    id: "collect",
    title: "采集与本地保存行走过程中的足踝运动数据",
    detail: "用于本次步态检测的计算与报告生成。",
  },
  {
    id: "upload",
    title: "将本次检测数据上传至所属机构的账户",
    detail: "供机构内的专业人员查看与随访对比。",
  },
];

const OPTIONAL_PURPOSES = [
  {
    id: "research",
    title: "用于改进算法的去标识化研究",
    detail: "数据不含姓名等身份信息。不同意不影响本次检测。",
  },
];

function ConsentItem({ purpose, checked, onToggle }) {
  return (
    <label className="consent-item">
      <input
        type="checkbox"
        checked={checked}
        onChange={(event) => onToggle(purpose.id, event.target.checked)}
      />
      <span className="consent-item__text">
        <span className="consent-item__title">{purpose.title}</span>
        <span className="consent-item__detail">{purpose.detail}</span>
      </span>
    </label>
  );
}

export function ConsentScreen({ subjectLabel, onAgree, onDecline }) {
  const [ticked, setTicked] = useState({});

  function toggle(id, next) {
    setTicked((current) => ({ ...current, [id]: next }));
  }

  const allRequiredTicked = REQUIRED_PURPOSES.every((purpose) => ticked[purpose.id]);

  return (
    <WizardShell
      step={2}
      title="数据授权"
      lead={
        subjectLabel
          ? `请向受试者（${subjectLabel}）说明以下内容，并由其决定是否同意。`
          : "请向受试者说明以下内容，并由其决定是否同意。"
      }
      actions={
        <>
          <Button variant="secondary" onClick={onDecline}>不同意，结束本次</Button>
          <Button
            size="lg"
            disabled={!allRequiredTicked}
            onClick={() =>
              onAgree({
                required: REQUIRED_PURPOSES.map((purpose) => purpose.id),
                optional: OPTIONAL_PURPOSES.filter((purpose) => ticked[purpose.id]).map(
                  (purpose) => purpose.id,
                ),
              })
            }
          >
            同意并继续
          </Button>
        </>
      }
    >
      <section className="consent-section" aria-labelledby="consent-required">
        <h2 id="consent-required">进行本次检测所必需</h2>
        {REQUIRED_PURPOSES.map((purpose) => (
          <ConsentItem
            key={purpose.id}
            purpose={purpose}
            checked={Boolean(ticked[purpose.id])}
            onToggle={toggle}
          />
        ))}
      </section>

      <section className="consent-section" aria-labelledby="consent-optional">
        <h2 id="consent-optional">额外用途（可以不同意）</h2>
        {OPTIONAL_PURPOSES.map((purpose) => (
          <ConsentItem
            key={purpose.id}
            purpose={purpose}
            checked={Boolean(ticked[purpose.id])}
            onToggle={toggle}
          />
        ))}
      </section>

      {!allRequiredTicked ? (
        <p className="consent-gate" role="status">
          勾选上方两项必需内容后，才能继续。
        </p>
      ) : null}
    </WizardShell>
  );
}
