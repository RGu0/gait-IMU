import { Button, StatusPill } from "@gait/design-system";
import { AppBar } from "./AppBar.jsx";

/**
 * 收尾或出报告失败（RAY-493）。
 *
 * 以前 `reportFor` 被拒（如 E-QLT-5003「没有可用的步态周期」）没人接，界面就停在
 * 采集页的「可以停下了」上，操作员只能关窗。现在接住它，并且**只排版 sidecar 给的
 * 现象、动作与码**（RAY-248：渲染进程不得自造错误文案）。
 *
 * 错误形状有三种来源：`TerminalFailure`（message/action/code）、`SidecarDown`
 * （notice.message/notice.action，无码）、以及其他异常（只有 message）。缺哪段就不画哪段。
 */
export function ReportErrorScreen({ error, onBackToHub, onRetry, title = "报告未能生成" }) {
  const message = error?.notice?.message ?? error?.message;
  const action = error?.notice?.action ?? error?.action;
  const code = error?.code;

  return (
    <div className="invalid-page">
      <AppBar />
      <main className="invalid-body" role="alert" aria-label={title}>
        <StatusPill tone="warning" icon="warning">{title}</StatusPill>
        <h1>{title}</h1>
        {message ? <p className="invalid-reason">{message}</p> : null}
        {action ? <p className="invalid-advice">{action}</p> : null}
        {code ? <p className="invalid-advice">错误码 {code}</p> : null}
        <div className="invalid-actions">
          {onRetry ? <Button size="lg" onClick={onRetry}>重新检测</Button> : null}
          <Button variant="secondary" onClick={onBackToHub}>返回工作台</Button>
        </div>
      </main>
    </div>
  );
}
