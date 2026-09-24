/**
 * RAY-323 R2：登录页只在终端配了云端访问时出现。
 *
 * 判据是 sidecar 快照里的 `loginRequired`（与 sidecar 的登录闸同源）。这里走真
 * sidecar 适配器 + 假进程，因为「票据过期被闸回来」靠的是 `TerminalFailure.code`，
 * 而那个类型只有真适配器会造。
 *
 * 最要紧的一条是「死胡同回归」：R2 之前渲染端没有登录页、sidecar 却有闸，配了云端
 * 的终端每次开检测都拿到 `E-NET-6044`「请重新登录」，却无处可登。
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { TerminalApp } from "./TerminalApp.jsx";
import { createSidecarAdapter } from "./sidecarTerminalAdapter.js";
import {
  CREATE_SUBJECT,
  PREFLIGHT_WAIVED,
  RECORDS,
  SNAPSHOT,
  START_SESSION,
  fail,
  fakeTransport,
} from "./testing/sidecarFixtures.js";

const OPERATOR = { operatorId: "op-77", displayName: "张医生", expiresAt: "2026-10-01T00:00:00+00:00" };
const WRONG_PASSWORD = {
  code: "E-NET-6040",
  domain: "E-NET",
  message: "机构账号或口令不对。",
  action: "请核对后重新输入；多次失败请联系服务方。",
  blocking: true,
};
const TICKET_EXPIRED = {
  code: "E-NET-6044",
  domain: "E-NET",
  message: "登录已过期。",
  action: "请重新登录机构账号后再开始检测。",
  blocking: true,
};

/**
 * 有状态的假 sidecar：`login` / `logout` 真的改 `operator`，快照读它。
 * `loginRequired` 固定 —— 它说的是终端配置，不随登录变。`overrides` 可以是
 * `(state) => handlers`，好让替换的方法也能改操作员（真 sidecar 的闸会清掉它）。
 */
function rig({ loginRequired, operator = null, overrides = {} }) {
  const state = { operator };
  const fake = fakeTransport({
    snapshot: () => ({ ...SNAPSHOT, loginRequired, operator: state.operator }),
    listRecords: RECORDS,
    login: ({ password }) => {
      if (password !== "right") return fail(WRONG_PASSWORD);
      state.operator = OPERATOR;
      return { ...SNAPSHOT, loginRequired, operator: state.operator };
    },
    logout: () => {
      state.operator = null;
      return { ...SNAPSHOT, loginRequired, operator: null };
    },
    createSubject: CREATE_SUBJECT,
    runPreflight: PREFLIGHT_WAIVED,
    startSession: START_SESSION,
    ...(typeof overrides === "function" ? overrides(state) : overrides),
  });
  const adapter = createSidecarAdapter(fake.transport, { now: () => 100 });
  const sent = () => fake.calls.map((request) => request.method);
  return { adapter, state, sent };
}

const click = (name) => fireEvent.click(screen.getByRole("button", { name }));
const tickAll = () => screen.getAllByRole("checkbox").forEach((box) => fireEvent.click(box));

function typeCredentials(organization, password) {
  fireEvent.change(screen.getByLabelText("机构账号"), { target: { value: organization } });
  fireEvent.change(screen.getByLabelText("登录密码"), { target: { value: password } });
}

/** 从工作台走到「确认无误，开始检测」—— 即 startSession 被调用的那一下。 */
async function walkToStart() {
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
  await screen.findByRole("heading", { name: "确认左右" });
  tickAll();
  click("确认无误，开始检测");
}

describe("未配置云端访问的终端（预览版）", () => {
  it("冷启动直接进工作台：没有登录表单，也没有登出", async () => {
    const { adapter, sent } = rig({ loginRequired: false });
    render(<TerminalApp adapter={adapter} />);
    expect(await screen.findByRole("button", { name: "开始新的检测" })).toBeVisible();
    expect(screen.queryByLabelText("机构账号")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "登出" })).not.toBeInTheDocument();
    expect(sent()).not.toContain("login");
  });
});

describe("配了云端访问的终端", () => {
  it("冷启动进 P-00，未登录进不了工作台", async () => {
    const { adapter } = rig({ loginRequired: true });
    render(<TerminalApp adapter={adapter} />);
    expect(await screen.findByLabelText("机构账号")).toBeVisible();
    expect(screen.queryByRole("button", { name: "开始新的检测" })).not.toBeInTheDocument();
  });

  it("口令不对就停在 P-00，显示 sidecar 给的现象 + 动作 + 码", async () => {
    const { adapter } = rig({ loginRequired: true });
    render(<TerminalApp adapter={adapter} />);
    await screen.findByLabelText("机构账号");
    typeCredentials("clinic-a", "wrong");
    click("登录");
    expect(await screen.findByText(/机构账号或口令不对。/)).toHaveTextContent(
      "机构账号或口令不对。请核对后重新输入；多次失败请联系服务方。（错误码 E-NET-6040）",
    );
    expect(screen.queryByRole("button", { name: "开始新的检测" })).not.toBeInTheDocument();
    // 口令不留在框里等下一个人看
    expect(screen.getByLabelText("登录密码")).toHaveValue("");
  });

  it("空字段在渲染端挡住，不发 login", async () => {
    const { adapter, sent } = rig({ loginRequired: true });
    render(<TerminalApp adapter={adapter} />);
    await screen.findByLabelText("机构账号");
    click("登录");
    expect(await screen.findByText("请输入机构账号和登录密码。")).toBeVisible();
    expect(sent()).not.toContain("login");
  });

  it("登录成功进工作台，顶栏显示是谁在操作，并能登出回 P-00", async () => {
    const { adapter, state, sent } = rig({ loginRequired: true });
    render(<TerminalApp adapter={adapter} />);
    await screen.findByLabelText("机构账号");
    typeCredentials("clinic-a", "right");
    click("登录");

    expect(await screen.findByRole("button", { name: "开始新的检测" })).toBeVisible();
    expect(screen.getByText("张医生")).toBeVisible();

    click("登出");
    expect(await screen.findByLabelText("机构账号")).toBeVisible();
    expect(sent()).toContain("logout");
    expect(state.operator).toBeNull();
    expect(screen.queryByRole("button", { name: "开始新的检测" })).not.toBeInTheDocument();
  });

  it("冷启动已有未过期票据（sidecar 已恢复操作员）就直接进工作台", async () => {
    const { adapter } = rig({ loginRequired: true, operator: OPERATOR });
    render(<TerminalApp adapter={adapter} />);
    expect(await screen.findByRole("button", { name: "开始新的检测" })).toBeVisible();
    expect(screen.queryByLabelText("机构账号")).not.toBeInTheDocument();
  });

  it(
    "死胡同回归：开检测被登录闸拒绝（E-NET-6044）时回到 P-00，而不是停在错误屏",
    async () => {
      const { adapter } = rig({
        loginRequired: true,
        operator: OPERATOR,
        // 同真 sidecar（service.py 的登录闸）：拒绝的同时清掉操作员。
        overrides: (state) => ({
          startSession: () => {
            state.operator = null;
            return fail(TICKET_EXPIRED);
          },
        }),
      });
      render(<TerminalApp adapter={adapter} />);
      await walkToStart();

      expect(await screen.findByLabelText("机构账号", {}, { timeout: 5000 })).toBeVisible();
      expect(screen.getByText(/登录已过期。/)).toHaveTextContent(
        "登录已过期。请重新登录机构账号后再开始检测。（错误码 E-NET-6044）",
      );
      // 不是「检测未能开始」那一屏：那一屏只有「返回工作台 / 重新检测」，两条路都回不到登录
      expect(screen.queryByRole("button", { name: "重新检测" })).not.toBeInTheDocument();
      await waitFor(() => expect(screen.queryByText("检测未能开始")).not.toBeInTheDocument());
    },
    15000,
  );
});
