# UI Language — narration surfaces

**Status:** spec of record for *how narration surfaces are drawn*. Not for *when* they are chosen
(that is the narrator doc, `src/fabulist/prompts/library_mini.py`).

**Sources of truth:**
- `~/Projects/Stabs-EDH/Stabs-GLM5.1-Directives-v3.0.1.json` — `Visual Toolkit 🌐` (VTK) + `Extra Assistant (prompt)`.
- `~/Projects/Stabs-EDH/preview-images/*.png` — rendered references (live-roleplay output).
- `frontend/src/app.css` — the existing token system (R4 dynamic theming).
- the maintainer's private spike notes (unshipped) — the DSL contract + empirical surface demand.

---

## 0. Why this document exists

The current 18 flavors ported every *schema* and dropped every *pictorial noun*. The preset said
"hyper-enlarged emoji, 150–250px"; we render 37px. It said "hierarchical screams, enlarged mental
debris, texture backgrounds"; we render a `<ul>`. They are **clean and well-designed, for the wrong
purpose** — the purpose is *playful, joyful, entertaining*.

The preset achieved that by letting the model write **arbitrary HTML/CSS per turn** — maximum
freedom, zero reliability (see `SPIKE-OPENUI-ASSETS.md`: empty panes, raw-text dumps). Fabulist
cannot do that. This doc relocates the play: **creativity at author-time (unbounded), reliability at
run-time (fixed names, positional args, tokens).**

---

## 1. Two registers

Everything narration-side belongs to exactly one of two registers. **Never mix the frames.**

| | **VTK** | **Assistant** |
|---|---|---|
| Diegetic status | an object *in* the fiction | a side conversation *outside* it |
| Addresses | the scene | `<USER>`, aware of the main narrative |
| References | crew document; terminal popup; mindscape; env tracker | `assistant_example0{1,3,5}.png` |
| Frame family | document / terminal / screen / ledger / lock-screen | commentary / margin / side-channel |
| Tone of voice | in-world | observational, OOC, "here's what I'd do" |

> **SEGFAULT note:** in `preview-images/2.6-*` / `Bonnie G5.png` SEGFAULT appears as an *assistant*
> persona. He gets **no special treatment** — his blocks are a normal Assistant rendering.

---

## 2. Colour — one source, derived

R4 already writes `--accent-hue`, `--accent-sat`, `--bg-warmth` onto `:root` per session
(`frontend/src/routes/chat/+page.svelte:189 applyTheme()` → `GET /api/sessions/{id}/theme`).
**Keep that.** The bug is not "static vs dynamic" — it is that **each component invents its own hue
offset**.

**Audit (current state):** 22 offsets across 18 flavor files, spanning ~400° of the wheel —
`Afterglow +133 · Artifact −32/+56/+143 · Clash +98 · Document +143 · Entrance +150 ·
Interface −112/−32 · Memory −214 · Mindscape 0 · Passage −52 · Power +182 ·
ReactionSpotlight +148 · Romance +88 · Subtext +68 · Undress +74 · …`

With a violet accent, Mindscape lands violet, Afterglow green, Memory orange, Power cyan. **That is
the rainbow** — structurally the same failure as the preset's *"Infer theme — derive an appropriate
color palette from the persona summary"*, one level down.

**Rule: derive once, at the token level. Components reference tones; they never add offsets.**

```css
/* :root — app.css */
--tone-1: hsl(var(--accent-hue), var(--accent-sat), 66%);            /* the base accent   */
--tone-2: hsl(calc(var(--accent-hue) + 150), var(--accent-sat), 70%);
--tone-3: hsl(calc(var(--accent-hue) - 150), var(--accent-sat), 70%);

--tone-1-soft: hsl(var(--accent-hue), var(--accent-sat), 66%, 0.16);
--tone-1-line: hsl(var(--accent-hue), var(--accent-sat), 66%, 0.45);
/* …soft/line variants for tone-2 / tone-3 likewise */
```

- `--tone-2` and `--tone-3` are **complementary to each other** and both harmonise with the base.
- **Alternation (allowed, wanted):** alternate `tone-2` / `tone-3` for rhythm — option rows, sections,
  alternating panels. Reserve `tone-1` for emphasis.
- **Forbidden:** entity-coded hue identities. Differentiation comes from **structure, type register,
  and composition — not colour.**
- Surface warmth/coolness shifts derive from `--bg-warmth`; no new hues.

---

## 3. Type — two registers

- **System register** — monospace, uppercase, letterspaced. Used for VTK chrome, section markers,
  tags, Assistant rail labels. Markers: `▶`, `//`, `[ ]`, `·`.
- **Narrative register** — the shell prose ramp (`--font-body`, 15px). Used for commentary bodies,
  in-world prose, option text.

**Floors (preset rules, keep them):** VTK text **≥ 14px**; Assistant **body 15px / footer 11px**;
shell floor 14px (`--font-ui`).

The chrome kit's own default is the 14px shell floor; a **footer tagline is a micro-label** and may
drop to `--font-micro` (11px), matching the shell convention (`app.css`: only true micro-labels go
below 14px). Nothing else in the kit goes below 14px.

---

## 4. Chrome kit — shared primitives

One token-driven CSS layer: `frontend/src/lib/openui/chrome.css`. Both registers use the *same*
primitives with different **frames**.

| Primitive | Purpose |
|---|---|
| `.ui-panel` | bordered surface + radius |
| `.ui-frame-diegetic` | VTK frame (document / terminal / screen / ledger) |
| `.ui-frame-sidechannel` | Assistant frame (commentary / margin) |
| `.ui-head` | header bar — glyph + title + meta (mono, uppercase) |
| `.ui-section` | `▶ LABEL` section marker |
| `.ui-tag` `.ui-bracket` `.ui-chip` | descriptor / bracket / chip labels |
| `.ui-inset` | nested sub-panel (e.g. `SUGGESTED RESPONSES`) |
| `.ui-stat` | key/value row |
| `.ui-rule` | divider (solid `·` dashed) |
| `.ui-glyph` | graphic slot — image / emoji / inline SVG |
| `.ui-foot` | footer tagline |
| `.ui-glow` | static glow (never animated box-shadow) |
| `.ui-redact` | redaction hatch — `[REDACTED — MEDICAL EMERGENCY]` |
| `.ui-strike` | struck-through line (humour device, e.g. a struck responsibility) |
| `.ui-texture` | background texture (dots / grid / grain) |

**Alternation helper:** `.ui-alt { --tone: var(--tone-2) } .ui-alt:nth-child(even) { --tone: var(--tone-3) }`
— components consume `var(--tone, var(--tone-1))`.

**Motion:** keyframes on `opacity` / `transform` / `color` only. **Animated `box-shadow` is banned.**
Honour `prefers-reduced-motion`.

---

## 5. Per-flavor briefs (acceptance criteria)

Each flavor carries the preset's verbatim *Example Elements* as its **acceptance test**, plus its
reference image and its prop shape. A port is done when it satisfies the brief, not when it renders.

**Ledger (post Wave-2a cull — user review 2026-10-11):**

*North star (owner, verbatim):* **“front load design and modulate it at runtime to be expressive”** — authored decoration lives in the component; the model supplies words and small choices (which glyph, which line, how intense).

- **Kept as-is:** MeterBar · BeatCard · DialogueSpotlight · ReactionSpotlight · FailureAchievement (user: “GOOD”).
- **Wave 2b reworks (landed):** Artifact (RPG inspection card, rarity ramp) · Document (letter structure) · Memory (polaroid/notepad shape) · Interface (real terminal + art slot, no `<ol>`) · ObjectCard (was ArtifactCard — the old name remains a parse alias) · Mindscape (scatter/motion/contradiction) · Clash (impact frame) · Entrance (cinematic card + eyes/hair marker slots from persona canon).
- **Dropped (9):** StatusPanel (time/loc belong to injected chrome — was a state-emission leak) · Subtext (→ prose or DialogueSpotlight `tell`) · Perception (→ prose) · Passage (→ prose + a new ScenePane title) · Romance · Intimacy · Afterglow (→ prose; warmth in sensory detail) · Power (→ DialogueSpotlight / Entrance) · Undress (→ prose).
- **Shelved:** Passage-as-minimap/tracker (revivable if wanted).
- **Slots (parser-verified):** every component accepts ONE trailing optional options object `{glyph?, art?}` — bare named args (`glyph: "…"`) are INVALID syntax (excess-args → fallback).

**Worked example — Mindscape**
- *Brief:* "Hierarchical screams, scattered trails of unrelated thoughts and many enlarged mental
  debris of emoji/kaomoji/ASCII, contradictory and self-questioning thoughts, texture backgrounds."
- *Reference:* `preview-images/Bonnie G5.png`.
- *Acceptance:* ≥3 fragments at **different sizes and indents**; a contradictory pair; ≥1
  **enlarged** glyph (emoji/kaomoji/ASCII); a texture or image backdrop; never a plain list.
- *Props:* `Mindscape(title, debris[], texture, image, mood)` with
  `debris[] = { text, weight, kind }` (`weight` drives size/opacity; `kind` ∈ `thought|scream|aside`).

---

## 6. Assistant spec

From the preset's `Extra Assistant (prompt)` block + `preview-images/assistant_example0{1,3,5}.png`:

- **Rail** (fixed width): circular avatar (*image if provided, else initials/symbol*), **name**,
  subtitle/role, mood indicator (1–2 emoji).
- **Commentary**: the persona's voice, reacting to the current beat; **addresses `<USER>`**; may hint
  at what it would do. Supports the playful register — emphasis, emoji/kaomoji, `*stage directions*`.
- **Options**: 3–4 numbered. Each = `"sample dialogue/action"` + a short **descriptor tag** + optional
  in-voice rationale. **One option is emphasised by structure** (frame / weight / marker) — *never* by
  a different hue.
- **Footer**: optional flavour line.
- **Placement**: a pinned side-panel **above the composer**; enabled from Settings (**max 1**);
  when disabled it collapses (on focus loss).
- **Interaction**: clicking an option **fills the composer — it does not auto-send.**

**Mechanism (verified against `@openuidev/lang-core@0.3.2`):** the DSL can declare an action —
`ACTION_NAMES = [Action, Run, ToAssistant, OpenUrl, Set, Reset]`, `ToAssistant → continue_conversation`,
`ActionEvent.humanFriendlyMessage` = "displayed as user message in chat". `Renderer` accepts
`onAction?: (e: ActionEvent) => void` — **Fabulist does not pass it today**
(`frontend/src/routes/chat/DslFallback.svelte:29`). Wire `onAction` → composer store.

---

## 7. Authoring pipeline

```
VTK brief cell + reference images (few-shot)
        ▼
design-capable model drafts the surface        ← creativity lives here (unbounded)
        ▼
port to Svelte + tokens + chrome kit           ← the freeze step
        ▼
review in /components gallery beside the reference   ← human taste gate
        ▼
frozen component → narrator doc teaches selection
```

The library **grows** from the same brief. Fixing the emission contract (positional args, declared
children) is a prerequisite, not a substitute.

---

## 8. Verification

- Gallery route `/components` renders every flavor from one DSL payload with live
  theme re-tinting — the standing style guide. It exists in `vite dev` only (the production
  build excludes it; `DEV_ONLY_ROUTES` in `frontend/vite.config.ts`), and needs no `VITE_MOCK`
  flag: the payload is inline and rendered through the real library.
- Headless chromium screenshots compared **side-by-side with `preview-images/*`**.
- `cd frontend && npm run check && npm run build` must stay green.

### 8.1 Render capture

`scripts/render_shots.py` boots the dev server on a scratch port, opens the `/components`
gallery, captures a
full-page PNG plus one clipped PNG per rendered component from the seeded
`frontend/src/lib/openui/fixtures/*.openui`
payload, and writes `data/shots/<UTC>/manifest.json` (viewport, theme tokens, fixture/element map).
`--themes` cycles the accent tokens; `--baseline`/`--diff` store and compare a baseline at a per-pixel
tolerance; it exits non-zero if the server never readies, nothing renders, or a fixture node is dropped.
Run: `uv run --with playwright python scripts/render_shots.py`.

---

## 9. Ledger / open items

- [ ] Migrate the 22 hue offsets → `var(--tone-N)` (18 files).
- [ ] Confirm chrome-kit primitive names before lanes consume them.
- [ ] References: referenced from Stabs-EDH vs vendored — currently **referenced**.
- [ ] `+6hr forecast` in the HUD has no producer today (in-fiction weather model vs external feed) — **open**.
- [ ] `$facts` cannot feed the env tracker as drawn (`[ 🕐 Thu, Mar 15, 2347. 09:17 AM | 🚀 Bridge, Epoch 27 | 🌫️ Clear Void ]`): no date/calendar, no emoji, no location sub-key. State expansion is upstream of the HUD — **open**.
