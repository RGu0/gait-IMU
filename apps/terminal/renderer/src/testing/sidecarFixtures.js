/**
 * 真 sidecar 的应答形状（RAY-493）。
 *
 * 取自 2026-09-15 对 `uv run --no-sync python -m gait.app` 的一次真实往返
 * （GAIT_SESSION_ROOT 指向临时目录），只删了与本测试无关的长字段。mock adapter 直接
 * 给视图形状，所以浏览器里走通的流程在真 sidecar 上一步一崩 —— 这份夹具就是为了让
 * 测试看见的是后者。并行 scope 将新增的字段（source / preview / createdAt / complete /
 * waived）单独标注，缺席时 adapter 也必须能走。
 */
import { IPC_CONTRACT_VERSION } from "@gait/terminal-contract";

export const V = IPC_CONTRACT_VERSION;

export const SNAPSHOT = {
  operator: null,
  protocolSeconds: 180,
  deviceSummary: { ready: true, issues: [] },
  uploadSummary: {
    tracked: true, sessions: 2, bytes: 0, oldest_age_s: 0.0, conflicts: 0,
    over_sessions: false, over_bytes: false, warn: false, drain: null,
  },
  ipcContractVersion: V,
};

export const CREATE_SUBJECT = { subjectUuid: "e8e02054-48ff-402d-ab03-687b57f9140f", consentValid: false };

export const RECORDS = [
  { id: "20260914T010203Z-11111111", subjectUuid: "abcd1234-0000-0000-0000-000000000000", protocolSeconds: 120, algoVersion: "gait-contract-1.1" },
  // 并行 scope 的新字段：createdAt / complete / source
  { id: "20260915T072802Z-6746526d", subjectUuid: "e12b17b4-51e4-4f8b-bb26-6caf5803e6ef", protocolSeconds: 180, algoVersion: "gait-contract-1.1", createdAt: "2026-09-15T07:28:02+00:00", complete: false, source: "stub" },
];

export const DEVICE_SUPPORT = {
  modules: [
    { side: "left", maskedAddress: "…:9A:4C", batteryPercent: 82, factoryCalibrated: false },
    { side: "right", maskedAddress: "…:9A:51", batteryPercent: 76, factoryCalibrated: false },
  ],
  ipcContractVersion: V,
};

export const PREFLIGHT = [
  { id: "link-l", label: "左模块连接", status: "pass", hint: "已连接", error: null },
  { id: "link-r", label: "右模块连接", status: "pass", hint: "已连接", error: null },
  {
    id: "factory-cal", label: "出厂标定参数", status: "fail", hint: null,
    error: { code: "E-CAL-3001", domain: "E-CAL", message: "有模块没有匹配到出厂标定参数。", action: "请联系服务方按模块 MAC 下发。", blocking: true },
  },
  { id: "disk", label: "磁盘空间", status: "pass", hint: "剩余 64 GB", error: null },
  { id: "battery", label: "左右模块电量", status: "pass", hint: "左 82% · 右 76%", error: null },
  { id: "arrival", label: "链路到达率", status: "pass", hint: "左 99% · 右 98%", error: null },
];

/** 并行 scope 的 waived：不拦路，带 hint，error 为 null。 */
export const PREFLIGHT_WAIVED = PREFLIGHT.map((item) =>
  item.id === "factory-cal"
    ? {
        ...item,
        status: "waived",
        hint: "预览版：未匹配出厂标定参数，已按预览策略放行，报告将注明。",
        error: null,
        waiver: { code: "E-CAL-3001", reasons: ["左：这台模块没有出厂标定参数。"] },
      }
    : item,
);

export const START_SESSION = {
  totalSeconds: 180,
  instruction: "请按平时走路的速度，在两个标志之间来回走",
  steps: { left: 0, right: 0 },
  link: { left: "good", right: "good" },
  remainingSeconds: 180,
  sessionId: "20260915T072802Z-6746526d",
};

export const STOP_SESSION = { state: "finished", validSeconds: 2.0, capture: { complete: true, chunks_written: { L: 0, R: 0 }, problems: [] } };

export const SESSION_RESULT_READY = {
  overall: "indeterminate",
  verdict: { wearing: "pass", link: "pass", duration: "pass", overall: "indeterminate", reasons: [] },
  integrity: { complete: true, problems: [], links: [] },
  validSteps: 0,
  report: { status: "ready", sessionId: "20260915T072802Z-6746526d" },
};

export const REPORT_NO_CYCLES = {
  code: "E-QLT-5003", domain: "E-QLT",
  message: "这次检测没有可用的步态周期，无法生成报告。",
  action: "请确认会话有效性判定的结果；会话级无效不生成报告。",
  blocking: true,
};

export const ok = (id, result) => ({ kind: "response", v: V, id, status: "ok", result });
export const err = (id, error) => ({ kind: "response", v: V, id, status: "error", error });

/**
 * 按方法名回放的假传输。`handlers[method]` 可以是值、函数（收 params，返回值），
 * 或 `{ error }`。每次请求记进 `calls`。
 */
export function fakeTransport(handlers) {
  const calls = [];
  const transport = async (request) => {
    calls.push(request);
    const handler = handlers[request.method];
    if (handler === undefined) throw new Error(`fake sidecar: 未登记 ${request.method}`);
    const value = typeof handler === "function" ? await handler(request.params) : handler;
    if (value && value.__error) return err(request.id, value.__error);
    return ok(request.id, value);
  };
  return { transport, calls };
}

export const fail = (error) => ({ __error: error });
