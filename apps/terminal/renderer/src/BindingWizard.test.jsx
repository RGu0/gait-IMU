/**
 * RAY-479 `color-binding-wizard`：左蓝右橙、先左后右的配对向导，以及它在生产
 * `TerminalApp` 里真的接得上（此前 `onRepair={() => {}}`，按钮什么都不做）。
 */
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { BindingWizardScreen } from "./BindingWizardScreen.jsx";
import { HubScreen } from "./HubScreen.jsx";
import { PreflightScreen } from "./PreflightScreen.jsx";
import { TerminalApp } from "./TerminalApp.jsx";
import { createSidecarAdapter } from "./sidecarTerminalAdapter.js";
import { DEVICE_SUPPORT, SNAPSHOT, fail, fakeTransport } from "./testing/sidecarFixtures.js";

const WAIT = { timeout: 5000 };

const UNBOUND = {
  available: true, required: true, left: null, right: null, complete: false, boundAt: null,
  problem: "左脚尚未绑定：请先完成配对。 右脚尚未绑定：请先完成配对。",
};
const BOUND = {
  ...UNBOUND,
  complete: true,
  problem: null,
  left: { mac: "F1:11:11:11:11:11", masked: "…:11:11", boundAt: "2026-09-16T08:30:00+00:00" },
  right: { mac: "F2:22:22:22:22:22", masked: "…:22:22", boundAt: "2026-09-16T08:30:00+00:00" },
};

const MULTIPLE = {
  code: "E-BLE-1031", domain: "E-BLE",
  message: "发现多个未绑定模块（2 台）：无法确定哪一台是蓝色（左脚）模块。",
  action: "请只打开蓝色（左脚）模块，其余模块先关机，然后重试。",
  blocking: true,
};

const bindResult = (foot) => ({
  foot,
  mac: foot === "L" ? BOUND.left.mac : BOUND.right.mac,
  masked: foot === "L" ? "…:11:11" : "…:22:22",
  binding: BOUND,
});

describe("配对向导：先左（蓝色）后右（橙色）", () => {
  it("两步都成功后出现「完成」，文案只说外壳颜色", async () => {
    const bindFoot = vi.fn(async (foot) => bindResult(foot));
    const onDone = vi.fn();
    render(<BindingWizardScreen bindFoot={bindFoot} onDone={onDone} onCancel={vi.fn()} />);

    expect(screen.getByRole("heading", { name: "绑定左脚：只打开蓝色模块" })).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "开始识别" }));
    expect(await screen.findByRole("heading", { name: "绑定右脚：只打开橙色模块" }, WAIT)).toBeVisible();
    expect(bindFoot).toHaveBeenNthCalledWith(1, "L");
    expect(screen.getByLabelText("左脚绑定结果")).toHaveTextContent("左脚 已绑定 …:11:11");

    fireEvent.click(screen.getByRole("button", { name: "开始识别" }));
    expect(await screen.findByRole("heading", { name: "左右模块已绑定" }, WAIT)).toBeVisible();
    expect(bindFoot).toHaveBeenNthCalledWith(2, "R");
    expect(screen.getByLabelText("右脚绑定结果")).toHaveTextContent("右脚 已绑定 …:22:22");

    fireEvent.click(screen.getByRole("button", { name: "完成" }));
    expect(onDone).toHaveBeenCalledOnce();
  });

  it("多台模块开着时停在这一步，给出 sidecar 的现象与动作，并可重试", async () => {
    const error = Object.assign(new Error(MULTIPLE.message), { code: MULTIPLE.code, action: MULTIPLE.action });
    const bindFoot = vi.fn().mockRejectedValueOnce(error).mockResolvedValueOnce(bindResult("L"));
    render(<BindingWizardScreen bindFoot={bindFoot} onDone={vi.fn()} onCancel={vi.fn()} />);

    fireEvent.click(screen.getByRole("button", { name: "开始识别" }));
    const banner = await screen.findByLabelText("识别失败", {}, WAIT);
    expect(banner).toHaveTextContent("发现多个未绑定模块");
    expect(banner).toHaveTextContent("请只打开蓝色（左脚）模块");
    expect(screen.getByRole("heading", { name: "绑定左脚：只打开蓝色模块" })).toBeVisible();

    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    expect(await screen.findByRole("heading", { name: "绑定右脚：只打开橙色模块" }, WAIT)).toBeVisible();
    expect(screen.queryByLabelText("识别失败")).not.toBeInTheDocument();
  });

  it("没有「跳过」或「对调」", () => {
    render(<BindingWizardScreen bindFoot={vi.fn()} onDone={vi.fn()} onCancel={vi.fn()} />);
    expect(screen.queryByRole("button", { name: /跳过|对调/ })).not.toBeInTheDocument();
  });
});

describe("工作台：真实传感器未绑定时先去绑定", () => {
  const hub = (binding, props = {}) =>
    render(
      <HubScreen
        snapshot={{ ...SNAPSHOT, deviceSummary: { ready: false, issues: ["左脚电量读不到"] }, binding }}
        onNavigate={vi.fn()}
        onRecheck={vi.fn()}
        onStartNewAssessment={vi.fn()}
        {...props}
      />,
    );

  it("未绑定：警示条 + 主按钮「绑定左右模块」，没有「开始新的检测」", () => {
    const onBind = vi.fn();
    hub(UNBOUND, { onBind });
    expect(screen.getByText("左右模块尚未绑定")).toBeVisible();
    expect(screen.queryByRole("button", { name: "开始新的检测" })).not.toBeInTheDocument();
    expect(screen.queryByText("左脚电量读不到")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "绑定左右模块" }));
    expect(onBind).toHaveBeenCalledOnce();
  });

  it("演示模式（required=false）不出现绑定提示", () => {
    hub({ ...UNBOUND, required: false });
    expect(screen.queryByText("左右模块尚未绑定")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "绑定左右模块" })).not.toBeInTheDocument();
  });

  it("已绑定：回到原有的检查/开始逻辑", () => {
    hub(BOUND);
    expect(screen.queryByText("左右模块尚未绑定")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "重新检查设备" })).toBeVisible();
  });
});

describe("自检：绑定项失败时直接给出配对入口", () => {
  it("binding 项 fail → 「绑定左右模块」按钮", async () => {
    const onRepairBinding = vi.fn();
    const runChecks = vi.fn().mockResolvedValue([
      {
        id: "binding", label: "左右模块绑定", status: "fail", hint: null,
        error: { code: "E-BLE-1030", message: "左脚尚未绑定：请先完成配对。", action: "请到「设备与支持」点「重新配对模块」。" },
      },
    ]);
    render(<PreflightScreen runChecks={runChecks} onReady={vi.fn()} onRepairBinding={onRepairBinding} />);
    screen.getAllByRole("checkbox").forEach((box) => fireEvent.click(box));
    fireEvent.click(await screen.findByRole("button", { name: "绑定左右模块" }, WAIT));
    expect(onRepairBinding).toHaveBeenCalledOnce();
  });
});

describe("生产 TerminalApp 的「重新配对模块」不是空操作（验收 4）", () => {
  function realSidecar() {
    let binding = UNBOUND;
    const fake = fakeTransport({
      snapshot: () => ({ ...SNAPSHOT, deviceSummary: { ready: false, issues: [] }, binding }),
      listRecords: [],
      deviceSupport: DEVICE_SUPPORT,
      bindingStatus: () => binding,
      bindFoot: ({ foot }) => {
        binding = foot === "R" ? BOUND : { ...UNBOUND, left: BOUND.left };
        return bindResult(foot);
      },
    });
    return { adapter: createSidecarAdapter(fake.transport, { now: () => 100 }), calls: fake.calls };
  }

  it("设备与支持 → 重新配对模块 → 确认 → 向导真的调用 bindFoot，完成后回到设备页", async () => {
    const { adapter, calls } = realSidecar();
    render(<TerminalApp adapter={adapter} />);
    await screen.findByRole("heading", { name: "工作台" }, WAIT);
    fireEvent.click(screen.getByRole("button", { name: "设备与支持" }));
    await screen.findByRole("heading", { name: "设备与支持" }, WAIT);

    fireEvent.click(screen.getByRole("button", { name: "重新配对模块" }));
    fireEvent.click(within(screen.getByRole("alertdialog")).getByRole("button", { name: "重新配对" }));
    expect(await screen.findByRole("heading", { name: "绑定左脚：只打开蓝色模块" }, WAIT)).toBeVisible();

    fireEvent.click(screen.getByRole("button", { name: "开始识别" }));
    await screen.findByRole("heading", { name: "绑定右脚：只打开橙色模块" }, WAIT);
    fireEvent.click(screen.getByRole("button", { name: "开始识别" }));
    fireEvent.click(await screen.findByRole("button", { name: "完成" }, WAIT));
    expect(await screen.findByRole("heading", { name: "设备与支持" }, WAIT)).toBeVisible();

    const binds = calls.filter((call) => call.method === "bindFoot").map((call) => call.params.foot);
    expect(binds).toEqual(["L", "R"]);
  });

  it("工作台在未绑定时进入向导，完成后回到工作台并重拉快照", async () => {
    const { adapter, calls } = realSidecar();
    render(<TerminalApp adapter={adapter} />);
    fireEvent.click(await screen.findByRole("button", { name: "绑定左右模块" }, WAIT));
    fireEvent.click(await screen.findByRole("button", { name: "开始识别" }, WAIT));
    await screen.findByRole("heading", { name: "绑定右脚：只打开橙色模块" }, WAIT);
    fireEvent.click(screen.getByRole("button", { name: "开始识别" }));
    fireEvent.click(await screen.findByRole("button", { name: "完成" }, WAIT));
    await screen.findByRole("heading", { name: "工作台" }, WAIT);
    await waitFor(() => expect(screen.queryByText("左右模块尚未绑定")).not.toBeInTheDocument(), WAIT);
    expect(calls.filter((call) => call.method === "snapshot").length).toBeGreaterThanOrEqual(2);
  });

  it("配对失败的错误码与动作原样显示", async () => {
    const fake = fakeTransport({
      snapshot: { ...SNAPSHOT, binding: UNBOUND },
      listRecords: [],
      bindFoot: fail(MULTIPLE),
    });
    render(<TerminalApp adapter={createSidecarAdapter(fake.transport)} />);
    fireEvent.click(await screen.findByRole("button", { name: "绑定左右模块" }, WAIT));
    fireEvent.click(await screen.findByRole("button", { name: "开始识别" }, WAIT));
    const banner = await screen.findByLabelText("识别失败", {}, WAIT);
    expect(banner).toHaveTextContent("E-BLE-1031");
    expect(banner).toHaveTextContent("请只打开蓝色（左脚）模块");
  });
});

describe("adapter", () => {
  it("bindingStatus / bindFoot 走契约方法，reportFor 不再发 swapped", async () => {
    const fake = fakeTransport({ bindingStatus: BOUND, bindFoot: bindResult("R"), reportFor: {} });
    const adapter = createSidecarAdapter(fake.transport);
    expect(await adapter.bindingStatus()).toEqual(BOUND);
    expect((await adapter.bindFoot("R")).masked).toBe("…:22:22");
    await adapter.reportFor({ id: "s1", swapped: true });
    expect(fake.calls.map((call) => call.method)).toEqual(["bindingStatus", "bindFoot", "reportFor"]);
    expect(fake.calls[1].params).toEqual({ foot: "R" });
    expect(fake.calls[2].params).not.toHaveProperty("swapped");
  });
});
