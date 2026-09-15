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
  // 真 sidecar 的会话三态（RAY-493）
  完成: { tone: "success", icon: "check" },
  不完整: { tone: "warning", icon: "warning" },
  未正常结束: { tone: "warning", icon: "warning" },
};

/**
 * 会话结局的配色（RAY-496）。按 `statusKind` 查，不按文案查 —— 「已停止（5/60 秒）」
 * 的文案里带着数字，按文案查永远查不到，然后落进兜底的绿勾，而一个中途停掉的检测
 * 画成绿勾，比不画还糟。
 *
 * `stopped` 用 neutral 而不是 warning：操作员自己按的停止不是故障，它只是**不是**
 * 一次完整检测。warning 留给真出了事的三种 —— 丢块、中断、没正常收尾 —— 否则
 * 警告色出现得太频繁，就没人再看它。
 */
const KIND_TONE = {
  finished: { tone: "success", icon: "check" },
  stopped: { tone: "neutral", icon: "dot" },
  aborted: { tone: "warning", icon: "warning" },
  incomplete: { tone: "warning", icon: "warning" },
  unfinished: { tone: "warning", icon: "warning" },
};

export function statusShapeOf(status, kind) {
  return KIND_TONE[kind] ?? STATUS_TONE[status] ?? { tone: "info", icon: "check" };
}

function statusCell(status, row) {
  return DataTable.status({ ...statusShapeOf(status, row?.statusKind), label: status });
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
