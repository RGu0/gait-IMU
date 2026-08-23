const fixture = {
  operator: null,
  // The session protocol the terminal is configured for right now. P-02 compares
  // it against the subject's previous run so the operator is told, before the
  // walk, that the two will not be comparable (C-13).
  protocolSeconds: 120,
  deviceSummary: {
    ready: false,
    issues: ["左侧传感器需要重新检查"],
  },
  uploadSummary: {
    pending: 0,
    uploaded: 12,
  },
  recentRecords: [
    { subjectLabel: "**1234", status: "已完成" },
    { subjectLabel: "**2345", status: "已完成" },
    { subjectLabel: "**3456", status: "待上传" },
    { subjectLabel: "**4567", status: "已完成" },
    { subjectLabel: "**5678", status: "已完成" },
  ],
};

/**
 * Three fixed lookups, one per state P-02 has to render. They are keyed by what
 * the operator types so a demo or a test can reach any state deterministically —
 * a random fixture would make the conflict path show up once in a while, which
 * is the same as never reviewing it.
 */
const SUBJECTS = {
  "2781": {
    kind: "found",
    subject: {
      maskedId: "**2781",
      ageBand: "60–74",
      sex: "女",
      lastAssessedAt: "2026-06-14",
      lastProtocolSeconds: 180, // differs from protocolSeconds on purpose
      consentValid: true,
    },
  },
  "3140": {
    kind: "found",
    subject: {
      maskedId: "**3140",
      ageBand: "40–59",
      sex: "男",
      lastAssessedAt: "2026-07-02",
      lastProtocolSeconds: 120, // matches — no comparability warning
      consentValid: false,
    },
  },
  "9000": {
    kind: "conflict",
    candidates: [
      { maskedId: "**9000", ageBand: "60–74", sex: "女", lastAssessedAt: "2026-05-08" },
      { maskedId: "**9000", ageBand: "60–74", sex: "女", lastAssessedAt: "2026-07-19" },
    ],
  },
};

function copiedSnapshot() {
  const snapshot = {
    ...fixture,
    operator: fixture.operator && { ...fixture.operator },
    deviceSummary: {
      ...fixture.deviceSummary,
      issues: [...fixture.deviceSummary.issues],
    },
    uploadSummary: { ...fixture.uploadSummary },
    recentRecords: fixture.recentRecords.map((record) => ({ ...record })),
  };

  if (snapshot.operator) {
    Object.freeze(snapshot.operator);
  }
  Object.freeze(snapshot.deviceSummary.issues);
  Object.freeze(snapshot.deviceSummary);
  Object.freeze(snapshot.uploadSummary);
  for (const record of snapshot.recentRecords) {
    Object.freeze(record);
  }
  Object.freeze(snapshot.recentRecords);
  return Object.freeze(snapshot);
}

let quickCreateCounter = 0;

/**
 * One finished session. `疲劳衰减` is deliberately uncomputable here: a metric
 * the terminal cannot produce must still occupy its slot and say why, rather
 * than vanish (C-7). A missing card reads as "there is no such measure"; an
 * empty one reads as "the measure is zero". Both are wrong.
 */
export const VALID_RESULT = Object.freeze({
  valid: true,
  fullReportNote: "完整分析正在后台生成，约 2 分钟内可见。",
  fullReportReady: false,
  validSteps: 118,
  symmetryIndex: "0.96",
  annotations: ["受试者使用了拄拐"],
  metrics: [
    { key: "speed", title: "步速", value: "1.04", unit: "m/s", grade: "normal" },
    { key: "cadence", title: "步频", value: "108", unit: "步/分", grade: "normal" },
    { key: "stride", title: "步长", value: "1.15", unit: "m", grade: "normal" },
    {
      key: "double-support",
      title: "双支撑期占比",
      value: "27.9",
      unit: "%",
      grade: "low",
      note: "两侧同步误差约 ±18 ms，本项仅供参考。",
    },
  ],
  comparison: [
    { label: "步长", left: 1.16, right: 1.14, unit: "m" },
    { label: "站立相时长", left: 0.62, right: 0.64, unit: "s" },
  ],
  variability: [
    { key: "stride-cv", title: "步长变异系数", value: "4.2", unit: "%", grade: "normal" },
    {
      key: "cycle-cv",
      title: "步周期变异系数",
      value: "",
      unit: "",
      grade: "uncomputable",
      note: "有效步数不足以估计步周期变异。",
    },
  ],
  conditions: [
    { label: "时长配置", value: "120 秒" },
    { label: "有效时长", value: "112 秒（93%）" },
    { label: "转身次数", value: "14" },
    { label: "辅助器具", value: "拄拐" },
    { label: "步行特征", value: "拖步" },
  ],
});

/** Exported so the invalid layout can be exercised without faking a bad walk. */
export const INVALID_RESULT = Object.freeze({
  valid: false,
  reason: "有效步行时长 96 秒，低于本次配置 180 秒的 70%（E-QLT-5002）。",
  advice: "请确认通道长度与转身标志位置，然后重新检测。",
});

/**
 * The pre-check fails on its first run and passes afterwards.
 *
 * Deliberate: the blocked state is the one people forget to look at, and a mock
 * that always passes means nobody ever sees it outside a test. Failing first
 * also makes 「重新检查」 do something visible, which is the only way to tell a
 * working retry from a button that merely re-renders.
 */
let preflightRuns = 0;

const PREFLIGHT_ITEMS = [
  { id: "link-left", label: "左模块连接", passHint: "已连接" },
  { id: "link-right", label: "右模块连接", passHint: "已连接" },
  { id: "factory-cal", label: "出厂标定参数", passHint: "已匹配" },
  { id: "disk", label: "磁盘空间", passHint: "充足" },
  { id: "battery", label: "左右模块电量", passHint: "左 82% · 右 76%" },
  { id: "arrival", label: "链路到达率", passHint: "观察 5 秒通过" },
  { id: "baseline", label: "静置基线", passHint: "两个模块均静置" },
];

export const mockTerminalAdapter = Object.freeze({
  async snapshot() {
    return copiedSnapshot();
  },

  async login({ organization, password }) {
    if (!organization || !password) {
      throw new Error("组织和密码均为必填项");
    }

    fixture.operator = { organization };
    return copiedSnapshot();
  },

  async recheckDevices() {
    fixture.deviceSummary.ready = true;
    fixture.deviceSummary.issues = [];
    return copiedSnapshot();
  },

  async lookupSubject(enteredId) {
    const hit = SUBJECTS[enteredId];
    if (!hit) {
      throw new Error("本机构没有这个档案号。请核对，或选择「无编号，快速建档」。");
    }
    return JSON.parse(JSON.stringify(hit));
  },

  /**
   * No identifying input at all — the record is a bare uuid until the optional
   * profile page, and that page can be skipped entirely (AC-02).
   */
  async createSubject() {
    quickCreateCounter += 1;
    return {
      maskedId: `临时${String(quickCreateCounter).padStart(3, "0")}`,
      ageBand: "未提供",
      sex: "未提供",
      lastAssessedAt: "—",
      lastProtocolSeconds: null,
      consentValid: false,
    };
  },

  /**
   * A short session so the screen can be walked through by hand. Real capture
   * is 60/120/180 s; 20 s here keeps a manual review to a few seconds without
   * changing any of the behaviour being reviewed.
   */
  startSession() {
    return {
      totalSeconds: 20,
      instruction: "请按平时走路的速度，在两个标志之间来回走",
      steps: { left: 0, right: 0 },
      link: { left: "good", right: "good" },
      footfalls: { left: [], right: [] },
      notices: [],
      aborted: null,
    };
  },

  /**
   * Emits the live sidebar values. Footfalls are appended at the moment they
   * happen — the strip draws real timestamps, never an evenly spaced animation,
   * because an evenly spaced one is a metronome (C-4).
   */
  subscribeSession(onUpdate, { intervalMs = 700 } = {}) {
    let left = 0;
    let right = 0;
    let x = 8;
    const leftMarks = [];
    const rightMarks = [];
    let ticks = 0;

    const id = setInterval(() => {
      ticks += 1;
      x = (x + 22) % 470 || 8;
      if (ticks % 2) {
        left += 1;
        leftMarks.push(x);
      } else {
        right += 1;
        rightMarks.push(x);
      }
      onUpdate({
        steps: { left, right },
        link: { left: "good", right: ticks > 8 && ticks < 12 ? "fair" : "good" },
        footfalls: { left: leftMarks.slice(-8), right: rightMarks.slice(-8) },
        notices: ticks === 6 ? ["已记录一次停顿，测试继续。"] : [],
      });
    }, intervalMs);

    return () => clearInterval(id);
  },

  async sessionResult() {
    return VALID_RESULT;
  },

  async runPreflight() {
    preflightRuns += 1;
    const firstRun = preflightRuns === 1;
    return PREFLIGHT_ITEMS.map((item) => {
      if (firstRun && item.id === "battery") {
        return {
          ...item,
          status: "fail",
          // Actionable only: what to do, not what went wrong internally.
          hint: "左模块电量不足 22%（E-BLE-1005）。请更换或充电后重新检查。",
        };
      }
      return { ...item, status: "pass", hint: item.passHint };
    });
  },
});
