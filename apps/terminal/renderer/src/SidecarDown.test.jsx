/**
 * V-U4 的界面那一半：sidecar 不在时，操作员看到的是什么。
 */
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { SidecarDownScreen } from "./SidecarDownScreen.jsx";
import { TerminalApp } from "./TerminalApp.jsx";
import { createSidecarAdapter, SidecarDown } from "./sidecarTerminalAdapter.js";
import { mockTerminalAdapter } from "./mockTerminalAdapter.js";
import { selectAdapter } from "./main.jsx";

const DOWN = {
  kind: "sidecar-unavailable",
  message: "采集服务连续 3 次未能启动。",
  action: "本次检测无法继续。请退出并重新打开应用；若仍不行，请联系服务方。",
  recoverable: false,
};

/** 让 TerminalApp 处在某个 sidecar 状态。 */
function lifecycleAt(state, notice) {
  return { subscribe: (handler) => { handler({ state, notice }); return () => {}; } };
}

describe("接管屏", () => {
  it("显示主进程给的现象与动作", () => {
    render(<SidecarDownScreen notice={DOWN} onRetry={vi.fn()} />);
    expect(screen.getByText(DOWN.message)).toBeInTheDocument();
    expect(screen.getByText(DOWN.action)).toBeInTheDocument();
  });

  it("不显示任何六域错误码", () => {
    // 进程没了不属于六个域中的任何一个；编一个 E-BLE 会让操作员去排查
    // 一根好好的蓝牙链路。
    const { container } = render(<SidecarDownScreen notice={DOWN} onRetry={vi.fn()} />);
    expect(container.textContent).not.toMatch(/E-(BLE|WEAR|CAL|SYNC|QLT|NET)/);
  });

  it("可恢复时不给按钮 —— 没什么可点的，等就是了", () => {
    render(<SidecarDownScreen notice={{ ...DOWN, recoverable: true }} onRetry={vi.fn()} />);
    expect(screen.queryByRole("button")).toBeNull();
  });
});

describe("接管发生在所有其他屏之前", () => {
  it("sidecar 不可用时盖住工作台", () => {
    render(
      <TerminalApp adapter={mockTerminalAdapter} lifecycle={lifecycleAt("unavailable", DOWN)} />,
    );
    expect(screen.getByRole("alert")).toHaveTextContent("采集服务连续 3 次未能启动");
    // 登录屏的东西不该同时在场
    expect(screen.queryByLabelText("机构账号")).toBeNull();
  });

  it("重启中同样接管 —— 此刻点什么都只会再失败一次", () => {
    render(
      <TerminalApp
        adapter={mockTerminalAdapter}
        lifecycle={lifecycleAt("restarting", { ...DOWN, message: "采集服务已停止，正在自动重新启动。", recoverable: true })}
      />,
    );
    expect(screen.getByRole("alert")).toHaveTextContent("正在自动重新启动");
  });

  it("ready 时不接管", () => {
    render(<TerminalApp adapter={mockTerminalAdapter} lifecycle={lifecycleAt("ready")} />);
    expect(screen.getByLabelText("机构账号")).toBeInTheDocument();
  });

  it("没有 lifecycle（mock 路径）时不接管", () => {
    // 走 mock 时没有进程可看护。给它造一个永远 ready 的假生命周期，
    // 等于让「进程健康」这件事在没有进程时也报告通过。
    render(<TerminalApp adapter={mockTerminalAdapter} />);
    expect(screen.getByLabelText("机构账号")).toBeInTheDocument();
  });
});

describe("adapter 把进程级失败与契约错误分开", () => {
  it("sidecarUnavailable 抛 SidecarDown，而不是 TerminalFailure", async () => {
    const adapter = createSidecarAdapter(async () => ({
      kind: "response",
      id: "1",
      status: "error",
      sidecarUnavailable: DOWN,
    }));
    const error = await adapter.snapshot().catch((caught) => caught);
    expect(error).toBeInstanceOf(SidecarDown);
    expect(error.notice).not.toHaveProperty("code");
    expect(error.recoverable).toBe(false);
  });
});

describe("桥接带生命周期时 main.jsx 把它传下去", () => {
  it("有 onSidecarState 就订阅", () => {
    const chosen = selectAdapter({
      gaitSidecar: { request: async () => ({}), onSidecarState: () => () => {} },
    });
    expect(chosen.mocked).toBe(false);
    expect(typeof chosen.lifecycle.subscribe).toBe("function");
  });

  it("mock 路径没有生命周期", () => {
    expect(selectAdapter({}).lifecycle).toBeNull();
  });
});
