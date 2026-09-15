/**
 * 说 IPC 契约的真实 adapter。
 *
 * ## 它为什么不认识传输
 *
 * 构造时注入一个 `transport(request) -> Promise<response>`，与 Python 侧
 * `TerminalService.handle()` 同样不认识 stdio 的理由一样：传输形态属于 RAY-250
 * （Electron 主进程怎么拉起 sidecar）。把 `window.electron.invoke` 焊在这里，会让那个
 * 决定变成一次重写，也会让契约在打包完成之前无法被端到端验证。
 *
 * ## 未实现不是错误，也不是成功
 *
 * `runCalibration` / `lookupSubject` 在缺口未接通时**返回**一个带 `unimplemented`
 * 的值，而不是 throw。throw 会让界面显示一个查无此事的故障；返回占位数据则是拿假
 * 结果冒充真结果。返回一个显式的缺口，界面就必须把它画出来 —— 这正是本 scope 要的：
 * 诚实地断在半路，而不是看起来走完了。`reportFor` 在 RAY-345 已接通，不再返回缺口。
 */
import {
  STATUS_OK,
  STATUS_UNIMPLEMENTED,
  interpret,
} from "@gait/terminal-contract";

/**
 * sidecar 进程本身不在了。
 *
 * 这**不是** `TerminalFailure` —— 它没有六域错误码，因为它不属于六个域中的任何一个：
 * 那六个说的是采集现场出了什么事，而进程没了是另一回事。文案由**主进程**给出
 * （sidecar 死了写不了自己的讣告），渲染端同样只排版不改写。
 */
export class SidecarDown extends Error {
  constructor(notice) {
    super(notice.message);
    this.name = "SidecarDown";
    this.notice = notice;
    this.recoverable = notice.recoverable !== false;
  }
}

/** 携带 sidecar 给的码与动作。渲染端只排版这三段，不改写。 */
export class TerminalFailure extends Error {
  constructor(error) {
    super(error.message);
    this.name = "TerminalFailure";
    this.code = error.code;
    this.domain = error.domain;
    this.action = error.action;
    this.blocking = error.blocking;
  }
}

/**
 * 视图形状归渲染端（RAY-493）。
 *
 * sidecar 返回的是**数据**形状（`subjectUuid`、`protocolSeconds`、`modules`），界面
 * 读的是**视图**形状（`maskedId`、`protocol`、`devices.modules`）。翻译只在这里做一次：
 * 散在各屏里各翻一遍，就是每一屏各自在真 sidecar 上崩一次的原因 —— mock 恰好直接
 * 给视图形状，所以浏览器里从来看不出来。
 *
 * 缺字段时给**说得出含义的文字**（「未提供」「未记录」），不给空白、0 或破折号 ——
 * 那三者在屏上读起来都像一个测到的值。
 */
export const SUBJECT_UNKNOWN = "未提供";
export const NOT_RECORDED = "未记录";
export const NOT_CONFIGURED = "未配置";

/** 临时受检者的显示标签：uuid 前 4 位。不含任何身份明文（FR-02）。 */
export function subjectLabelOf(subjectUuid) {
  if (typeof subjectUuid !== "string" || subjectUuid.length === 0) return SUBJECT_UNKNOWN;
  return `临时-${subjectUuid.slice(0, 4).toUpperCase()}`;
}

const SESSION_ID_TIME = /^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})Z/;

function pad(value) {
  return String(value).padStart(2, "0");
}

function dateOf(record) {
  if (typeof record?.createdAt === "string") {
    const parsed = new Date(record.createdAt);
    if (!Number.isNaN(parsed.getTime())) return parsed;
  }
  if (typeof record?.id === "string") {
    const m = SESSION_ID_TIME.exec(record.id);
    if (m) return new Date(Date.UTC(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +m[6]));
  }
  return null;
}

/** 会话时间：优先 `createdAt`（ISO），否则从会话 id `YYYYMMDDTHHMMSSZ-xxxx` 解出。本地时间显示。 */
export function assessedAtOf(record) {
  const date = dateOf(record);
  if (!date) return NOT_RECORDED;
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

/** `complete` 三态：true 完成 / false 采集不完整 / null 进程没正常收尾。字段缺失另说。 */
export function recordStatusOf(complete) {
  if (complete === true) return "完成";
  if (complete === false) return "不完整";
  if (complete === null) return "未正常结束";
  return "状态未知";
}

/**
 * 走满协议算「完成」的容差，单位秒。
 *
 * 收尾由渲染端倒计时触发，`now` 取自它自己的时钟，所以走满 60 秒的会话实测落在
 * 60 秒上下几十毫秒。没有容差，正常走完的检测会被写成「已停止（60/60 秒）」。
 */
const FULL_WALK_TOLERANCE_S = 1;

/**
 * 会话结局（RAY-496）。
 *
 * 在这之前状态只看 `complete`，而 `complete` 说的是**写队列有没有丢块**，
 * 回答不了「走满了没有」—— 于是走满 60 秒与第 5 秒手动停止都显示「完成」。
 * 现在先看协议侧的事实；只有本次改动之前落盘的旧会话（磁盘上没有这些字段）
 * 才退回 `complete` 三态，而不是替它编一个结局。
 */
export function recordOutcomeOf(record) {
  // 丢块比走没走满严重：它说的是这份数据本身不可信。先说这个。
  if (record?.complete === false) return { status: "不完整", kind: "incomplete" };
  const state = record?.protocolState;
  if (typeof state !== "string") {
    return { status: recordStatusOf(record?.complete), kind: "legacy" };
  }
  if (state === "aborted") return { status: "已中断", kind: "aborted" };
  // `finished` 是唯一走到终点的状态。停在 `walking` 之类的，是收尾前进程就没了。
  if (state !== "finished") return { status: "未正常结束", kind: "unfinished" };
  const elapsed = record?.elapsedSeconds;
  const configured = record?.protocolSeconds;
  if (!Number.isFinite(elapsed) || !Number.isFinite(configured)) {
    return { status: recordStatusOf(record?.complete), kind: "legacy" };
  }
  if (elapsed + FULL_WALK_TOLERANCE_S >= configured) return { status: "完成", kind: "finished" };
  return { status: `已停止（${Math.round(elapsed)}/${configured} 秒）`, kind: "stopped" };
}

function sortKey(record) {
  return dateOf(record)?.getTime() ?? -Infinity;
}

export function toRecordView(record) {
  const seconds = record?.protocolSeconds;
  const outcome = recordOutcomeOf(record);
  return {
    ...record,
    id: record?.id,
    assessedAt: assessedAtOf(record),
    subjectLabel: subjectLabelOf(record?.subjectUuid),
    protocol: Number.isFinite(seconds) ? `${seconds} 秒` : NOT_RECORDED,
    status: outcome.status,
    // 「已停止（5/60 秒）」的文案带着数字，按文案查配色会查不到。
    // 配色查这个稳定的键，文案只管给人看。
    statusKind: outcome.kind,
    reportVersion: record?.id ?? NOT_RECORDED,
    validSteps: Number.isFinite(record?.validSteps) ? record.validSteps : "未统计",
  };
}

export function toRecordViews(records) {
  if (!Array.isArray(records)) return [];
  return [...records]
    .sort((a, b) => sortKey(b) - sortKey(a))
    .map(toRecordView);
}

function toUploadSummary(summary) {
  const raw = summary && typeof summary === "object" ? summary : {};
  const pendingCount = Number.isFinite(raw.pending) ? raw.pending : raw.sessions;
  return {
    ...raw,
    pending: Number.isFinite(pendingCount) ? pendingCount : 0,
  };
}

function toModuleView(module) {
  return {
    ...module,
    firmware: module?.firmware ?? NOT_RECORDED,
    lastConnected: module?.lastConnected ?? NOT_RECORDED,
    factoryCalibrated: Boolean(module?.factoryCalibrated),
  };
}

export function toDeviceSupportView(raw) {
  if (raw?.devices && raw?.support) return raw;
  const modules = Array.isArray(raw?.modules) ? raw.modules.map(toModuleView) : [];
  const battery = (side) => modules.find((m) => m.side === side)?.batteryPercent;
  return {
    devices: {
      leftBattery: battery("left"),
      rightBattery: battery("right"),
      modules,
    },
    support: {
      phone: NOT_CONFIGURED,
      terminalId: NOT_CONFIGURED,
      appVersion: NOT_CONFIGURED,
      algoVersion: NOT_CONFIGURED,
      ...(raw?.support ?? {}),
    },
    ipcContractVersion: raw?.ipcContractVersion,
  };
}

export function toSubjectView(raw) {
  return {
    ageBand: SUBJECT_UNKNOWN,
    sex: SUBJECT_UNKNOWN,
    lastAssessedAt: "无检测记录",
    lastProtocolSeconds: null,
    consentValid: false,
    ...raw,
    maskedId: raw?.maskedId ?? subjectLabelOf(raw?.subjectUuid),
  };
}

/** 步数键：sidecar 说 left/right；万一给的是足标签 L/R 也认。 */
function toSides(value) {
  if (!value || typeof value !== "object") return undefined;
  return {
    left: value.left ?? value.L,
    right: value.right ?? value.R,
  };
}

export function createSidecarAdapter(
  transport,
  { now = () => Date.now() / 1000, events = null, recentLimit = 5 } = {},
) {
  let counter = 0;

  async function call(method, params = {}) {
    counter += 1;
    const response = await transport({
      kind: "request",
      id: String(counter),
      method,
      params,
    });
    // 主进程把进程级失败以响应的形状送回来。它先于契约解释处理：
    // `interpret` 只认识 sidecar 说的话，而这句话是主进程说的。
    if (response?.sidecarUnavailable) {
      throw new SidecarDown(response.sidecarUnavailable);
    }
    const outcome = interpret(response);
    if (outcome.status === STATUS_OK) return outcome.result;
    if (outcome.status === STATUS_UNIMPLEMENTED) return { unimplemented: outcome.gap };
    throw new TerminalFailure(outcome.error);
  }

  /** 未实现的调用不该被当成结果用；这个判定只写一次。 */
  function gapOf(value) {
    return value && typeof value === "object" && value.unimplemented ? value.unimplemented : null;
  }

  async function listRecords() {
    return toRecordViews(await call("listRecords"));
  }

  async function snapshot() {
    const raw = await call("snapshot");
    // 最近记录拉不到不挡工作台：工作台是「开始检测」的入口，不该被一张列表拖住。
    let recentRecords = [];
    try {
      recentRecords = (await listRecords()).slice(0, recentLimit);
    } catch {
      recentRecords = [];
    }
    return {
      ...raw,
      deviceSummary: {
        ready: false,
        issues: [],
        ...(raw?.deviceSummary ?? {}),
      },
      uploadSummary: toUploadSummary(raw?.uploadSummary),
      recentRecords,
    };
  }

  return {
    call,
    gapOf,

    describe: () => call("describe"),
    snapshot,
    login: ({ organization, password }) => call("login", { organization, password }),
    recheckDevices: () => call("recheckDevices"),
    createSubject: async () => toSubjectView(await call("createSubject")),
    listRecords,
    deviceSupport: async () => toDeviceSupportView(await call("deviceSupport")),

    // 真实后端：三态电量准入、到达率、出厂标定、磁盘
    runPreflight: () => call("runPreflight"),

    // 真实后端：TimedWalk。受检者 uuid 随会话落进元数据。
    startSession: async (subject) => {
      const params = { now: now() };
      if (subject?.subjectUuid) params.subjectUuid = subject.subjectUuid;
      const started = await call("startSession", params);
      if (gapOf(started)) return started;
      const steps = toSides(started?.steps);
      const link = toSides(started?.link);
      return { ...started, ...(steps ? { steps } : {}), ...(link ? { link } : {}) };
    },
    stopSession: () => call("stopSession", { now: now() }),

    // 真实后端：TimedWalk.verdict + summarize_session
    sessionResult: (params = {}) => call("sessionResult", params),

    // 显式缺口
    runCalibration: () => call("runCalibration"),
    lookupSubject: (enteredId) => call("lookupSubject", { enteredId }),

    // RAY-345：报告已接通。record 来自 listRecords（含 id=sessionId）；缺省时
    // sidecar 用当前会话（startSession 之后）。swapped 是佩戴确认里的一键对调。
    reportFor: (record) => {
      const params = { sessionId: record?.id, swapped: record?.swapped ?? false };
      if (record?.subjectLabel) params.subjectLabel = record.subjectLabel;
      return call("reportFor", params);
    },

    /** 采集中的实时值（P-08）。桥接没给事件源时，诚实地什么也不推。 */
    subscribeSession: (onUpdate) => (events ? subscribeEvents(events, onUpdate) : () => {}),
  };
}

/**
 * 把 sidecar 推来的 tick 事件接成 `subscribeSession` 那个形状。
 *
 * 事件是**单向**的：sidecar 推，渲染端收。这里不做轮询 —— 轮询会让「剩余时间」由
 * 渲染端的定时器决定，而窗口失焦时浏览器会节流定时器，受试者却还在走（这正是
 * `TimedWalk` 不持有时钟的原因）。
 */
export function subscribeEvents(source, onUpdate) {
  let last = -1;
  const unsubscribe = source((event) => {
    if (event.kind !== "event") return;
    if (event.seq <= last) return; // 迟到或重放的事件不能让计数倒退
    last = event.seq;
    const payload = event.payload ?? {};
    if (event.topic === "session.tick") {
      // 缺的字段不带出去：`{ steps: undefined }` 合进 live 会把上一拍的步数抹掉，
      // 下一次渲染就在 `live.steps.left` 上崩。
      const update = {};
      if (payload.remainingSeconds !== undefined) update.remainingSeconds = payload.remainingSeconds;
      const steps = toSides(payload.steps);
      if (steps) update.steps = steps;
      const link = toSides(payload.link);
      if (link) update.link = link;
      onUpdate(update);
    } else if (event.topic === "session.notice") {
      onUpdate({ notices: [payload.text] });
    } else if (event.topic === "session.aborted") {
      onUpdate({ aborted: payload.error });
    }
  });
  return unsubscribe;
}
