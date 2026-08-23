import { useState } from "react";
import { DataTable, StatusPill } from "@gait/design-system";
import { AppBar } from "./AppBar.jsx";

/**
 * P-10a — screening records.
 *
 * The status column is the load-bearing part: it carries text, an icon and a
 * colour at once. An operator scanning a list for the one session that failed
 * quality is doing it under time pressure and often on a terminal whose colour
 * rendering nobody has ever checked. Colour alone would make that row findable
 * only by luck.
 */

const STATUS_TONE = {
  "已完成（完整版）": { tone: "success", icon: "check" },
  "已完成（基础版）": { tone: "success", icon: "check" },
  处理中: { tone: "info", icon: "spinner", spin: true },
  未通过质检: { tone: "warning", icon: "warning" },
  上传中: { tone: "info", icon: "spinner", spin: true },
};

function statusCell(status) {
  const shape = STATUS_TONE[status] ?? { tone: "info", icon: "check" };
  return DataTable.status({ ...shape, label: status });
}

const ANY = "全部";

export function RecordsScreen({ records, onOpenRecord, onNavigate }) {
  const [status, setStatus] = useState(ANY);
  const [protocol, setProtocol] = useState(ANY);

  const statuses = [ANY, ...new Set(records.map((r) => r.status))];
  const protocols = [ANY, ...new Set(records.map((r) => r.protocol))];

  const shown = records.filter(
    (record) =>
      (status === ANY || record.status === status) &&
      (protocol === ANY || record.protocol === protocol),
  );

  return (
    <div className="page">
      <AppBar current="检测记录" onNavigate={onNavigate} />
      <main className="page-body">
        <h1>检测记录</h1>

        <div className="filter-bar">
          <label className="filter">
            <span>状态</span>
            <select value={status} onChange={(event) => setStatus(event.target.value)}>
              {statuses.map((option) => <option key={option}>{option}</option>)}
            </select>
          </label>
          <label className="filter">
            <span>时长配置</span>
            <select value={protocol} onChange={(event) => setProtocol(event.target.value)}>
              {protocols.map((option) => <option key={option}>{option}</option>)}
            </select>
          </label>
          <span className="filter-count">{shown.length} / {records.length} 条</span>
        </div>

        {shown.length ? (
          <DataTable
            aria-label="检测记录"
            columns={[
              { key: "assessedAt", header: "时间" },
              { key: "subjectLabel", header: "受检者编号" },
              { key: "protocol", header: "时长配置" },
              { key: "validSteps", header: "有效步数", numeric: true },
              { key: "status", header: "状态", render: statusCell },
              { key: "reportVersion", header: "报告版本" },
            ]}
            rows={shown}
            onRowAction={(row) => onOpenRecord(row)}
            actionLabel="查看"
          />
        ) : (
          <p className="empty-note">当前筛选条件下没有记录。</p>
        )}
      </main>
    </div>
  );
}
