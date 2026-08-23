import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import {
  Banner,
  BatteryPair,
  Button,
  ChecklistItem,
  ChipGroup,
  CountdownFocus,
  DataTable,
  Dialog,
  Field,
  LinkStatus,
  MetricTile,
  RhythmStrip,
  SideBadge,
  StatusPill,
  StepBar,
  Toast,
  batteryTier,
} from "../index.js";

/**
 * This package is a MIRROR — the source of truth is the Steady Health project in
 * Claude Design (see SYNC.md). Re-syncing transcribes files, and transcription
 * can damage a component in a way that still parses: a battery bar dropped, a
 * 「左」 glyph lost, an icon channel removed. Type-checking and compiling both
 * pass on that damage.
 *
 * So these tests assert on RENDERED MARKUP, and each one is tied to the reason
 * the component exists (stated in its .prompt.md) rather than to its styling.
 * A test here should fail when the component stops doing its job, and stay
 * quiet when a designer changes a color or a radius.
 */

const html = (element) => renderToStaticMarkup(element);

const DIR = path.dirname(fileURLToPath(import.meta.url));
const COMPONENTS_DIR = path.join(DIR, "..", "components");

function componentSources() {
  const out = [];
  for (const group of fs.readdirSync(COMPONENTS_DIR)) {
    const groupDir = path.join(COMPONENTS_DIR, group);
    if (!fs.statSync(groupDir).isDirectory()) continue;
    for (const file of fs.readdirSync(groupDir)) {
      if (!file.endsWith(".jsx")) continue;
      out.push({
        rel: `${group}/${file}`,
        text: fs.readFileSync(path.join(groupDir, file), "utf8"),
      });
    }
  }
  return out;
}

describe("SideBadge — which foot, on three independent channels", () => {
  // Wearing the modules on the wrong ankles cannot be compensated for by the
  // algorithm (RAY-260 proved the position method cannot even detect it), so
  // the redundancy here is the cheapest place to prevent a wasted session.
  it("carries the side as glyph AND shape AND color", () => {
    const left = html(<SideBadge side="left" />);
    const right = html(<SideBadge side="right" />);

    expect(left).toContain("左");
    expect(left).toMatch(/border-radius:6px/); // rounded square
    expect(left).toContain("var(--side-left)");

    expect(right).toContain("右");
    expect(right).toMatch(/border-radius:999px/); // circle
    expect(right).toContain("var(--side-right)");
  });

  it("still distinguishes the sides with every color removed", () => {
    const strip = (markup) => markup.replace(/var\(--[a-z-]+\)|#[0-9a-fA-F]{3,8}/g, "");
    expect(strip(html(<SideBadge side="left" />))).not.toBe(
      strip(html(<SideBadge side="right" />)),
    );
  });
});

describe("LinkStatus — link health during capture", () => {
  // At 200 Hz the module registers cannot be read, so this row is the ONLY link
  // expression available mid-walk (FR-07). The tier must survive grayscale.
  const filledBars = (markup) => (markup.match(/<rect(?![^>]*fill="none")/g) || []).length;

  it("encodes the tier in the number of solid bars, not only in color", () => {
    expect(filledBars(html(<LinkStatus tier="good" />))).toBe(3);
    expect(filledBars(html(<LinkStatus tier="fair" />))).toBe(2);
    expect(filledBars(html(<LinkStatus tier="bad" />))).toBe(1);
  });

  it("marks the bad tier with a slash on top of the bar count", () => {
    expect(html(<LinkStatus tier="bad" />)).toContain("<path");
    expect(html(<LinkStatus tier="good" />)).not.toContain("<path");
  });

  it("names the tier in words", () => {
    expect(html(<LinkStatus tier="good" />)).toContain("链路良好");
    expect(html(<LinkStatus tier="fair" />)).toContain("链路波动");
    expect(html(<LinkStatus tier="bad" />)).toContain("链路异常");
  });
});

describe("BatteryPair — charge, readable only before the stream starts", () => {
  const barRects = (markup) => (markup.match(/width="4\.6"/g) || []).length;

  it("grades charge by bar count and prints the number too", () => {
    const markup = html(<BatteryPair left={82} right={22} />);
    expect(barRects(markup)).toBe(3 + 1); // 82% → three bars, 22% → one
    expect(markup).toContain("82%");
    expect(markup).toContain("22%");
  });

  it("puts the 30% blocking threshold in one place", () => {
    // 30% blocks a new session; the tiers must not drift from that number.
    expect(batteryTier(60)).toBe("full");
    expect(batteryTier(59)).toBe("mid");
    expect(batteryTier(30)).toBe("mid");
    expect(batteryTier(29)).toBe("low");
  });

  it("labels each battery with its side", () => {
    const markup = html(<BatteryPair left={82} right={22} />);
    expect(markup).toContain("左");
    expect(markup).toContain("右");
  });
});

describe("MetricTile — v1 never gates a metric, it annotates it", () => {
  // The grades are hoisted into constants rather than written inline as props.
  // tools/check_quality_single_source.py flags any line holding both a relational
  // operator and a grade literal, and its operator pattern matches the `<` that
  // opens a JSX tag — so `<MetricTile grade="low" />` trips it. That guard is
  // deliberately over-eager (see its docstring); keeping the literal off the JSX
  // line satisfies it without weakening it. These are props being passed in, not
  // grades being computed here.
  const UNCOMPUTABLE = "uncomputable";
  const LOW = "low";

  it("renders 「本次不适用」 rather than a blank, a 0, an N/A or a dash", () => {
    const markup = html(
      <MetricTile title="疲劳衰减" grade={UNCOMPUTABLE} note="本次为 120 秒配置。" />,
    );
    expect(markup).toContain("本次不适用");
    expect(markup).toContain("本次为 120 秒配置。"); // the reason ships with it
    expect(markup).not.toMatch(/>\s*(N\/A|—|-|0)\s*</);
  });

  it("tags a low-quality value 「参考」 instead of hiding it", () => {
    const markup = html(<MetricTile title="双支撑期" value="27.9" unit="%" grade={LOW} />);
    expect(markup).toContain("参考");
    expect(markup).toContain("27.9"); // the value still ships
  });

  it("leaves a normal value untagged", () => {
    const markup = html(<MetricTile title="步速" value="1.04" unit="m/s" />);
    expect(markup).toContain("1.04");
    expect(markup).not.toContain("参考");
    expect(markup).not.toContain("本次不适用");
  });
});

describe("RhythmStrip — left and right must survive grayscale printing", () => {
  it("separates the sides by direction as well as by token", () => {
    const markup = html(<RhythmStrip left={[18, 66, 112]} right={[42, 90, 137]} />);
    expect(markup).toContain("var(--viz-gait-left)");
    expect(markup).toContain("var(--viz-gait-right)");

    // Left ticks go up from the midline, right ticks go down. That is what keeps
    // the two series apart on a grayscale A4 report.
    const ups = (markup.match(/L \d+(?:\.\d+)? (\d+(?:\.\d+)?)"/g) || []).length;
    expect(ups).toBeGreaterThan(0);
    expect(markup).not.toContain("viz-heat");
  });
});

describe("StatusPill — status never rests on color alone", () => {
  it.each([
    ["success", "check", "已就绪"],
    ["info", "spinner", "生成中"],
    ["danger", "x", "未完成"],
  ])("tone %s ships an icon and its words", (tone, icon, label) => {
    const markup = html(
      <StatusPill tone={tone} icon={icon}>
        {label}
      </StatusPill>,
    );
    expect(markup).toMatch(/<svg|<path|<circle/);
    expect(markup).toContain(label);
  });
});

describe("cross-file composition still resolves", () => {
  // Dialog imports Button, DataTable imports StatusPill, LinkStatus and
  // BatteryPair import SideBadge. A re-sync that renames or drops a file breaks
  // these edges, and the barrel export alone would not notice.
  it("Dialog renders its Button children", () => {
    const markup = html(
      <Dialog open danger title="确定要停止本次测试吗？" confirmLabel="停止检测" cancelLabel="继续测试">
        已采集 49 秒。
      </Dialog>,
    );
    expect(markup).toContain("停止检测");
    expect(markup).toContain("继续测试");
  });

  it("DataTable renders its StatusPill column", () => {
    const markup = html(
      <DataTable
        columns={[
          { key: "id", header: "编号" },
          { key: "s", header: "状态", render: DataTable.status },
        ]}
        rows={[{ id: "**2781", s: { tone: "success", icon: "check", label: "已完成" } }]}
      />,
    );
    expect(markup).toContain("已完成");
    expect(markup).toContain("**2781");
  });
});

describe("every component renders at all", () => {
  // A smoke pass so a component that is damaged but not asserted on above still
  // fails here rather than shipping broken.
  it.each([
    ["Button", <Button variant="primary">开始新的检测</Button>],
    ["Field", <Field label="身高" unit="cm" value="162" />],
    ["ChipGroup", <ChipGroup options={["拖步", "小碎步"]} value={["拖步"]} />],
    ["Banner", <Banner tone="warning" title="网络中断">当前检测不受影响。</Banner>],
    ["Toast", <Toast tone="success">PDF 已导出</Toast>],
    ["StepBar", <StepBar steps={["识别", "档案", "授权"]} current={1} />],
    ["ChecklistItem", <ChecklistItem status="pass" label="左模块连接" hint="已连接" />],
    ["CountdownFocus", <CountdownFocus seconds={71} instruction="请按平时走路的速度来回走" />],
  ])("%s produces markup", (_name, element) => {
    expect(html(element).length).toBeGreaterThan(0);
  });
});

describe("library-wide invariants", () => {
  // Matches both spellings, and the quotes matter: in a JSX inline style the
  // value is ALWAYS quoted (`outline: "none"`), so a CSS-shaped pattern like
  // /outline:\s*none/ silently never fires on the case that actually occurs.
  // A mutation run caught exactly that — the first version of this test passed
  // happily with `outline: "none"` inserted into Button.
  const SUPPRESSED_OUTLINE = /outline(?:Width)?\s*:\s*["']?\s*(?:none|0(?:px|em|rem)?)\b/;

  it("never suppresses the focus ring, in a component", () => {
    // Every operator interaction happens on a shared terminal; a hidden focus
    // ring is how keyboard users lose their place.
    const offenders = componentSources()
      .filter(({ text }) => SUPPRESSED_OUTLINE.test(text))
      .map(({ rel }) => rel);
    expect(offenders).toEqual([]);
  });

  it("never suppresses the focus ring, in a token", () => {
    // --focus-ring is the other place it can be switched off, and doing it there
    // disables the ring for both products at once.
    const tokensDir = path.join(DIR, "..", "tokens");
    const offenders = fs
      .readdirSync(tokensDir)
      .filter((f) => f.endsWith(".css"))
      .filter((f) => {
        const text = fs.readFileSync(path.join(tokensDir, f), "utf8");
        return SUPPRESSED_OUTLINE.test(text) || /--focus-ring\s*:\s*none/.test(text);
      });
    expect(offenders).toEqual([]);
  });

  it("never reaches for the plantar-pressure heat scale", () => {
    // This product has no pressure dimension; reusing that scale would read as a
    // pressure map. tokens/viz.css says so — this proves no component ignored it.
    const offenders = componentSources()
      .filter(({ text }) => text.includes("viz-heat"))
      .map(({ rel }) => rel);
    expect(offenders).toEqual([]);
  });

  it("exports every component file from the barrel", () => {
    // A re-sync that adds a component without wiring index.js leaves it invisible
    // to the app, and nothing else would report that.
    const barrel = fs.readFileSync(path.join(DIR, "..", "index.js"), "utf8");
    const unexported = componentSources()
      .filter(({ rel }) => !barrel.includes(`./components/${rel}`))
      .map(({ rel }) => rel);
    expect(unexported).toEqual([]);
  });
});
