/**
 * 会话有效性判定的呈现 —— 这是本次检测唯一真实产出的结论。
 *
 * ## 三态，不是两态
 *
 * `overall` 有 valid / invalid / **indeterminate** 三种。第三种不是凑数：PRD §13 的
 * 佩戴底线含「左右正确」，而 RAY-260 证明位置法在数学上不可判定，v1.4 因此把它改成
 * P-06 手工裁定 —— 在操作员裁定之前，这条底线的诚实答案是「评不了」。
 *
 * 把「评不了」画成「无效」会让操作员去重测一场其实没问题的检测；画成「有效」则正是
 * PRD §13 唯一硬拦截被悄悄架空的方式。所以它自己占一档。
 *
 * ## 会话有效性与双足完整性分开显示
 *
 * `summarize_session` 的 `complete` 为假**不表示数据不可用** —— 单足指标可能仍然
 * 可算，不可算的是双足对称性。合成一个「成功/失败」会把这个区别抹掉，而下游正是
 * 靠它决定拒绝算哪些量。
 */
const OVERALL_COPY = {
  valid: "本次会话有效",
  invalid: "本次会话无效",
  indeterminate: "本次会话尚不能判定",
};

const CHECK_COPY = { pass: "通过", fail: "未通过", unknown: "未裁定" };

export function SessionVerdictSummary({ result }) {
  const verdict = result?.verdict;
  const integrity = result?.integrity;
  if (!verdict) return null;

  return (
    <div className="verdict-summary" data-overall={result.overall}>
      <p className="verdict-summary__headline">{OVERALL_COPY[result.overall] ?? result.overall}</p>
      <dl className="verdict-summary__checks">
        <div>
          <dt>佩戴</dt>
          <dd>{CHECK_COPY[verdict.wearing]}</dd>
        </div>
        <div>
          <dt>链路</dt>
          <dd>{CHECK_COPY[verdict.link]}</dd>
        </div>
        <div>
          <dt>有效时长</dt>
          <dd>
            {CHECK_COPY[verdict.duration]}（{verdict.valid_seconds.toFixed(0)} /{" "}
            {verdict.required_seconds.toFixed(0)} 秒）
          </dd>
        </div>
      </dl>
      {integrity && !integrity.complete ? (
        <p className="verdict-summary__integrity">
          双足数据不完整，双足对称性指标不可算；单侧指标可能仍然可算。
          {integrity.problems.map((problem) => (
            <span key={problem} className="verdict-summary__problem">{problem}</span>
          ))}
        </p>
      ) : null}
    </div>
  );
}
