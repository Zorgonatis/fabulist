# Fabulist — Agent Guide

Personal RP narrative engine: LangGraph (Python 3.12, uv) + SvelteKit 3/Svelte 5 + openui.

## What this is

An entertaining, seamless, self-healing roleplay platform.

**A roleplay UI, for those who want to edit the outcome instead of the prompt.**
Wherever you notice something, that's where you can change it.

The story and the sidebar never disagree. You cannot wreck it.

(The README carries the same lines.)

Read this, then `docs/decisions/README.md` (the design rulings) before any work.


Architecture and every load-bearing decision live in `docs/decisions/` — one entry per ruling,
citing the historical log id (`D9`, `D38`, …) that module docstrings also cite. Superseded wave
records and closed design gates are kept privately (unshipped).

Local-only working notes (current-state snapshot, the full decision log, this machine's
services, model servers and browser paths) live in a private, unshipped folder. If
you are working in a clone, that directory will not exist; do not recreate it.
(In a clone these do not exist — discover host services yourself via `ss -tlnp` / `docker ps`; do not recreate the folder.)

## Commands

- Backend: `uv run pytest -q` (all tests), `uv run python scripts/serve.py` (API on :2727)
- Harness: `uv run python scripts/feel_test.py` (live multi-turn loop against the configured
  narrator connection)
- Frontend: `cd frontend && npm run check && npm run build`; dev `npm run dev` (:5173);
  mock demo `VITE_MOCK=1 npm run dev` (UI shows a red MOCK badge when active)
- A long-lived local stack usually runs the engine and the UI as user services — the specific
  unit names and URLs are in `the maintainer's private ops notes (unshipped)`.

## Releases

- The changelog gets one line per change with a reference to its closed issue (`(#NN)`).
  The issue thread carries the detail — the changelog is an index, not a report.

## Non-negotiables

1. **Verify live, not just with fakes.** Unit tests with mock transports have lied at every
   seam so far. A lane is done when it works against a real model and through a real browser.
2. **The event log owns everything** (ADR-001). Agents propose; the dispatcher appends.
   Narrative corrections are revision events (ADR-002). LangGraph checkpoints are rebuildable
   cache (ADR-003).
3. **The narrator pass = prose + openui DSL ONLY.** No state/env/tracker emission — the env HUD
   (`EnvTracker`) is ENGINE-emitted and deliberately absent from the narrator library. State is
   injected as facts at the end of the prompt and captured post-turn (ADR-004).
4. **Lane discipline:** work only your owned files; `pyproject.toml` changes are
   orchestrator-owned; `configs/connections.json` and `data/` are gitignored user state.
5. Solo-maintainer project: keep solutions boring; every new surface must earn itself.
6. **Prompt layout is a cache contract** (ADR-005). The STABLE PREFIX (system prompt,
   character/cast block, the `{{char}}` snapshot) must not change turn-to-turn — that is what
   keeps prompt caching effective; the snapshot is persisted FOR this reason, not as staleness.
   Everything that changes per turn (memories, state/facts, recalled, runtime notices such as a
   cast update) belongs at the END of the prompt. Runtime changes are surfaced as dynamic
   notices — never by rewriting the prefix.
7. **Asset definitions stay offline-editable.** Personas/worlds/books/assistants are edited by
   plain CRUD (the studio) with NO chat open and no session required; keep definition CRUD
   independent of chat/session state. Selections and defaults are settings/state — never
   rewrites of an asset.

## Services serve the working tree

- The dev UI server runs with **HMR** — mid-edit code is live. Never test against a tree another
  lane is editing.
- An engine process started by a service manager is a **snapshot from process start** (no
  reload). After backend edits, restart it and compare start time to edit time before trusting
  a result.
- Isolated verification (spare ports): `scripts/serve.py --port 8001 --log /tmp/x.db
  --checkpoints /tmp/x-cp.db …` plus `FABULIST_DEV_ORIGINS=http://localhost:<vite-port>` on the
  engine — CORS defaults to :5173 only. Kill by port (`fuser -k 8001/tcp`), never `pkill -f`.

## Choosing an event-log read (forks)

`get_events` = session-local audit view (forks copy nothing). `get_chain_events` = fork-aware —
ALL session-constant context (persona/cast/world/modules/macros/memory scope) must walk the
chain. A fork's config carry folds the source's CHAIN latest-wins per whitelisted kind
(`FORK_CONFIG_EVENTS`), never the source's own rows: a branch owns none, so an own-row carry
loses the persona, the actor and the name at generation 2. The above-the-cut test applies to
the winning row's own `seq`, never to a numeric filter over the merged chain, because `seq`
restarts per segment (#108). `get_projection` = fork- AND revision-aware (history, state). Using `get_events` for
context assembly silently starves branch turns (fixed in 4e59afd — do not reintroduce).
`get_state_snapshot` = the state a fork CARRIED into the new chat: the `system`/
`session_state_snapshot` row is a BASE written ONCE at fork time (the state AT the cut) and
never rewritten. `get_session_state` = every read folds the branch's own rows ABOVE that base
to the tip (own state-deltas win over the cut; `at_seq` reports the resolved tip, not the cut),
and the chain is the fallback for a root session or a pre-#62 branch. The snapshot is where a
branch starts, not where it stops — never report it as a branch's current state (#78).

## Vocabulary

`ObjectCard` is canonical (`ArtifactCard` is a parse alias for legacy sessions — not a bug to
fix). **Tracked state is the ENTITY REGISTRY** (ADR-013): the tracker records identity-stable
objects (`entity_establish/set/update/move/retire/summary` + the `__entity_catalog__` attribute
catalog) through write-action tools; the free-form `[BTS]` notation is a READ SHIM for old logs
and must never come back as a write path. Presence is registry-first (a being is in-scene iff
its location assignment equals the player's — a move is a deterministic exit); `flat_render`
never emits `forecast` (the user's manual pin owns it). `chat` is a READ-ONLY PROJECTION
(ADR-029), never a stored unit: the root of a session's fork chain plus every session that
climbs to it through `base_session`. The list derives `chat_id` and `chat_size`; the rail folds
by `chat_id`, and the chat header's versions dropdown lists the members. Do not add a chat
table, a chat event or a backfill. Fixed canon is an authored `constant` lorebook entry: it
outranks the tracked state in the narrator's truth tier and rides the state block, never the
recalled block (ADR-030). `request_parts` is the ONE request assembly per interaction; the
live call site and the studio preview call the same builder. The served tracker prompt comes from the
COMMITTED FACTORY ARTIFACT (`prompts/factory-defaults.json`) — editing the constant in `bts.py`
is not enough: run `scripts/seed_prompt_defaults.py --force` (and check flow row ORDER — its
write path can reorder rows; the prompt-flows test pins it). The planner directive is the same
class: the served text comes from the committed factory artifact, so editing
`DEFAULT_PLANNER_DIRECTIVE` alone changes nothing for a seeded install; bump the factory
version and reseed (#105). `StatusPanel` is gone. `assistant`
is canonical; `sidekick` survives only as deliberate read-side legacy shims. The engine-emitted
HUD statement is `EnvTracker`. `Queries` is now `Repair triggers` (#99) — COPY ONLY: kind id
`queries`, `DecisionQueriesPane.svelte`, `decisionQueries.ts`, `/api/decision-queries`,
`src/fabulist/decision_queries.py` and the `__decision_queries__` anchor all stay on purpose —
a UI word off its identifier is deliberate, not a bug; a trigger is the CAUSE (the check that
fires), a repair the EFFECT (the turn sent back).

## Component vocabulary is ONE generated tree — regenerate, never hand-sync Python
(was: ONE pipeline; ADR-025 split it — one generated spec **per set**, plus one union; the engine reads the active set, the renderer the union.)

openui component defs are generated FROM the frontend components

openui component defs are generated FROM the frontend components (`schemas/flavors.ts` →
`generate-library-spec.mjs`) into `prompts/generated/library.spec.json` + `library.doc.txt`, and
the engine's DSL validator READS those artifacts (`dsl/spec.py:_load_spec_entries`) — there is no
component list in Python to edit. So a component shape/name change is: edit the
component/schema → run the generator → `--check` green. The only hand-maintained mirror is the
generator's inline `INFRASTRUCTURE` block (four infra components), which must track `library.ts`.

## Generated artifacts — regenerate, never hand-edit

`prompts/generated/*` are produced by `frontend/scripts/generate-library-spec.mjs` and
`scripts/generate_agent_map.py` (both support `--check`; tests pin artifact sync).

## Gotchas already solved (do not reintroduce)

- Facts inside final user msg (templates reject mid-system); commit turn before extraction;
  sync+async and dataclass+dict extractor seams both accepted; `finish_reason=length` surfaced
  (and anything outside the finish-reason allowlist discarded — ADR-021); DSL statements may
  arrive unfenced (nodes.py `_split_unfenced_statements` + frontend `segment.ts`); `pkill -f`
  self-matches — use `fuser -k <port>/tcp`.
- The studio preview must not carry its own parts assembly. One named request builder per
  interaction (`prompts/request_parts.py`, plus `graph.nodes.planner_request` for the planner)
  is called by both the live call site and the preview, and `tests/test_prompt_preview_parity.py`
  pins the byte-for-byte match and the builder completeness. A re-copied parts dict in the
  router fails the AST pin (#104).
- The keep-last window reads the ordered `segments` channel, never `prose` — `prose` is the joined
  prose segments only and never holds panel markup, so a window that read prose kept nothing while
  every unit test with a hand-written fixture still passed (#100); the commit path stores `prose`,
  `dsl` and `segments` as separate channels.

## Verification (browser interaction)

- **Never drive clicks with synthetic `Runtime.evaluate('.click()')` over raw CDP** — it produced
  *phantom* 500s AND masked real bugs in one session. Use **Playwright** (real trusted input,
  actionability waits, strict locators). Install/launch details for this machine are in
  `the maintainer's private ops notes (unshipped)`.
- `h1 500 / "Internal Error"` with **no second request in the vite log** = client-side render
  boundary; a 500 **with** a server request = SSR crash (read the dev-server log; the unit names
  are in `the maintainer's private ops notes (unshipped)`).
- Svelte `each_key_duplicate` fires on **conditional re-mount** — an SSR-first render will NOT
  surface it; exercise the mount path interactively.
- Header `setPointerCapture` swallows clicks on **child buttons** of the same header. Guard:
  `if (target.closest('button')) return;` before capture.
- Frontend fetches MUST use `API_BASE` (from `#lib/api`), not relative `/api/...`.
- `window.innerWidth` in a prop expression crashes SSR; guard with `typeof window !== 'undefined'`.
- Svelte 5 reactivity: `get(store)` inside an `$effect` does NOT track — read the `$store` value
  directly.
- A full turn is slow by design (serial agent work plus model latency) — measured turn times and
  what is normal for this stack are in `the maintainer's private state snapshot (unshipped)`; a quiet minute is latency, not a
  hang, and nothing should serialize the turn lock waiting on it.
- A flex header whose tallest item is a column anchors its centred items to the MIDDLE row —
  the studio head's drag handle and window buttons sat on the middle tab band, because the tab
  strip is a column of bands (#103). Start the head's items at the top and size each control to
  one band row. In a column, DOM order is vertical order: the collapsed state bar's control was
  the strip's last child and therefore bottom-anchored, so it is ordered first, and the undock
  reserve is the head's margin, not its padding.
