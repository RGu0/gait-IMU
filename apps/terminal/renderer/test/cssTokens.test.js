// RAY-543 css-token-guard：app.css 里不带回退的 var(--x)，--x 必须真的有定义。
//
// 设计系统是严格 8px 体系，没有 --space-5（20px）。三类卡片曾写 `padding: var(--space-5);`
// —— 未定义且无回退的 var() 在计算值阶段无效，padding 静悄悄地回落成 0，内容贴着边框，
// 而 lint、测试、构建全都是绿的。
//
// 这里守：app.css 中每个不带回退的 var(--x)，--x 要么在设计系统 styles.css 引入的 tokens 里、
// 要么在 app.css 自己的规则里有定义。带回退的 var(--x, …) 不管：它有明确的兜底值。
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

// 返回 { selector, name }：不带回退、且 defined 里没有的用法。
function undefinedVarUses(css, defined) {
  const clean = stripComments(css);
  const hits = [];
  for (const m of clean.matchAll(/var\(\s*(--[\w-]+)\s*\)/g)) {
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
  it("every var() without a fallback names a token that is defined", () => {
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
      ".ok { margin: var(--space-5, 20px); gap: var(--local); }",
      "@media (max-width: 900px) {",
      "  .narrow { padding: var( --space-5 ); }",
      "}",
    ].join("\n");
    const hits = undefinedVarUses(css, new Set(["--local"]));
    expect(hits.map((h) => `${h.selector} ${h.name}`)).toEqual([".card --space-5", ".narrow --space-5"]);
  });
});
