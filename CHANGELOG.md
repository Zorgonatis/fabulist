# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
