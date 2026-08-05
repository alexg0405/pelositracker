# UI cleanup prompt — PelosiTracker dashboard (2026-08-04)

Paste everything below the line into a fresh coding-agent session with this repo as the working
directory. It is self-contained: the audit numbers are already measured, so the agent does not
need to rediscover them.

---

## Task

Do a full visual and structural overhaul of the PelosiTracker web dashboard. **Zero functional
change.** Every button, form, filter, poll, and API call keeps working exactly as it does today.
This is a styling and information-architecture job only.

## What you are working on

A single-page vanilla-JS dashboard served as static files by FastAPI. No bundler, no framework,
no build step.

| File | Size | Role |
| --- | --- | --- |
| `app/static/index.html` | 1,058 lines | Entire SPA shell — all four tabs, all panels, markup for every control |
| `app/static/index.css` | 3,046 lines | The entire stylesheet |
| `app/static/index.js` | 5,763 lines | All behavior; renders rows via inline HTML template literals |
| `app/static/watch.{html,css,js}` | small | Secondary chart-popout page |
| `app/static/vendor/chart.umd.min.js` | — | Chart.js, used for price charts |

Served from `app/main.py`. Login is a full-screen overlay (`#login-overlay`) over the dashboard.

## The four problems to solve (stated by the owner, in priority order)

1. **It is all on ONE page.** The default tab ("Polymarket US Research") holds eight full-size
   sections *plus* a six-step form with ~90 inputs. The "Workspace map" nav bar looks like tabs
   but is only anchor links that scroll-jump within one enormous document.
2. **The text is tiny.** Most of the interface is set at 9–11px, much of it ALL-CAPS with added
   letter-spacing — the least legible possible combination.
3. **There is no info affordance.** Instead of a small `ⓘ` you can tap for an explanation, every
   section dumps two to four lines of explanatory prose inline, permanently, above the controls.
4. **It is bloated and requires heavy scrolling.**

## Measured baseline (verify these yourself, then improve them)

Typography — **162 font-size declarations are below 12px** (8px × 4, 9px × 40, 10px × 69,
11px × 49) against only ~18 declarations at 14px or larger. 66 `text-transform: uppercase`
rules. 64 `letter-spacing` declarations. 11 distinct computed font sizes on a single render.

Design system — **286 distinct hard-coded color literals** against only 22 `:root` custom
properties. **38 distinct px values** used for padding/margin/gap, following no scale.
1,020 style rules. 397 distinct class selectors. Only 16 are dead, so this is not a dead-code
problem — it is a *volume and layering* problem.

Layering — the stylesheet is a stack of successive re-skins that never removed the previous one:

| Line | Block |
| --- | --- |
| 1 | base dark theme (`:root`, admits in a comment that earlier passes stacked three conflicting `:root` blocks) |
| 1909 | "Workstation legibility and navigation pass" |
| 2159 | "Workstation visual system: calmer surfaces, clearer hierarchy" |
| 2585 | "Readability and density tuning" |
| **2841** | **"FORTNITE-INSPIRED THEME — battle-royale lobby look layered on top of the base rules"** — a complete second theme: blue→purple gradients, `clip-path` parallelogram buttons, skewed headings, rarity-colored chips, gradient scrollbars |
| 2945 | "Mobile execution cockpit" |
| 3025 | "Phone-first research controls" |

Responsive — 8 distinct breakpoints (380, 460, 560, 680, 720, 760, 820, 1120px), each
re-declared across several of the passes above.

Content density — 49 inline `.field-note` prose blocks, 130 `<input>`, 14 `<select>`,
64 `<button>`, 36 `aria-live` regions, 11 `<details>` disclosures, all in one document.

Decorative cost — a fixed full-viewport scanline overlay (`body:after`), animated `glitch`
pseudo-element text effects on six headings, a `▸` glyph injected before every `.title` via
`::before`, 40 gradient rules, 44 shadow rules.

## Target design language

Clean, white, editorial — the Ramp look. High-contrast near-black text on white, generous
whitespace, hairline borders, one saturated yellow used sparingly as the single accent, almost
no shadows, no gradients, no glow. Confident and quiet. This is a financial tool: numbers should
be the loudest thing on screen, and nothing else should compete with them.

Establish exactly one token block and use it everywhere. Suggested starting values — tune the
yellow to taste, but keep the structure:

```css
:root {
  color-scheme: light;

  /* Surfaces */
  --bg:            #ffffff;
  --surface:       #fafaf8;   /* subtle panel fill */
  --surface-sunk:  #f4f4f1;   /* table stripe, inset wells */
  --border:        #e6e6e1;   /* hairline — the primary separator */
  --border-strong: #d3d3cc;

  /* Text */
  --text:          #1a1a1a;
  --text-secondary:#5c5c5c;
  --text-muted:    #8a8a8a;   /* metadata only, never a value or a label */

  /* Accent — one yellow, dark text on it */
  --accent:        #f5d800;
  --accent-hover:  #e3c800;
  --accent-wash:   #fffbe0;   /* tint for highlighted rows/callouts */
  --on-accent:     #1a1a1a;

  /* Semantic — must pass AA on white; the current cyan/coral will not */
  --positive:      #067647;
  --negative:      #b42318;
  --warning:       #b54708;
  --info:          #344054;

  /* Type scale — 12px is a hard floor */
  --text-xs:   12px;  /* metadata only */
  --text-sm:   13px;
  --text-base: 15px;  /* body default */
  --text-lg:   17px;
  --text-xl:   20px;
  --text-2xl:  24px;
  --text-3xl:  30px;

  /* Spacing — 4px base, no other values allowed */
  --sp-1: 4px;  --sp-2: 8px;  --sp-3: 12px; --sp-4: 16px;
  --sp-6: 24px; --sp-8: 32px; --sp-12: 48px; --sp-16: 64px;

  --radius:      6px;   /* controls */
  --radius-card: 8px;
  --radius-pill: 999px;

  --font-sans: "Inter", system-ui, -apple-system, "Segoe UI", sans-serif;
  --font-mono: ui-monospace, "SF Mono", "Cascadia Mono", Consolas, monospace;
}
```

Typography rules: body copy at `--text-base`. Numeric values in `--font-mono` with
`font-variant-numeric: tabular-nums` so columns align. Uppercase is allowed only for table
column headers and only at `--text-sm` or larger — nowhere else. Nothing below 12px, ever.

Component rules: buttons are plain rectangles with `--radius` — no `clip-path`, no skew, no
gradient. Primary = yellow fill, dark text. Secondary = white fill, hairline border. Danger =
red text on white with a red border, filled red only for genuinely destructive confirmation.
Panels are white with a hairline border and no shadow. Reserve the single allowed elevation
(`0 4px 12px rgba(0,0,0,.08)`) for popovers and modals only.

## Hard constraints

- **Do not change behavior.** No edits to `app/**/*.py`, the engine, or any request/response
  shape. If a visual change appears to require a behavior change, stop and ask.
- **Preserve every hook the JS binds to**: all `id` attributes, `data-*` attributes, form
  field `name`s, `role`s, and `aria-live` regions. `index.js` queries these directly.
- **~203 class names appear inside JS template literals** in `index.js` (e.g. `marketRow`,
  `positionRow`, `eventCard`, `renderTradingJournal` build HTML strings with inline
  `class="..."`). Prefer **keeping the existing class names and replacing only their
  declarations** — that decouples this restyle from a risky 5,700-line JS refactor. Rename a
  class only when you update every JS and HTML reference in the same commit.
- **No build step.** Keep plain `<link>` and `<script>`. Do not introduce Tailwind, Sass, or a
  bundler.
- Bump the `?v=` cache-buster on the `index.css` link when you ship.
- Keep `watch.html` / `watch.css` visually consistent with the new system.
- Restyle Chart.js defaults for a light background (grid, ticks, tooltip, series colors).
- Accessibility is a requirement, not a nice-to-have: keep the skip link, keep focus visible,
  maintain AA contrast, and keep `prefers-reduced-motion` honored.

## Work plan

Work in phases. **Each phase must end with the app running and visually verified in a browser
before you start the next.** Do not attempt the whole thing in one pass.

**Phase 0 — baseline.** Start the server and capture a screenshot of every view in its current
state, so you can diff against them later. Then inventory every class name referenced inside
`index.js` template literals and save the list; you will need it as a safety net.

```bash
cd /d C:\Users\alexa\Downloads\pelositracker-main\pelositracker-main && .venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8765
```

**Phase 1 — one flat stylesheet.** Delete the Fortnite block (≈2841–2944) and the four
successive override passes outright. Rebuild `index.css` as a single layer: token block, reset,
then primitives (panel, title, button, input, select, table, chip, badge, callout, nav). Remove
the scanline `body:after`, the `glitch` effects, the `.title::before` `▸` glyph, the heading
skew, every surface gradient, and the gradient scrollbars. **Get one screen right first** — use
the "Trade performance datasheet" section as the reference implementation, confirm it, then
apply the system outward.

**Phase 2 — the `ⓘ` affordance.** Build one reusable info component and migrate all 49
`.field-note` blocks behind it. The prose is compliance-relevant, so *keep the text* — just move
it out of the default view. Requirements: a small icon button; opens a popover on click and on
keyboard `Enter`/`Space`; `Esc` closes and returns focus; correct `aria-expanded` and
`aria-describedby`; works on touch; one controller handles every instance. Rule after migration:
**at most one line of helper text visible by default per section.** Long safety notices collapse
into a single persistent callout per view, or into the relevant control's `ⓘ`.

**Phase 3 — real navigation.** This is the fix for problem 1. Convert the eight anchor sections
of the research tab into **real views, one visible at a time**: Status, Performance, Trade data,
Advisor, Auto trader, Positions & log, Model lab, US markets. Replace the fake "Workspace map"
anchor bar with actual view switching, driven by hash routing so existing deep links and the
back button both keep working. Give the six-step policy form (~90 inputs) its own dedicated
view: one step per screen, with a sticky footer holding save/step controls instead of a
5,000-pixel scroll.

**Phase 4 — density.** Convert repeated-record card stacks to real tables: the execution
journal, advisor session history, managed positions, venue positions. Collapse the three
identical Dry run / Live / Combined tally cards into one segmented table. Eliminate nested
scroll containers — the journal panel currently has its own inner scrollbar inside the page
scroll, which is disorienting. Ensure no view exceeds roughly three viewport heights at
1280×800.

**Phase 5 — responsive and a11y.** Collapse 8 breakpoints to at most 3 (suggest 640 / 900 /
1280). Run a contrast pass over every semantic color on white — the existing cyan-on-dark
"good" state will fail and must be re-derived. Verify keyboard traversal of the new nav and
popovers.

**Phase 6 — verify.** Check yourself against the acceptance criteria below and report each one
with the actual measured number. Screenshot every view and compare against the Phase 0 baseline
to confirm nothing went missing.

## Acceptance criteria

| # | Criterion | Baseline |
| --- | --- | --- |
| 1 | Zero `font-size` (or `font:` shorthand) declarations below 12px | 162 |
| 2 | ≤8 distinct font sizes, every one from a token | 11 computed / 25 declared |
| 3 | ≤10 distinct spacing values, every one from a token | 38 |
| 4 | ≤24 color literals outside the `:root` block | 286 |
| 5 | ≤2 `text-transform: uppercase` rules, none under 13px | 66 |
| 6 | Zero surface gradients, zero `clip-path` on controls, no glitch/scanline | 40 gradients |
| 7 | `index.css` under 1,200 lines | 3,046 |
| 8 | ≤3 breakpoints | 8 |
| 9 | No view taller than ~3 viewport heights at 1280×800 | one ~20-screen view |
| 10 | Every previously-visible `.field-note` reachable via `ⓘ`, none shown by default | 49 inline |
| 11 | No AA contrast failures | unmeasured |
| 12 | Every control still works; no console errors; no failed requests | — |

## Do not

- Do not delete any explanatory copy. Move it behind `ⓘ`.
- Do not remove or rename any control, filter, or form field.
- Do not add a CSS framework, a bundler, or a JS framework.
- Do not "improve" trading logic, thresholds, defaults, or copy accuracy while you are in here.
- Do not leave a second theme layered on top of the first. That is the mistake that produced
  the current state — if a rule needs to change, change it in place.

## Open decision

Light-only is assumed, matching the stated preference. If a dark variant is wanted later, it
should be a second token block toggled by `[data-theme]` on `<html>` — never a second
stylesheet layered on top of the first.
