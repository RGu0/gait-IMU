/**
 * sidecar 的看护者：拉起、探活、异常退出后重启、退不动就降级。
 *
 * ## 它为什么不 import electron
 *
 * 整个模块只用 `node:child_process` 与 `node:events`。这样它能在 vitest 里被完整
 * 驱动 —— 真的起进程、真的杀掉它、真的看着它重启 —— 而不需要一个窗口。
 *
 * 这不是为了测试方便而做的妥协，恰恰相反：**V-U4 要验的「sidecar 崩溃时 UI 有明确
 * 的表现」，其判定全在这里**。如果它长在 Electron 主进程里，唯一的验证方式就是人肉
 * 启动应用再去 kill 一个进程，那种验证不会每次提交都跑，也就等于没有。
 *
 * ## 红线 R-2：主进程不做业务判定
 *
 * 本模块只回答进程层面的事实：起来了没有、还活着吗、退出码是几、重启了几次。
 * 它**不判断**自检是否通过、会话是否有效、质量如何 —— 那些只有 sidecar 知道。
 *
 * ## 「sidecar 死了」这句话由谁来写
 *
 * RAY-248 的验收写明渲染进程不得自造错误文案，文案与错误码同源于 sidecar。
 * 但 sidecar 死了的时候，它显然写不了自己的讣告。
 *
 * 这不是矛盾，是第三方：**进程的生死是主进程的职责范围**（UI 设计 §11.1：窗口生命
 * 周期、sidecar 拉起与看护），所以这句话由这里写。关键是它**不带六域错误码** ——
 * 六域说的是采集现场出了什么事（`E-BLE` 是连接故障），而 sidecar 进程没了不是其中
 * 任何一种。给它编一个 `E-BLE-xxxx` 会在日志里造出一个查无此事的设备故障。
 *
 * 这与 `gait/app/__main__.py` 里 `_fatal` 拒绝给协议层失败编码，是同一条理由。
 */
import { spawn } from "node:child_process";
import { EventEmitter } from "node:events";

/** 生命周期状态。渲染进程按它决定显示什么。 */
export const SIDECAR_STARTING = "starting";
export const SIDECAR_READY = "ready";
export const SIDECAR_RESTARTING = "restarting";
export const SIDECAR_UNAVAILABLE = "unavailable";

/**
 * 连续重启的上限。
 *
 * 无限重启比不重启更糟：一个每 200 ms 复活又立刻死掉的 sidecar，会让界面在
 * 「正在恢复」和「失败」之间闪烁，而操作员看到的是一个一直在动、永远不好的东西。
 * 到达上限就停在 unavailable，把「需要人来处理」这件事说清楚。
 */
export const MAX_CONSECUTIVE_RESTARTS = 3;

/** 重启退避的基数；第 n 次重启等 RESTART_BACKOFF_MS * n。 */
export const RESTART_BACKOFF_MS = 300;

/**
 * 认为「这次启动已经站稳」的时长。活过它，连续重启计数归零。
 *
 * 没有这个概念的话，一个跑了两小时才崩一次的 sidecar 会和一个起来就死的 sidecar
 * 共用同一个计数器，跑到第三次崩溃就再也不重启了 —— 而那是一整天里第三次，不是
 * 三秒内第三次。两者要区别对待。
 */
export const HEALTHY_UPTIME_MS = 5_000;

export class SidecarSupervisor extends EventEmitter {
  /**
   * @param {object} options
   * @param {string} options.command 可执行文件
   * @param {string[]} options.args 参数
   * @param {string} options.cwd 工作目录
   * @param {object} [options.env] 环境变量
   * @param {number} [options.requestTimeoutMs] 单条请求的超时
   */
  constructor({ command, args = [], cwd, env, requestTimeoutMs = 30_000, now = () => Date.now() }) {
    super();
    this.command = command;
    this.args = args;
    this.cwd = cwd;
    this.env = env;
    this.requestTimeoutMs = requestTimeoutMs;
    this._now = now;

    this.child = null;
    this.state = SIDECAR_STARTING;
    this.restarts = 0;
    this.lastExit = null;

    this._buffer = "";
    this._pending = new Map();
    this._nextId = 0;
    this._startedAt = null;
    this._restartTimer = null;
    this._stopping = false;
  }

  start() {
    this._stopping = false;
    this._spawn();
    return this;
  }

  _setState(state, detail = {}) {
    this.state = state;
    this.emit("state", { state, ...detail });
  }

  _spawn() {
    this._setState(this.restarts === 0 ? SIDECAR_STARTING : SIDECAR_RESTARTING, {
      attempt: this.restarts,
    });
    const child = spawn(this.command, this.args, {
      cwd: this.cwd,
      env: this.env,
      stdio: ["pipe", "pipe", "pipe"],
    });
    this.child = child;
    this._startedAt = this._now();

    child.stdout.setEncoding("utf8");
    child.stdout.on("data", (chunk) => this._consume(chunk));

    // stderr 只做诊断。**协议只走 stdout** —— sidecar 那侧同样约定（见其模块文档），
    // 否则一行日志会被当成一条消息。
    child.stderr.setEncoding("utf8");
    child.stderr.on("data", (chunk) => this.emit("diagnostic", chunk));

    child.on("spawn", () => this._setState(SIDECAR_READY, { pid: child.pid }));
    child.on("error", (error) => this._died({ reason: error.message, code: null, signal: null }));
    child.on("exit", (code, signal) => {
      if (code === 0 && this._stopping) return;
      this._died({ reason: "sidecar 进程退出", code, signal });
    });
  }

  _died({ reason, code, signal }) {
    if (this._stopping) return;
    this.lastExit = { reason, code, signal, at: this._now() };
    this.child = null;

    // 在飞的请求必须当场失败。放着不管，渲染端会一直等一个永远不会来的回应 ——
    // 那是最糟的失败方式：界面看起来只是慢。
    const failure = this.failureNotice();
    for (const [, pending] of this._pending) pending.reject(new SidecarUnavailable(failure));
    this._pending.clear();
    this._buffer = "";

    const stoodUp = this._startedAt !== null && this._now() - this._startedAt >= HEALTHY_UPTIME_MS;
    if (stoodUp) this.restarts = 0;

    if (this.restarts >= MAX_CONSECUTIVE_RESTARTS) {
      this._setState(SIDECAR_UNAVAILABLE, { notice: failure, exit: this.lastExit });
      return;
    }
    this.restarts += 1;
    this._setState(SIDECAR_RESTARTING, { attempt: this.restarts, notice: failure });
    this._restartTimer = setTimeout(() => {
      this._restartTimer = null;
      if (!this._stopping) this._spawn();
    }, RESTART_BACKOFF_MS * this.restarts);
    if (typeof this._restartTimer.unref === "function") this._restartTimer.unref();
  }

  /**
   * 进程级失败的说明。**现象 + 动作**，与 sidecar 给的错误同格式，但**没有错误码** ——
   * 它不属于六域中的任何一个。
   */
  failureNotice() {
    const exhausted = this.restarts >= MAX_CONSECUTIVE_RESTARTS;
    return {
      kind: "sidecar-unavailable",
      message: exhausted
        ? `采集服务连续 ${MAX_CONSECUTIVE_RESTARTS} 次未能启动。`
        : "采集服务已停止，正在自动重新启动。",
      action: exhausted
        ? "本次检测无法继续。请退出并重新打开应用；若仍不行，请联系服务方。"
        : "请稍候。如果这一屏持续超过一分钟，请退出并重新打开应用。",
      // 刻意没有 code：六域说的是采集现场的故障，进程没了不是其中任何一种。
      // 给它编一个会在日志里造出一个查无此事的设备故障。
      recoverable: !exhausted,
    };
  }

  _consume(chunk) {
    this._buffer += chunk;
    let index;
    while ((index = this._buffer.indexOf("\n")) >= 0) {
      const line = this._buffer.slice(0, index).trim();
      this._buffer = this._buffer.slice(index + 1);
      if (!line) continue;
      let message;
      try {
        message = JSON.parse(line);
      } catch {
        this.emit("diagnostic", `sidecar 输出了一行非 JSON：${line}`);
        continue;
      }
      if (message.kind === "event") {
        this.emit("event", message);
        continue;
      }
      const pending = this._pending.get(String(message.id));
      if (!pending) {
        this.emit("diagnostic", `收到无人认领的回应 id=${message.id}`);
        continue;
      }
      this._pending.delete(String(message.id));
      clearTimeout(pending.timer);
      pending.resolve(message);
    }
  }

  /** 发一条请求。sidecar 不在时**立刻**失败，而不是排队等它回来。 */
  request(message) {
    if (!this.child || this.state !== SIDECAR_READY) {
      return Promise.reject(new SidecarUnavailable(this.failureNotice()));
    }
    this._nextId += 1;
    const id = String(this._nextId);
    const outgoing = { ...message, id };
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this._pending.delete(id);
        reject(new SidecarUnavailable({
          kind: "sidecar-timeout",
          message: "采集服务没有在预期时间内回应。",
          action: "请重试一次；若反复如此，请退出并重新打开应用。",
          recoverable: true,
        }));
      }, this.requestTimeoutMs);
      if (typeof timer.unref === "function") timer.unref();
      this._pending.set(id, { resolve, reject, timer });
      this.child.stdin.write(`${JSON.stringify(outgoing)}\n`);
    });
  }

  async stop() {
    this._stopping = true;
    if (this._restartTimer) {
      clearTimeout(this._restartTimer);
      this._restartTimer = null;
    }
    for (const [, pending] of this._pending) clearTimeout(pending.timer);
    this._pending.clear();
    const child = this.child;
    this.child = null;
    if (!child) return;
    child.stdin.end();
    await new Promise((resolve) => {
      const timer = setTimeout(() => {
        child.kill("SIGKILL");
        resolve();
      }, 2_000);
      if (typeof timer.unref === "function") timer.unref();
      child.once("exit", () => {
        clearTimeout(timer);
        resolve();
      });
    });
  }
}

/** sidecar 不可用时抛出。带主进程写的说明，渲染端只排版。 */
export class SidecarUnavailable extends Error {
  constructor(notice) {
    super(notice.message);
    this.name = "SidecarUnavailable";
    this.notice = notice;
  }
}
