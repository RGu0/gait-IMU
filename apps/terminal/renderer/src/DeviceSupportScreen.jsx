import { useState } from "react";
import { BatteryPair, Button, Dialog, SideBadge, StatusPill } from "@gait/design-system";
import { AppBar } from "./AppBar.jsx";

/**
 * P-10c — devices and support.
 *
 * Two things the spec is firm about:
 *
 * · **Re-pairing asks twice and is written to the audit log.** Re-pairing
 *   silently rebinds which physical module is "left"; if it happens by a
 *   mis-tap, every subsequent session is mirrored and each individual metric
 *   still looks plausible. A confirmation is cheap next to that.
 *
 * · **No engineering entry point is visible here.** Not greyed out, not behind
 *   a long-press — absent. A control that exists on screen will eventually be
 *   pressed by someone who was told to "try things".
 */

function ModuleCard({ module }) {
  return (
    <article className="module-card">
      <header>
        <SideBadge side={module.side} size={24} />
        <span>{module.side === "left" ? "左侧模块" : "右侧模块"}</span>
        <StatusPill
          tone={module.factoryCalibrated ? "success" : "danger"}
          icon={module.factoryCalibrated ? "check" : "x"}
        >
          {module.factoryCalibrated ? "出厂标定已匹配" : "缺少出厂标定"}
        </StatusPill>
      </header>
      <dl className="review-list">
        <div className="review-row"><dt>设备地址</dt><dd className="review-row__value">{module.maskedAddress}</dd></div>
        <div className="review-row"><dt>固件版本</dt><dd className="review-row__value">{module.firmware}</dd></div>
        <div className="review-row"><dt>上次连接</dt><dd className="review-row__value">{module.lastConnected}</dd></div>
      </dl>
    </article>
  );
}

export function DeviceSupportScreen({ devices, support, onRecheck, onRepair, onNavigate }) {
  const [confirmingRepair, setConfirmingRepair] = useState(false);

  return (
    <div className="page">
      <AppBar current="设备与支持" onNavigate={onNavigate} />
      <main className="page-body">
        <h1>设备与支持</h1>

        <section className="module-grid" aria-label="采集模块">
          {devices.modules.map((module) => (
            <ModuleCard key={module.side} module={module} />
          ))}
        </section>

        <section className="device-battery" aria-label="模块电量">
          <h2>电量</h2>
          <BatteryPair left={devices.leftBattery} right={devices.rightBattery} />
        </section>

        <div className="device-actions">
          <Button variant="secondary" onClick={onRecheck}>重新检查</Button>
          <Button variant="secondary" onClick={() => setConfirmingRepair(true)}>重新配对模块</Button>
        </div>

        <section className="support-info" aria-label="支持信息">
          <h2>支持</h2>
          <dl className="review-list">
            <div className="review-row"><dt>服务电话</dt><dd className="review-row__value">{support.phone}</dd></div>
            <div className="review-row"><dt>终端编号</dt><dd className="review-row__value">{support.terminalId}</dd></div>
            <div className="review-row"><dt>软件版本</dt><dd className="review-row__value">{support.appVersion}</dd></div>
            <div className="review-row"><dt>算法版本</dt><dd className="review-row__value">{support.algoVersion}</dd></div>
          </dl>
        </section>
      </main>

      <Dialog
        open={confirmingRepair}
        title="确定要重新配对模块吗？"
        confirmLabel="重新配对"
        cancelLabel="取消"
        onConfirm={() => {
          setConfirmingRepair(false);
          onRepair();
        }}
        onCancel={() => setConfirmingRepair(false)}
      >
        重新配对会重新绑定左右模块，操作将被记录。请确认两个模块都在手边并已开机。
      </Dialog>
    </div>
  );
}
