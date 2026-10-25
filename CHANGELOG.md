# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
