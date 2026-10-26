# Decisions

The load-bearing rulings of Fabulist, one entry each: the ruling, and the failure it prevents
from coming back. This is a curated digest of the design log, not a changelog — build history,
wave records and superseded gates are kept privately (unshipped).

Module docstrings and code comments cite historical log ids (`D9`, `D38`, …). Each entry
carries its id so a citation resolves to the ruling it names. Where an entry was later amended,
the amendment is folded into the text and the superseded shape is named.

- [1. Log and state](#1-log-and-state)
- [2. Prompt assembly](#2-prompt-assembly)
- [3. Models and connections](#3-models-and-connections)
- [4. The turn](#4-the-turn)
- [5. Tracked state](#5-tracked-state)
- [6. Rendering](#6-rendering)
- [7. Context and delivery](#7-context-and-delivery)

---

## 1. Log and state

### ADR-001 · The event log owns everything (D9)

One append-only SQLite log is the source of truth for a session. Agents are stateless between
turns and *propose* events; only the dispatcher appends, through a single chokepoint that
applies the schema and permission gates. The transcript, state panels, the memory index and
every agent's context are projections of the log, never parallel stores.

Consequence: replay is a swipe, a fork is a branch pointer, and no subsystem can quietly
become a second truth. Anything derived (indexes, summaries, checkpoints) is rebuildable and
may be dropped.

### ADR-002 · Corrections are revision events (D10)

A narrative is never rewritten in place. A correction appends `revision(narrative_event_id,
new_content, reason)`; reads render latest-wins while history, audit trail and branch
inheritance stay intact. Human edits from the UI use the same door as automated ones, and
every narrative records the configuration snapshot that produced it (model, sampler, prompt
version, injected-facts hash).

Consequence: provenance is always answerable, and a fork cannot inherit a half-rewritten past.

### ADR-003 · Graph checkpoints are cache, not truth (D11)

The LangGraph checkpoint database is derived state, in the same class as the vector index. On
divergence the log wins, and no graph state may exist that the log cannot reconstruct.

Consequence: two stores can never disagree about what happened.

### ADR-004 · The narrator pass emits prose and generative-UI DSL only (R2, D2)

The forward pass produces narrative prose plus component statements and nothing else — no
state, no trackers, no environment fields. State leaves the narrator's pass entirely: it is
injected into the prompt as facts, and captured after the turn by a separate extractor agent
that reads the user message and the turn's prose and proposes deltas in a compact, versioned
pipe-YAML form at the log boundary.

Consequence: the state schema can be tuned without touching narrator behaviour, and a
mis-formed state line can never damage a turn of prose. The engine-emitted environment HUD is
deliberately absent from the component library the narrator is taught, so the model cannot
learn to emit it.

## 2. Prompt assembly

### ADR-005 · The prompt layout is a cache contract (D22, D34, D43)

The stable prefix — system prompt, character/cast block, and the frozen `{{char}}` snapshot —
must not change from turn to turn; that is what keeps prompt caching effective. Everything that
varies per turn (recalled memories, state facts, runtime notices, the speaker palette) belongs
at the end of the prompt, after the final user message, framed as non-binding reference.
Runtime changes are surfaced as dynamic notices, never by rewriting the prefix.

The cast value is frozen at first consumption and persisted, because a roster change at turn 50
must not retroactively rewrite what the model was told at turn 1. The user identity stays live
per turn: reassigning a persona is a deliberate act and must take effect.

Consequence: per-turn cost stays close to the size of the turn, not the size of the session,
and the persisted snapshot exists for stability rather than for staleness.

### ADR-006 · Prompt surfaces are runtime data, seeded from a factory image (D18, D26, D27, D34)

Every text that contributes tokens to an agent's context is editable data carried by the
active preset: ordered prompt modules, directive switches, structural inserters and per-flow
system prompts. The default preset is a persisted record seeded from a committed factory
artifact only when it is missing; editing it *is* editing default behaviour. A code change to
factory content bumps the factory version so existing installs get a "factory defaults changed
— review/reset" notice instead of a silent overwrite of their edits. No model-facing prompt
lives only in a Python file.

Consequence: prompt iteration needs no deploy, and an upgrade can never quietly undo authored
choices.

## 3. Models and connections

### ADR-007 · Routing is a per-connection registry; keys live in the environment (D78)

`configs/connections.json` maps a connection name to `{base_url, model, api_key_env, sampling,
timeout, capabilities}` plus an `agents` map that binds each agent to a connection. There is no
global profile, and the file is gitignored user state. A pasted key is written as
`FABULIST_<NAME>_KEY` into the project `.env` (mode 0600) by rewriting exactly that one line,
and the registry overlays the env file before parsing so a new key applies to the next call
without a restart. The literal key is never serialized back: not in a response body, not in a
log line, not in the config document.

Consequence: a user can move between local servers and hosted APIs by editing data, and a
support request can never leak a credential. Timeouts are per connection, never per run — one
slow lane fails alone.

### ADR-008 · Model selection is typed, not name-matched (D84)

Connections resolve through types — `completion` (with the tasks `narration`, `extraction`,
`state`), `embedding`, `decision` — with the historical per-agent names kept as aliases. One
resolution rule serves every call site: preset binding, then per-item default, then affinity
task, then type. Context size is declared by the user per connection and is never probed; it
drives budget warnings, the compaction threshold and prompt headroom accounting.

Consequence: pointing one slot at a different model is a data edit, and the engine stops being
blind to how large the window it is filling actually is.

## 4. The turn

### ADR-009 · The turn is an executable graph, and the graph is editable (D70, D71, D72)

A turn is a set of real nodes compiled from an authored topology that seeds the default preset:
pre-turn, narrate, commit, validate, gate, critic, planner, tracker, HUD, attribution, done.
Contracts are a type system — each node declares what it requires and provides — and an
invalid topology is refused at the write door with the violation named. The engine rebuilds the
graph when the stored topology's revision changes, and a stored topology that fails validation
falls back to the canonical one rather than failing boot. Node templates are assets in the log;
the engine's rebuild digest covers the template library as well as the topology.

Consequence: the workflow is inspectable and changeable without code, structural mistakes are
caught before a turn runs, and an old saved topology cannot silently execute a different shape.

### ADR-010 · The commit barrier releases the user; the tail runs behind them (D73)

`commit` emits a `committed` frame. From that point the turn is kept, the composer unlocks, and
the post-commit work (validation, gate, critic, planner, tracker, HUD, attribution) runs as
background work that still streams — a late revision replaces the bubble in place. The request
door refuses only the user-facing phase; past the barrier the next turn queues behind the tail,
because the tail writes the state the next context is assembled from.

Consequence: a turn's duration stops being the user's wait, while the ordering guarantee that
keeps context honest is preserved.

### ADR-011 · Agent loops live at the nodes; cost is measured, not assumed (D83)

Repair happens in working memory at the node that found the problem: narrate drafts, validates
deterministically and applies targeted prose/DSL patches, so a clean turn costs exactly one
completion and a failed turn is never re-run from scratch. State tracking is a read-tools loop
that ends with one reporting call. Fan-out lanes are real parallel branches with a declared
per-session dispatch policy, and lanes are written in dispatcher order rather than completion
order. Concurrency limits are set from the backend's measured parallelism — if the server
serializes requests, concurrent work cannot beat its serial sum.

Consequence: latency work targets the parts of a turn that are actually serial, and each
restructure is accepted on measured numbers rather than on architecture appeal.

### ADR-012 · Speaker attribution is an emission, not an inference (D85)

The engine hands the narrator a per-turn speaker palette (active cast, one label and colour
each) as the last tail message, and the narrator writes each spoken line inside that speaker's
colour span. The engine parses that markup — never a CSS lookup — to attribute the line. An
untagged line is strictly unattributed and raises a flag that the repair loop fixes in place;
colour-only spans are carved out of the raw-markup check per tag. The earlier heuristic
attribution tiers are deleted rather than kept as a fallback.

Consequence: attribution is exact when the model complies and honestly missing when it does
not, instead of plausibly wrong.

### ADR-023 · A provider failure aborts the turn; only content failures are contained

A model call fails either because the provider ANSWERED with something unusable (empty,
unparseable, schema-invalid, truncated — or a 400/422 naming that call's own payload) or because
it never answered at all (429/401/403/404/408/409/425, any 5xx, a connection or transport
failure). Only the first class is contained: the phase's job is skipped, its `*_failed` row is
written, and the previous plan or the rest of the turn carries on. The second class aborts the
turn from the node that owns the phase, through the same error frame a narrator failure takes,
and the message names the phase, the status and the provider's own text (rate limit,
Retry-After). Containing it does not save the turn — the next phase meets the same wall — it
only hides the cause behind whatever breaks next, and lets the state a phase was supposed to
write go stale in silence. Post-commit phases keep their containment: past the barrier the turn
is already kept (ADR-010).

Consequence: the reader is told which phase failed and why, at the moment it fails, instead of
first meeting the provider's error at a phase that merely inherited it.

## 5. Tracked state

### ADR-013 · Tracked state is an identity-stable entity registry (D89, D90)

The tracker records objects — places, items, threads, relationships, meters, conditions,
economy — as typed, identity-stable events through write-action tools, not as free-form prose
lines. Ids are minted once and reused, so a value can be updated across turns. Presence is
registry-first: a being is in the scene when its location assignment matches the player's, and
a move is a deterministic exit. The attribute catalog for meters and traits is a log-owned asset
the user can edit. The old free-form notation survives only as a read shim for existing logs and
must never return as a write path.

Permanence rides the existing proposal lanes: the registry is session-owned and ungated, while
making a fact permanent produces a proposal a human accepts. Accept is the only asset write;
undo is a compensating append that restores the pre-image exactly.

Consequence: one copy of every fact, so the registry, the prompt and the sidebar cannot drift,
and no model write can silently redefine an asset.

### ADR-014 · Cast admission is user-gated (D79)

Detection of a new character ends at a proposal. Until it is accepted, a candidate is never a
cast member, never gets a lane, and never appears in user-facing world-state reads — though the
narrator's continuity still sees it, because a character in the room must not vanish from the
story while a proposal waits. Accept admits with provenance and aliases; undo retires; reject is
a no-op. Earlier automatic promotion on a measured threshold was deleted outright.

Consequence: the cast is what the user says it is, and automation cannot quietly grow it.

### ADR-015 · The planner bootstraps the object stores (D95)

The planner runs rarely — mandatory on turn one, then on a cadence. Before its first plan it
runs a bounded tool phase that seeds the object stores using the same tools as the tracker;
every later refresh runs a repair subset with the create tools absent, so re-seeding is
impossible by construction rather than by instruction. Failures are contained and logged, and
the plan still runs. Tool budgets charge an attempt and spend a write only on success, so a
mistake is free but a loop is not.

Consequence: turn one starts from a real place tree rather than from nothing, and the bootstrap
cannot become a per-turn cost.

### ADR-016 · The session is one addressable tree (D100)

A session is served as a single tree — clock, scene, places, items, threads, relations, meters,
conditions, economy, cast and the player — as a pure projection over the same typed events. No
storage change: per-object appends, per-object validation, per-object history. Ids are handles
addressing a write, revealed only while that leaf is being edited; every other place an object
or a person appears renders a display name. The player is a node like any other, with the same
body, condition and inventory mechanics.

The clock has a proposal channel with engine guardrails: the model may propose a time change
with prose as evidence, and the engine refuses anything earlier than the current time or beyond
a bounded jump, correcting rather than accepting. Conditions expire on clock time and are swept
deterministically.

Consequence: the narrator can influence time without owning it, and the UI never shows a
machine key where a name belongs.

## 6. Rendering

### ADR-017 · Generative UI is component-restricted, and every rendered value is editable (D45, D60, D64)

The narrator composes only from the taught component library; a raw HTML escape hatch exists as
one component with its own policy gate. Validated-but-unrenderable statements are kept verbatim
and tagged with the validator notes rather than silently dropped — degrading to hide a problem
is not an option. Every value the UI displays has a write path back into the log (time, scene,
weather, tone, genre, forecast, and every tracked object), and those writes go through the same
validation the agents' tools use. Derived renders must never overwrite a manual pin: the flat
state render deliberately omits the forecast because the user's own value owns it.

Consequence: what you see is what the log says, and what you edit is what the engine validates.

### ADR-018 · The component vocabulary is one generated spec (D63)

Component definitions are generated from the frontend components into a committed spec and
library document; the engine's DSL validator reads those artifacts at runtime, and the streaming
segmenter derives its component names from the same registry. There is no component list in
Python to keep in sync, and a `--check` gate fails the build when an artifact is stale or a hand
list reappears.

Consequence: a component can never be taught by the engine and unrenderable in the browser, or
the reverse.

### ADR-024 · The shipped component set is frozen as the Technology set (D45, D58, D60, D63)

The twenty components the generator emits today are frozen as a named, versioned set:
**`technology`**, display name **Technology**, version **`1.0.0`** (owner ruling, issue #17),
fingerprinted by the spec's own `spec_version` (`eb01762f0dbf`). The name is the set's REAL
identity, not documentation about it: `id`, `name` and `version` are authored in the generator's
`SETS` entry and emitted into `prompts/generated/set.json` and the `prompts/sets/index.json`
registry (which names `default_set: "technology"`), pinned by `tests/test_library_spec.py`. Only
the DIRECTORY keeps the historical `prompts/generated/` name — a path is not an identity, and
moving it is a separate change. The set is partitioned in `docs/COMPONENT-SETS.md`: fourteen
components are the reusable core — their shape encodes no technology, period or genre, so every
future profile set inherits them — and six deliberately reproduce present-day furniture
(terminal, phone, text, computer screen, social feed, sticky note), which are the only components
a new set replaces. The freeze pins names, positional prop order, prop types and the taught doc,
because that is the surface the model learned and the gate validates; renderer styling may move
inside a version, but changing the call shape is what makes a new MAJOR version. Versioning is
`major.minor.patch` throughout — there are no integer or `tech/1`-style slugs (an earlier draft
of this record said `tech/1`; that means `technology/1.0.0`).

Consequence: a new profile starts from a working structural vocabulary instead of reinventing the
root, the gauge, the card and the escape hatch, and what it must author is named up front. The
freeze pins the vocabulary; renaming the set changed no component, no schema and no taught
byte — `library.spec.json` and `library.doc.txt` are byte-identical across the rename, so
`spec_version` was unmoved (the user-facing release version and the content fingerprint answer
different questions). Old renderers are explicitly not a compatibility liability pre-full-release
(owner ruling): a set swap may break surfaces built for an older set.

### ADR-025 · The renderer knows every set; the model is taught one (issue #15)

**Ruling.** Component vocabulary is split in two, and the split is enforceable rather than
conventional:

- **The renderer's vocabulary is the UNION of every shipped set.** The client registers every
  renderer it has ever shipped and re-splits narrative prose with that registry, so a transcript
  written under a set that is no longer active still renders its components. The union is
  derived from the declared sets and emitted as `prompts/sets/union.spec.json` — never authored.
- **The model is taught exactly one set: the ACTIVE set.** The taught doc, the gate table, the
  strip list and the server-side segmenter all read the active set's directory
  (`prompts/generated/` for the shipped default, `prompts/sets/<id>/` otherwise) through the
  component store. Inactive sets are never merged into the taught vocabulary.
- **A set is loadable iff its components ⊆ the union** — the machine-checkable form of "the
  client can render it" (`fabulist.dsl.sets.check_set_loadable`). The generator runs the same
  check when it emits the tree, and a set naming a component the library does not define is
  refused at generation time with the offending names.
- **Infrastructure is core by definition**: the four narrator infrastructure components
  (`ScenePane`, `MeterBar`, `ObjectCard`, `BeatCard`) are always present, always merged into the
  gate table, and never removable — a discarded set is a generator error, not a runtime detail.
  The engine-emitted panels (`EnvTracker` via `ChatHud`, `AssistantCard` via `panelLibrary.ts`)
  are not narrator components at all, so no narrator set can add or drop them either.

**Artifacts** (generated by `frontend/scripts/generate-library-spec.mjs`; `--check` is the
oracle, covering the whole tree):

```
prompts/generated/{set.json,library.spec.json,library.doc.txt}   # the shipped set (id "technology", v1.0.0)
prompts/sets/index.json                                          # the sets registry
prompts/sets/union.spec.json                                     # the render contract
prompts/sets/<id>/{set.json,library.spec.json,library.doc.txt}   # any set whose dir is not prompts/generated
prompts/sets/fixture-core/…                                      # test scaffolding (kind: "fixture")
```

`index.json` also states the always-present core (`core_components`, `panel_components`) and the
render-only legacy aliases (`ArtifactCard` → `ObjectCard`), which are registered client-side and
never taught. The shipped set keeps its DIRECTORY (`prompts/generated/`) while its identity is
`technology` / "Technology" / `1.0.0` (ADR-024, owner ruling #17) — id and version move, the
taught bytes do not.

**What an inactive vocabulary looks like from the engine.** Nothing silently disappears and
nothing silently keeps working:

- a statement naming a component the active set does not teach is *unknown* to the strip helper,
  so it stays in the text verbatim (never eaten);
- the segmenter classes it as prose rather than DSL (visible degradation: the reader sees the
  raw line instead of a panel that the active vocabulary cannot validate);
- a NEW emission of it fires the gate's `unknown_component` finding, the turn takes a repair
  round, and the failed statement is kept and tagged (ADR-017);
- the client still renders it — its renderer ships in the union — so old transcripts are intact.

**The cost, stated plainly: a set swap invalidates the prompt prefix cache.** The taught doc is
the `library` module's content, so it sits in the *stable prefix* (message 2 on the preset path,
inside message 1 on the no-preset compiler path). A swap changes those bytes, so nothing after
them can be reused: the persona/cast block, the frozen `{{char}}` snapshot, the history, the
facts and the player's message are all re-prefilled once (the doc is ≈12.6 KB ≈ 3.2k tokens).
It is a one-time full re-prefill per swap, not a per-turn cost — the per-set doc is byte-stable
turn to turn, which is why per-set docs must be generated and never hand-tuned at runtime. A
second-order effect: the strip list changes with the set, so already-committed history bytes
shift once as well. Accepted by the owner: swaps are rare and deliberate, cache economics per
turn are not.

**Consequence.** The vocabulary can grow a second set without ever stranding a session, and the
thing that keeps the model honest (it can only emit what it was taught) no longer also decides
what the browser is able to draw. Per-session selection and the store's de-globalization are
deliberately out of this ruling — they are issues #16 and #24, and this entry only fixes the
split they build on.

### ADR-026 · Component-set selection is per-session and the swap is a log fact (issues #16, #24)

**Ruling.** A component set is selected PER SESSION. The selection is an event-log fact — a
`set_swapped` system row appended by the selection door
(`POST /api/sessions/{sid}/component-set`) — and it takes effect at the NEXT turn's narrator
build, which resolves the session's latest row through its fork-aware chain. The engine holds no
global selection: one session's swap can never retarget another.

The store's ownership is **content, not selection**: snapshots are cached per set DIRECTORY and
shared by every session that selects that set. Selection state (the log row) and content state
(the snapshot cache) are separate, which is why two sessions on two sets keep two stable
prefixes without either one moving the other's vocabulary.

The door refuses a set the client cannot render — ADR-025's `check_set_loadable` seam — with a
422 naming the components the union lacks, and refuses a set whose artifacts do not read with
the readable reason. A refused selection never becomes a log row, so it cannot strand the next
turn. Selecting the set already in effect is a no-op (no row, no re-prefill).

The cache consequence of a swap is ADR-025's and is not restated here: the taught doc sits in the
stable prefix, so a swap re-prefills everything after it once (~12.6 KB ≈ 3.2k tokens), and the
per-set doc is byte-stable turn to turn.

**Consequence.** The picker (`GET /api/component-sets` for id/name/version/kind/loadable) is a
selection door, not a settings write; a swap is one log row, replay-visible and fork-inherited,
and no running turn can change vocabulary underneath another session.

### ADR-027 · Appearance is four presets plus one CSS override, and tone outranks both (issues #12, #37)

Appearance is **user state, not narrative state**: the reader's preset and their one pasted
stylesheet live in `data/appearance.json` (the same gitignored per-install directory the event
log sits in), written atomically by `fabulist.theme.appearance` and served by
`GET/PATCH /api/appearance`. They are deliberately NOT log events — a theme is not replayable
history, it must not fork with a branch, and a slider move must never spray system events into a
session's audit trail (the ADR-001 carve-out). Unknown document keys are preserved on every
write, so the pending wallpaper (issue #13) is a new field, not a second location.

Four first-party presets — **light / dark (default) / oled / savilen** — are
`:root[data-theme='…']` blocks in `app.css` over a shared `:root`. The differing token set is
exactly: surfaces (`--bg*`, six), text (`--text/-muted/-dim`), lines (`--line/-strong`), accent
geometry (`--accent-l/-hover-l/-strong-l` plus the preset's neutral `--accent-sat`, replacing
the hard-coded 66/60/78) and `color-scheme`, with the status colours re-solved where the
preset's surfaces move out from under them. Every table is WCAG math against the same floors
the #21 audit set (surface steps 1.15/1.15/1.20/1.15/1.20, AA for text on the settled rungs,
3:1 for `--line-strong` on every surface it borders), recomputed per preset by
`tests/test_theme_contrast_ladder.py`.

**Catalog order (issue #37, owner ruling).** Presets are presented greyscale-first, white →
black (**light**, **dark**, **oled**), then coloured themes in RGB order by primary hue
(**savilen**, hue 175.2). The order is DERIVED from `PRESET_META` through `preset_sort_key()`
in `fabulist.theme.appearance` — never a hand-maintained tuple — and the frontend mirrors the
same key for its offline catalog. `mid` is retired (owner ruling); a stored `"mid"` value falls
back to the `dark` default through the existing unknown-preset read path.

**OLED (issue #37).** The page, inset and panel rungs are true black (`#000`), so those two
adjacent steps cannot hold a luminance floor by definition; the separation is drawn by
`--line-strong`, which still holds 3:1 on every rung, and every rung that *can* move keeps the
standard floors (raised 1.21, hover 1.18, active 1.38). This is the one recorded exception to
the #21 step floors, pinned positively in `test_oled_black_panels_are_actual_black_and_the_
boundary_carries_the_rest`. Greyscale by construction (`--accent-sat: 0%`); a session tone may
still supply its own hue inline.

**Savilen (issue #37).** The reference image's extracted palette: page `#001012`, accent/border
`#1f8f86`, primary text `#4fe6d8`, mint `#5dcea5`, amber `#ffcf6b`. The image separates its
surfaces with 1px borders on a near-black page — a ramp too fine for the #21 floors — so the
surfaces are a teal ladder derived from the image's page hue while the primaries are the
extracted hexes themselves (`--accent` resolves to `#1f8f86` exactly, `--text` to `#4fe6d8`).

**Line weight (D-A).** WCAG 1.4.11 exempts decorative dividers, so `--line` stays the quiet
hairline and `--line-strong` carries the 3:1 boundary wherever it borders interactive chrome;
the **light** preset is the strict one, where `--line` holds 3:1 as well because a pale hairline
on a pale page is not a hairline, it is a rumour. The ratios the tables state are recorded
as measured numbers rather than floors where they are accepted exceptions: dark's white-on-accent
3.78 (a fill's text, never small copy), oled's black-on-grey accent 13.75, and savilen's
accent-vs-page 4.92 (a fill, not small text).

**Precedence (D-D).** `base -> preset -> user CSS -> session tone`. The tone tokens are *inline*
custom properties on `<html>`, so the fiction's palette beats any stylesheet rule that is not
`!important`: the tone wins, a reader who wants to pin a hue writes `!important`, and the paste
box says so in one sentence. `app.css` uses no `!important` at all, which is what keeps that
sentence true. The pasted stylesheet is appended as ONE `<style>` element last in `<head>`.

**The override is guarded at the write door** (`fabulist.theme.custom_css`). A property
**allowlist** — colours, backgrounds, border colours/radii, fonts at ≥14px, decoration — is
enforced mechanically, and the must-not list is dropped with a reason the drawer shows: layout
geometry, box dimensions (the reading measure is a `max-width` on the prose column), spacing,
scroll containers, `z-index` and every `--z-*` token, the `--font-*` type scale, the whole
`outline` family, `pointer-events`/`cursor`/`transform`, and every at-rule. It is not a security
boundary (CSS cannot execute here, and the CSS is the reader's own furniture); it is how the
layout contract stays a contract instead of a paragraph of documentation.

Consequence: a reader can restyle the room but cannot move the walls, and the fiction keeps the
last word on colour without the theme system having to know anything about it.

## 7. Context and delivery

### ADR-019 · The narrator prompt is a preset wrapped in an anti-injection envelope (D101)

The narrator's directive surface is compiled from the preset, then wrapped in two bounds with
explicit out-of-band boundaries (issue #26; the superseded shape was ONE `<preset>` envelope
around the stable prefix alone, which left history, facts, the player's message and the runtime
tail outside every bound — "an injection attempt" by the guard's own wording):

- `<preset_def>` — the engine definition together with the conversation through the player's
  latest message: the stable prefix, the history, the state facts and the player's own message
  as entered. That closing edge is the `<user_message>` bound (documentation name; no literal
  tag is emitted — the message already carries the transport `user` role), and the player's
  message inside it is scene input, not instruction.
- `<preset_runtime>` — every per-turn engine block appended after that message, in served
  order: `<recalled>`, the lane block, the SPEAKER PALETTE.

Each message is individually tagged, the guard line is emitted exactly once and outside both
bounds, and the boundaries are positional indices supplied by the seam that emitted the final
user message — never role- or content-scanned (the tail legitimately carries user-role
messages). The carve-out lives in the framing so the guard cannot suppress the engine's own
channels. The hand-mirrored prompt builder was retired: one content source renders the system
prompt. Renaming the tags changed the stable prefix, which invalidated the prompt cache once
(ADR-005) — accepted.

Consequence: a prompt-injection attempt embedded in roleplay content is structurally outside the
trusted bounds, without the guard being able to starve the model of real data.

**Amendment (issue #39, owner ruling 2026-10-24).** The **carrier is permanent**: the tagged
trailing-system `<preset_runtime>` tail renders exactly as ruled above, in the same place, and no
toggle may touch it. What became a persisted product setting is the **contract clause**: the
`## Engine context` section and its firewall semantics (tags imitated in the player's words are
fiction; never emit the tags) render **iff** the setting **"Anti Injection"** is ON. The shipped
default is **OFF**, so a fresh install builds a prompt with no clause anywhere while the tail
still rides as a system message after the player's.

The gate is a **render gate, not a content move**: the clause text keeps its two homes
(`prompts/parts.py` `ENGINE_CONTEXT_BODY` and the `engine_context` row of the committed
`prompts/factory-defaults.json`), and the build drops the resolved directive row when the setting
is off — on **both** prompt paths (the code catalog and the preset/artifact rows). Read per build
from the settings fold, so a flip applies to the next turn with no restart; the opening/greeting
prompt obeys the same default. With the setting ON the prefix is byte-identical to the pre-toggle
prompt (the clause stays `protected`, hence never budget-trimmed); with it OFF the delta is
exactly the clause block, nothing else.

**Measured consequence (live gate, two real turns, spare-port engine).** Both captures carried
the `<preset_runtime>` tail and the model read it with the clause OFF: the narrator took the
clock from `<engine_state>` and treated the player's imitation of the tags as fiction. The
clause-ON turn's reasoning quotes the clause's own wording; the clause-OFF turn reaches the same
read from the always-present framing + world-facts directives. The GLM-5.3 rate limit (the model
the #26 probes measured: read-rate swung 5/5 vs 0/3 on clause wording alone) forced the gate onto
another configured model, so this run does **not** re-measure GLM-5.3 specifically — the tradeoff
the owner accepted stays recorded here as measured-on-GLM-by-#26, read-rate-unverified-by-#39.

**Second amendment (issue #77, owner ruling on the issue: "gate the NPC lane here too").** The
toggle governs **instruction framing**, not just the clause. Instruction framing is: the guard
line, the `<preset_def>` / `<preset_runtime>` bounds, the framing's second paragraph (the
two-bounds description, the precedence order, the `<user_message>` naming and the emission ban —
`parts.SYSTEM_FRAMING_BOUNDS`, split from the head at its paragraph break so
`SYSTEM_FRAMING = SYSTEM_FRAMING_HEAD + "\n\n" + SYSTEM_FRAMING_BOUNDS` stays byte-identical),
the `## Engine context` clause, and the NPC lane tail's own `<preset_runtime>` wrap
(`graph/nodes.py`, threaded from the fan-out's `route`). With the setting **ON** every one of
those renders byte-identically to the pre-gate prompt. With it **OFF** they are all absent and
the prompt is the same assembly in the same order minus those pieces: one system prefix (the
framing's HEAD paragraph + directives), history, the ephemeral envelope, and the player's
message — bare and untagged.

**ALL content framing renders in BOTH states.** The toggle is a wrapper gate, never a content
gate: the six envelope tags (`<engine_state>`, `<engine_notice>`, `<engine_memory>`,
`<engine_lane>`, `<steering>`, `<engine_palette>`), `<validation_notice>`, and the lane brief's
own tags (`<scene_digest>`, `<facts>`, `<user_message>`) are engine CONTENT addressing and stay
verbatim either way. The envelope MESSAGE is still injected immediately before the player's
message when OFF (issue #44 is unchanged); only its outer bound gates. The gate is still a
render gate at the assembly seams — no new config key, no per-preset override, no OFF
reordering, no content copies — read once per build from the settings fold.

### ADR-020 · Context is a managed layer; compaction is a fork (D102)

Each committed turn is measured: the turn's peak usage on the call that carries the context,
against the measuring connection's declared window minus its response allowance. At 80% the
transcript shows one low-context notice per session and variant; at 90% compaction is forced,
with a bounded number of failed attempts per era. Compacting summarises a turn-pair span,
forks the session at the summary, and marks the child as a continuation — so the parent stays
readable and the raw turns stay in the log forever. The era's prompt is the stable prefix plus a
recap block in the tail; the prefix is untouched. Because the turn stream closes at `done`
before these hooks run, delivery is polling plus explicit conflict statuses rather than an
in-stream push.

Consequence: the context window is a product surface with visible state, and reclaiming context
never destroys history.

### ADR-021 · Lane trust is a finish-reason allowlist; a missed cadence job retries (D86)

Structured output from a small model is trusted only when the completion ends in an expected way
(`stop` or `tool_calls`, or a stream that ended without a stop frame). Truncation is repaired
once; any other terminal reason discards the reply even when it parses. A scheduled job that
misses its window advances no clock, so the next committed turn retries it and re-covers the
turns that were missed; a partial run consumes its window, because re-judging settled turns is
worse than one bounded slice.

Consequence: no half-generated object becomes state, and a transient outage costs one turn
rather than a whole cadence interval.

### ADR-022 · Markup in prose is detected, never stripped (D80)

Raw HTML arriving in narrative prose is flagged and routed into the repair path; the prose
itself passes through untouched. The teaching lives in the generated library document, which
names the component that owns authored HTML. An earlier strip was removed because it silently
changed what the model wrote and hid the cause.

Consequence: a narrator-contract violation is visible and repairable instead of being cleaned up
in transit.
