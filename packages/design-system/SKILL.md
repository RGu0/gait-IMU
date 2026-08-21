---
name: steady-health-design
description: Use this skill to generate well-branded interfaces and assets for 天富智柔 TechFlex — maker of the FeetForcePlate (足底压力健康筛查) plantar-pressure screening desktop platform and the dual-ankle IMU gait-screening terminal (可穿戴步态健康筛查); "稳步 Steady" is the design-language name — for production or throwaway prototypes/mocks/etc. Contains essential design guidelines, colors, type, fonts, logos, and UI kit components for prototyping.
user-invocable: true
---

Read the `readme.md` file within this skill, and explore the other available files.

If creating visual artifacts (slides, mocks, throwaway prototypes, etc), copy assets out and create static HTML files for the user to view. If working on production code, you can copy assets and read the rules here to become an expert in designing with this brand.

If the user invokes this skill without any other guidance, ask them what they want to build or design, ask some questions, and act as an expert designer who outputs HTML artifacts _or_ production code, depending on the need.

**Two products share this system.** FeetForcePlate (plantar pressure, a mat) and the dual-ankle IMU gait terminal (a 4 m walkway) run on one cloud platform, one subject-record system, and are operated by the same technicians in a single shift. Tokens, primitives and copy register are shared deliberately — do not fork them. What differs is which component family and which viz palette applies; see below.

## Map of this design system

- `readme.md` — the design guide: context, content fundamentals, visual foundations, iconography, component & UI-kit index, caveats.
- `styles.css` — the single entry point; `@import`s all tokens. Link this one file.
- `tokens/` — `colors.css`, `typography.css`, `spacing.css`, `effects.css`, `viz.css`, `fonts.css`.
- `components/{forms,feedback,flow,data}/` — shared React primitives (`.jsx` + `.d.ts` + `.prompt.md`). Read the `.prompt.md` for each component's usage.
- `components/gait/` — **dual-ankle IMU only**: `SideBadge` (left/right identity), `LinkStatus` (arrival-rate tiers), `BatteryPair` (charge, pre-capture only), `MetricTile` (quality grades), `CountdownFocus` (subject-facing countdown), `RhythmStrip` (operator-side cadence). Each `.prompt.md` carries the rule the component exists to enforce — read it before using the component, not after.
- `ui_kits/feetforceplate/` — the screening app recreation (Hub → wizard → scan → result).
- `guidelines/` — foundation specimen cards.

## Non-negotiable rules (full detail in readme.md §9)

- Light theme only; the data canvas is also light (`--viz-canvas`), never dark-reversed.
- One high-emphasis primary button per screen; primary ≥ 48px tall; touch targets ≥ 44px.
- Status = icon + text + color together; red only for block/fail/danger-stop.
- Transparency gradients & glow only inside charts/data canvas; regular controls stay solid & flat.
- Heatmap scale (`--viz-heat-*`) is never used for UI decoration.
- Body ≥ 16px, secondary ≥ 14px; labels always visible (never placeholder-as-label).
- Calm, executable copy; non-diagnostic wording; never disguise failure as success. No emoji.

### Additionally, on the gait product

- **Left/right is never color alone.** Character (左/右) + shape (left = rounded square, right = circle) + color, always; charts add stroke style (left solid, right dashed) as a fourth channel. Wearing the modules on the wrong ankles cannot be compensated for by the algorithm, and the client report is a grayscale A4 print.
- **`--viz-heat-*` is disabled outright here.** The gait product has no pressure dimension, so the heat scale would read as a pressure map. Its data canvas uses `--viz-gait-*` instead.
- **A metric is never blank.** Three grades: `normal`, `low` (presented differently, never withheld), `uncomputable` (「本次不适用」 + a plain-language reason). A blank, a `0` or an `N/A` reads as a measurement rather than an absence.
- **Nothing on a capture screen may pace the subject.** No fixed-cadence animation, no metronome; the cadence strip belongs in the operator column, drawn at real footfall times.
- **During capture, battery is not shown at all** — at 200 Hz the module registers are unreadable, so arrival rate (`LinkStatus`) is the only honest signal. Remove the battery element rather than greying it or leaving a stale value.
