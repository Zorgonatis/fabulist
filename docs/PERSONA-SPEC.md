# Fabulist Persona System — Normative Specification

**Status:** Draft 2, owner-steered 2026-10-11. Extends PLAN.md **D36/D37**; supersedes the
actor-side sections of the maintainer's private design notes (unshipped) (whose remaining scope is the user-persona
injection point, macros, hot-swap).
**Language:** RFC 2119 — MUST / SHOULD / MAY are normative.
**Verification basis:** `src/fabulist/personas/schema.py`, `personas/registry.py`,
`context/facts.py`, `context/roster.py`, `frontend/src/lib/openui/schemas/flavors.ts`
(`EntranceOptionsSchema`), `tests/test_persona_schema.py` (12 tests, green), parser probes
2026-10-11.

## 1. Scope

Structured canon for **three scopes — actor, user, world** — hosted in the ST v2 envelope,
stored log-owned, rendered into the character description context, feature-selected by the
narrator at render time, and mutated only by owner-verified extractor proposals.

## 2. Terms

- **Card** — Character Card V2 envelope. **Persona event** — D16 log entity (latest-wins).
- **Canon** — authored truth. Extraction observes and proposes; it never overwrites.
- **Scope** — the mutation target: `user` | `actor:<persona_id>` | `world`.
- **Markers** — the visual details featured at an Entrance; **selected by the narrator**,
  not vetted in schema.

## 3. Lifecycle

```
card import → envelope preserved (D28) → persona event @ log (D16/D31)
  → prompt assembly: canon renders into the character description context
  → narrator selects + renders markers generatively (Entrance {markers})
  → extractor proposes scoped canon diffs (additions/changes) as events
  → [OPEN] owner verifies → applied by appending a new persona event (D10)
```

## 4. Storage — one mechanism, three scopes

- **P1** [IMPLEMENTED] Actor canon MUST be hosted at `data.extensions.fabulist.persona`.
- **P2** [IMPLEMENTED] Import MUST preserve the envelope verbatim; unknown keys survive round-trip.
- **P3** [IMPLEMENTED] The schema rides the persona event's `extensions` into the append-only log; folding is latest-wins per `persona_id`.
- **P4** [DECIDED] The **user persona MUST reuse the same mechanism entirely** — same CardV2-shaped
  entity (`name/description/personality/scenario/first_mes/book_ids/extensions/provenance`),
  same extension key, same renderer. Distinction is a **scope tag on the entity**, not a
  second system. Injection point into the prompt is owned by `PERSONA-DESIGN.md` (open).
- **P5** [DECIDED] World canon SHOULD use the same diff/proposal pipeline (§9); its storage
  (books / world-state) is out of this spec's normative scope.
- **P6** [IMPLEMENTED] Cards without a persona produce byte-identical output to pre-D36; old logs fold unchanged.

## 5. Schema — lenient and atomic

- **P7** [IMPLEMENTED] Identity block + recursive `body` tree + `extra` (unknown keys preserved). Every field MUST be optional; parsing MUST NOT raise on any input.
- **P8** [IMPLEMENTED] Null body leaf renders as a bare descriptor; rendering is a pure function.
- **P9** [NORMATIVE] **Atomicity:** a partial envelope MUST render partial structure — never
  dropped, never an error, never silently defaulted. What exists is what is sent.
- **P10** [IMPLEMENTED] Two renderers: **full tree** (default) and **identity core**.
- **P11** [OPEN — owner] Fidelity default: full tree ≈ **372** tok/turn on the reference
  payload; core ≈ **174**. One-call-site switch; D27 trimming either way.

## 6. Prompt rendering

- **P12** [IMPLEMENTED] Canon renders into the **character description context** (single-card
  `### Character:` / cast `### Cast`) — never the facts pipeline, never a bespoke lane.
- **P13** [IMPLEMENTED] Rendered text flows the D27 token surface; no parallel path.
- **P14** [IMPLEMENTED] Header `Persona canon:` marks the block. No pre-selected marker
  emphasis — selection bias is removed by design.

## 7. Markers — narrator authority

- **P15** [IMPLEMENTED] The narrator SELECTS the markers: the two-to-four visual details that
  define a character on sight, chosen from canon (or established for new characters).
  No vetted key set exists or MAY be reintroduced as a rendering bias.
- **P16** [IMPLEMENTED] Entrance exposes them structurally:
  `Entrance(name, role, signature, {markers: "Label: value; Label: value", glyph})`.
  Unlabelled segments render as bare rows; empty/absent renders nothing.
- **P17** [TAUGHT, NOT ENFORCED] Canon-known actor: the narrator MUST NOT contradict canon.
  Unknown character: first Entrance ESTABLISHES concrete markers; thereafter fixed.

## 8. Discovery — "referred to as short, not captured" → proposed lore

- **P18** [PROPOSED] The extractor MUST watch turns for durable character detail absent from
  stored canon (actor, user, or world) and **propose an addition/change as a scoped diff**.
- **P19** [PROPOSED] Proposals MUST be events (append-only); they MUST NOT alter rendering
  until verified.

## 9. Verification → application (OPEN — owner acknowledges unfinished)

- **P20** [OPEN] The review surface for proposed diffs (accept/edit/reject per diff) is
  undesigned. Until it exists, proposals accumulate and nothing auto-applies.
- **P21** [NORMATIVE] Application MUST append a new persona event (D10/D16 latest-wins).
  Silent mutation of stored canon is forbidden in every scope.
- **P22** [PROPOSED] Severity tiers on CONTRADICTION (vs proposal): marker-level canon = hard
  (revision); soft traits = advisory (development is legitimate).

## 10. Conformance

`tests/test_persona_schema.py` (12 tests) MUST stay green: parse fidelity, unknown-key
preservation, lenience, both renderers, **absence of marker bias**, single-card + cast
rendering, no-extension byte-identity, backfill forwarding, old-log folding, end-to-end
HTTP import → log → prompt.

## 11. Decisions owed by the owner

1. **P11** — fidelity default (372 vs 174 tok/turn/actor).
2. **§9** — the verification→fix surface (design session owed).
3. **P4** — user-persona injection point (PERSONA-DESIGN §2, incl. the live D18 `{{user}}` bug).
4. **P5** — world-canon storage alignment (books vs world-state).
