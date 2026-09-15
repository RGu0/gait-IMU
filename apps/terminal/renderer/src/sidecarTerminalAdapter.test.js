import { describe, expect, it } from "vitest";

import {
  assessedAtOf,
  createSidecarAdapter,
  recordStatusOf,
  subscribeEvents,
  TerminalFailure,
} from "./sidecarTerminalAdapter.js";
import {
  CREATE_SUBJECT,
  DEVICE_SUPPORT,
  RECORDS,
  REPORT_NO_CYCLES,
  SNAPSHOT,
  START_SESSION,
  fail,
  fakeTransport,
} from "./testing/sidecarFixtures.js";

function adapterWith(handlers, options) {
  const fake = fakeTransport(handlers);
  return { adapter: createSidecarAdapter(fake.transport, options), calls: fake.calls };
}

describe("snapshot 补齐工作台读的字段", () => {
  it("带上最近 5 条记录（新的在前）与待上传数", async () => {
    const many = Array.from({ length: 7 }, (_, i) => ({
      ...RECORDS[0],
      id: `2026091${i}T010203Z-0000000${i}`,
    }));
    const { adapter } = adapterWith({ snapshot: SNAPSHOT, listRecords: many });
    const snap = await adapter.snapshot();
    expect(snap.recentRecords).toHaveLength(5);
    expect(snap.recentRecords[0].id).toBe("20260916T010203Z-00000006");
    expect(snap.uploadSummary.pending).toBe(2);
    expect(snap.deviceSummary.ready).toBe(true);
  });

  it("listRecords 失败时最近记录为空，而不是整个快照失败", async () => {
    const { adapter } = adapterWith({ snapshot: SNAPSHOT, listRecords: fail(REPORT_NO_CYCLES) });
    const snap = await adapter.snapshot();
    expect(snap.recentRecords).toEqual([]);
  });

  it("透传并行 scope 的 source / preview，缺席时不编", async () => {
    const withSource = adapterWith({ snapshot: { ...SNAPSHOT, source: "stub", preview: true }, listRecords: [] });
    expect(await withSource.adapter.snapshot()).toMatchObject({ source: "stub", preview: true });
    const without = adapterWith({ snapshot: SNAPSHOT, listRecords: [] });
    expect(await without.adapter.snapshot()).not.toHaveProperty("source");
  });

  it("没有上传队列时保留 tracked:false", async () => {
    const { adapter } = adapterWith({
      snapshot: { ...SNAPSHOT, uploadSummary: { tracked: false, drain: null } },
      listRecords: [],
    });
    expect((await adapter.snapshot()).uploadSummary).toMatchObject({ tracked: false, pending: 0 });
  });
});

describe("数据形状翻成视图形状", () => {
  it("createSubject 给出受检者屏要的字段", async () => {
    const { adapter } = adapterWith({ createSubject: CREATE_SUBJECT });
    expect(await adapter.createSubject()).toMatchObject({
      subjectUuid: CREATE_SUBJECT.subjectUuid,
      maskedId: "临时-E8E0",
      ageBand: "未提供",
      sex: "未提供",
      consentValid: false,
    });
  });

  it("listRecords 给出记录屏要的列", async () => {
    const { adapter } = adapterWith({ listRecords: RECORDS });
    const [newest, older] = await adapter.listRecords();
    expect(newest).toMatchObject({
      id: RECORDS[1].id,
      subjectLabel: "临时-E12B",
      protocol: "180 秒",
      status: "不完整",
      reportVersion: RECORDS[1].id,
      validSteps: "未统计",
    });
    expect(newest.assessedAt).toMatch(/^2026-09-1[45] \d{2}:\d{2}$/);
    // 旧 sidecar 没有 createdAt / complete：时间从会话 id 解，状态说「未知」而不是编一个
    expect(older.assessedAt).toBe(assessedAtOf({ id: RECORDS[0].id }));
    expect(older.status).toBe("状态未知");
  });

  it("complete 三态各有各的话", () => {
    expect([true, false, null, undefined].map(recordStatusOf)).toEqual(["完成", "不完整", "未正常结束", "状态未知"]);
  });

  it("deviceSupport 包成设备屏的 devices / support", async () => {
    const { adapter } = adapterWith({ deviceSupport: DEVICE_SUPPORT });
    const info = await adapter.deviceSupport();
    expect(info.devices.leftBattery).toBe(82);
    expect(info.devices.rightBattery).toBe(76);
    expect(info.devices.modules).toHaveLength(2);
    expect(info.devices.modules[0]).toMatchObject({ side: "left", firmware: "未记录", factoryCalibrated: false });
    expect(Object.values(info.support).every((v) => typeof v === "string" && v.length > 0)).toBe(true);
  });
});

describe("会话", () => {
  it("startSession 把受检者 uuid 带给 sidecar", async () => {
    const { adapter, calls } = adapterWith({ startSession: START_SESSION }, { now: () => 42 });
    const started = await adapter.startSession({ subjectUuid: "u-1" });
    expect(calls[0].params).toEqual({ now: 42, subjectUuid: "u-1" });
    expect(started.steps).toEqual({ left: 0, right: 0 });
  });

  it("没有受检者时不发空 uuid", async () => {
    const { adapter, calls } = adapterWith({ startSession: START_SESSION }, { now: () => 1 });
    await adapter.startSession();
    expect(calls[0].params).toEqual({ now: 1 });
  });

  it("reportFor 的拒绝以 TerminalFailure 抛出，带码与动作", async () => {
    const { adapter } = adapterWith({ reportFor: fail(REPORT_NO_CYCLES) });
    const error = await adapter.reportFor({ id: "x" }).catch((caught) => caught);
    expect(error).toBeInstanceOf(TerminalFailure);
    expect(error).toMatchObject({ code: "E-QLT-5003", action: REPORT_NO_CYCLES.action });
  });

  it("subscribeSession 接桥接的 onEvent，把三种话题翻成 live 的字段", () => {
    let emit;
    let unsubscribed = false;
    const events = (handler) => {
      emit = handler;
      return () => { unsubscribed = true; };
    };
    const { adapter } = adapterWith({}, { events });
    const seen = [];
    const stop = adapter.subscribeSession((update) => seen.push(update));
    emit({ kind: "event", v: "1.0", topic: "session.tick", seq: 1, payload: { remainingSeconds: 170.5, steps: { left: 3, right: 2 }, link: { left: "good", right: "fair" } } });
    emit({ kind: "event", v: "1.0", topic: "session.notice", seq: 2, payload: { text: "已记录一次停顿" } });
    emit({ kind: "event", v: "1.0", topic: "session.aborted", seq: 3, payload: { error: REPORT_NO_CYCLES } });
    stop();
    expect(seen).toEqual([
      { remainingSeconds: 170.5, steps: { left: 3, right: 2 }, link: { left: "good", right: "fair" } },
      { notices: ["已记录一次停顿"] },
      { aborted: REPORT_NO_CYCLES },
    ]);
    expect(unsubscribed).toBe(true);
  });

  it("tick 缺字段时不把上一拍的值抹成 undefined", () => {
    const seen = [];
    subscribeEvents(
      (emit) => {
        emit({ kind: "event", topic: "session.tick", seq: 1, payload: { remainingSeconds: 10 } });
        emit({ kind: "event", topic: "session.tick", seq: 2, payload: { steps: { L: 4, R: 5 } } });
        return () => {};
      },
      (update) => seen.push(update),
    );
    expect(seen).toEqual([{ remainingSeconds: 10 }, { steps: { left: 4, right: 5 } }]);
  });

  it("桥接没有事件源时 subscribeSession 是个空操作", () => {
    const { adapter } = adapterWith({});
    expect(typeof adapter.subscribeSession(() => {})).toBe("function");
  });
});
