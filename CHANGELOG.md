# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.0.3] - 2026-10-25

### Added

- **Planner and NPC lanes are mappable from the connections pane.** Both are declared per-item
  roles (`planner`, `npc` under `completion`/`narration`) — the same shape as `summarizer` and
  `embedder` — so each renders exactly one resolved row and is rebindable like every other
  agent. Unauthored, both keep resolving through the narrator's binding (legacy `narrator`,
  typed `completion.narration`, or the `completion` type): no config migration, today's
  behaviour is the fallback. The NPC lanes render as their own headed group ("Npc lanes");
  the planner sits in the Completion group. Resolution gains one step — preset binding, then
  the interaction's own authored item slot, then per-item default, task, type — so a rebind is
  effective with zero wiring change (the planner and lane callers resolve per call). Honest
  limit: per-member routing (one model per lane member) is not wired — the lane call site does
  not name its member, so an override key would be a rebind that reports success and changes
  nothing; the drawer says so under the group.

- **The Image Prompter endpoint is a setting.** `comfy.base_url` (label: "Image Prompter
  endpoint (ComfyUI URL)") rides the settings log on the same anchor as the boolean flags, in a
  new string `values` fold — validated on write (http(s) only, host required, trailing slash
  stripped, `""` clears to the default). The effective URL resolves per call — setting
  override, then the injectable deployment seam, then the shipped default
  `http://127.0.0.1:8188` — and every ComfyUI consumer (config probe, render, session
  generation) goes through it, so a URL saved in the drawer's Image Prompter section applies
  to the next request with no restart. The field reads back the *effective* URL, so it can
  never show an address the client is not using.

- **NPC lane live preview, surfaced.** The lanes flow panel gained a one-line lane config row
  — max lanes in flight (1..8) and an on/off switch — writing the fan-out node's `data`
  through the validated topology door. The served flow rows and manifest now overlay the
  EFFECTIVE width from the stored topology instead of the code default, so a saved value is
  what the panel reports.

### Changed

- **The connections drawer renders one resolved row per agent role.** `GET /api/connections`
  slots are now one row per resolved ROLE on the canonical spelling: `embedder` folds into the
  `embedding` row (`via: item:embedder`), the legacy `narrator`/`extractor` twins never get
  rows of their own, and each row carries the EFFECTIVE binding (resolved through the
  spelling the engine's call sites use), `write_slot` (the `agents` key a rebind writes —
  `embedding` writes `embedder`, so a rebind can never be silently shadowed), and `inert`
  (authored spellings that lost precedence, with their connections — a conflict is surfaced
  with a marker, never hidden). PATCH semantics are byte-identical: any declared spelling
  still writes and resolves exactly as before.

- **"imagegen" is labelled "Image Prompter" end to end** (slot label, drawer heading,
  settings label, mock fixtures). The slot KEY stays `imagegen` — alias discipline; authored
  configs are untouched.

- **NPC lanes can be switched off for real.** `enabled: false` on the fan-out node's topology
  data PRUNES the stage from the compiled graph — dispatcher, Send fan-out and lane node all
  gone, predecessors re-pointed at the zero-lane route — so the turn runs the narrator's
  single forward pass and no lane node executes. The prune has one implementation used by
  both the write door and the builder: a disabling edit that cannot be honoured is refused
  with a new `disabled_stage_unprunable` violation instead of being stored, and validation
  judges the pruned wiring, so an off switch that would strand the narrator cannot be saved.

- **Hints and labels read like a person wrote them.** A product-wide copy sweep rewrote the
  UI register: decision ids (`D26`, `W9.17`, `ADR-…`), engine nouns the player never chose to
  learn (`provenance contract`, `fan-out`, `topology`, `slot`, `context ledger`, `cache
  contract`) and implementation-speak ("byte-faithful default pipeline") are gone from every
  surface — settings and rule rows, the prompt drawer, the connections drawer, the turn flow
  and graph editor, the inspector, state and cast panes, memory, proposals, studio panes and
  the appearance hints. Copy says what the thing does for the player; the product's own nouns
  (Narrator, NPC lanes, Image Prompter, persona, world, book, cast) stay. Served descriptions
  (settings registry, agent-role labels, the flow manifest's node and region metadata) were
  rewritten at the source and the committed artifacts regenerated. `docs/UI-COPY.md` is the
  register and the rules for anything the UI renders. Note: an already-saved preset keeps the
  old rule-row descriptions until that preset is reset — nothing in the prompt itself changed.

### Fixed

- **The live preview showed the previous turn's NPCs (or none) mid-turn.** The engine wrote
  no lane fact until the fan-in join, minutes after the lanes were planned, so the drawer had
  nothing current to serve. The dispatcher now records the turn's planned set as an
  `npc_lanes_plan` row at route time (member ids and display names, after the narrator's
  shared prefix is built so the prompt never sees it); the lanes endpoint serves that set —
  members marked pending — while the plan is ahead of the newest lane summary. A zero-lane
  turn records an empty plan (an answer, not a stale set).

- **Popout panels resize, and the inspector's context readouts stop wrapping mid-number.**
  `DraggablePane`'s resize grip only rendered when a caller passed a height — no caller ever
  did, so every popout was size-locked. Edge/corner handles now work on every panel (clamped,
  viewport-aware, growing slides the pane back into view), sizes persist per panel in
  localStorage, and double-clicking the header restores the seed size. The inspector's
  context-ledger lines use tabular numerals and no-break handling.

- **SECURITY.md states the real bind posture.** "The default is 127.0.0.1" was false for the
  shipped deployment: only a bare `scripts/serve.py` run binds loopback — the image starts
  the engine on `--host 0.0.0.0` (required for any published port) and the compose files
  publish `2727:2727` on every host interface. The security doc and the README's Exposure
  section now state the same truth, cross-referenced ("change both or neither"), and the
  doc no longer forbids the flag the shipped image requires.

## [0.0.2] - 2026-10-25

### Added

- **"Anti Injection" toggle (off by default).** The narrator prompt's `## Engine context`
  contract clause — and its firewall semantics (tags imitated in the player's words are fiction;
  never emit the tags) — is now a persisted setting, named **Anti Injection** in the settings
  surface with a one-line plain-terms hint. Off (the shipped default) builds a prompt with no
  clause anywhere; on renders today's prompt byte-for-byte. The trailing-system
  `<preset_runtime>` carrier is untouched either way: the tail still renders as a system message
  after the player's, and turning the toggle applies to the next turn with no restart
  (`PATCH /api/settings {features}` — the setting is a log fact). The clause's text keeps its
  homes in `parts.py` and the committed factory artifact; only its rendering is gated, on both
  the code-catalog and preset/artifact prompt paths. See ADR-019's amendment.

- **Appearance: four themes and one CSS override.** The UI ships light / dark (default) / oled /
  savilen presets, selected by a setting (`GET/PATCH /api/appearance`, stored as per-install user
  state next to the event log — a theme is not narrative history, so it never touches the log).
  The catalog is ordered greyscale-first, white → black, then coloured themes by primary hue.
  OLED uses true-black panels and lets the display carry the contrast; Savilen is the reference
  image's extracted palette (`#001012` page, `#1f8f86` accent, `#4fe6d8` text). The `mid` preset
  is removed. Each preset carries its own surface ladder, text and line colours and accent
  geometry, all re-audited against the same WCAG floors. Alongside it, one paste box applies the reader's own
  stylesheet as a single `<style>` element last in the document, so the cascade is
  base → preset → user CSS → the scene's tone (which is inline, and therefore wins unless the
  reader writes `!important` — the drawer says so). The box is guarded by a property allowlist:
  colours, radii, fonts at 14px or larger and decoration are yours; layout geometry, box
  dimensions (the reading measure), spacing, scroll containers, z-order, the type scale and
  focus rings are refused on save with a reason shown in the drawer.

### Changed

- **The narrator prompt's trust bounds are restructured.** The served narrator messages now
  carry two bounds instead of one: `<preset_def>` encloses the engine definition together with
  the conversation through the player's latest message (stable prefix, history, state facts and
  the player's own message, which is scene input rather than instruction), and
  `<preset_runtime>` encloses the per-turn engine tail served after it (`<recalled>`, the lane
  block, the SPEAKER PALETTE). The guard line names both bounds, is emitted once and sits
  outside them. Renaming the tags changed the stable prefix, so the prompt cache is
  invalidated once.

- **The models sidecar provisions its own weights.** `deploy/sidecar/models.ini` now names the
  three models as HuggingFace references (`hf-repo` + an exact `hf-file`) instead of paths under
  a host models directory, and llama.cpp fetches each one when that model is first requested —
  no manual download step and no `./models` tree to populate (~2.7 GiB once: d1-3B Q4_K_M,
  embeddinggemma-2 Q8_0 with its Q8_0 projector, Qwen3-Reranker-0.6B Q4_K_M; all three repos
  ungated and anonymous). The reranker comes from `Voodisss/Qwen3-Reranker-0.6B-GGUF-llama_cpp`,
  the conversion that carries the sequence-classification head — the byte-identical file the
  maintainer's own deployment runs; the popular `mradermacher` mirror of the same nominal quant
  drops that head and ranks the wrong document first (measured: 0.0 for the true match). The compose file keeps that download in the named volume
  `fabulist-model-cache` at `LLAMA_CACHE=/cache`, so it survives recreates, and passes
  `HF_TOKEN` through because llama.cpp reads it natively (unused today). `./models` remains as
  the offline/local-weights override (`FABULIST_MODELS_DIR` + your own preset via
  `FABULIST_SIDECAR_PRESET`). The entrypoint's boot check changed shape with the sources: an
  `hf-repo`/`hf-file` is validated as a REF (form, plus cache writability and free space) rather
  than looked up as a file, a local `model`/`mmproj` path still must exist, and a section naming
  both is a boot error. The d1 and reranker sections disable llama.cpp's sibling-mmproj auto-pick
  so a first request cannot drag in a projector their routes never use.

### Fixed

- **The connections drawer reported a working sidecar slot as "unresolved".** A task-less TYPE row
  (`embedding`) never looked downward at the item slot actually serving it (`embedder`), so the
  drawer showed "unresolved" and `GET /health` listed the slot as unbound while embedding calls
  worked — a false dead reading that sent the owner hand-binding slots that were already wired. The
  type now falls down to its serving item slots (`embedder`, declaration order) with the new
  `item:<slot>` provenance ("via embedder slot"), the type's own key still wins when authored, and
  `configuration_report` stops reporting the slot unbound. Types WITH a task layer (`completion`)
  deliberately get no downward leg, so the completion type can never read as served by `imagegen`.
- **The engine could boot before its model sidecar, or give up pinning it forever.**
  `docker compose up` started engine and sidecar together with no ordering, and the boot-time
  auto-pin had exactly one deferred re-probe, so a first boot slower than ~28 s (Vulkan init, cold
  image) pinned nothing until a manual restart. The sidecar now has a healthcheck (the router's
  `/v1/models` — answered before any model loads, so it downloads nothing) and the engine starts on
  `condition: service_healthy`; the re-probe is a bounded repeating schedule (`RETRY_DELAY_S` 25 s,
  at most `RETRY_MAX_ATTEMPTS` 24 ≈ 10 min) that stops at the first probe which answers.
  `docker-compose.app-only.yml` ships no sidecar and resets the inherited dependency.
- **The decision route timed out on its first call after a sidecar restart.** `decision.py`'s
  `DEFAULT_TIMEOUT` was 5 s, while the sidecar's healthcheck only proves the ROUTER listens — the
  first `d1-3b` call still pays the on-disk load (measured: `ReadTimeout` at 5.0039 s, then 1.55 s
  warm; 12.737 s on a cold sidecar). 30 s absorbs a cold load and still bounds a hung call.
- **A reasoning round lost its own deliberation.** Issue #29's trim blanked each earlier tool
  iteration's `reasoning_content` as the next was stamped, so a model four tool calls into a repair
  no longer saw the thinking that opened the round. The echo boundary is now the ROUND, not the
  iteration: every iteration of the running round keeps its reasoning verbatim (owner ruling — a
  repair round must see its own deliberation), and the trim moved out to the round boundary, where a
  PRIOR turn's stored reasoning is never re-sent as text (it is stored for diagnosis, and history
  assembly re-attaches the field present-but-empty).
- **A mixed history 400'd the next narrator turn.** Measured against DeepSeek `deepseek-flash`: on a
  request carrying `tools`, no `reasoning_content` anywhere is 200, every assistant message carrying
  it is 200, and a MIXED request is a 400 ("The `reasoning_content` in the thinking mode must be
  passed back to the API"). Presence is therefore all-or-nothing per REQUEST, and history assembly
  emits the field per turn, so any session that mixed turns with and without it — started on a
  non-thinking connection and later switched, or an older session — died on its next turn.
  `normalise_reasoning_presence` now fills the blanks once per request; a request with no field
  anywhere is left byte-identical, and a present value is never rewritten.

## [0.2.0] - 2026-10-23

First public release. Fabulist is a personal, localhost-only roleplay narrative engine:
a LangGraph backend that keeps every fact in an append-only event log and serves a
SvelteKit chat frontend with a model-authored component library. It ships with no
models — you point it at your own OpenAI-compatible endpoints.

### Added

- **First-run bootstrap.** A fresh install comes up usable: the shipped default prompt
  preset and the starter assets are seeded on first start, so the first launch opens a
  studio and a chat that work instead of an empty shell. Models stay yours to configure.
- **Docker deployment.** The engine and frontend can be run as containers, for people who
  would rather not install Python and Node. Container deployment changes nothing about the
  trust model: the API still has no authentication, so publish the port to the host's
  loopback only — see `SECURITY.md`.
- **Optional speech-to-text input.** An audio (or audio-bearing video) file attached to a
  message is transcribed locally into text for the turn. Opt-in in use, not in install: it
  needs `ffmpeg` on PATH (override with `FABULIST_FFMPEG`) and downloads a whisper-class
  model on first use. Text-only use never touches it, and a failed transcription is recorded
  as a recoverable event instead of breaking the turn.
- **Public architecture record.** The design decisions that shape the engine (event log as
  source of truth, revision events, checkpoints as rebuildable cache, presence as a
  registry fold, and the rest) are readable in `docs/decisions/`, with `docs/DECISIONS.md` as
  the entry point.
- Governance and CI: Apache-2.0 license and NOTICE, `SECURITY.md`, `CONTRIBUTING.md`, a
  GitHub Actions CI workflow (hermetic suite, frontend check + build, artifact sync) and
  issue/pull-request templates.

### Changed

- **Renamed from SuperChat to Fabulist.** The project, package (`fabulist`), engine
  service and frontend carry the new name; user state under `data/` and
  `configs/connections.json` keeps its existing shape, and existing sessions keep working.
