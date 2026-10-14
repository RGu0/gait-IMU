/**
 * RAY-754：sidecar 以 `E-CAL-3005` 拒绝开检测时，渲染端走 `startWalk` 现有的失败页，
 * 原样显示 sidecar 给的现象、动作与码 —— 不另写文案。
 *
 * 正常流程走不到这里（标定屏不通过就到不了确认屏）。这里模拟的正是「界面以为过了、
 * sidecar 说没过」的那一种：渲染端不能把它吞掉，也不能换一套自己的话去说。
 */
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { TerminalApp } from "./TerminalApp.jsx";
import { createSidecarAdapter } from "./sidecarTerminalAdapter.js";
import {
  CALIBRATION_PASSED,
  CREATE_SUBJECT,
  PREFLIGHT_WAIVED,
  RECORDS,
  SNAPSHOT,
  fail,
  fakeTransport,
} from "./testing/sidecarFixtures.js";

const REFUSAL = {
  code: "E-CAL-3005",
  domain: "E-CAL",
  message: "这次佩戴还没有完成标定，不能开始检测。",
  action: "请回到「标定」一步，按提示让受试者静立、直线走 10 步并站定，通过后再开始检测。",
  blocking: true,
};

const click = (name) => fireEvent.click(screen.getByRole("button", { name }));
const tickAll = () => screen.getAllByRole("checkbox").forEach((box) => fireEvent.click(box));

describe("sidecar 拒绝开检测（未完成会话标定）", () => {
  it("走现有的失败页，三段都是 sidecar 的原话", async () => {
    const fake = fakeTransport({
      snapshot: SNAPSHOT,
      listRecords: RECORDS,
      createSubject: CREATE_SUBJECT,
      runCalibration: CALIBRATION_PASSED,
      runPreflight: PREFLIGHT_WAIVED,
      startSession: fail(REFUSAL),
    });
    const adapter = createSidecarAdapter(fake.transport, { now: () => 100 });
    render(<TerminalApp adapter={adapter} />);

    fireEvent.click(await screen.findByRole("button", { name: "开始新的检测" }));
    fireEvent.click(await screen.findByRole("button", { name: "无编号，快速建档" }));
    fireEvent.click(await screen.findByRole("button", { name: "跳过" }));
    await screen.findByRole("heading", { name: "数据授权" });
    tickAll();
    click("同意并继续");
    await screen.findByRole("heading", { name: "安全确认与设备自检" });
    tickAll();
    await screen.findByRole("heading", { name: "佩戴引导" }, { timeout: 5000 });
    tickAll();
    click("佩戴完成，开始标定");
    await screen.findByRole("heading", { name: "确认左右" }, { timeout: 3000 });
    tickAll();
    click("确认无误，开始检测");

    const page = await screen.findByRole("alert", { name: "检测未能开始" });
    expect(page).toHaveTextContent(REFUSAL.message);
    expect(page).toHaveTextContent(REFUSAL.action);
    expect(page).toHaveTextContent("E-CAL-3005");
    // 没进采集页。
    expect(screen.queryByLabelText("操作员侧栏")).toBeNull();
  });
});
