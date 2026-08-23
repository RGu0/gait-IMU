import { useState } from "react";
import { Banner, Button, ChipGroup, Field } from "@gait/design-system";
import { WizardShell } from "./WizardShell.jsx";

/**
 * P-03 — optional profile. Every field on this page can be skipped (AC-02).
 *
 * The rule that shapes the whole screen is C-12: **"not provided" and "none"
 * are different answers and must stay separate.** A binary toggle collapses
 * them, and the report then cannot tell "this person has no fall history" from
 * "nobody asked". The first is a finding; the second is a gap. Printing one as
 * the other is how a report starts lying quietly.
 *
 * So every categorical field is a chip group that carries 「未提供」 as its own
 * option, and 「未提供」 is what an untouched field means.
 */

export const NOT_PROVIDED = "未提供";

const AGE_BANDS = ["18–39", "40–59", "60–74", "75 及以上", NOT_PROVIDED];
const SEXES = ["女", "男", "其他", NOT_PROVIDED];
const FALL_HISTORY = ["有", "无", NOT_PROVIDED];
const WALKING_AIDS = ["拄拐", "助行架", "其他", "无", NOT_PROVIDED];
// PRD P-03: the operator sees plain description only. Which detector preset this
// selects is an implementation detail and must not surface here.
const GAIT_TRAITS = ["平稳", "拖步", "小碎步", "明显跛行", "不确定"];

export const EMPTY_PROFILE = Object.freeze({
  ageBand: NOT_PROVIDED,
  sex: NOT_PROVIDED,
  heightCm: "",
  weightKg: "",
  fallHistory: NOT_PROVIDED,
  walkingAids: [NOT_PROVIDED],
  gaitTraits: [],
});

/**
 * ChipGroup is multi-select by design. Single-choice fields wrap it and keep
 * only the value the operator just added, so the shared component stays
 * untouched (changing it would ripple into the other product).
 */
function SingleChoice({ label, options, value, onChange }) {
  return (
    // role=group + aria-label: without it a chip reading 「无」 is indistinguishable
    // from the 「无」 in another field — for a screen reader, and for a test.
    <div className="profile-field" role="group" aria-label={label}>
      <span className="profile-field__label">{label}</span>
      <ChipGroup
        options={options}
        value={[value]}
        onChange={(next) => {
          const added = next.find((candidate) => candidate !== value);
          onChange(added ?? value); // deselecting the only chip would mean "no answer at all"
        }}
      />
    </div>
  );
}

function MultiChoice({ label, options, value, onChange, exclusive }) {
  return (
    <div className="profile-field" role="group" aria-label={label}>
      <span className="profile-field__label">{label}</span>
      <ChipGroup
        options={options}
        value={value}
        onChange={(next) => {
          // 「无」 and 「未提供」 cannot coexist with a concrete item, or the
          // record would claim both an absence and a presence.
          const justAdded = next.find((candidate) => !value.includes(candidate));
          if (justAdded && exclusive.includes(justAdded)) {
            onChange([justAdded]);
            return;
          }
          const cleaned = next.filter((candidate) => !exclusive.includes(candidate));
          onChange(cleaned.length ? cleaned : [NOT_PROVIDED]);
        }}
      />
    </div>
  );
}

export function ProfileScreen({ profile, onChange, onContinue, onSkip }) {
  const [local, setLocal] = useState(profile ?? EMPTY_PROFILE);

  function update(patch) {
    const next = { ...local, ...patch };
    setLocal(next);
    onChange?.(next);
  }

  const declaredAid = local.walkingAids.some(
    (aid) => aid !== "无" && aid !== NOT_PROVIDED,
  );

  return (
    <WizardShell
      step={1}
      title="选填档案"
      lead="以下全部为选填，可直接继续。"
      actions={
        <>
          <Button variant="secondary" onClick={onSkip}>跳过</Button>
          <Button size="lg" onClick={() => onContinue(local)}>继续</Button>
        </>
      }
    >
      <section className="profile-grid" aria-label="选填档案项">
        <SingleChoice
          label="年龄段"
          options={AGE_BANDS}
          value={local.ageBand}
          onChange={(ageBand) => update({ ageBand })}
        />
        <SingleChoice
          label="性别"
          options={SEXES}
          value={local.sex}
          onChange={(sex) => update({ sex })}
        />

        <div className="profile-measures">
          <Field
            label="身高"
            optional
            unit="cm"
            value={local.heightCm}
            onChange={(event) => update({ heightCm: event.target.value })}
          />
          <Field
            label="体重"
            optional
            unit="kg"
            value={local.weightKg}
            onChange={(event) => update({ weightKg: event.target.value })}
          />
        </div>

        <SingleChoice
          label="跌倒史"
          options={FALL_HISTORY}
          value={local.fallHistory}
          onChange={(fallHistory) => update({ fallHistory })}
        />

        <MultiChoice
          label="辅助器具"
          options={WALKING_AIDS}
          value={local.walkingAids}
          exclusive={["无", NOT_PROVIDED]}
          onChange={(walkingAids) => update({ walkingAids })}
        />
        {declaredAid ? (
          <Banner tone="info" title="报告将显式标注">
            使用辅助器具会影响步态指标的解读，报告中会写明。
          </Banner>
        ) : null}

        <MultiChoice
          label="步行特征"
          options={GAIT_TRAITS}
          value={local.gaitTraits}
          exclusive={["不确定"]}
          onChange={(gaitTraits) => update({ gaitTraits })}
        />
      </section>
    </WizardShell>
  );
}
