#!/usr/bin/env python
"""Seed the out-of-the-box SEGFAULT persona + assistant (Bug 4).

Owner directive: a fresh install ships a working assistant. The preset of record
(``~/Projects/Stabs-EDH``, the user's SillyTavern pack) carries SEGFAULT as a
*prompt* (``║ SEGFAULT (Dumb AI)``) rather than a standalone character-card
file — the tree has ``assets/segfault.png`` (an avatar, no ``chara`` chunk) and
no card JSON. This script therefore derives a Character Card V2 object from that
prompt's ``<SEGFAULT_DEFINITION>`` block and imports it through the standard
definition-only persona path. It writes nothing to a side table (D9).

Steps (all HTTP; never touches ``data/`` or SQLite directly)
------------------------------------------------------------
1. Read the preset JSON, find the prompt carrying ``<SEGFAULT_DEFINITION>``,
   extract the definition text.
2. ``POST {api}/api/cards/import?persona=1`` with the derived card — the
   definition-only import (parses + ``upsert_persona_from_card``, creates NO
   session). Name-keyed, so re-importing never duplicates the persona.
3. ``POST {api}/api/assistants`` with an assistant whose ``actor`` references
   the persona_id (a REFERENCE, not a clone), ``trigger`` manual, and
   ``style_notes`` distilled from the preset's Extra Assistant contract
   (proactive scene moves, panel/DSL usage per the narrator contract, concise
   prose).

Idempotent: when a persona named SEGFAULT and an assistant referencing it (or
named SEGFAULT) already exist, the script prints the skip and sends no
state-mutating request. ``--dry-run`` prints the plan and writes nothing.

Usage::

    uv run python scripts/seed_defaults.py                      # apply (:2727)
    uv run python scripts/seed_defaults.py --api http://127.0.0.1:8017
    uv run python scripts/seed_defaults.py --dry-run

``--preset`` overrides the auto-discovered preset file.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

import httpx

#: Default engine base URL (the script is HTTP-first; never writes the log file).
DEFAULT_API = "http://127.0.0.1:2727"

#: Where the user's SillyTavern pack lives.
PRESET_DIR = Path.home() / "Projects" / "Stabs-EDH"

#: Preset filenames to consider, newest-mtime first (the pack keeps several).
PRESET_GLOB = "Stabs-*Directives*.json"

#: The character this seed materializes.
CHARACTER_NAME = "SEGFAULT"

#: Markers delimiting the persona definition inside the preset prompt.
DEFINITION_START = "<SEGFAULT_DEFINITION>"
DEFINITION_END = "</SEGFAULT_DEFINITION>"

#: Avatar URL line inside the prompt (kept verbatim as provenance; not required).
AVATAR_RE = re.compile(r"AvatarURL:\s*(\S+)")

#: Behavior instructions distilled from the preset's "Extra Assistants" contract
#: (``╔══ 𝗘𝘅𝘁𝗿𝗮 𝗔𝘀𝘀𝗶𝘀𝘁𝗮𝗻𝘁 (prompt)``): proactive moves, the panel/DSL output
#: contract, concise prose. This is the assistant's ``style_notes`` slot.
ASSISTANT_STYLE_NOTES = (
    "Distilled from the preset's Extra Assistant contract:\n"
    "- Stay wholly in SEGFAULT's voice: friendly, over-confident, wildly "
    "inaccurate, scattered with emoji / kaomoji / ASCII art that somehow lands "
    "at the right moment.\n"
    "- React to the current scene only: one fresh line of in-voice commentary, "
    "addressing the user directly. Never advance the shared narrative state and "
    "never write for other characters.\n"
    "- Make proactive scene moves: offer three to six suggested replies phrased "
    "as the user would send them, each with a one-word tag and an optional note; "
    "exactly one carries emphasis: true — the move that best fits SEGFAULT.\n"
    "- Keep the prose concise and markdown-free.\n"
    "- Drive the panel: end every reply with the single asst = AssistantCard(...) "
    "statement the card contract below specifies."
)


def resolve_preset(explicit: str | None) -> Path:
    """Return the preset JSON to read: the explicit path, else the newest match."""
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_file():
            raise SystemExit(f"preset not found: {path}")
        return path
    candidates = candidate_presets()
    if not candidates:
        raise SystemExit(
            f"no preset JSON matched {PRESET_GLOB!r} under {PRESET_DIR}; "
            "pass --preset <path>"
        )
    return candidates[0]


def candidate_presets() -> list[Path]:
    """Preset files under the pack, newest mtime first."""
    return sorted(
        (p for p in PRESET_DIR.glob(PRESET_GLOB) if p.is_file()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )


def load_preset_definition(explicit: str | None) -> tuple[Path, str, str]:
    """Pick the newest preset that carries the SEGFAULT definition.

    The pack keeps several preset files; a newer one that does not carry the
    character must not break the seed, so candidates are tried newest-first and
    the first one with the ``<SEGFAULT_DEFINITION>`` prompt wins. Returns
    ``(preset_path, definition, avatar_url)``.
    """
    if explicit:
        path = resolve_preset(explicit)
        preset = json.loads(path.read_text(encoding="utf-8"))
        definition, avatar = find_definition(preset, path)
        return path, definition, avatar

    candidates = candidate_presets()
    if not candidates:
        raise SystemExit(
            f"no preset JSON matched {PRESET_GLOB!r} under {PRESET_DIR}; "
            "pass --preset <path>"
        )
    for path in candidates:
        try:
            preset = json.loads(path.read_text(encoding="utf-8"))
            definition, avatar = find_definition(preset, path)
        except (json.JSONDecodeError, SystemExit):
            continue
        return path, definition, avatar
    raise SystemExit(f"no preset under {PRESET_DIR} carries {DEFINITION_START}")


def find_definition(preset: dict, preset_path: Path) -> tuple[str, str]:
    """Extract ``(definition_text, avatar_url)`` from the preset's SEGFAULT prompt."""
    prompts = preset.get("prompts")
    if not isinstance(prompts, list):
        raise SystemExit(f"{preset_path}: no 'prompts' list (not an ST preset?)")
    for prompt in prompts:
        content = prompt.get("content") if isinstance(prompt, dict) else None
        if not isinstance(content, str) or DEFINITION_START not in content:
            continue
        start = content.index(DEFINITION_START) + len(DEFINITION_START)
        end = content.index(DEFINITION_END, start)
        definition = content[start:end].strip()
        avatar = AVATAR_RE.search(content)
        return definition, (avatar.group(1) if avatar else "")
    raise SystemExit(f"{preset_path}: no prompt carries {DEFINITION_START}")


def build_card(definition: str, avatar_url: str, preset_path: Path) -> dict:
    """Build a Character Card V2 object carrying the SEGFAULT definition."""
    extensions: dict = {"source": f"preset:{preset_path.name}"}
    if avatar_url:
        extensions["avatarUrl"] = avatar_url
    return {
        "spec": "chara_card_v2",
        "spec_version": "2.0",
        "data": {
            "name": CHARACTER_NAME,
            "description": definition,
            "personality": "",
            "scenario": "",
            "first_mes": "",
            "creator": "Stabs-EDH preset",
            "character_version": preset_path.name,
            "extensions": extensions,
        },
    }


def fetch_personas(client: httpx.Client) -> list[dict]:
    """GET the persona catalog (``{"personas": [...]}``)."""
    res = client.get("/api/personas", headers={"Accept": "application/json"})
    res.raise_for_status()
    body = res.json()
    rows = body.get("personas") if isinstance(body, dict) else None
    return rows if isinstance(rows, list) else []


def fetch_assistants(client: httpx.Client) -> list[dict]:
    """GET the assistant catalog (``{"assistants": [...]}``)."""
    res = client.get("/api/assistants", headers={"Accept": "application/json"})
    res.raise_for_status()
    body = res.json()
    rows = body.get("assistants") if isinstance(body, dict) else None
    return rows if isinstance(rows, list) else []


def import_persona(client: httpx.Client, card: dict) -> dict:
    """Definition-only card import (``?persona=1``): parse + persona upsert."""
    res = client.post(
        "/api/cards/import",
        params={"persona": "1"},
        content=json.dumps(card).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    res.raise_for_status()
    body = res.json()
    if not isinstance(body, dict) or not body.get("ok"):
        raise SystemExit(f"persona import failed: {body!r}")
    persona = body.get("persona")
    if not isinstance(persona, dict) or not persona.get("persona_id"):
        raise SystemExit(f"persona import returned no persona: {body!r}")
    return persona


def create_assistant(client: httpx.Client, persona_id: str, definition: str) -> dict:
    """Create the assistant referencing ``persona_id`` (actor, manual, narrator)."""
    payload = {
        "name": CHARACTER_NAME,
        "persona": definition,
        "style_notes": ASSISTANT_STYLE_NOTES,
        "agent": "narrator",
        "trigger": "manual",
        "keywords": [],
        "actor": persona_id,
    }
    res = client.post(
        "/api/assistants",
        json=payload,
        headers={"Accept": "application/json"},
    )
    res.raise_for_status()
    body = res.json()
    assistant = body.get("assistant") if isinstance(body, dict) else None
    if not isinstance(assistant, dict):
        raise SystemExit(f"assistant create returned no definition: {body!r}")
    return assistant


def find_persona(personas: list[dict]) -> dict | None:
    """The persona named SEGFAULT, if it is already in the catalog."""
    for persona in personas:
        if str(persona.get("name") or "").strip().upper() == CHARACTER_NAME:
            return persona
    return None


def find_assistant(
    assistants: list[dict], persona_id: str | None
) -> dict | None:
    """An assistant referencing ``persona_id`` (actor) or named SEGFAULT."""
    for assistant in assistants:
        if persona_id and assistant.get("actor") == persona_id:
            return assistant
        if str(assistant.get("name") or "").strip().upper() == CHARACTER_NAME:
            return assistant
    return None


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--api",
        default=DEFAULT_API,
        help=f"engine base URL (default {DEFAULT_API})",
    )
    parser.add_argument(
        "--preset",
        default=None,
        help=f"preset JSON path (default: newest {PRESET_GLOB} under {PRESET_DIR})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the plan and write nothing",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    preset_path, definition, avatar_url = load_preset_definition(args.preset)
    card = build_card(definition, avatar_url, preset_path)
    print(f"[seed] preset: {preset_path}")
    print(f"[seed] SEGFAULT definition: {len(definition)} chars")

    with httpx.Client(base_url=args.api, timeout=60.0) as client:
        persona = find_persona(fetch_personas(client))
        assistant = find_assistant(fetch_assistants(client), (persona or {}).get("persona_id"))

        if persona is not None and assistant is not None:
            print(
                f"[seed] already seeded: persona {persona['persona_id']} + "
                f"assistant {assistant.get('id')!r} — nothing to do"
            )
            return 0

        if args.dry_run:
            print(
                "[seed] dry-run: would import persona "
                f"{'SEGFAULT (exists)' if persona else 'SEGFAULT'} and create the "
                f"SEGFAULT assistant ({'exists' if assistant else 'missing'})"
            )
            return 0

        if persona is None:
            persona = import_persona(client, card)
            print(f"[seed] persona created: {persona['persona_id']} ({persona.get('name')})")
        persona_id = str(persona["persona_id"])

        if assistant is None:
            assistant = create_assistant(client, persona_id, definition)
            print(
                f"[seed] assistant created: {assistant.get('id')!r} "
                f"(actor={assistant.get('actor')}, trigger={assistant.get('trigger')})"
            )
        else:
            print(f"[seed] assistant already references persona {persona_id} — skipped")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
