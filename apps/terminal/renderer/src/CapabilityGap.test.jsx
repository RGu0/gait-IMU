/**
 * 缺口必须**看得见**（RAY-248）。
 *
 * 这些断言的价值不在于「有没有渲染出一个 div」，而在于挡住三件事：静默跳过、
 * 用假数据冒充、以及把缺口画成设备故障。
 */
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { CalibrationScreen } from "./CalibrationScreen.jsx";
import { CapabilityGap } from "./CapabilityGap.jsx";
import { SessionVerdictSummary } from "./SessionVerdictSummary.jsx";
import { TerminalApp } from "./TerminalApp.jsx";
import { selectAdapter } from "./main.jsx";
import { mockTerminalAdapter } from "./mockTerminalAdapter.js";

const CALIB_GAP = {
  unimplemented: {
    capability: "calibration",
    issue: "RAY-208",
    summary: "会话标定尚未实现；gait/calib/ 目前是空包",
  },
};

async function renderCalibrationToGap(onDone = vi.fn(), onAbandon = vi.fn()) {
  vi.useFakeTimers();
  render(
    <CalibrationScreen
      runCalibration={() => Promise.resolve(CALIB_GAP)}
      onDone={onDone}
      onAbandon={onAbandon}
      tickMs={1}
    />,
  );
  await vi.advanceTimersByTimeAsync(200);
  vi.useRealTimers();
  await screen.findByText("本步骤尚未接通");
  return { onDone, onAbandon };
}

describe("P-07 标定尚未接通", () => {
  it("停下来说清楚，而不是悄悄放行到测试段", async () => {
    const { onDone } = await renderCalibrationToGap();
    expect(screen.getByText(/RAY-208/)).toBeInTheDocument();
    expect(screen.getByText(/gait\/calib\/ 目前是空包/)).toBeInTheDocument();
    // 关键：没有自己走下去
    expect(onDone).not.toHaveBeenCalled();
  });

  it("继续下去是操作员的一次明确动作，且界面说明了代价", async () => {
    const { onDone } = await renderCalibrationToGap();
    expect(screen.getByText(/不会用占位数据冒充/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "跳过标定，继续检测" }));
    expect(onDone).toHaveBeenCalled();
  });

  it("不画成故障：缺口是 status，不是 alert", async () => {
    // 用 danger/alert 会让操作员去排查一个不存在的设备故障。
    await renderCalibrationToGap();
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.getByRole("status")).toHaveAttribute("data-capability", "calibration");
  });

  it("标定真的失败时走的仍是失败路径，不是缺口路径", async () => {
    // 两条路必须分得开：一个是「设备/佩戴出了事」，一个是「这条通路还没建」。
    vi.useFakeTimers();
    render(
      <CalibrationScreen
        runCalibration={() => Promise.resolve({ ok: false, reason: "loose" })}
        onDone={vi.fn()}
        onAbandon={vi.fn()}
        tickMs={1}
      />,
    );
    await vi.advanceTimersByTimeAsync(200);
    vi.useRealTimers();
    expect(await screen.findByRole("alert")).toHaveTextContent("模块有些松动");
    expect(screen.queryByText("本步骤尚未接通")).toBeNull();
  });
});

const REAL_RESULT = {
  overall: "indeterminate",
  verdict: {
    wearing: "unknown",
    link: "pass",
    duration: "pass",
    valid_seconds: 182,
    required_seconds: 126,
    reasons: ["wearing_unknown"],
  },
  integrity: { complete: true, problems: [] },
  report: { status: "unimplemented", capability: "report", issue: "RAY-224" },
};

describe("P-09 基础报告尚未接通", () => {
  it("显示真实的会话判定，同时说明为什么没有报告", () => {
    // TerminalApp 在这一步就是这个组合：真实判定当 details，报告缺口当主体。
    render(
      <CapabilityGap
        gap={{
          capability: REAL_RESULT.report.capability,
          issue: REAL_RESULT.report.issue,
          summary: "本地基础报告尚未实现，因此这次检测没有生成报告。",
        }}
        step={6}
        details={<SessionVerdictSummary result={REAL_RESULT} />}
        onBack={vi.fn()}
      />,
    );
    expect(screen.getByText("本次会话尚不能判定")).toBeInTheDocument();
    expect(screen.getByText(/RAY-224/)).toBeInTheDocument();
    expect(screen.getByText(/没有生成报告/)).toBeInTheDocument();
  });

  it("三态判定里「评不了」不显示成「无效」", () => {
    // RAY-260：左右戴反位置法不可判定，v1.4 改为 P-06 手工裁定。把「评不了」
    // 画成「无效」会让操作员去重测一场其实没问题的检测。
    render(<SessionVerdictSummary result={REAL_RESULT} />);
    expect(screen.getByText("本次会话尚不能判定")).toBeInTheDocument();
    expect(screen.getByText("未裁定")).toBeInTheDocument();
    expect(screen.queryByText("本次会话无效")).toBeNull();
  });

  it("双足不完整时说明单侧仍可能可算", () => {
    render(
      <SessionVerdictSummary
        result={{
          ...REAL_RESULT,
          integrity: { complete: false, problems: ["左脚在 41.0s 处断连：…"] },
        }}
      />,
    );
    expect(screen.getByText(/单侧指标可能仍然可算/)).toBeInTheDocument();
    expect(screen.getByText(/左脚在 41.0s 处断连/)).toBeInTheDocument();
  });

  it("会话有效时不谎称报告已生成", () => {
    render(<SessionVerdictSummary result={{ ...REAL_RESULT, overall: "valid" }} />);
    expect(screen.getByText("本次会话有效")).toBeInTheDocument();
  });
});

describe("尚无 Issue 认领的缺口", () => {
  it("把这件事说出来，而不是编一个号", () => {
    render(
      <CapabilityGap
        gap={{ capability: "subject-directory", issue: null, summary: "需要云端加密身份库" }}
        step={2}
        onBack={vi.fn()}
      />,
    );
    expect(screen.getByText("尚无 Issue 认领这个缺口")).toBeInTheDocument();
  });
});

describe("P-00 登录尚未接通", () => {
  it("不静默穿过：登录返回缺口时停在缺口屏，不进工作台", async () => {
    // adapter.login 返回缺口而不是抛错，所以「不接住」的后果不是报错，
    // 是直接进入工作台 —— 一个没有登录过的工作台。
    const adapter = {
      ...mockTerminalAdapter,
      login: async () => ({
        unimplemented: { capability: "operator-auth", issue: null, summary: "登录没有后端" },
      }),
    };
    render(<TerminalApp adapter={adapter} />);
    fireEvent.change(screen.getByLabelText("机构账号"), { target: { value: "康健" } });
    fireEvent.change(screen.getByLabelText("登录密码"), { target: { value: "x" } });
    fireEvent.click(screen.getByRole("button", { name: "登录" }));

    expect(await screen.findByText("本步骤尚未接通")).toBeInTheDocument();
    expect(screen.queryByText("开始新的检测")).toBeNull();
  });
});

describe("mock 不再是生产路径的唯一来源", () => {
  it("没有 sidecar 桥接时退回 mock", () => {
    const chosen = selectAdapter({});
    expect(chosen.mocked).toBe(true);
    expect(chosen.adapter).toBe(mockTerminalAdapter);
  });

  it("桥接在时用真实 adapter", () => {
    const chosen = selectAdapter({ gaitSidecar: { request: async () => ({}) } });
    expect(chosen.mocked).toBe(false);
    expect(chosen.adapter).not.toBe(mockTerminalAdapter);
    expect(typeof chosen.adapter.runPreflight).toBe("function");
  });
});
