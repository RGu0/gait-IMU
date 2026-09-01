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
 * `runCalibration` / `reportFor` / `lookupSubject` 在缺口未接通时**返回**一个带
 * `unimplemented` 的值，而不是 throw。throw 会让界面显示一个查无此事的故障；返回
 * 占位数据则是拿假结果冒充真结果。返回一个显式的缺口，界面就必须把它画出来 ——
 * 这正是本 scope 要的：诚实地断在半路，而不是看起来走完了。
 */
import {
  STATUS_OK,
  STATUS_UNIMPLEMENTED,
  interpret,
} from "@gait/terminal-contract";

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
    reportFor: () => call("reportFor"),
    lookupSubject: (enteredId) => call("lookupSubject", { enteredId }),
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
