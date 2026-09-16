// RAY-493 preview-rc2-fixes：终端必须在没有外网的机构里照常开屏。
//
// 打包后的 renderer CSS 第一行曾是 `@import "https://fonts.googleapis.com/…"`（来自
// design-system 的 tokens/fonts.css）。渲染阻塞样式表回来之前，入口脚本不执行 ——
// 装机 app 冷启动白屏实测 1.1–56.6 s，断网时可能一直白下去。
//
// 这里守两件事：
// 1. packages/ 与 apps/terminal/renderer 的源码里不出现任何 http(s) 的样式 / 字体引用
//    （CSS 的 @import 与 url()、HTML/JSX 的 <link href>、以及 Google Fonts 域名本身）；
// 2. fonts.css 引入的每个字体包 CSS 都能在本地解析到，其中 url() 全是相对路径且文件真的存在
//    —— 否则 Vite 会把它原样留成一个运行时请求。
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.resolve(HERE, "../../../..");
const ROOTS = [path.join(REPO, "packages"), path.join(REPO, "apps/terminal/renderer")];
const SKIP_DIRS = new Set(["node_modules", "dist", ".vite", "coverage"]);
const SCANNED = /\.(css|html|js|jsx|mjs|cjs)$/;
const SELF = fileURLToPath(import.meta.url);

function walk(dir, out = []) {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    if (SKIP_DIRS.has(entry.name)) continue;
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) walk(full, out);
    else if (SCANNED.test(entry.name) && full !== SELF) out.push(full);
  }
  return out;
}

// 拆开写，免得本文件自己命中。
const REMOTE = "https?:" + "//";
const RULES = [
  { name: "CSS @import of a remote URL", re: new RegExp(`@import\\s+(url\\(\\s*)?["']?${REMOTE}`, "i") },
  { name: "CSS url() pointing at a remote URL", re: new RegExp(`url\\(\\s*["']?${REMOTE}`, "i") },
  { name: "<link href> to a remote stylesheet/font", re: new RegExp(`<link[^>]+href=\\{?["'\`]${REMOTE}`, "i") },
  { name: "Google Fonts host", re: /fonts\.(googleapis|gstatic)\.com/i },
];

function findRemoteAssetRefs(files) {
  const hits = [];
  for (const file of files) {
    const text = fs.readFileSync(file, "utf8");
    text.split("\n").forEach((line, i) => {
      for (const rule of RULES) {
        if (rule.re.test(line)) hits.push(`${path.relative(REPO, file)}:${i + 1} ${rule.name}: ${line.trim()}`);
      }
    });
  }
  return hits;
}

describe("renderer boots without the network", () => {
  it("has no remote style or font references in packages/ or the renderer", () => {
    const files = ROOTS.flatMap((root) => walk(root));
    // 保险：确认真的扫到了该扫的文件，而不是路径错了扫了个空。
    expect(files.some((f) => f.endsWith(path.join("design-system", "tokens", "fonts.css")))).toBe(true);
    expect(files.some((f) => f.endsWith(path.join("report-template", "report.css")))).toBe(true);
    expect(findRemoteAssetRefs(files)).toEqual([]);
  });

  it("the guard itself catches the regression it exists for", () => {
    const tmp = fs.mkdtempSync(path.join(fs.realpathSync(process.env.TMPDIR || "/tmp"), "no-remote-"));
    const bad = path.join(tmp, "fonts.css");
    fs.writeFileSync(
      bad,
      `@import url("${"https"}://fonts.${"googleapis"}.com/css2?family=Inter&display=swap");\n` +
        `@font-face { src: url('${"http"}://example.test/a.woff2'); }\n`,
    );
    try {
      expect(findRemoteAssetRefs([bad]).length).toBeGreaterThanOrEqual(2);
    } finally {
      fs.rmSync(tmp, { recursive: true, force: true });
    }
  });

  it("every font CSS that fonts.css imports resolves locally with bundled, relative url()s", () => {
    const fontsCss = path.join(REPO, "packages/design-system/tokens/fonts.css");
    const imports = [...fs.readFileSync(fontsCss, "utf8").matchAll(/@import\s+["']([^"']+)["']/g)].map((m) => m[1]);
    expect(imports.length).toBeGreaterThan(0);
    for (const spec of imports) {
      const cssPath = path.join(REPO, "packages/design-system/node_modules", spec);
      expect(fs.existsSync(cssPath), `${spec} must resolve from packages/design-system`).toBe(true);
      const css = fs.readFileSync(cssPath, "utf8");
      const urls = [...css.matchAll(/url\(\s*["']?([^"')]+)["']?\s*\)/g)].map((m) => m[1]);
      expect(urls.length).toBeGreaterThan(0);
      for (const url of urls) {
        expect(url, `${spec} references ${url}`).toMatch(/^\.\//);
        expect(fs.existsSync(path.resolve(path.dirname(cssPath), url)), `${spec}: ${url} missing`).toBe(true);
      }
      expect(css).toMatch(/font-display:\s*swap/);
    }
  });
});
