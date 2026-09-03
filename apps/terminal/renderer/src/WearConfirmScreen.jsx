import { useState } from "react";
import { Button, SideBadge } from "@gait/design-system";
import { WizardShell } from "./WizardShell.jsx";

/**
 * P-07 — 最小左右确认（RAY-345 的「最小接线」）。
 *
 * 完整会话标定（静立零偏 + 直线安装角，RAY-208）尚未实现。本 MVP 把 P-07 收窄成
 * 唯一一件机器做不了、只有人能做的事：确认左右模块没有戴反。这是 PRD §13 的佩戴
 * 底线 —— 一旦左右戴反，后续所有左右对比指标都会静默地错，而那不是报错，是一份
 * 看着正常的错误报告。
 *
 * 「一键对调」是软件层面的：操作员说「戴反了」，这里就把左右数据归属对调，不需要
 * 让受试者重新佩戴。对调是数据标签的纠正，不是物理动作。确认后 `wearing=pass`、
 * `swapped` 随报告生成一起交给 sidecar，由它决定读哪一侧的录制。
 */

export function WearConfirmScreen({ onDone, onBack }) {
  const [swapped, setSwapped] = useState(false);

  // 左踝/右踝分别由哪个模块供数。swapped 时对调。
  const leftModule = swapped ? "右" : "左";
  const rightModule = swapped ? "左" : "右";

  return (
    <WizardShell
      step={5}
      width="wide"
      title="确认左右"
      lead="数据左右归属的最后一道人工确认。若左右戴反了，点「对调左右」即可，无需重新佩戴。"
      actions={
        <>
          <Button variant="secondary" onClick={onBack}>返回佩戴引导</Button>
          <Button variant="secondary" onClick={() => setSwapped((current) => !current)}>
            {swapped ? "恢复左右" : "对调左右"}
          </Button>
          <Button size="lg" onClick={() => onDone({ wearing: "pass", swapped })}>
            确认无误，开始检测
          </Button>
        </>
      }
    >
      <div className="two-column">
        <div className="two-column__text">
          <ul className="wear-points">
            <li>
              <SideBadge side="left" size={20} />
              <strong>受试者左踝</strong>
              <span>← {leftModule}侧模块</span>
            </li>
            <li>
              <SideBadge side="right" size={20} />
              <strong>受试者右踝</strong>
              <span>← {rightModule}侧模块</span>
            </li>
          </ul>
        </div>
      </div>
    </WizardShell>
  );
}
