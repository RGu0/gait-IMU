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

export function createSidecarAdapter(transport, { now = () => Date.now() / 1000 } = {}) {
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

  return {
    call,
    gapOf,

    describe: () => call("describe"),
    snapshot: () => call("snapshot"),
    login: ({ organization, password }) => call("login", { organization, password }),
    recheckDevices: () => call("recheckDevices"),
    createSubject: () => call("createSubject"),
    listRecords: () => call("listRecords"),
    deviceSupport: () => call("deviceSupport"),

    // 真实后端：三态电量准入、到达率、出厂标定、磁盘
    runPreflight: () => call("runPreflight"),

    // 真实后端：TimedWalk
    startSession: () => call("startSession", { now: now() }),
    stopSession: () => call("stopSession", { now: now() }),

    // 真实后端：TimedWalk.verdict + summarize_session
    sessionResult: (params = {}) => call("sessionResult", params),

    // 显式缺口
    runCalibration: () => call("runCalibration"),
    lookupSubject: (enteredId) => call("lookupSubject", { enteredId }),

    // RAY-345：报告已接通。record 来自 listRecords（含 id=sessionId）；缺省时
    // sidecar 用当前会话（startSession 之后）。swapped 是佩戴确认里的一键对调。
    reportFor: (record) =>
      call("reportFor", { sessionId: record?.id, swapped: record?.swapped ?? false }),
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
    if (event.topic === "session.tick") {
      onUpdate({
        remainingSeconds: event.payload.remainingSeconds,
        steps: event.payload.steps,
        link: event.payload.link,
      });
    } else if (event.topic === "session.notice") {
      onUpdate({ notices: [event.payload.text] });
    } else if (event.topic === "session.aborted") {
      onUpdate({ aborted: event.payload.error });
    }
  });
  return unsubscribe;
}
