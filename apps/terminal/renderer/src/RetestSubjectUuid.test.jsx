/**
 * RAY-759 R2：受试者卡片里没有 subjectUuid 时（查档得来的就是这样），渲染端记下
 * `startSession` 应答里 sidecar 用的那个，「重新检测」带同一个。
 *
 * 不带的话，sidecar 每场各生成一个随机 UUID，而沿用的标定只给同一位受试者 ——
 * 重测就会被当成换了人，以 E-CAL-3005 拒绝。
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { TerminalApp } from "./TerminalApp.jsx";
import { createSidecarAdapter } from "./sidecarTerminalAdapter.js";
import {
  CALIBRATION_PASSED,
  PREFLIGHT_WAIVED,
  RECORDS,
  SESSION_RESULT_READY,
  SNAPSHOT,
  START_SESSION,
  STOP_SESSION,
  fakeTransport,
} from "./testing/sidecarFixtures.js";

const ISSUED = "5b0c4f7e-2d0a-4c1e-9a55-0f3b8d2c7a11";

const click = (name) => fireEvent.click(screen.getByRole("button", { name }));
const tickAll = () => screen.getAllByRole("checkbox").forEach((box) => fireEvent.click(box));

describe("受试者没有 subjectUuid 时，重测带 sidecar 第一次用的那个", () => {
  it(
    "第一场不带 → 应答给出 → 「重新检测」带同一个",
    async () => {
      const fake = fakeTransport({
        snapshot: SNAPSHOT,
        listRecords: RECORDS,
        // 卡片没有 subjectUuid —— 与查档得来的受试者同形。
        createSubject: { consentValid: false },
        runCalibration: CALIBRATION_PASSED,
        runPreflight: PREFLIGHT_WAIVED,
        startSession: (params) => ({
          ...START_SESSION,
          totalSeconds: 0,
          remainingSeconds: 0,
          subjectUuid: params?.subjectUuid ?? ISSUED,
        }),
        stopSession: STOP_SESSION,
        // 会话级无效：结果页直接给「重新检测」，它不回 P-07。
        sessionResult: {
          ...SESSION_RESULT_READY,
          overall: "invalid",
          verdict: {
            ...SESSION_RESULT_READY.verdict,
            overall: "invalid",
            duration: "fail",
            valid_seconds: 0,
            required_seconds: 126,
            reasons: ["duration_short"],
          },
          report: { status: "invalid", sessionId: START_SESSION.sessionId },
        },
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

      await screen.findByRole("heading", { name: "本次检测未生成报告" }, { timeout: 8000 });
      click("重新检测");

      await waitFor(() =>
        expect(fake.calls.filter((c) => c.method === "startSession")).toHaveLength(2),
      );
      const [first, second] = fake.calls.filter((c) => c.method === "startSession");
      expect(first.params.subjectUuid).toBeUndefined();
      expect(second.params.subjectUuid).toBe(ISSUED);
    },
    20000,
  );
});
