# BTS-labeled extractor eval fixtures (W2.12a)

Ten hand-authored cases that pin the post-turn extractor (PLAN D2) against a small
oracle. One JSON file per case; the rig (`scripts/eval_extractor.py`) loads every
`*.json` here in filename order.

The expected values are grounded in the **BTS v3.0.1 directive text conventions**
(`~/Projects/Stabs-EDH/Behind-the-Scenes-BTS-Directive.md`) and the preset's
Env Tracking / Tone State vocabulary, mapped into the log-boundary schema from
PLAN D2:

```
Δ[time: 14:35 | loc: night market | weather: light rain | tone: tense→warm | inv: +lantern]
```

## Schema (one case per file)

| Field | Type | Required | Meaning |
|-------|------|----------|---------|
| `id` | string | yes | Stable slug, no whitespace, unique across fixtures |
| `description` | string | yes | One-line human summary of the beat being tested |
| `user_msg` | string | yes | The user's turn text handed to the extractor |
| `turn` | string | yes | The narrator's prose for the turn (may be empty) |
| `expected_delta` | object | yes | Expected delta as `{key: value}`; may be `{}` for no change |
| `expected_action_class` | string | yes | One of `dialogue`, `inspection`, `travel`, `task`, `rest`, `other` |
| `notes` | string | no | Grounding / rationale for the expected values |

Delta keys are the fixed schema vocabulary (`time`, `loc`, `weather`, `tone`,
`inv`); only keys that changed in the turn appear. `Δ[]` is represented as `{}`.

## Value conventions (oracle)

- **time** — normalized from the preset's Env Tracking `h:mm tt` display to 24h
  `HH:MM` (e.g. `2:35 PM` -> `14:35`).
- **loc** — the bare place token behind BTS `@place` notation (e.g. `@night_market`
  -> `night market`).
- **weather** — a lowercase descriptor as Env Tracking would carry it
  (e.g. `light rain`, `downpour`).
- **tone** — a Tone State register name (Bleak / Tense / Warm / Absurd / Reverent /
  Frenetic / Melancholic), lowercased; a shift is rendered `old→new` using BTS's
  trend arrow.
- **inv** — BTS `+item` / `-item` signs; a gain and a loss in one turn share the
  key, space-separated (`+lantern -torch`).

The clock-derived `time` that `post_turn` folds into the persisted delta is **out
of scope** here: the rig calls the extractor directly, so expected deltas contain
only what the turn text states.

## Coverage map

| File | Beat |
|------|------|
| `01_first_scene.json` | first-scene time + loc + weather set |
| `02_location_change.json` | location change only |
| `03_time_advance.json` | time advance only |
| `04_tone_shift.json` | tone shift |
| `05_inventory_add_remove.json` | inventory `+item` / `-item` |
| `06_weather_only.json` | weather-only change |
| `07_ooc_no_delta.json` | OOC, no delta |
| `08_junk_input.json` | junk input (`asdf`; sibling probe `test`), no delta, prose-agnostic |
| `09_multi_key_turn.json` | multi-key turn (loc + time + inv) |
| `10_no_change.json` | no-change turn |

## Running

```
uv run python scripts/eval_extractor.py --list          # list case ids
uv run python scripts/eval_extractor.py                 # stub mode (no network)
uv run python scripts/eval_extractor.py --model live    # real extractor (opt-in)
uv run python scripts/eval_extractor.py --json          # machine-readable report
```

Accuracy is informational at this stage; `tests/test_eval_rig.py` asserts report
structure and determinism, not a particular score.
