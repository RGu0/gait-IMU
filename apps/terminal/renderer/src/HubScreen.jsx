import { Banner, BatteryPair, Button, DataTable, SideBadge, StatusPill } from "@gait/design-system";
import { AppBar } from "./AppBar.jsx";

const ATTENTION_STATUSES = new Set(["不完整", "未正常结束", "未通过质检"]);
const PENDING_STATUSES = new Set(["待上传", "状态未知"]);

function recordStatus(status) {
  if (ATTENTION_STATUSES.has(status)) {
    return DataTable.status({ tone: "warning", icon: "warning", label: status });
  }
  return DataTable.status({
    tone: PENDING_STATUSES.has(status) ? "info" : "success",
    icon: status === "待上传" ? "spinner" : "check",
    label: status,
  });
}

export function HubScreen({ snapshot, onRecheck, onStartNewAssessment, onNavigate }) {
  const { deviceSummary, uploadSummary = {}, recentRecords = [] } = snapshot;
  const issues = deviceSummary.issues ?? [];
  // sidecar 没有上传队列时说「没在记账」，界面也照说，不报一个 0（见 service._upload_summary）。
  const uploadTracked = uploadSummary.tracked !== false;
  const pending = Number.isFinite(uploadSummary.pending) ? uploadSummary.pending : 0;
  const hasBattery = Number.isFinite(deviceSummary.leftBattery) && Number.isFinite(deviceSummary.rightBattery);
  const needsAttention = !deviceSummary.ready;

  return (
    <div className="hub-page">
      <AppBar current="工作台" onNavigate={onNavigate} />
      <main className="hub-content">
        <section className="hub-heading" aria-labelledby="hub-title">
          <div>
            <h1 id="hub-title">工作台</h1>
            <p>确认终端状态后，开始新的步态检测。</p>
          </div>
          <StatusPill tone={needsAttention ? "warning" : "success"} icon={needsAttention ? "warning" : "check"}>
            {needsAttention ? "设备需要检查" : "设备已就绪"}
          </StatusPill>
        </section>

        {needsAttention ? issues.map((issue) => (
          <Banner key={issue} tone="warning" title="设备需要检查">{issue}</Banner>
        )) : null}

        <section className="hub-summary" aria-label="终端状态概览">
          <article className="hub-card">
            <h2>双侧采集模块</h2>
            <div className="device-identities">
              <div><SideBadge side="left" size={24} />左侧模块</div>
              <div><SideBadge side="right" size={24} />右侧模块</div>
            </div>
            {hasBattery ? <BatteryPair left={deviceSummary.leftBattery} right={deviceSummary.rightBattery} /> : null}
          </article>
          <article className="hub-card">
            <h2>数据上传</h2>
            {uploadTracked ? (
              <StatusPill tone="info" icon="spinner" spin={pending > 0}>
                待上传 {pending} 条
              </StatusPill>
            ) : (
              <StatusPill tone="info" icon="check">本机未接入上传</StatusPill>
            )}
            {Number.isFinite(uploadSummary.uploaded) ? <p>已上传 {uploadSummary.uploaded} 条记录</p> : null}
          </article>
        </section>

        <section className="recent-records" aria-labelledby="recent-records-title">
          <div className="recent-records__heading">
            <h2 id="recent-records-title">最近检测记录</h2>
            <span>{recentRecords.length} 条</span>
          </div>
          <DataTable
            aria-label="最近检测记录"
            columns={[
              { key: "subjectLabel", header: "受检者编号" },
              { key: "status", header: "状态", render: recordStatus },
            ]}
            rows={recentRecords}
          />
        </section>

        <div className="hub-action">
          {needsAttention ? (
            <Button size="lg" onClick={onRecheck}>重新检查设备</Button>
          ) : (
            <Button size="lg" onClick={onStartNewAssessment}>开始新的检测</Button>
          )}
        </div>
      </main>
    </div>
  );
}
