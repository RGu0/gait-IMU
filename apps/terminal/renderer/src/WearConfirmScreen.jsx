import { useState } from "react";
import { Button, SideBadge } from "@gait/design-system";
import { WizardShell } from "./WizardShell.jsx";

/**
 * P-07 — 左右确认闸（RAY-287 R2）。
 *
 * 完整会话标定（静立零偏 + 直线安装角，RAY-208）尚未实现。本 MVP 把 P-07 收窄成
 * 唯一一件机器做不了、只有人能做的事：确认左右模块没有戴反。这是 PRD §13 的佩戴
 * 底线 —— 一旦左右戴反，后续所有左右对比指标都会静默地错，而那不是报错，是一份
 * 看着正常的错误报告。
 *
 * 「一键对调」是软件层面的：操作员说「戴反了」，这里就把左右数据归属对调，不需要
 * 让受试者重新佩戴。对调是数据标签的纠正，不是物理动作。
 *
 * ## 这一屏是闸，不是确认屏
 *
 * RAY-345 先交付了这一屏的**信息**（左右归属 + 一键对调），但主按钮恒为可用、且
 * 无条件发出 `wearing: "pass"` —— 那样的 `pass` 是个恒真值，它记录的不是"操作员
 * 确认过"，而是"操作员到过这一页"。PRD §13 的硬拦截语义已由「检测到戴反即阻断」
 * 改为「**未经确认即阻断**」，所以这里必须真的拦得住。
 *
 * P-06 也有一条勾选，但它问的是凭印象的"没戴反吧"，**发生在左右归属被摆出来之前**。
 * 真正能核对的信息只在本屏，闸也就必须在本屏。
 *
 * ## 对调会作废已有的确认
 *
 * 确认的对象是**当前显示的那一份归属**。先确认 A、再对调成 B，那份确认就不再针对
 * 屏幕上的东西了 —— 于是 `toggleSwap` 把 `confirmed` 清掉，操作员必须照新归属重看
 * 一遍。少这一行，闸会在最需要它的那一步（操作员发现戴反并纠正）失效。
 */

export function WearConfirmScreen({ onDone, onBack }) {
  const [swapped, setSwapped] = useState(false);
  const [confirmed, setConfirmed] = useState(false);

  // 左踝/右踝分别由哪个模块供数。swapped 时对调。
  const leftModule = swapped ? "右" : "左";
  const rightModule = swapped ? "左" : "右";

  function toggleSwap() {
    setSwapped((current) => !current);
    setConfirmed(false);
  }

  function finish() {
    // 未确认不出 pass。主按钮此刻本就是禁用的，这一行是为了让将来任何
    // "顺手把 disabled 去掉"的改动无法静默地把恒真值放出去 —— 闸的语义
    // 写在这里，而不是只写在按钮的可用性上。
    if (!confirmed) return;
    onDone({ wearing: "pass", swapped });
  }

  return (
    <WizardShell
      step={5}
      width="wide"
      title="确认左右"
      lead="数据左右归属的最后一道人工确认。若左右戴反了，点「对调左右」即可，无需重新佩戴。"
      actions={
        <>
          <Button variant="secondary" onClick={onBack}>返回佩戴引导</Button>
          <Button variant="secondary" onClick={toggleSwap}>
            {swapped ? "恢复左右" : "对调左右"}
          </Button>
          <Button size="lg" disabled={!confirmed} onClick={finish}>
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

          <label className="wear-confirm">
            <input
              type="checkbox"
              checked={confirmed}
              onChange={(event) => setConfirmed(event.target.checked)}
            />
            <span>已逐一核对：上面的左右归属与受试者实际佩戴一致。</span>
          </label>

          {!confirmed ? (
            <p className="wear-gate" role="status">
              核对并勾选后，才能开始检测。
            </p>
          ) : null}
        </div>
      </div>
    </WizardShell>
  );
}
