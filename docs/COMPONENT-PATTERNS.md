# Component Patterns — the reusable canon

> Owner directive (2026-10-14): the artifact-components build (D58–D62) is the
> reference standard for future narrator components. This document is that
> standard written down — copy it deliberately, not by archaeology. The living
> exemplars: `Phone.svelte` (device chrome), `PostIt.svelte` (small artifact),
> `Custom.svelte` (raw-markup + sanitize seam), `customSanitize.ts`,
> `avatars.ts`, `chrome.css` (reveal contract), `dsl/custom_policy.py`.

## 1. What a narrator component IS

A component is a **typed container the model fills**, not a layout the model
designs. The design decision (mode, theme, arrangement) is made ONCE, in the
component, front-loaded and documented. The narrator supplies words, a mode,
and a few small choices. That is what keeps a phone reading as a phone and not
as "a list with a chat label" — and what keeps per-turn tokens cheap: a typed
DSL call is 1/3–1/10 the tokens of equivalent hand-built HTML.

## 2. The file contract (every `.svelte` under `lib/openui/components/`)

1. **Header comment block first**, Interface.svelte-style: flavor identity,
   the exact OpenUI Lang call, front-loaded design rationale (WHY this shape),
   colour policy with UI-LANGUAGE §2/§4 citations, motion budget, degradation
   contract. A future author reads the header and knows the component's mind.
2. **Local prop types mirror the schema exactly** (names, order, types) until
   integration swaps them for the `schemas/flavors` imports. Order = DSL
   positional order; `options` is always the uniform trailing object.
   `glyph`/`art` ride `OptionsSchema`; if a component renders neither, they are
   accepted and IGNORED (never invented anchors — ObjectCard/Clash precedent).
   Component-specific options EXTEND `OptionsSchema` (`PhoneOptionsSchema`).
3. **Normalize before markup**: build a `$derived` render-model (e.g. Phone's
   `Row[]`) so the template contains presence checks only, no logic.
4. **Degrade totally and silently**: every read `?? ''` / `.filter(Boolean)`;
   missing field ⇒ that affordance simply does not render. A component NEVER
   fails the pane. Free-string enums fall back to the primary mode (unknown
   `mode` → first mode — the Interface convention).
5. **Renderer clamps alongside gate caps** (`slice(0, n)`), so a hand-edited
   log can never widen a component past its design.
6. **`data-openui="Name"` on the root** (+ `data-mode`/`data-theme`/
   `data-count` where they vary) — this is the stable test seam; Playwright
   asserts against attributes, never against svelte classes or layout.

## 3. Colour: tokens first, an ENUMERATED exception list

Tone tokens + the neutral scale everywhere (`--tone-1..-3`, `-soft`, `-line`,
`--bg-panel`, `--text`, `--line`). Structural differentiation, never
per-entity hue (sender labels alternate `--tone-2`/`--tone-3` by INDEX). An
artifact-inherent literal (CRT phosphor `hsl(130 90% 60%)`, BSOD
`hsl(222 62% 38%)`, sticky yellow `hsl(46 80% 70%)`) must: (a) be one of the
approved list in the plan/decision record, (b) be written ONCE as a custom
property or `color-mix()` input, (c) still derive from theme vars where
possible (`calc(70% + var(--bg-warmth, 0%))`). Likeness is structural, never
1:1: reproduce the grammar of the real object, never its brand pixels or
verbatim copy.

## 4. Motion: the shared reveal contract (D61)

CSS only. Per-item stagger:
`animation-delay: calc(var(--i) * min(var(--reveal-stagger, 250ms), 3000ms / var(--n)))`
with `--n` set inline from item count; ALL content fully readable ≤3.5 s.
Vars live once in `chrome.css` on `.ui-frame-diegetic`; components consume
them WITH literal fallbacks. Keyframes use `both` fill so the settled state
equals the static base transform. `prefers-reduced-motion: reduce` disables
at the SAME specificity as the animating rule (a bare selector silently loses
to a scoped one — live lesson from Monitor). No click-driven reveals, no
random seeds — replay/fork must render identically. Static `box-shadow` (kit
rule); infinite ambient loops allowed only in the `iface-signal` precedent
class, off under reduced-motion.

## 5. Avatars: the store, directly

`avatars.ts` exports `avatarIndex` (store), `initialsFor`, `hydrateAvatars`.
In markup: `$avatarIndex.get(name) ?? initialsFor(name)` — read the STORE
directly. NEVER wrap it in `get()` inside a `$derived` (does not track,
AGENTS.md); that is why no `avatarFor` helper exists. Non-cast names
(inventions, "Unknown Number") get initials by design. Hydration is
idempotent, additive, quiet; the module owns and revokes its object URLs.

## 6. Raw markup (Custom-class components): one sanitize seam

If a component accepts author-controlled markup, ALL sanitizing lives in ONE
module mirroring `markdown.ts` (`customSanitize.ts`): one config object, one
comment noting that relaxing safety is a one-line change there. DOMPurify is
browser-only — the SSR path is a pure escape-only branch swapped in by a
client-only `$effect` (no hydration mismatch), rendering the escaped
paper-scrap. Shadow DOM is the isolation contract: app CSS styles only the
host; inherited custom properties are the only channel crossing IN.
Inline `style="…url(…)"` attributes ride through DOMPurify and must be
scrubbed with the same rule as the css channel. The ENGINE gate
(`dsl/custom_policy.py`) is the first line (the log stays clean); the browser
seam is defense-in-depth and must not assume the gate ran.

## 7. Fixtures and the dev harness

Every component ships a `fixtures/<name>.openui` with REALISTIC RP content
and a max-shape case. The per-lane harness routes that used to render them
through the real parse path with `data-case` anchors
(`routes/dev-artifacts/<lane>/+page.svelte`, local non-canonical schemas) were
retired once every component landed in `library.ts` / `flavors.ts`; the
fixtures remain the screenshot/audit input for `scripts/render_shots.py`. The
gallery (`routes/components`) is the post-integration wall of everything — a
DEV-ONLY route, left out of the production build by `DEV_ONLY_ROUTES` in
`frontend/vite.config.ts`.

## 8. Verification playbook (hard-won)

- `cd frontend && npm run check && npm run build` — 0 errors before anything.
- Dev server: `VITE_MOCK=1 npm run dev -- --port <N> --host 127.0.0.1` — the
  `--host` flag is REQUIRED (vite binds `localhost`/`::1` otherwise and
  127.0.0.1 probes get ECONNREFUSED).
- WARM the route (curl or initial goto + retry) before timing-sensitive
  waits — first-hit on-demand compile exceeds naive timeouts; a "missing
  component" on a cold server is usually compile latency, not a defect.
- Playwright: `uv run --with playwright python <script>`, chromium at
  `~/.cache/ms-playwright/chromium-1234/chrome-linux64/chrome`, `--no-sandbox`.
  Assert via `data-openui` anchors + computed styles; emulate
  `reducedMotion: 'reduce'` and assert `animation-name: none`.
- Kill your dev server with `fuser -k <port>/tcp` — NEVER `pkill -f`
  (self-match trap).
- Backend: `uv run pytest -q`; hermetic spec injection via the
  `FABULIST_DSL_SPEC` env var (never regen artifacts for a test).

## 9. Regeneration pipeline (integration-time)

`npm run gen:flavors` → `node scripts/generate-library-spec.mjs` (+ `--check`
idempotence) — regenerates `flavors.json` (engine mirror),
`library.spec.json`, `library.doc.txt`. Count/partition pins live in
`tests/test_library_spec.py` (20 components), doc-shape pins in
`test_library_doc.py`, parity pins in `test_validation.py`, and the nudge
text has THREE copies that must move together: the generator constant, the
`library_mini._FLAVOR_NUDGE` mirror, and the test-file copies
(`test_library_doc.py`, `test_prompts.py`). A library change is DONE when
`--check` is idempotent and the full suite is green.

## 10. Polish-checkpoint recipes (2026-10-14, owner reference picks)

- **Curled paper corner** (PostIt): a `clip-path`-notched sheet pseudo-element + a hard-45°-stop-gradient fold pseudo-element carrying a static `drop-shadow` (the transparent gradient half makes the shadow follow the triangle), with the element's own `box-shadow` on an unclipped parent — `clip-path` clips fills **and** shadow output, so paper and shadow can never share an element.
- **Machine screens** (Monitor): `error` = token-styled dialog on a desktop field with a dead ✕ close glyph and dead OK; `bsod` = modern crash field (`:(` + verbatim lines + a decorative pure-CSS QR felt grid via `repeating-conic-gradient`); `crt` adds scanlines, a two-layer phosphor bloom and a curved-glass edge vignette — the only literals stay CRT phosphor green and BSOD blue.
- **Receipts/stats tell the truth** (SMS/SocialPost): SMS hangs iMessage's receipt line under the **newest outgoing** bubble only when that bubble's own status says so (`delivered→"Delivered"`, `read→"Read"`, `sent`→nothing); SocialPost shows "quieter" as a token-only register — one neutral hairline, no accent cap.
- **Artifact pastels**: the sticky-pastel family (yellow/pink/blue/green) is the generalized artifact-inherent exception, each slot warmed by the same `calc(L% + var(--bg-warmth, 0%))` idiom so the family re-tints with the theme; slot 0 stays byte-identical to the classic yellow.
