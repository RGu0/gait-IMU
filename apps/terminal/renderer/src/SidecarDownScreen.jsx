/**
 * sidecar 不在时的整页接管。
 *
 * ## 为什么是整页，而不是一条 banner
 *
 * UI 设计 §7 的原则是「采集中链路波动只改侧栏图标，不弹窗」—— 那是因为链路波动时
 * **检测仍在进行**，打断它才是伤害。sidecar 没了不一样：此刻**什么都在进行不下去**，
 * 所有按钮点下去都只会再失败一次。一条 banner 会让界面看起来还能用。
 *
 * ## 为什么这里不显示错误码
 *
 * 它没有码。六域说的是采集现场出了什么事，而进程没了不是其中任何一种 —— 编一个
 * `E-BLE` 会让操作员去排查一根好好的蓝牙链路。文案由主进程给出（见
 * `sidecarSupervisor.js`），这一屏只排版。
 */
import { Button } from "@gait/design-system";

export function SidecarDownScreen({ notice, onRetry }) {
  return (
    <div className="sidecar-down" role="alert" data-recoverable={String(notice.recoverable !== false)}>
      <div className="sidecar-down__panel">
        <p className="sidecar-down__message">{notice.message}</p>
        <p className="sidecar-down__action">{notice.action}</p>
        {notice.recoverable === false && onRetry ? (
          <Button size="lg" onClick={onRetry}>返回工作台</Button>
        ) : null}
      </div>
    </div>
  );
}
