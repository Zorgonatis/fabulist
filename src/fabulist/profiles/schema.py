"""Experience-profile envelope schema (D33): shape + canonical signatures.

PLAN.md **D33** — *Experience profile envelope*: a portable, **definitions-only**
bundle that drops an entire experience (a world + its cast + their books, a
prompt preset, and the assistant roster) into any Fabulist log. The export reads
**only the anchor sessions** (``__worlds__`` / ``__personas__`` / ``__books__`` /
``__presets__`` / ``__assistants__``) and is therefore structurally incapable of
touching a chat session; the import compares each entity's definition against the
local fold by **canonical signature** and appends a supersede only when the
signatures differ (last-import-wins, no merge).

This module owns the envelope's *shape* and the one spelling of "canonical":
:func:`canonical_json` (sorted keys, no insignificant whitespace) hashed into an
:func:`entity_signature` namespaced by entity kind. The exporter
(:mod:`fabulist.profiles.exporter`) and importer
(:mod:`fabulist.profiles.importer`) both build their definitions through the
normalizers here, so an incoming definition and the folded entity it produces
always reduce to the same signature — that is what makes a re-import append
nothing.

Definition normalizers (per entity kind)
----------------------------------------
Each kind has a ``normalize_*`` that folds a loose wire dict into the canonical
payload the matching registry append stores:

* ``world`` -> :func:`normalize_world` (``world_to_wire(wire_to_world(...))``)
* ``persona`` -> :func:`normalize_persona` (mirrors ``append_persona`` coercion)
* ``book`` -> :func:`normalize_book` (``book_to_wire(wire_to_book(...))``)
* ``preset`` -> :func:`normalize_preset` (``preset_to_json(preset_from_json(...))``)
* ``assistants`` -> :func:`normalize_assistants` (mirrors ``assistants_api``)

The assistant fold (:func:`assistant_definitions`) mirrors
:func:`fabulist.api.assistants_api._fold_anchor` event-for-event: a whole-roster
list payload resets the fold, a per-id ``assistant_defined`` upserts one id
(latest-wins, D47), and a per-id ``assistant_deleted`` removes one id — so a
roster built through the CRUD writes is exported exactly as it folds.

``provenance`` rides verbatim on every definition (D31) — the importer never
rewrites it, so an exported entity survives a round trip byte-for-byte. The
import's own stamp (``origin`` / ``imported_at``) is event **meta**, not a
payload field, precisely so it cannot perturb a definition or its signature.

Public API
----------
:data:`FORMAT`, :data:`FORMAT_VERSION`, :data:`KNOWN_ENVELOPE_KEYS`,
:data:`ASSISTANTS_ANCHOR`, :data:`ASSISTANTS_EVENT`,
:data:`ASSISTANT_DEFINED_EVENT`, :data:`ASSISTANT_DELETED_EVENT`,
:func:`canonical_json`,
:func:`entity_signature`, :func:`new_envelope`, :func:`is_profile`,
:func:`unknown_envelope_keys`, :func:`persona_to_wire`, :func:`normalize_world`,
:func:`normalize_persona`, :func:`normalize_book`, :func:`normalize_preset`,
:func:`normalize_assistant`, :func:`normalize_assistants`,
:func:`assistant_definitions`.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from fabulist.context.books import book_to_wire, wire_to_book
from fabulist.personas.registry import Persona
from fabulist.prompts.preset_store import preset_from_json, preset_to_json
from fabulist.worlds.registry import wire_to_world, world_to_wire

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fabulist.log import EventLog

__all__ = [
    "FORMAT",
    "FORMAT_VERSION",
    "KNOWN_ENVELOPE_KEYS",
    "PROFILE_EXTRA_META_KEY",
    "PROVENANCE_META_KEY",
    "ASSISTANTS_ANCHOR",
    "ASSISTANTS_EVENT",
    "ASSISTANT_DEFINED_EVENT",
    "ASSISTANT_DELETED_EVENT",
    "canonical_json",
    "entity_signature",
    "is_profile",
    "new_envelope",
    "normalize_book",
    "normalize_persona",
    "normalize_preset",
    "normalize_assistant",
    "normalize_assistants",
    "normalize_world",
    "now_iso",
    "persona_to_wire",
    "assistant_definitions",
    "unknown_envelope_keys",
]

#: Envelope discriminator (first thing a reader checks).
FORMAT = "fabulist-profile"

#: Envelope schema version. v1 is the only version this lane writes or reads.
FORMAT_VERSION = 1

#: The top-level keys a v1 envelope defines. Any *other* top-level key is an
#: unknown key — an envelope from a future writer — and travels through import
#: (event meta) so a re-export reproduces it without loss (forward-compat,
#: mirroring the registries' "extras pass" stance, D4).
KNOWN_ENVELOPE_KEYS: frozenset[str] = frozenset(
    {
        "format",
        "format_version",
        "name",
        "exported_at",
        "world",
        "preset",
        "assistants",
        # Legacy pre-rename key, recognized (never emitted) so an old envelope
        # is not mistaken for a future-format one on import.
        "sidekicks",
    }
)

#: Event-meta key holding the import stamp (``{"origin", "imported_at"}``).
PROVENANCE_META_KEY = "provenance"

#: Event-meta key holding the envelope's unknown top-level keys (so the exporter
#: can carry them forward on a re-export).
PROFILE_EXTRA_META_KEY = "profile_extra"

#: Reserved session id anchoring assistant definitions. Duplicated as a literal
#: (with citation) rather than imported so this package never depends on the API
#: layer: :mod:`fabulist.api.assistants_api` folds ``assistants_defined`` (list),
#: ``assistant_defined`` (per id) and ``assistant_deleted`` (tombstone) events on
#: this session (``ASSISTANTS_SESSION_ID``).
ASSISTANTS_ANCHOR = "__assistants__"

#: Legacy (pre-rename) anchor session; read-only fallback for existing logs.
LEGACY_SIDEKICKS_ANCHOR = "__sidekicks__"

#: System-event kind carrying one assistant definition list (whole-roster
#: snapshot — resets the fold; the profile-import and legacy write shape).
ASSISTANTS_EVENT = "assistants_defined"

#: System-event kind upserting ONE assistant definition by ``id`` (latest wins;
#: the CRUD create/update write shape, D47).
ASSISTANT_DEFINED_EVENT = "assistant_defined"

#: System-event kind tombstoning ONE assistant ``id`` (removes it from the fold,
#: D9/D10 — never a row mutation).
ASSISTANT_DELETED_EVENT = "assistant_deleted"

#: Legacy system-event kind (read-only; the fold accepts it but never writes it).
LEGACY_SIDEKICKS_EVENT = "sidekicks_defined"

#: Payload keys an assistant definition list may hide behind (first present wins).
_ASSISTANT_DEFINITION_KEYS = ("definitions", "assistants", "sidekicks")


def now_iso() -> str:
    """UTC ISO-8601 timestamp (the exporter/importer's one clock spelling)."""
    return datetime.now(timezone.utc).isoformat()


def canonical_json(value: Any) -> str:
    """Canonical JSON of ``value``: sorted keys, no insignificant whitespace.

    The one spelling of "canonical" in this package (D33): both the signatures
    below and the round-trip equality checks reduce to this string, so a stable
    digest is a byte-level equality test across processes.
    """
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def entity_signature(kind: str, payload: Any) -> str:
    """sha256 signature of one entity definition, namespaced by ``kind``.

    ``payload`` is the canonical definition form for the entity — a world /
    persona / book wire dict, :func:`~fabulist.prompts.preset_store.preset_to_json`,
    or the normalized assistant list. ``kind`` is folded into the digest so
    identical payloads under different kinds never collide. Import compares this
    digest against the local fold's: equal -> zero appends; different or missing
    -> exactly one supersede (D33).
    """
    canonical = canonical_json({"kind": kind, "payload": payload})
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def unknown_envelope_keys(envelope: Mapping[str, Any]) -> dict[str, Any]:
    """The envelope's top-level keys this v1 reader does not define.

    Empty for a well-formed v1 envelope; non-empty when an envelope from a newer
    writer is imported. The importer stashes the result in event meta so a later
    export reproduces it (forward-compat, D33).
    """
    return {
        str(key): value
        for key, value in envelope.items()
        if key not in KNOWN_ENVELOPE_KEYS
    }


def is_profile(envelope: Any) -> bool:
    """True when ``envelope`` carries this lane's format + version."""
    return (
        isinstance(envelope, Mapping)
        and envelope.get("format") == FORMAT
        and envelope.get("format_version") == FORMAT_VERSION
    )


def new_envelope(
    name: str,
    *,
    preset: Mapping[str, Any],
    world: Mapping[str, Any] | None = None,
    assistants: list[dict] | None = None,
    extra: Mapping[str, Any] | None = None,
    exported_at: str | None = None,
) -> dict[str, Any]:
    """Build a v1 envelope, laying any unknown (future) keys back on top.

    ``extra`` carries an imported envelope's unknown top-level keys forward: they
    are copied *after* the known fields and never overwrite them, so a re-export
    of a future-format profile is lossless for the keys this reader ignores.
    ``exported_at`` is injectable so a test can compare two envelopes exactly.
    """
    envelope: dict[str, Any] = {
        "format": FORMAT,
        "format_version": FORMAT_VERSION,
        "name": name,
        "exported_at": exported_at or now_iso(),
        "world": dict(world) if world is not None else None,
        "preset": dict(preset),
    }
    # Absent ≠ empty (owner 2026-10-11): a missing ``assistants`` key is an
    # import NO-OP — never overwrite a local roster with nothing. Only an
    # explicit list (``[]`` included) carries upsert semantics. (The legacy
    # ``sidekicks`` key is accepted on import identically; it is never written.)
    if assistants is not None:
        envelope["assistants"] = list(assistants)
    if extra:
        for key, value in extra.items():
            if key not in KNOWN_ENVELOPE_KEYS:
                envelope[str(key)] = value
    return envelope


# -- coercion helpers (mirror the registries' tolerant field handling) -------


def _text(value: Any) -> str:
    """Coerce an optional field to a plain string (``None`` -> ``""``)."""
    if value is None:
        return ""
    return value if isinstance(value, str) else str(value)


def _str_list(value: Any) -> list[str]:
    """Coerce an optional id-list field to ``list[str]`` (missing -> ``[]``)."""
    if not isinstance(value, (list, tuple)):
        return []
    return [item if isinstance(item, str) else str(item) for item in value]


def _optional_str(value: Any) -> str | None:
    """Coerce an optional single id/ref to ``str | None`` (empty -> ``None``).

    Mirrors :func:`fabulist.personas.registry._optional_str` /
    :func:`fabulist.worlds.registry._optional_str`, so a definition normalized
    here and the folded entity it supersedes reduce to the same signature.
    """
    return value if isinstance(value, str) and value else None


def _mapping(value: Any) -> dict:
    """Coerce an optional mapping field to ``dict`` (non-object -> ``{}``)."""
    return dict(value) if isinstance(value, Mapping) else {}


# -- per-kind normalizers ----------------------------------------------------


def persona_to_wire(persona: Persona) -> dict[str, Any]:
    """Wire shape of one folded :class:`~fabulist.personas.registry.Persona`.

    Mirrors :func:`fabulist.api.personas_api._wire` field-for-field so a persona
    exported here is indistinguishable from the frontend/other-lane contract.
    """
    return {
        "persona_id": persona.persona_id,
        "name": persona.name,
        "description": persona.description,
        "personality": persona.personality,
        "scenario": persona.scenario,
        "first_mes": persona.first_mes,
        "book_ids": list(persona.book_ids),
        "alternate_greetings": list(persona.alternate_greetings),
        "extensions": dict(persona.extensions),
        "provenance": dict(persona.provenance),
        # T4a definition image ref, so a profile's cast layer can carry images
        # (item G). The ASSET itself is log-local and is NOT exported: a ref
        # that does not resolve on the importing log is skipped by
        # :func:`fabulist.profiles.image.assigned_image`.
        "image_asset": persona.image_asset,
    }


def normalize_world(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Canonical world wire dict (raises ``ValueError`` on a bad shape).

    Requires a non-empty ``world_id`` so the appended row can never fold out of
    the ``__worlds__`` catalog (the registry's fold rejects blank ids).
    """
    wire = world_to_wire(wire_to_world(dict(raw)))
    if not wire["world_id"]:
        raise ValueError("world requires a non-empty 'world_id'")
    return wire


def normalize_persona(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Canonical persona wire dict (raises ``ValueError`` on a bad shape).

    Mirrors :func:`fabulist.personas.registry.append_persona`'s coercion exactly
    (the same defaults and stringification), so a definition normalized here and
    appended there produce the identical payload — and thus the identical
    signature on a re-import.
    """
    if not isinstance(raw, Mapping):
        raise ValueError("persona must be an object")
    persona_id = raw.get("persona_id")
    name = raw.get("name")
    if not isinstance(persona_id, str) or not persona_id:
        raise ValueError("persona requires a non-empty 'persona_id'")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("persona requires a non-empty 'name'")
    return {
        "persona_id": persona_id,
        "name": name,
        "description": _text(raw.get("description")),
        "personality": _text(raw.get("personality")),
        "scenario": _text(raw.get("scenario")),
        "first_mes": _text(raw.get("first_mes")),
        "book_ids": _str_list(raw.get("book_ids")),
        "alternate_greetings": _str_list(raw.get("alternate_greetings")),
        "extensions": _mapping(raw.get("extensions")),
        "provenance": _mapping(raw.get("provenance")),
        # T4a definition image ref (see :func:`persona_to_wire`), coerced exactly
        # like :func:`fabulist.personas.registry.append_persona`.
        "image_asset": _optional_str(raw.get("image_asset")),
    }


def normalize_book(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Canonical book wire dict (raises ``ValueError`` on a bad shape).

    Entries are carried verbatim (unknown keys survive, D4/D31); a blank
    ``book_id`` is rejected because the registry fold would drop the row.
    """
    wire = book_to_wire(wire_to_book(dict(raw)))
    if not wire["book_id"]:
        raise ValueError("book requires a non-empty 'book_id'")
    return wire


def normalize_preset(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Canonical preset payload (the exact ``preset_to_json`` shape).

    ``preset_from_json`` mints an id when absent, so a hand-authored envelope
    without one still folds; exported presets always carry their id.
    """
    return preset_to_json(preset_from_json(dict(raw)))


def normalize_assistant(raw: Any) -> dict[str, Any] | None:
    """Canonical assistant definition, or ``None`` when malformed.

    Mirrors :func:`fabulist.api.assistants_api._coerce_definition`'s acceptance
    (``id`` / ``name`` / ``persona`` required, ``agent`` defaults ``narrator``,
    ``trigger`` defaults ``manual``, unknown ``trigger`` values are left for the
    manager to reject) so an entry that folds in one path folds identically in
    the other. ``actor`` (D47) is carried through and **absent stays absent**:
    a definition that names no actor folds to ``""`` — no default is invented.
    """
    if not isinstance(raw, Mapping):
        return None
    ident = raw.get("id")
    name = raw.get("name")
    persona = raw.get("persona")
    if not all(isinstance(value, str) and value for value in (ident, name, persona)):
        return None
    keywords = raw.get("keywords")
    if not isinstance(keywords, list):
        keywords = []
    return {
        "id": ident,
        "name": name,
        "persona": persona,
        "style_notes": _text(raw.get("style_notes")),
        "agent": _text(raw.get("agent")) or "narrator",
        "trigger": _text(raw.get("trigger")) or "manual",
        "keywords": [str(keyword) for keyword in keywords],
        "actor": _text(raw.get("actor")),
    }


def normalize_assistants(raw: Any) -> list[dict[str, Any]]:
    """Canonical assistant list: normalized entries, first id wins, bad dropped."""
    if not isinstance(raw, (list, tuple)):
        return []
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in raw:
        definition = normalize_assistant(entry)
        if definition is None or definition["id"] in seen:
            continue
        seen.add(definition["id"])
        normalized.append(definition)
    return normalized


# -- anchor folds (export reads + import local comparison) -------------------


def _definition_list_from_payload(payload: Any) -> list[Any] | None:
    """A whole-roster list carried by one event payload, or ``None``.

    A bare list, or a dict carrying ``definitions`` / ``assistants`` / the legacy
    ``sidekicks`` key (first present wins). Mirrors
    :func:`fabulist.api.assistants_api._definition_list_from_payload`.
    """
    if isinstance(payload, list):
        return payload
    if isinstance(payload, Mapping):
        for key in _ASSISTANT_DEFINITION_KEYS:
            value = payload.get(key)
            if isinstance(value, list):
                return value
    return None


def _fold_assistant_anchor(
    log: "EventLog", anchor: str
) -> tuple[dict[str, dict[str, Any]], bool]:
    """Fold one anchor into ``{id: normalized definition}`` + a defined flag.

    Mirrors :func:`fabulist.api.assistants_api._fold_anchor` event-for-event, at
    the wire level (the API fold works in ``AssistantDefinition`` objects): events
    are read in seq order (append-only log, D9/D10) — a list payload resets the
    roster (whole-roster snapshot, first id wins within the list); an
    ``assistant_defined`` event upserts one id (latest-wins); an
    ``assistant_deleted`` event removes one id. ``defined`` becomes ``True`` on any
    of the three (including a tombstone), so an anchor that only ever emptied its
    roster still counts as the roster definition and is not skipped.

    Deliberately a mirror, not an import of the API helper: that helper returns
    ``AssistantDefinition`` objects and is private, so reusing it would pull the
    FastAPI app module into this package (see :data:`ASSISTANTS_ANCHOR`); this
    module's fold is pinned to it by ``tests/test_profiles.py``.
    """
    roster: dict[str, dict[str, Any]] | None = None
    defined = False
    for event in log.get_events(anchor):
        if event.type != "system":
            continue
        payload = event.payload
        listed = _definition_list_from_payload(payload)
        if listed is not None:
            roster = {}
            for entry in listed:
                definition = normalize_assistant(entry)
                if definition is not None and definition["id"] not in roster:
                    roster[definition["id"]] = definition
            defined = True
            continue
        if not isinstance(payload, Mapping):
            continue
        kind = payload.get("event")
        if kind == ASSISTANT_DEFINED_EVENT:
            definition = normalize_assistant(payload)
            if definition is not None:
                if roster is None:
                    roster = {}
                roster[definition["id"]] = definition
                defined = True
        elif kind == ASSISTANT_DELETED_EVENT:
            defined = True
            ident = payload.get("id")
            if isinstance(ident, str) and ident and roster is not None:
                roster.pop(ident, None)
    return roster or {}, defined


def assistant_definitions(log: "EventLog") -> list[dict[str, Any]]:
    """The log's current assistant definitions, normalized (``[]`` when none).

    The local fold the importer signs against and the exporter emits — one
    spelling, so an imported list and the folded list it produces compare equal.
    ``__assistants__`` is read first, then the legacy ``__sidekicks__`` read-side
    shim; the built-in demo placeholders are deliberately ignored (a profile must
    never carry placeholders as if they were definitions).
    """
    for anchor in (ASSISTANTS_ANCHOR, LEGACY_SIDEKICKS_ANCHOR):
        roster, defined = _fold_assistant_anchor(log, anchor)
        if defined:
            return list(roster.values())
    return []
