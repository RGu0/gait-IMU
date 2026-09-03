import { fireEvent, render, screen, within } from "@testing-library/react";
import { ConsentScreen } from "./ConsentScreen.jsx";
import { ProfileScreen } from "./ProfileScreen.jsx";
import { SubjectScreen } from "./SubjectScreen.jsx";
import { TerminalApp } from "./TerminalApp.jsx";

/**
 * These assertions are written against the hard constraints in
 * `UI布局设计_v0.5.md` §2, not against the current markup. Each one names the
 * constraint it guards, because the reason a rule exists is the part that gets
 * lost when someone later "simplifies" the screen.
 */

const foundSubject = {
  maskedId: "**2781",
  ageBand: "60–74",
  sex: "女",
  lastAssessedAt: "2026-06-14",
  lastProtocolSeconds: 180,
  consentValid: true,
};

const snapshot = {
  operator: { organization: "community-kangjian" },
  protocolSeconds: 120,
  deviceSummary: { ready: true, issues: [], leftBattery: 82, rightBattery: 76 },
  uploadSummary: { pending: 0, uploaded: 12 },
  recentRecords: [{ subjectLabel: "**1234", status: "已完成" }],
};

function adapterWith(overrides = {}) {
  return {
    snapshot: async () => snapshot,
    login: async () => snapshot,
    recheckDevices: async () => snapshot,
    lookupSubject: async () => ({ kind: "found", subject: foundSubject }),
    createSubject: async () => ({
      maskedId: "临时001",
      ageBand: "未提供",
      sex: "未提供",
      lastAssessedAt: "—",
      lastProtocolSeconds: null,
      consentValid: false,
    }),
    ...overrides,
  };
}

async function signIn(adapter) {
  render(<TerminalApp adapter={adapter} />);
  // 最小 MVP 无登录（P-00 暂不考虑）：冷启动直接进工作台，点「开始新的检测」。
  fireEvent.click(await screen.findByRole("button", { name: "开始新的检测" }));
  return screen.findByRole("heading", { name: "受试者识别" });
}

/** A filled primary button is the one carrying the brand background inline. */
function filledPrimaryButtons() {
  return screen
    .getAllByRole("button")
    .filter((button) => button.style.background.includes("--brand-primary"));
}

describe("AC-02 — the whole optional profile can be skipped", () => {
  it("reaches consent without the operator answering anything optional", async () => {
    await signIn(adapterWith());

    fireEvent.click(screen.getByRole("button", { name: "无编号，快速建档" }));
    await screen.findByRole("heading", { name: "选填档案" });

    // Not "continue" — the explicit skip, which is the path a rushed operator takes.
    fireEvent.click(screen.getByRole("button", { name: "跳过" }));

    expect(await screen.findByRole("heading", { name: "数据授权" })).toBeVisible();
  });

  it("says so on the page, above every field", async () => {
    render(<ProfileScreen onContinue={vi.fn()} onSkip={vi.fn()} />);
    expect(screen.getByText("以下全部为选填，可直接继续。")).toBeVisible();
  });
});

describe("C-13 — a different protocol length must be called out before the walk", () => {
  it("warns that the two runs are not comparable", async () => {
    await signIn(adapterWith());
    fireEvent.change(screen.getByLabelText("档案号"), { target: { value: "2781" } });
    fireEvent.click(screen.getByRole("button", { name: "查找" }));

    const banner = await screen.findByText(/不可直接比较/);
    expect(banner).toBeVisible();
    expect(banner).toHaveTextContent("120");
    expect(banner).toHaveTextContent("180");
  });

  it("stays quiet when the two runs used the same length", async () => {
    const sameLength = { ...foundSubject, lastProtocolSeconds: 120 };
    await signIn(adapterWith({ lookupSubject: async () => ({ kind: "found", subject: sameLength }) }));
    fireEvent.change(screen.getByLabelText("档案号"), { target: { value: "3140" } });
    fireEvent.click(screen.getByRole("button", { name: "查找" }));

    await screen.findByRole("heading", { name: "核对受试者信息" });
    expect(screen.queryByText(/不可直接比较/)).not.toBeInTheDocument();
  });

  it("always shows the previous protocol row, matching or not", async () => {
    await signIn(adapterWith());
    fireEvent.change(screen.getByLabelText("档案号"), { target: { value: "2781" } });
    fireEvent.click(screen.getByRole("button", { name: "查找" }));

    expect(await screen.findByText("上次时长配置")).toBeVisible();
    expect(screen.getByText("180 秒")).toBeVisible();
  });
});

describe("C-10 — the conflict screen must not decide for the operator", () => {
  const conflict = {
    kind: "conflict",
    candidates: [
      { maskedId: "**9000", ageBand: "60–74", sex: "女", lastAssessedAt: "2026-05-08" },
      { maskedId: "**9000", ageBand: "60–74", sex: "女", lastAssessedAt: "2026-07-19" },
    ],
  };

  async function openConflict() {
    await signIn(adapterWith({ lookupSubject: async () => conflict }));
    fireEvent.change(screen.getByLabelText("档案号"), { target: { value: "9000" } });
    fireEvent.click(screen.getByRole("button", { name: "查找" }));
    return screen.findByRole("heading", { name: "找到两条可能相同的档案" });
  }

  it("offers zero filled primary buttons", async () => {
    await openConflict();
    // A filled button here would read as "the system already picked one".
    expect(filledPrimaryButtons()).toHaveLength(0);
  });

  it("presents both candidates identically, neither pre-endorsed", async () => {
    await openConflict();
    const choosers = screen.getAllByRole("button", { name: "选择这一条" });
    expect(choosers).toHaveLength(2);
    expect(choosers[0].className).toBe(choosers[1].className);
    expect(choosers[0].style.background).toBe(choosers[1].style.background);
  });

  it("states in words that nothing is merged automatically", async () => {
    await openConflict();
    expect(screen.getByText(/系统不会自动合并档案/)).toBeVisible();
  });

  it("keeps a way out that is neither candidate", async () => {
    await openConflict();
    expect(screen.getByRole("button", { name: "都不是，新建档案" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "重新输入档案号" })).toBeEnabled();
  });
});

describe("C-12 — 「未提供」 and 「无」 are different answers", () => {
  it("offers both as separate options wherever an absence is meaningful", () => {
    render(<ProfileScreen onContinue={vi.fn()} onSkip={vi.fn()} />);
    // 跌倒史 and 辅助器具 each carry their own 无 / 未提供 pair.
    expect(screen.getAllByText("无").length).toBeGreaterThanOrEqual(2);
    expect(screen.getAllByText("未提供").length).toBeGreaterThanOrEqual(4);
  });

  it("treats an untouched field as 未提供, never as 无", () => {
    const onContinue = vi.fn();
    render(<ProfileScreen onContinue={onContinue} onSkip={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "继续" }));

    const submitted = onContinue.mock.calls[0][0];
    expect(submitted.fallHistory).toBe("未提供");
    expect(submitted.ageBand).toBe("未提供");
    expect(submitted.walkingAids).toEqual(["未提供"]);
  });

  it("does not let 无 and a concrete aid be recorded together", () => {
    const onContinue = vi.fn();
    render(<ProfileScreen onContinue={onContinue} onSkip={vi.fn()} />);

    // Scoped to the field: 「无」 also exists under 跌倒史, and an unscoped query
    // would silently pick whichever comes first.
    const aids = screen.getByRole("group", { name: "辅助器具" });
    fireEvent.click(within(aids).getByText("拄拐"));
    fireEvent.click(within(aids).getByText("无"));
    fireEvent.click(screen.getByRole("button", { name: "继续" }));

    expect(onContinue.mock.calls[0][0].walkingAids).toEqual(["无"]);
  });

  it("keeps 无 and 未提供 separately selectable in each field that offers both", () => {
    for (const field of ["跌倒史", "辅助器具"]) {
      const { unmount } = render(<ProfileScreen onContinue={vi.fn()} onSkip={vi.fn()} />);
      const group = screen.getByRole("group", { name: field });
      expect(within(group).getByText("无")).toBeVisible();
      expect(within(group).getByText("未提供")).toBeVisible();
      unmount();
    }
  });
});

describe("P-03 — the screen never mentions the algorithm", () => {
  it("describes walking in plain words only", () => {
    const { container } = render(<ProfileScreen onContinue={vi.fn()} onSkip={vi.fn()} />);
    expect(container.textContent).not.toMatch(/算法|检测器|预设|模型|参数|preset/i);
    expect(screen.getByText("拖步")).toBeVisible();
  });
});

describe("P-04 — consent starts un-given", () => {
  it("ticks nothing and disables the primary action", () => {
    render(<ConsentScreen onAgree={vi.fn()} onDecline={vi.fn()} />);
    for (const box of screen.getAllByRole("checkbox")) {
      expect(box).not.toBeChecked();
    }
    expect(screen.getByRole("button", { name: "同意并继续" })).toBeDisabled();
  });

  it("enables the primary action only after both required items are ticked", () => {
    render(<ConsentScreen onAgree={vi.fn()} onDecline={vi.fn()} />);
    const required = screen.getByRole("region", { name: "进行本次检测所必需" });
    const boxes = within(required).getAllByRole("checkbox");

    fireEvent.click(boxes[0]);
    expect(screen.getByRole("button", { name: "同意并继续" })).toBeDisabled();

    fireEvent.click(boxes[1]);
    expect(screen.getByRole("button", { name: "同意并继续" })).toBeEnabled();
  });

  it("does not require the optional purpose", () => {
    const onAgree = vi.fn();
    render(<ConsentScreen onAgree={onAgree} onDecline={vi.fn()} />);
    const required = screen.getByRole("region", { name: "进行本次检测所必需" });
    for (const box of within(required).getAllByRole("checkbox")) {
      fireEvent.click(box);
    }
    fireEvent.click(screen.getByRole("button", { name: "同意并继续" }));

    expect(onAgree).toHaveBeenCalledWith({ required: ["collect", "upload"], optional: [] });
  });

  it("lets the operator decline without being warned off it", () => {
    const { container } = render(<ConsentScreen onAgree={vi.fn()} onDecline={vi.fn()} />);
    expect(screen.getByRole("button", { name: "不同意，结束本次" })).toBeEnabled();
    expect(container.textContent).not.toMatch(/无法|后果|风险|please|必须同意/i);
  });

  it("names the ankle data in the words the subject will recognise", () => {
    render(<ConsentScreen onAgree={vi.fn()} onDecline={vi.fn()} />);
    expect(screen.getByText(/足踝运动数据/)).toBeVisible();
  });
});

describe("P-02 — input paths", () => {
  it("accepts a barcode scanner's Enter as a submit", async () => {
    const lookupSubject = vi.fn().mockResolvedValue({ kind: "found", subject: foundSubject });
    await signIn(adapterWith({ lookupSubject }));

    fireEvent.change(screen.getByLabelText("档案号"), { target: { value: "2781" } });
    fireEvent.submit(screen.getByLabelText("档案号").closest("form"));

    expect(await screen.findByRole("heading", { name: "核对受试者信息" })).toBeVisible();
    expect(lookupSubject).toHaveBeenCalledWith("2781");
  });

  it("explains an unknown id instead of silently doing nothing", async () => {
    await signIn(
      adapterWith({
        lookupSubject: async () => {
          throw new Error("本机构没有这个档案号。");
        },
      }),
    );
    fireEvent.change(screen.getByLabelText("档案号"), { target: { value: "0000" } });
    fireEvent.click(screen.getByRole("button", { name: "查找" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("本机构没有这个档案号。");
  });

  it("reaches the profile page with no id typed at all", async () => {
    await signIn(adapterWith());
    fireEvent.click(screen.getByRole("button", { name: "无编号，快速建档" }));
    expect(await screen.findByRole("heading", { name: "选填档案" })).toBeVisible();
  });
});
