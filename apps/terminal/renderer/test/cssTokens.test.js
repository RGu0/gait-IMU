// RAY-543 css-token-guard：app.css 里不带回退的 var(--x)，--x 必须真的有定义。
//
// 设计系统是严格 8px 体系，没有 --space-5（20px）。三类卡片曾写 `padding: var(--space-5);`
// —— 未定义且无回退的 var() 在计算值阶段无效，padding 静悄悄地回落成 0，内容贴着边框，
// 而 lint、测试、构建全都是绿的。
//
// 这里守：app.css 中每个 var(--x)，--x 要么在设计系统 styles.css 引入的 tokens 里、
// 要么在 app.css 自己的规则里有定义。
//
// RAY-753 起**带回退的也查**。当初放过 var(--x, …) 的理由是「它有明确的兜底值」—— 可兜底值
// 一旦永远生效，就等于把一个硬编码颜色藏在 token 名后面：app.css 曾有 15 处
// var(--color-…, #hex)，设计系统根本没有 --color-* 这一族，于是每一处都静默地用着回退色，
// 接管页的红线是 #a12b2b 而不是设计系统的危险色。回退值保护的是「token 偶尔没加载」，
// 不该替一个写错的名字背书。
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.resolve(HERE, "../../../..");
const APP_CSS = path.join(HERE, "../src/app.css");
const DS_ENTRY = path.join(REPO, "packages/design-system/styles.css");

const stripComments = (css) => css.replace(/\/\*[\s\S]*?\*\//g, "");

function definedIn(css) {
  return new Set([...stripComments(css).matchAll(/(--[\w-]+)\s*:/g)].map((m) => m[1]));
}

function designSystemTokens() {
  const entry = fs.readFileSync(DS_ENTRY, "utf8");
  const files = [...entry.matchAll(/@import\s+["']([^"']+)["']/g)].map((m) => path.resolve(path.dirname(DS_ENTRY), m[1]));
  const names = new Set();
  for (const file of files) for (const name of definedIn(fs.readFileSync(file, "utf8"))) names.add(name);
  return names;
}

// 返回 { selector, name }：defined 里没有的用法（带不带回退都算）。
function undefinedVarUses(css, defined) {
  const clean = stripComments(css);
  const hits = [];
  for (const m of clean.matchAll(/var\(\s*(--[\w-]+)\s*[,)]/g)) {
    if (defined.has(m[1])) continue;
    const before = clean.slice(0, m.index);
    const open = before.lastIndexOf("{");
    // 选择器从上一个 } 或（@media 里的第一条规则）上一个 { 之后开始。
    const start = Math.max(before.lastIndexOf("}", open), before.lastIndexOf("{", open - 1));
    const selector = before.slice(start + 1, open).trim();
    hits.push({ selector, name: m[1] });
  }
  return hits;
}

describe("app.css custom properties", () => {
  it("every var() names a token that is defined, with or without a fallback", () => {
    const appCss = fs.readFileSync(APP_CSS, "utf8");
    const tokens = designSystemTokens();
    // 保险：确认真的读到了 tokens，而不是路径错了拿个空集合让一切都「未定义」或一切都放行。
    expect(tokens.has("--space-6")).toBe(true);
    expect(tokens.has("--bg-surface")).toBe(true);
    const defined = new Set([...tokens, ...definedIn(appCss)]);
    const bad = undefinedVarUses(appCss, defined).map((h) => `${h.selector} { … var(${h.name}) … }`);
    expect(bad).toEqual([]);
  });

  it("the guard itself catches the regression it exists for", () => {
    const css = [
      "/* var(--in-a-comment) */",
      ":root { --local: 1px; }",
      ".card { padding: var(--space-5); }",
      ".ok { gap: var(--local); margin: var(--local, 4px); }",
      ".fallback { margin: var(--space-5, 20px); }",
      ".wrong-family { color: var(--color-text-primary, #1a1a1a); }",
      "@media (max-width: 900px) {",
      "  .narrow { padding: var( --space-5 ); }",
      "}",
    ].join("\n");
    const hits = undefinedVarUses(css, new Set(["--local"]));
    expect(hits.map((h) => `${h.selector} ${h.name}`)).toEqual([
      ".card --space-5",
      ".fallback --space-5",
      ".wrong-family --color-text-primary",
      ".narrow --space-5",
    ]);
  });
});

describe("接管层只占内容区（RAY-753）", () => {
  // 接管层曾是 position: fixed; inset: 0; z-index: 100，把外层的预览横幅一起盖住。
  // jsdom 不排版，横幅在场由 src/SidecarDown.test.jsx 证；这里证样式没有盖住窗口的手段。
  it(".sidecar-down 不靠 fixed / absolute / z-index / inset", () => {
    const rule = /\.sidecar-down\s*\{([^}]*)\}/.exec(stripComments(fs.readFileSync(APP_CSS, "utf8")));
    expect(rule).not.toBeNull();
    expect(rule[1]).not.toMatch(/position\s*:\s*(fixed|absolute)/);
    expect(rule[1]).not.toMatch(/z-index/);
    expect(rule[1]).not.toMatch(/inset\s*:/);
  });
});

describe("报告预览的纸张随内容长高（RAY-760）", () => {
  // .preview-pane 是 flex 容器；默认 stretch 会把纸张拉成窗格可视高度，min-height 的一页
  // A4 就成了实际高度，比一页长的报告后半份画在灰底上。jsdom 不排版，这里钉住样式。
  it(".preview-sheet 不被 flex 拉伸", () => {
    const css = stripComments(fs.readFileSync(APP_CSS, "utf8"));
    const sheet = /(?:^|\})\s*\.preview-sheet\s*\{([^}]*)\}/.exec(css);
    expect(sheet).not.toBeNull();
    expect(sheet[1]).toMatch(/align-self\s*:\s*flex-start/);
    expect(sheet[1]).toMatch(/min-height\s*:\s*297mm/);
  });
});
