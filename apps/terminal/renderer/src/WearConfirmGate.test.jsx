import { fireEvent, render, screen } from "@testing-library/react";
import { WearConfirmScreen } from "./WearConfirmScreen.jsx";

/**
 * RAY-287 `wear-confirm-gate` — 验收 1：未经确认无法进入下一步，且 `wearing`
 * 不得在未确认时被写成 `pass`。
 *
 * 这一屏此前是「确认屏」而不是「闸」：主按钮恒可用、无条件发 pass。那样的 pass
 * 记录的不是"操作员确认过"，而是"操作员到过这一页" —— 而 PRD §13 的硬拦截语义
 * 已改为「未经确认即阻断」。下面每条都在钉住这个区别。
 */

const renderScreen = (props = {}) =>
  render(<WearConfirmScreen onDone={vi.fn()} onBack={vi.fn()} {...props} />);

const primary = () => screen.getByRole("button", { name: "确认无误，开始检测" });

describe("P-07 — the left/right confirmation is a gate", () => {
  it("will not start the session before a person confirms", () => {
    const onDone = vi.fn();
    renderScreen({ onDone });

    expect(primary()).toBeDisabled();
    fireEvent.click(primary());
    // 一个禁用的按钮同时挡住鼠标与键盘路径：它不可聚焦、不派发 click。
    expect(onDone).not.toHaveBeenCalled();
  });

  it("never reports pass while unconfirmed", () => {
    const onDone = vi.fn();
    renderScreen({ onDone });
    fireEvent.click(primary());

    // 关键不在"没进入下一步"，而在**没有发出 pass**。恒真的 pass 会被事后
    // 审计当成凭据信，比不记更糟。
    expect(onDone).not.toHaveBeenCalledWith(expect.objectContaining({ wearing: "pass" }));
  });

  it("opens the gate once confirmed, and says so as pass", () => {
    const onDone = vi.fn();
    renderScreen({ onDone });

    fireEvent.click(screen.getByRole("checkbox"));
    expect(primary()).toBeEnabled();
    fireEvent.click(primary());
    expect(onDone).toHaveBeenCalledWith({ wearing: "pass" });
  });

  it("explains why the primary action is not available yet", () => {
    renderScreen();
    // 禁用状态不解释自己；不写这句，操作员只能对着灰按钮猜。
    expect(screen.getByRole("status")).toHaveTextContent("核对并勾选后，才能开始检测。");

    fireEvent.click(screen.getByRole("checkbox"));
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });
});

describe("P-07 — no left/right swap (RAY-479)", () => {
  it("offers no swap control: sides come only from the colour binding", () => {
    renderScreen();
    // 用户拍板：左右只由配对绑定决定。任何能在一场里翻转左右的按钮都不该存在。
    expect(screen.queryByRole("button", { name: /对调|恢复左右/ })).not.toBeInTheDocument();
    expect(screen.getAllByRole("button").map((b) => b.textContent)).toEqual([
      "返回佩戴引导",
      "确认无误，开始检测",
    ]);
  });

  it("shows the physical shell colour reminder", () => {
    const { container } = renderScreen();
    expect(screen.getByLabelText("佩戴颜色提醒")).toHaveTextContent("蓝色模块戴左脚、橙色模块戴右脚");
    // 按 .wear-points 取，不按 role —— 向导头部的步骤条也是 listitem。
    const rows = container.querySelectorAll(".wear-points li");
    expect(rows[0]).toHaveTextContent("受试者左踝← 蓝色模块");
    expect(rows[1]).toHaveTextContent("受试者右踝← 橙色模块");
  });

  it("never sends a swapped flag", () => {
    const onDone = vi.fn();
    renderScreen({ onDone });
    fireEvent.click(screen.getByRole("checkbox"));
    fireEvent.click(primary());
    expect(onDone.mock.calls[0][0]).not.toHaveProperty("swapped");
  });
});
