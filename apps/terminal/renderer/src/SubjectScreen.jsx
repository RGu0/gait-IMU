import { useState } from "react";
import { Banner, Button, Field } from "@gait/design-system";
import { WizardShell } from "./WizardShell.jsx";

/**
 * P-02 — subject identification. Three states on one page: input → found, or
 * input → conflict.
 *
 * The conflict state is the one worth reading carefully. When two records could
 * be the same person the system does NOT merge them and does NOT guess: both
 * candidates are shown unselected, and the screen deliberately carries no filled
 * primary button (C-10). A default selection here would be the system quietly
 * deciding an identity question it cannot answer, and the operator would have to
 * notice the default to override it — which is exactly what people do not do
 * under time pressure.
 */

function ReviewRow({ label, value, emphasis }) {
  return (
    <div className="review-row">
      <dt>{label}</dt>
      <dd className={emphasis ? "review-row__value review-row__value--emphasis" : "review-row__value"}>
        {value}
      </dd>
    </div>
  );
}

function CandidateCard({ candidate, onChoose }) {
  return (
    <article className="candidate-card">
      <h3>{candidate.maskedId}</h3>
      <dl className="review-list">
        <ReviewRow label="年龄段" value={candidate.ageBand} />
        <ReviewRow label="性别" value={candidate.sex} />
        <ReviewRow label="上次检测" value={candidate.lastAssessedAt} />
      </dl>
      {/* secondary, not primary: neither candidate may look pre-endorsed */}
      <Button variant="secondary" onClick={() => onChoose(candidate)}>
        选择这一条
      </Button>
    </article>
  );
}

export function SubjectScreen({ lookup, protocolSeconds, onConfirm, onQuickCreate }) {
  const [enteredId, setEnteredId] = useState("");
  const [result, setResult] = useState(null);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");

  async function submitLookup(event) {
    event.preventDefault();
    if (!enteredId.trim()) {
      setError("请输入档案号，或选择「无编号，快速建档」。");
      return;
    }
    setPending(true);
    setError("");
    try {
      setResult(await lookup(enteredId.trim()));
    } catch (lookupError) {
      setError(lookupError instanceof Error ? lookupError.message : "查找失败，请重试。");
    } finally {
      setPending(false);
    }
  }

  function restart() {
    setResult(null);
    setEnteredId("");
    setError("");
  }

  if (result?.kind === "conflict") {
    return (
      <WizardShell
        step={0}
        title="找到两条可能相同的档案"
        lead="系统不会自动合并档案。请与受试者核对后选择，或新建一条。"
      >
        <div className="candidate-grid">
          {result.candidates.map((candidate) => (
            <CandidateCard key={candidate.maskedId} candidate={candidate} onChoose={onConfirm} />
          ))}
        </div>
        <div className="conflict-escape">
          <Button variant="secondary" onClick={onQuickCreate}>都不是，新建档案</Button>
          <Button variant="ghost" onClick={restart}>重新输入档案号</Button>
        </div>
      </WizardShell>
    );
  }

  if (result?.kind === "found") {
    const subject = result.subject;
    const mismatch =
      Number.isFinite(subject.lastProtocolSeconds) && subject.lastProtocolSeconds !== protocolSeconds;

    return (
      <WizardShell
        step={0}
        title="核对受试者信息"
        lead="请与受试者核对以上信息。"
        actions={
          <>
            <Button variant="secondary" onClick={restart}>不是这一位</Button>
            <Button size="lg" onClick={() => onConfirm(subject)}>确认并继续</Button>
          </>
        }
      >
        <section className="review-card" aria-label="受试者核对信息">
          <dl className="review-list">
            <ReviewRow label="档案号" value={subject.maskedId} emphasis />
            <ReviewRow label="年龄段" value={subject.ageBand} />
            <ReviewRow label="性别" value={subject.sex} />
            <ReviewRow label="上次检测" value={subject.lastAssessedAt} />
            {/* C-13: the protocol row is mandatory, not decoration */}
            <ReviewRow label="上次时长配置" value={`${subject.lastProtocolSeconds} 秒`} />
            <ReviewRow label="授权状态" value={subject.consentValid ? "在有效期内" : "需要重新授权"} />
          </dl>
        </section>

        {mismatch ? (
          <Banner tone="warning" title="本次与上次的时长配置不同">
            本次为 {protocolSeconds} 秒配置，与上次的 {subject.lastProtocolSeconds} 秒不同，
            两次结果不可直接比较。
          </Banner>
        ) : null}
      </WizardShell>
    );
  }

  return (
    <WizardShell
      step={0}
      title="受试者识别"
      lead="扫描或输入档案号。没有编号时可直接快速建档。"
      actions={
        <>
          <Button variant="secondary" onClick={onQuickCreate}>无编号，快速建档</Button>
          <Button size="lg" type="submit" form="subject-lookup" loading={pending} loadingText="正在查找…">
            查找
          </Button>
        </>
      }
    >
      {/* A barcode scanner types the whole string then sends Enter, so a plain
          form submit is the scanner path as well as the keyboard path. */}
      <form id="subject-lookup" className="lookup-form" onSubmit={submitLookup}>
        <Field
          label="档案号"
          id="subject-id"
          value={enteredId}
          onChange={(event) => setEnteredId(event.target.value)}
          placeholder="扫描条码或手动输入"
          error={error}
          hint="仅在本机构范围内查找。"
        />
      </form>
    </WizardShell>
  );
}
