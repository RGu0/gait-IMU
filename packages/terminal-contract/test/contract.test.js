import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import {
  CAPABILITIES,
  ContractViolation,
  ERROR_DOMAINS,
  IPC_CONTRACT_VERSION,
  STATUS_ERROR,
  STATUS_OK,
  STATUS_UNIMPLEMENTED,
  capabilityGap,
  checkError,
  interpret,
} from "../index.js";
import { subscribeEvents } from "../../../apps/terminal/renderer/src/sidecarTerminalAdapter.js";

const CONTRACT_FILE = fileURLToPath(
  new URL("../../../src/gait/app/contract.json", import.meta.url),
);

describe("契约事实只有一份", () => {
  it("JS 侧读的就是 Python 侧那个文件", () => {
    const onDisk = JSON.parse(readFileSync(CONTRACT_FILE, "utf8"));
    expect(IPC_CONTRACT_VERSION).toBe(onDisk.ipc_contract_version);
    expect(ERROR_DOMAINS).toEqual(Object.keys(onDisk.error_domains));
  });

  it("六域，不多不少", () => {
    expect([...ERROR_DOMAINS].sort()).toEqual(
      ["E-BLE", "E-CAL", "E-NET", "E-QLT", "E-SYNC", "E-WEAR"],
    );
  });
});

describe("三态而不是布尔", () => {
  it("ok 给出结果", () => {
    const outcome = interpret({ v: IPC_CONTRACT_VERSION, status: "ok", result: { a: 1 } });
    expect(outcome).toEqual({ status: STATUS_OK, result: { a: 1 } });
  });

  it("unimplemented 既不是成功也不是失败", () => {
    const outcome = interpret({
      v: IPC_CONTRACT_VERSION,
      status: "unimplemented",
      unimplemented: { capability: "report", issue: "RAY-224", summary: "尚未实现" },
    });
    expect(outcome.status).toBe(STATUS_UNIMPLEMENTED);
    expect(outcome.gap.issue).toBe("RAY-224");
    expect(outcome).not.toHaveProperty("result");
    expect(outcome).not.toHaveProperty("error");
  });

  it("未知 status 不被放行", () => {
    expect(() => interpret({ v: IPC_CONTRACT_VERSION, status: "maybe" })).toThrow(
      ContractViolation,
    );
  });
});

describe("渲染进程不得自造错误文案（RAY-248 验收第二条）", () => {
  const good = {
    code: "E-BLE-1005",
    domain: "E-BLE",
    message: "左脚电量 22% 低于 30%。",
    action: "请更换或充电后重新检查。",
    blocking: true,
  };

  it("完整的错误原样透传", () => {
    const outcome = interpret({ v: IPC_CONTRACT_VERSION, status: "error", error: good });
    expect(outcome.status).toBe(STATUS_ERROR);
    expect(outcome.error.action).toBe(good.action);
  });

  it.each(["message", "action", "code"])("缺 %s 时抛错而不是补一句兜底", (field) => {
    // 兜底看起来更稳，实际是把「文案只有一个来源」变成一句愿望：一旦有兜底，
    // sidecar 少给文案就再也不会有人发现。
    expect(() => checkError({ ...good, [field]: "  " })).toThrow(ContractViolation);
  });

  it("未知错误域不被放行", () => {
    expect(() => checkError({ ...good, domain: "E-NOPE" })).toThrow(ContractViolation);
  });
});

describe("版本不符要当场发现", () => {
  it("sidecar 报别的版本时拒绝解释", () => {
    expect(() => interpret({ v: "9.9", status: "ok", result: {} })).toThrow(/契约版本不符/);
  });

  it("缺 v 不是绕过版本检查的口子", () => {
    expect(() => interpret({ status: "ok", result: {} })).toThrow(/契约版本不符/);
  });

  it("只有协议层失败允许不带 v —— 它先于版本协商发生", () => {
    expect(() => interpret({ status: "error", protocolError: "未登记的方法 'nope'" })).toThrow(
      /协议层失败/,
    );
  });
});

describe("事件流", () => {
  it("按 seq 丢弃迟到或重放的事件", () => {
    // 事件迟到会让步数倒退，而操作员看到的是一个正在变小的计数 ——
    // 那比不更新更糟：它看起来是真的。
    const seen = [];
    subscribeEvents(
      (emit) => {
        emit({ kind: "event", topic: "session.tick", seq: 1, payload: { steps: { left: 10 } } });
        emit({ kind: "event", topic: "session.tick", seq: 3, payload: { steps: { left: 30 } } });
        emit({ kind: "event", topic: "session.tick", seq: 2, payload: { steps: { left: 20 } } });
        return () => {};
      },
      (update) => seen.push(update.steps.left),
    );
    expect(seen).toEqual([10, 30]);
  });

  it("把三种话题分别接到界面要的字段上", () => {
    const seen = [];
    subscribeEvents(
      (emit) => {
        emit({ kind: "event", topic: "session.notice", seq: 1, payload: { text: "已记录一次停顿" } });
        emit({
          kind: "event",
          topic: "session.aborted",
          seq: 2,
          payload: { error: { code: "E-BLE-1020" } },
        });
        emit({ kind: "response", status: "ok" });
        return () => {};
      },
      (update) => seen.push(update),
    );
    expect(seen).toEqual([
      { notices: ["已记录一次停顿"] },
      { aborted: { code: "E-BLE-1020" } },
    ]);
  });
});

describe("能力缺口", () => {
  it("calib 与 report 目前都是缺口，且各自带着 Issue", () => {
    expect(capabilityGap("calibration").issue).toBe("RAY-208");
    expect(capabilityGap("report").issue).toBe("RAY-224");
  });

  it("尚无 Issue 认领的缺口把 issue 留空，而不是编一个", () => {
    expect(capabilityGap("subject-directory").issue).toBeNull();
  });

  it("未登记的能力问不出结果", () => {
    expect(() => capabilityGap("teleportation")).toThrow(ContractViolation);
  });

  it("CAPABILITIES 是只读的", () => {
    expect(Object.isFrozen(CAPABILITIES)).toBe(true);
  });
});
