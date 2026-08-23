import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { ReportDocument } from "../index.js";

const DIR = path.dirname(fileURLToPath(import.meta.url));
const CSS = fs.readFileSync(path.join(DIR, "..", "report.css"), "utf8");

const report = {
  organization: "康健社区卫生服务中心",
  subjectLabel: "**2781",
  assessedAt: "2026-08-23",
  protocolName: "定时步行测试",
  protocolSeconds: 120,
  edition: "基础版",
  reportId: "R-2026-0823-0031",
  algoVersion: "gait-core 0.4.1",
  protocolVersion: "T-01 v3",
  annotations: ["受试者使用了拄拐"],
  summary: "本次步行的速度与节律处于日常活动可完成的范围。",
  advice: "建议关注近期是否有跌倒或步态改变，必要时复测。",
  metrics: [
    { key: "speed", title: "步速", value: "1.04", unit: "m/s", grade: "normal" },
    { key: "cadence", title: "步频", value: "108", unit: "步/分", grade: "normal" },
    { key: "ds", title: "双支撑期占比", value: "27.9", unit: "%", grade: "low" },
    { key: "fatigue", title: "疲劳衰减", grade: "uncomputable", reason: "本次为 120 秒配置。" },
  ],
  comparison: [
    { label: "步长", left: 1.16, right: 1.14, unit: "m" },
    { label: "站立相时长", left: 0.62, right: 0.64, unit: "s" },
  ],
  parameters: [
    { label: "步长变异系数", value: "4.2", unit: "%", grade: "normal", qualityLabel: "良好" },
    { label: "步周期变异系数", unit: "%", grade: "uncomputable", qualityLabel: "不适用" },
  ],
  timeline: { left: [40, 96, 152], right: [66, 122, 178] },
  conditions: [
    { label: "时长配置", value: "120 秒" },
    { label: "有效步数", value: "118" },
  ],
};

const html = () => renderToStaticMarkup(<ReportDocument report={report} />);

describe("the report never leaves a metric blank", () => {
  it("says 本次不适用 with a reason instead of an empty slot", () => {
    const markup = html();
    expect(markup).toContain("本次不适用");
    expect(markup).toContain("本次为 120 秒配置。");
  });

  it("renders no N/A, no bare dash and no lone zero as a value", () => {
    const markup = html();
    // Each of these reads as a measured value of nothing, which is a different
    // claim from "we could not measure this".
    expect(markup).not.toMatch(/>\s*(N\/A|—|--|n\/a)\s*</);
  });

  it("marks a low-quality metric as 参考 rather than hiding it", () => {
    const markup = html();
    expect(markup).toContain("rp-metric--low");
    expect(markup).toContain("本次有效步数较少，此项仅供参考。");
    expect(markup).toContain("27.9"); // the value still ships
  });
});

describe("section order is the one PRD §12 fixes", () => {
  it("runs cover → summary → metrics → comparison → parameters → chart → conditions → footer", () => {
    const markup = html();
    const order = [
      "步态检测报告",
      "筛查摘要",
      "核心指标",
      "左右对比",
      "专业参数",
      "步态时序",
      "测试条件",
      "报告编号",
    ].map((needle) => markup.indexOf(needle));

    expect(order.every((i) => i >= 0)).toBe(true);
    expect([...order].sort((a, b) => a - b)).toEqual(order);
  });

  it("puts the annotation strip immediately after the cover", () => {
    const markup = html();
    expect(markup.indexOf("受试者使用了拄拐")).toBeGreaterThan(markup.indexOf("步态检测报告"));
    expect(markup.indexOf("受试者使用了拄拐")).toBeLessThan(markup.indexOf("筛查摘要"));
  });
});

describe("wording stays inside what a screening tool may say", () => {
  it("uses no diagnostic language", () => {
    const markup = html();
    expect(markup).not.toMatch(/诊断|确诊|患有|疾病|异常步态|病症|阳性|阴性/);
  });

  it("keeps advice to the three permitted forms", () => {
    expect(report.advice).toMatch(/建议关注|建议复测|建议进一步评估/);
  });
});

describe("left and right survive a grayscale print (C-9)", () => {
  it("carries the side as a character as well as a colour", () => {
    const markup = html();
    expect(markup).toContain(">左<");
    expect(markup).toContain(">右<");
  });

  it("distinguishes the bars by shape and fill, not hue alone", () => {
    // rounded square vs circle, solid vs hatched
    expect(CSS).toMatch(/\.rp-sidemark--left\s*\{[^}]*border-radius:\s*1\.2mm/);
    expect(CSS).toMatch(/\.rp-sidemark--right\s*\{[^}]*border-radius:\s*50%/);
    expect(CSS).toMatch(/\.rp-sidebar__fill--right\s*\{[^}]*repeating-linear-gradient/);
  });

  it("dashes the right series in the timeline chart", () => {
    expect(html()).toContain('stroke-dasharray="6 4"');
  });
});

describe("C-8 — no reference bands anywhere", () => {
  it("draws no normal range, cohort line or band", () => {
    const markup = html();
    expect(markup).not.toMatch(/正常范围|参考区间|常模|健康人|同龄人/);
  });
});

describe("the page is built for A4, not for a screen", () => {
  it("declares A4 portrait with 18mm margins", () => {
    expect(CSS).toMatch(/@page\s*\{[^}]*size:\s*A4 portrait/);
    expect(CSS).toMatch(/@page\s*\{[^}]*margin:\s*18mm/);
  });

  it("sizes the content column to the printable width", () => {
    // 210mm − 2×18mm = 174mm
    expect(CSS).toMatch(/\.rp-page\s*\{[^}]*width:\s*174mm/);
  });

  it("embeds Chinese faces by name rather than relying on the host", () => {
    expect(CSS).toMatch(/font-family:[^;]*Noto Sans SC/);
  });

  it("allows small type only in the footer", () => {
    // The footer is the single place the spec permits type this small.
    const small = [...CSS.matchAll(/font-size:\s*(8(?:\.\d)?)pt/g)];
    expect(small.length).toBeGreaterThan(0);
    const footerBlock = CSS.slice(CSS.indexOf(".rp-footer"));
    expect(footerBlock).toMatch(/font-size:\s*8pt/);
  });
});
