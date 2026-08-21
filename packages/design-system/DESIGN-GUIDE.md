# Steady Health — Design System (稳步 Steady)

**"Clinical tech": the rigor of medical-device HMI + the restraint of Linear/Vercel.**

This is the design system for **天富智柔 TechFlex**'s flagship product **FeetForcePlate (足底压力健康筛查与分析平台)** — a *plantar-pressure health-screening and analysis platform*. **稳步 Steady** is the internal name of the design language itself. It now serves **two** products on one cloud platform and one subject-record system: FeetForcePlate, and the **dual-ankle IMU gait screening terminal** (`components/gait/`). The same operators move between both in a single shift, so the two share tokens, primitives and copy register rather than forking. FeetForcePlate is an institutional **desktop** tool (Windows-first, macOS for dev/select clients) operated by a technician who guides a subject onto a DO-P4864 pressure mat, runs a one-button linear screening flow, and produces a printable A4 report; raw pressure data streams to a cloud platform for a fuller analysis. It is a **screening / risk-flagging / analysis tool, not an automatic-diagnosis system**, and not a consumer app.

The whole system is **light-theme only** (v1): near-white backgrounds, one bright medical blue, restrained semantic colors. Expressiveness is concentrated in the **data visualization** (transparency layering, single-hue gradients, heat glow) on a light data canvas — reminiscent of Apple Health data cards. The mantra: **界面稳,图表锐 — the interface is calm, the charts are sharp.**

Four style pillars: **稳 Trustworthy · 简 One-thing-at-a-time · 明 Legible-at-a-glance · 锐 Tech-forward.**

---

## Sources

Built from the read-only mounted codebase **`HealthDesignSystem/`** plus user-provided brand + product files:
- `HealthDesignSystem/README.md` — the authoritative style spec (colors, type, spacing, 9 component specs, 6 layout patterns, A4 report, a11y, a prohibitions list, and copy tone). Version 1.1.0, 2026-07-20.
- `HealthDesignSystem/tokens/tokens.css` — the complete CSS custom-property set (transcribed and split by concern into `tokens/` here).
- `HealthDesignSystem/CLAUDE.md`, `CHANGELOG.md` — working instructions and revision history.
- **`产品需求文档_PRD.md`** — the FeetForcePlate PRD v1.0 (2026-07-20): page specs P-01–P-11, functional requirements, interaction rules. UI-kit screens follow it directly.
- **Brand logos** (`天富智柔LOGO横版/竖版.png`) → `assets/logo-horizontal.png`, `assets/logo-vertical.png`.

The logo mark is a **cyan rounded medical-cross** with a white puzzle/connector motif, paired with the 「天富智柔 / TechFlex」 wordmark set in black — designed for light backgrounds (consistent with the light-only theme).

---

## Content Fundamentals — how Steady writes copy

The product speaks **Simplified Chinese**, in two distinct registers depending on audience.

- **To the operator/technician** — short imperative verb phrases, terse and task-focused. Buttons are verbs: 「开始新的检测」(Start a new screening), 「同意并继续」(Agree & continue), 「重新检查」(Re-check), 「输入机构档案号」(Enter the institution file number). No pleasantries, no marketing.
- **To the subject (被检者)** — gentle, calm instructions designed to be read from ~2 m away: 「双脚自然站立,保持身体放松」(Stand naturally, relax your body), 「请站到压力垫中央」(Please stand at the center of the mat). Larger type, higher contrast.
- **Errors** — never scary. Always **one phenomenon + one action + an error code**: 「设备连接中断(E-2103)。请检查连接线后点击重新检查。」(Device connection lost (E-2103). Check the cable, then tap Re-check.) Calm, executable language — never blame the user, never dramatize a permission denial.
- **Never disguise failure as success.** 「检测完成」(Screening complete) appears *only* after quality gating passes. Missing optional fields read 「未提供」(Not provided), never 「正常」(Normal).
- **Report wording is non-diagnostic** by rule: only 「建议关注 / 建议复测 / 建议进一步评估」(suggest monitoring / suggest retest / suggest further evaluation) — never diagnostic phrasing.

**Casing / register**: Chinese, so no letter-casing; tone is set by sentence length and mood. Numerals and units are Latin (`cm`, `kg`, error codes like `E-2103`, masked IDs like `**2781` / `临时034`). **No emoji, ever.** Status is always conveyed as **icon + text + color together** — never text alone, never color alone. The overall vibe: quiet, ordered, confident. Trust comes from restraint.

---

## Visual Foundations

**Color.** One brand hue — **Pulse Blue**: `#2569BC` for text/buttons/focus (AA, 5.5:1 on white), a brighter `#3B8BEF` reserved for *large graphics only* (chart lines, progress, current step), and `#EFF5FC` subtle fill for selected/highlight regions. Neutrals are a **Slate** ramp (`#F8FAFC` page → `#FFFFFF` surface → `#0F172A` text). Semantic colors sit at a "modern SaaS" level — brighter than gray corporate palettes, calmer than alarm colors, all WCAG AA; danger red (`#C23B3B`) is deliberately slightly darkened so it doesn't alarm subjects. **Max ~1 brand hue + slate + 4 semantics across the whole UI.** Info/processing deliberately shares the blue family with the brand (medical-software convention); shape (filled button vs. subtle pill + spinner) disambiguates.

**The heatmap scale is physically isolated.** The 5-stop pressure scale (`#2D4FA8 → #1F9FCE → #63C685 → #F0C24A → #E25539`, cobalt→cyan→mint→amber→coral) appears **only** inside a bordered data canvas with a "压力/pressure" legend — never as UI decoration. This prevents reading pressure-red as a system error.

**Type.** UI is **Noto Sans SC** (substituting Source Han Sans SC); numerals are **Inter** with `tabular-nums` so countdowns and metrics don't jump. Scale: display 32/40, h1 28/36, h2 24/32, h3 20/28, body-lg 18/28, body 16/24, secondary 14/22, countdown 96. **Screen minimum is 14px** (12px only in the print report footer). Weights 400/600.

**Spacing** is a strict 8px system: `4 / 8 / 12 / 16 / 24 / 32 / 48 / 64`. Card padding 24, page margin 32, block spacing 32–48. Reading content maxes at 960–1120px centered; detection/heatmap views may go full-width.

**Radii**: controls & buttons `8`, cards `12`, status pills `999`. Never 0 (no hard right angles), never >16 (no big playful rounding).

**Shadows** are extremely light — cards `0 1px 3px rgb(15 23 42 / 0.06)`, dialogs `0 8px 24px rgb(15 23 42 / 0.12)`. Hierarchy is built from **whitespace + borders**, not stacked shadows. Cards = white surface + `1px #E2E8F0` border + 12px radius + the whisper-light shadow. No glassmorphism, no neumorphism.

**Backgrounds** are flat near-white (`#F8FAFC` page, `#FFFFFF` surface). **No** hero images, no repeating patterns, no textures, no decorative gradients. The *only* gradients/glows allowed are inside charts and the data canvas (§ viz rules): area fills fade `24% → 0%`, multi-series overlays at 12–16%, current-point glow `0 0 12px` at 30%. Regular controls stay **solid and flat**.

**Motion.** 150–200ms `ease-out`, only for state transitions and page changes. No flashing >3 Hz. Countdown ticks once per second as a number swap + text — never a fast animation. Loading rule: <300ms shows nothing; 300ms–2s a local spinner; >2s names the task + shows a persistent state; **never a fake percentage**, never a full-page overlay spinner.

**Hover / press.** Buttons darken on hover (primary → `--brand-primary-hover #1D549A`). Table rows / selectable surfaces use the brand alpha layers (`0.06 / 0.10 / 0.14`) for hover / selected / drag. Loading buttons keep brand color (not gray) and **lock their width**. There is no shrink-on-press; press is conveyed by the hover-darkened color.

**Focus** is always visible: `2px solid #2569BC` ring + 2px offset, never hidden.

**Transparency & blur** are confined to data viz (see above). No frosted-glass panels, no backdrop blur in chrome.

**Imagery color vibe**: there is no photographic imagery. The only "imagery" is the data canvas — cool light blue (`#F6FAFD`), a blue-tinted grid, and the isolated warm→cool heat scale. No grain, no B&W treatment.

---

## Iconography

The spec calls out status glyphs but ships **no icon font, no SVG sprite, and no icon assets** in the source. Icons are described functionally: 24px status icons (spinner / green check / red cross / warning triangle), a slowly-rotating processing glyph, a status-pill leading dot.

**Approach used in this system:** status/UI glyphs are drawn as **inline SVG paths inside the components** (no icon-set dependency, no CDN) — a clean, single-weight ~2px-stroke line style matching the "clinical tech" restraint. Keeping them inline means they inherit `currentColor` and need no network fetch. Consistent glyph set:
- ready/done → check / check-in-circle
- processing/info → spinner arc (slow spin) / info circle
- warning → triangle-alert
- danger/fail → x / x-circle
- StatusPill default leading mark → a simple filled dot (matches the spec's `●`)

If a broader glyph set is later needed (nav chevrons, printer, download, footprints for stance guidance), match this stroke weight/style — [Lucide](https://lucide.dev) is the closest ready-made set. Rules: icons **never** replace text in a status (icon + text + color together, always); no emoji; no Unicode-glyph-as-icon; danger red only in blocking/fail contexts.

---

## Components

React primitives under `components/<group>/`. Each is `<Name>.jsx` + `<Name>.d.ts` + `<Name>.prompt.md`, with one `@dsCard` HTML per directory. They reference tokens via CSS custom properties only.

- **forms/** — `Button` (primary/secondary/danger/ghost, sizes, loading, one-primary-per-screen rule), `Field` (label-always-visible input, unit suffix, error text), `ChipGroup` (multi-select tag chips).
- **feedback/** — `StatusPill` (dot/icon + text, 4 semantics), `Banner` (non-blocking top-of-page notice), `Toast` (transient success/info only), `Dialog` (blocking decisions only, ≤2 buttons).
- **flow/** — `StepBar` (linear wizard stepper), `ChecklistItem` (pre-check row: status icon + name + hint/action).
- **data/** — `DataTable` (screening records; 56px rows, StatusPill status column, masked IDs).
- **gait/** — dual-ankle IMU primitives: `SideBadge` (left/right in three channels at once), `LinkStatus` (arrival-rate tiers), `BatteryPair` (charge; pre-capture only), `MetricTile` (quality grades, never blank), `CountdownFocus` (subject-facing countdown), `RhythmStrip` (operator-side cadence, never a metronome).

Every family except `gait/` maps 1:1 to a README §5 spec. The `gait/` family is an **intentional extension** for the second product — see below.

### The gait extension — three rules that are not negotiable

1. **Left/right is never color alone.** Character + shape + color always, and stroke style as a fourth channel in charts. Wearing the modules on the wrong ankles cannot be compensated for by the algorithm, and the client report is a grayscale A4 print.
2. **The pressure heat scale is disabled here.** The gait product has no pressure dimension; `--viz-heat-*` would read as a pressure map. Its expressiveness lives in the gait data canvas (`--viz-gait-*`) instead.
3. **A metric is never blank.** Three grades — `normal`, `low` (presented differently, never withheld), `uncomputable` (「本次不适用」 + a plain-language reason). A blank, a `0` or an `N/A` reads as a measurement rather than an absence.

`--accent-cyan` was previously viz-only; its use is **widened** to the right-foot identity (`--side-right`). That is the one token whose role changed.

## UI Kits

- **ui_kits/feetforceplate/** — the desktop screening product. Screens: **Hub** (P-01), **Wizard/intake** (P-02–06), **Focus/detection** with the light heatmap canvas (P-07), **Result** (P-08). Composes the component primitives above.
- **gait terminal** — the 28 review artboards (P-00 login through P-10 report preview) live outside this project for now; only the `gait/` primitives and tokens are here. The UI kit lands when the terminal front-end does.

---

## Index / manifest (root)

- `styles.css` — **entry point**; `@import`s the token files below. Consumers link only this.
- `tokens/` — `fonts.css`, `colors.css`, `typography.css`, `spacing.css`, `effects.css`, `viz.css`.
- `components/{forms,feedback,flow,data,gait}/` — primitives + `.d.ts` + `.prompt.md` + card HTML.
- `ui_kits/feetforceplate/` — `index.html` + screen JSX + `README.md`.
- `guidelines/` — foundation specimen cards (Type / Colors / Spacing / Brand groups).
- `assets/` — `logo-horizontal.png`, `logo-vertical.png` (天富智柔 TechFlex brand lockups). No photographic imagery — the brand uses none.
- `thumbnail.html` — homepage tile.
- `SKILL.md` — Agent-Skills-compatible entry.
- `readme.md` — this file.

---

## Intentional additions

None to the component inventory — every component maps to a README §5 spec. The only additions to the *source* are: Google-Fonts substitutes for the two unshipped typefaces, and inline-SVG icons in the components (the spec describes icons but ships none).

## Caveats

- **Fonts substituted**: Source Han Sans SC → Noto Sans SC (open-source release of the same family); Inter unchanged. Swap for licensed binaries in production.
- **Icons**: hand-authored inline SVG in the components (the spec ships none). Swap for a licensed set if one exists.
- **PRD now provided**: UI-kit screens follow the FeetForcePlate PRD (P-01/P-02–06/P-07/P-08) and README §6 layout patterns. The kit collapses the multi-page intake (P-02 subject lookup → P-03 optional profile → P-04 consent → P-05 pre-check → P-06 stance) into one `StepBar` wizard; if you want each PRD page as its own discrete screen, say so.
- **No imagery/illustrations**: consistent with the brand — the system uses none beyond the logo.
