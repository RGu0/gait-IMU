import { BatteryPair, Button, DataTable, SideBadge, StatusPill } from "@gait/design-system";
import { AppBar } from "./AppBar.jsx";

function recordStatus(status) {
  return DataTable.status({
    tone: status === "待上传" ? "info" : "success",
    icon: status === "待上传" ? "spinner" : "check",
    label: status,
  });
}

export function HubScreen({ snapshot, onStartNewAssessment }) {
  const { deviceSummary, uploadSummary, recentRecords } = snapshot;
  const hasBattery = Number.isFinite(deviceSummary.leftBattery) && Number.isFinite(deviceSummary.rightBattery);

  return (
    <div className="hub-page">
      <AppBar />
      <main className="hub-content">
        <section className="hub-heading" aria-labelledby="hub-title">
          <div>
            <h1 id="hub-title">工作台</h1>
            <p>确认终端状态后，开始新的步态检测。</p>
          </div>
          <StatusPill tone="success" icon="check">设备已就绪</StatusPill>
        </section>

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
            <StatusPill tone="info" icon="spinner" spin={uploadSummary.pending > 0}>
              待上传 {uploadSummary.pending} 条
            </StatusPill>
            <p>已上传 {uploadSummary.uploaded} 条记录</p>
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
          <Button size="lg" onClick={onStartNewAssessment}>开始新的检测</Button>
        </div>
      </main>
    </div>
  );
}
