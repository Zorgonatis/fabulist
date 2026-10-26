# Changelog

All notable changes to this project are documented in this file.

One line per change, with a reference to the closed issue. The issue thread carries the
detail; this file is an index, not a report. Versioning follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- A missing event log announces itself at boot instead of silently recreating empty (#72)
- Regenerate forks the current session at the message acted on, never a stale pivot (#74)
- A failed preset or context read warns and leaves a `preset_degraded` notice instead of a silent wrong-model turn (#70)
- A lane that hits a provider error retries twice, then degrades; the turn still commits (#76)
- Regenerate after a failed turn forks at the failed turn's user message, not the previous one (#80)

### Changed

- Anti Injection OFF now removes the guard line and wrapping bounds too; content-framing tags stay (#77)
- The Engine context clause names every block's tag wherever it arrives, spells out the validation-notice case, and tells the narrator to read tags, never write them (#75)

## [0.0.4] - 2026-10-26

### Added

- A fork carries the whole session state into the new chat (#62)
- The sidecar warms every model at container start; no turn pays the download (#64)

### Changed

- The engine tail is never a system message: planner and BTS directives ride an assistant carrier (#67, #68, #69)
- A central guard refuses any system message after the first message
- The Studio opens popped out and larger instead of docked narrow (#65)
- One import door for cards: every import control runs one handler (#60)

### Fixed

- NPC and proposal lanes keep their brief on the engine carrier (#73)
- Swiping back no longer strands the newer branch; the swipe group is computed at the fork pivot (#59)
- Macros render in the transcript and in the opening message (#63)
- A long state tab scrolls inside its own column (#66)
- The post-turn notice list starts collapsed (#61)

## [0.0.3] - 2026-10-25

### Added

- Planner and NPC lanes are mappable from the connections pane (#55)
- The Image Prompter endpoint is a setting (#57)
- Lane config row in the flow panel: max lanes in flight and on/off (#56)

### Changed

- One resolved row per agent role in the connections drawer (#48)
- "imagegen" is labelled "Image Prompter" end to end
- NPC lanes can be switched off for real: off prunes the stage from the compiled graph (#56)
- Hints and labels read like a person wrote them (#58)
- NPC lane configuration lives where NPCs are managed: cast pane quick options plus a studio page (#56)

### Fixed

- The NPC lane preview shows the current turn's planned set, not the previous turn's
- Popout panels resize; the inspector's context readouts stop wrapping mid-number (#49)
- SECURITY.md states the real bind posture (#54)

## [0.0.2] - 2026-10-25

### Added

- "Anti Injection" toggle, off by default (#39)
- Appearance: four themes and one CSS override (#37)

### Changed

- The narrator prompt's trust bounds are restructured into `<preset_def>` and `<preset_runtime>` (#44)
- The models sidecar provisions its own weights from HuggingFace refs

### Fixed

- The connections drawer no longer reports a working sidecar slot as "unresolved"
- The engine waits for a healthy sidecar and keeps retrying the auto-pin (#36)
- The decision route no longer times out on its first call after a sidecar restart
- A reasoning round keeps its own deliberation (#29)
- A mixed reasoning history no longer 400s the next narrator turn (#42)
