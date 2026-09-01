/**
 * 「这一步还没接通」的统一呈现。
 *
 * ## 为什么它必须是一屏，而不是一行小字
 *
 * 本 scope 接线时，会话标定（RAY-208）与本地基础报告（RAY-224）都还不存在。处理这种
 * 缺口有三种做法，两种是错的：
 *
 * 1. 静默跳过 —— 流程看起来走完了，于是「操作员流程已验证」这句话会被人当真；
 * 2. 用 mock 数据填上 —— 更糟，它连「没走完」这个线索都抹掉了；
 * 3. 显式停在这里，说清楚缺什么、归谁 —— 只有这个不会骗人。
 *
 * 一个看起来能走完、实则中间两步是假的流程，比一个诚实断在半路的流程危险得多。
 * 所以这一屏是**刻意显眼**的：它不是错误（没有错误码，因为设备没出任何问题），
 * 也不是成功，它是第三种结局。
 *
 * ## 为什么带 Issue 号
 *
 * 「暂不可用」无从追查。写上 RAY-208 / RAY-224，看到这屏的人（可能是三个月后的自己）
 * 能立刻知道去哪看它什么时候会好。`issue` 允许为空 —— 那表示这个缺口**还没有 Issue
 * 认领**，那本身就是要报告的事，编一个号会把它藏起来。
 */
import { Button } from "@gait/design-system";
import { WizardShell } from "./WizardShell.jsx";

export function CapabilityGap({ gap, step, onContinue, onBack, continueLabel, details }) {
  return (
    <WizardShell
      step={step}
      width="wide"
      title="本步骤尚未接通"
      lead={gap.issue ? `跟踪于 ${gap.issue}` : "尚无 Issue 认领这个缺口"}
      actions={
        <>
          <Button variant="secondary" onClick={onBack}>返回工作台</Button>
          {onContinue ? (
            <Button size="lg" onClick={onContinue}>{continueLabel}</Button>
          ) : null}
        </>
      }
    >
      {details}
      <div className="capability-gap" role="status" data-capability={gap.capability}>
        <p className="capability-gap__summary">{gap.summary}</p>
        <p className="capability-gap__note">
          这一步没有运行，也没有结果。继续下去的话，本次会话不包含这一步 ——
          界面不会用占位数据冒充它跑过。
        </p>
      </div>
    </WizardShell>
  );
}
