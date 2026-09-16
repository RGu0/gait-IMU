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
 * ## 没有「对调」（RAY-479，2026-09-16 用户拍板）
 *
 * 这里曾有一个「一键对调」：戴反了就在软件里把左右数据归属换过来。它被删掉了。左右
 * 现在只由配对时绑定的模块决定 —— **蓝色模块是左脚、橙色模块是右脚** —— 于是「戴反」
 * 只剩一种纠正方式：让受试者按颜色重新戴。一个能在任何一场里随手翻转左右的按钮，
 * 会让绑定这件事失去意义，而翻错时每个指标单看都像真的。
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
 */

/** 物理外壳颜色的提醒。颜色是外壳的，不是屏幕上的左右识别色。 */
export const COLOR_REMINDER = "蓝色模块戴左脚、橙色模块戴右脚";

export function WearConfirmScreen({ onDone, onBack }) {
  const [confirmed, setConfirmed] = useState(false);

  function finish() {
    // 未确认不出 pass。主按钮此刻本就是禁用的，这一行是为了让将来任何
    // "顺手把 disabled 去掉"的改动无法静默地把恒真值放出去 —— 闸的语义
    // 写在这里，而不是只写在按钮的可用性上。
    if (!confirmed) return;
    onDone({ wearing: "pass" });
  }

  return (
    <WizardShell
      step={5}
      width="wide"
      title="确认左右"
      lead="数据左右归属的最后一道人工确认。左右由配对时绑定的模块决定；若戴反了，请按颜色让受试者重新佩戴。"
      actions={
        <>
          <Button variant="secondary" onClick={onBack}>返回佩戴引导</Button>
          <Button size="lg" disabled={!confirmed} onClick={finish}>
            确认无误，开始检测
          </Button>
        </>
      }
    >
      <div className="two-column">
        <div className="two-column__text">
          <p className="wear-color-reminder" aria-label="佩戴颜色提醒">
            <strong>{COLOR_REMINDER}</strong>
          </p>
          <ul className="wear-points">
            <li>
              <SideBadge side="left" size={20} />
              <strong>受试者左踝</strong>
              <span>← 蓝色模块</span>
            </li>
            <li>
              <SideBadge side="right" size={20} />
              <strong>受试者右踝</strong>
              <span>← 橙色模块</span>
            </li>
          </ul>

          <label className="wear-confirm">
            <input
              type="checkbox"
              checked={confirmed}
              onChange={(event) => setConfirmed(event.target.checked)}
            />
            <span>已逐一核对：蓝色模块在受试者左踝、橙色模块在受试者右踝。</span>
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
