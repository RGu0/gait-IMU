/**
 * 渲染进程侧的 IPC 契约。
 *
 * ## 它 import 的是 Python 侧那一份文件，不是它的副本
 *
 * `src/gait/app/contract.json` 同时被 `gait.app.errors.contract()` 和这里读取。
 * 抄一份到 `packages/` 下会立刻好用，代价是从那一刻起两边可以不一样 —— 而两边不一样
 * 的第一个征兆，通常是线上某个错误码在界面上显示成空白。契约测试能发现副本漂移，
 * 但只有「根本没有副本」能让它不可能发生。
 */
import contract from "../../src/gait/app/contract.json" with { type: "json" };

export const IPC_CONTRACT_VERSION = contract.ipc_contract_version;
export const DATA_CONTRACT_VERSION = contract.data_contract_version;
export const ERROR_DOMAINS = Object.freeze(Object.keys(contract.error_domains));
export const METHODS = Object.freeze([...contract.methods]);
export const EVENT_TOPICS = Object.freeze([...contract.event_topics]);
export const CAPABILITIES = Object.freeze(contract.capabilities);

export const STATUS_OK = "ok";
export const STATUS_ERROR = "error";
export const STATUS_UNIMPLEMENTED = "unimplemented";

/**
 * 采集中链路只有三档（FR-07：200 Hz 下寄存器不可读，不展示虚假电量）。
 */
export const LINK_GRADES = Object.freeze(["good", "fair", "bad"]);

export class ContractViolation extends Error {}

/**
 * 把一条响应翻成渲染端能用的三选一。
 *
 * **刻意不返回布尔。** 「成功 / 失败」装不下「这个能力还没实现」：把未实现折成失败，
 * 界面会显示一个查无此事的故障；折成成功并给个占位值，就是拿假数据冒充真结果。
 * 三态强迫每个调用点显式处理第三种情况 —— 那正是 calib 与 report 现在的处境。
 */
export function interpret(response) {
  if (!response || typeof response !== "object") {
    throw new ContractViolation("响应不是对象");
  }
  // 协议层失败先于版本协商发生（两端连话都没对上），所以只有它允许不带 v。
  // 放行任何缺 v 的响应，等于给版本检查开一个「不带版本就能穿过」的口子。
  if (!response.protocolError && response.v !== IPC_CONTRACT_VERSION) {
    throw new ContractViolation(
      `IPC 契约版本不符：sidecar 说 ${response.v}，渲染端是 ${IPC_CONTRACT_VERSION}`,
    );
  }
  switch (response.status) {
    case STATUS_OK:
      return { status: STATUS_OK, result: response.result };
    case STATUS_UNIMPLEMENTED:
      return { status: STATUS_UNIMPLEMENTED, gap: response.unimplemented };
    case STATUS_ERROR:
      if (response.protocolError) {
        throw new ContractViolation(`协议层失败：${response.protocolError}`);
      }
      return { status: STATUS_ERROR, error: checkError(response.error) };
    default:
      throw new ContractViolation(`未知 status ${JSON.stringify(response.status)}`);
  }
}

/**
 * 错误必须自带文案。
 *
 * RAY-248 验收第二条：**渲染进程不得自造错误文案**。这个检查是那条验收的可执行形式 ——
 * 缺文案时**抛错而不是补一句兜底**。补兜底看起来更稳，实际是把「文案只有一个来源」
 * 这条约束变成一句愿望：一旦有兜底，sidecar 少给文案就再也不会有人发现。
 */
export function checkError(error) {
  if (!error || typeof error !== "object") {
    throw new ContractViolation("错误对象缺失");
  }
  for (const field of ["code", "message", "action"]) {
    if (typeof error[field] !== "string" || error[field].trim() === "") {
      throw new ContractViolation(
        `错误 ${error.code ?? "?"} 缺少 ${field}；渲染进程不得自造错误文案（RAY-248）`,
      );
    }
  }
  if (!ERROR_DOMAINS.includes(error.domain)) {
    throw new ContractViolation(`未知错误域 ${error.domain}`);
  }
  return error;
}

/** 某个能力当前是否已接通。界面据此决定是否显示缺口标注。 */
export function capabilityGap(name) {
  const entry = CAPABILITIES[name];
  if (!entry) throw new ContractViolation(`未登记的能力 ${name}`);
  return entry.implemented ? null : entry;
}
