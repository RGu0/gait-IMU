// RAY-542 side-identity-screens — source-level guards for left/right identity.
//
// Before RAY-542 the renderer drew the right foot in the data cyan (#17A2C4) and
// the left in the brand blue (#2569BC), while the operator was holding a blue
// and an ORANGE module; no screen showed any orange. Side colour now goes
// through --side-* / --viz-gait-* / SideBadge only. These guards read source
// files, so they live here rather than under src/ (the renderer lint rule
// forbids filesystem access in src/).
//
// The rendering half — ModuleFigure on the three hands-on screens, P-07 row
// structure — is in src/SideIdentity.test.jsx.
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const SRC = path.join(HERE, "..", "src");

function sources(dir) {
  return fs.readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) return sources(full);
    if (!/\.(jsx?|css)$/.test(entry.name) || /\.test\./.test(entry.name)) return [];
    return [{ rel: path.relative(SRC, full), text: fs.readFileSync(full, "utf8") }];
  });
}

describe("the renderer no longer paints a side in the pre-RAY-542 colours", () => {
  it("has no hard-coded old side colours anywhere in the renderer source", () => {
    const hits = sources(SRC).filter(({ text }) => /#2569BC|#17A2C4/i.test(text)).map(({ rel }) => rel);
    expect(hits).toEqual([]);
  });

  it("never uses the brand blue or the data cyan to mean a foot", () => {
    // The shape of the old mistake: a side element filled with --brand-primary
    // or --accent-cyan. Side elements take --side-* instead.
    const offenders = sources(SRC).filter(({ text }) =>
      // Anchored on side markers (--side-*, side-*, side=, side ===) so "aside",
      // "inside" or ".sidebar" do not trip it.
      /(--side-|\bside-|\bside=|\bside ===)[^\n]{0,80}var\(--(accent-cyan|brand-primary)\)|var\(--(accent-cyan|brand-primary)\)[^\n]{0,80}(--side-|\bside-|\bside=|\bside ===)/.test(text),
    );
    expect(offenders.map(({ rel }) => rel)).toEqual([]);
  });
});

describe("P-07 keeps each side on one row", () => {
  // The rows used to sit in P-06's two-cell grid (72px + 1fr), and 「← 蓝色模块」
  // wrapped onto a narrow second line. Each row is now its own nowrap flex line.
  const css = fs.readFileSync(path.join(SRC, "app.css"), "utf8");

  it("lays the side rows out as single nowrap lines", () => {
    const rule = /\.side-rows li\s*\{([^}]*)\}/.exec(css)?.[1] ?? "";
    expect(rule).toMatch(/display:\s*flex/);
    expect(rule).toMatch(/white-space:\s*nowrap/);
  });

  it("spaces the P-06 / P-07 text column with a token that exists", () => {
    // --space-5 is not defined (the 8px system has no 20px step); used here it
    // collapsed the gap to nothing and the checkbox sat flush on the rows.
    const rule = /\.two-column__text\s*\{([^}]*)\}/.exec(css)?.[1] ?? "";
    expect(rule).not.toMatch(/--space-5\b/);
    expect(rule).toMatch(/gap:\s*var\(--space-(1|2|3|4|6|8|12|16)\)/);
  });
});
