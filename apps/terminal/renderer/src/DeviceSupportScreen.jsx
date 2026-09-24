import { useState } from "react";
import { Banner, BatteryPair, Button, Dialog, SideBadge, StatusPill } from "@gait/design-system";
import { AppBar } from "./AppBar.jsx";

/**
 * P-10c — devices and support.
 *
 * Two things the spec is firm about:
 *
 * · **Re-pairing asks twice and is written to the audit log** (RAY-479: the sidecar
 *   appends every bind to `device-binding-log.jsonl` under GAIT_CONFIG_ROOT). Re-pairing
 *   silently rebinds which physical module is "left"; if it happens by a
 *   mis-tap, every subsequent session is mirrored and each individual metric
 *   still looks plausible. A confirmation is cheap next to that.
 *
 * · **No engineering entry point is visible here.** Not greyed out, not behind
 *   a long-press — absent. A control that exists on screen will eventually be
 *   pressed by someone who was told to "try things".
 */

function checkedTime(at) {
  const pad = (n) => String(n).padStart(2, "0");
  return `${pad(at.getHours())}:${pad(at.getMinutes())}:${pad(at.getSeconds())}`;
}

/** 出厂标定一栏的三种状态。预览放行与自检的 `waived`、报告注记同一口径（RAY-530）。 */
function calibrationPill(module) {
  if (module.factoryCalibrated) return { tone: "success", icon: "check", label: "出厂标定已匹配" };
  if (module.factoryCalibrationWaived) return { tone: "warning", icon: "warning", label: "未匹配（预览放行）" };
  return { tone: "danger", icon: "x", label: "缺少出厂标定" };
}

function ModuleCard({ module }) {
  const pill = calibrationPill(module);
  return (
    <article className="module-card">
      <header>
        <SideBadge side={module.side} size={24} />
        <span>{module.side === "left" ? "左侧模块" : "右侧模块"}</span>
        <StatusPill tone={pill.tone} icon={pill.icon}>
          {pill.label}
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

export function DeviceSupportScreen({
  devices,
  support,
  onRecheck,
  onRepair,
  onNavigate,
  rechecking = false,
  recheckError = null,
  checkedAt = null,
  onDismissRecheckError,
}) {
  const [confirmingRepair, setConfirmingRepair] = useState(false);

  return (
    <div className="page">
      <AppBar current="设备与支持" onNavigate={onNavigate} />
      <main className="page-body">
        <h1>设备与支持</h1>

        {recheckError ? (
          <Banner tone="warning" aria-label="重新检查设备失败" onClose={onDismissRecheckError}>
            {recheckError}
          </Banner>
        ) : null}

        <section className="module-grid" aria-label="采集模块">
          {(devices.modules ?? []).map((module) => (
            <ModuleCard key={module.side} module={module} />
          ))}
        </section>

        <section className="device-battery" aria-label="模块电量">
          <h2>电量</h2>
          {Number.isFinite(devices.leftBattery) && Number.isFinite(devices.rightBattery) ? (
            <BatteryPair left={devices.leftBattery} right={devices.rightBattery} />
          ) : (
            <p>电量未读取</p>
          )}
        </section>

        <div className="device-actions">
          <Button variant="secondary" onClick={onRecheck} loading={rechecking} loadingText="正在重新检查…">
            重新检查
          </Button>
          <Button variant="secondary" onClick={() => setConfirmingRepair(true)}>重新配对模块</Button>
        </div>
        {checkedAt && !rechecking ? (
          <p className="device-checked" role="status">
            已重新检查（{checkedTime(checkedAt)}）
          </p>
        ) : null}

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
        重新配对会依次绑定蓝色（左脚）和橙色（右脚）模块，操作将被记录。请把两个模块都放在手边，先全部关机，按向导提示逐个打开。
      </Dialog>
    </div>
  );
}
