"""Experience-profile importer (D33): canonical-signature dedupe + supersede.

PLAN.md **D33** — the import half of the experience-profile envelope. Per entity
(a world, each cast persona, each book, the preset, and the assistant list) the
importer computes the **canonical signature** of the incoming definition and of
the local fold:

* signatures **identical** -> zero appends (idempotent re-import);
* **different or missing** -> exactly one supersede append, stamped with the
  import provenance ``{"origin": "profile:<name>", "imported_at": <iso>}``.

Last-import-wins, no merge: a changed definition replaces the folded one whole.
The stamp lives in the append's **event meta** (not the payload), so a definition
survives a round trip byte-for-byte and the signature is stable — which is what
makes a second import of the same envelope a no-op.

Agent bindings (D19/D33)
------------------------
The incoming preset's ``agent_bindings`` are validated against the **local**
connection registry's agent names (read via
:func:`fabulist.connections.client.configure` — used only to list names, never
exported) UNION the taxonomy's declared slot spellings (W25.5), exactly the known
set :func:`fabulist.prompts.agent_sets.validate_bindings` applies. A binding
naming an unknown agent is rewritten to
:data:`fabulist.prompts.agent_sets.DEFAULT_BINDINGS` for that interaction and a
warning ``{"type": "agent_binding", "interaction", "wanted", "fell_back_to"}``
is recorded (the ``type`` discriminator separates it from the ``preset_id``
warning). A typed spelling (``completion.extraction``) is a DECLARED slot, so it
is known, kept verbatim and never warned about.

Default preset (D26/D33)
------------------------
:data:`~fabulist.prompts.preset_store.DEFAULT_PRESET_ID` (``"default"``) is the
persisted default and must never be superseded by an import: a preset arriving
with that id is imported under a **new, deterministic** id
(``default-<profile-slug>``) and a warning is recorded, so the same envelope
re-imports idempotently.

Public API
----------
:data:`DEFAULT_CONNECTIONS_PATH`, :func:`import_profile`.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from typing import TYPE_CHECKING, Any

from fabulist.context.books import (
    BOOK_EVENT,
    BOOKS_ANCHOR,
    ENTRY_ID_KEY,
    book_by_id,
    book_to_wire,
    mint_entry_ids,
)
from fabulist.media.images import store_definition_image
from fabulist.personas.registry import (
    PERSONA_EVENT,
    PERSONAS_ANCHOR,
    list_personas,
)
from fabulist.prompts.agent_sets import DEFAULT_BINDINGS
from fabulist.prompts.preset_store import (
    DEFAULT_PRESET_ID,
    PRESETS_ANCHOR,
    get_preset,
    preset_to_json,
)
from fabulist.worlds.registry import (
    WORLD_EVENT,
    WORLDS_ANCHOR,
    world_by_id,
    world_to_wire,
)

from .schema import (
    FORMAT,
    FORMAT_VERSION,
    PROFILE_EXTRA_META_KEY,
    PROVENANCE_META_KEY,
    ASSISTANTS_ANCHOR,
    ASSISTANTS_EVENT,
    entity_signature,
    normalize_book,
    normalize_persona,
    normalize_preset,
    normalize_assistants,
    normalize_world,
    now_iso,
    persona_to_wire,
    assistant_definitions,
    unknown_envelope_keys,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fabulist.log import EventLog

__all__ = ["DEFAULT_CONNECTIONS_PATH", "import_profile", "persist_profile_image"]

#: Where the connection registry (§R5) lives by default — read only to list the
#: known agent names; the names are never written into a profile.
DEFAULT_CONNECTIONS_PATH = "configs/connections.json"

#: Actor stamped on import appends (mirrors the registries' human-intent actor).
ACTOR = "user"


def _as_mapping(value: Any, label: str) -> dict:
    """Coerce one definition to a dict, raising a precise ``ValueError``."""
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    return dict(value)


def _record(
    bucket: list[dict],
    kind: str,
    entity_id: str,
    *,
    name: str | None = None,
    count: int | None = None,
) -> None:
    """Append one ``{kind, id, ...}`` row to an ``imported``/``skipped`` bucket."""
    row: dict[str, Any] = {"kind": kind, "id": entity_id}
    if name is not None:
        row["name"] = name
    if count is not None:
        row["count"] = count
    bucket.append(row)


# -- local fold signatures ---------------------------------------------------


def _existing_world_signature(log: "EventLog", world_id: str) -> str | None:
    """Signature of the folded world, or ``None`` when absent/tombstoned."""
    world = world_by_id(log, world_id)
    return None if world is None else entity_signature("world", world_to_wire(world))


def _existing_persona_signature(log: "EventLog", persona_id: str) -> str | None:
    """Signature of the folded persona, or ``None`` when absent."""
    for persona in list_personas(log):
        if persona.persona_id == persona_id:
            return entity_signature("persona", persona_to_wire(persona))
    return None


def _unkeyed_book(wire: Mapping[str, Any]) -> dict[str, Any]:
    """The book wire with every minted :data:`ENTRY_ID_KEY` removed.

    The stable entry key is minted per write (a fresh ``uuid4``), so it must
    never enter a DEDUPE digest: a re-import of the same envelope has to be a
    no-op (D33), and the stored fold already carries the keys of its own write.
    Both sides of the comparison are therefore stripped — the incoming
    definition and the folded one — and the signature keeps meaning "the entry
    CONTENT is unchanged".
    """
    definition = dict(wire)
    entries = wire.get("entries")
    definition["entries"] = (
        [
            {
                key: value
                for key, value in entry.items()
                if key != ENTRY_ID_KEY
            }
            if isinstance(entry, Mapping)
            else entry
            for entry in entries
        ]
        if isinstance(entries, list)
        else []
    )
    return definition


def _existing_book_signature(log: "EventLog", book_id: str) -> str | None:
    """Signature of the folded book, or ``None`` when absent/tombstoned."""
    book = book_by_id(log, book_id)
    if book is None:
        return None
    return entity_signature("book", _unkeyed_book(book_to_wire(book)))


def _existing_preset_signature(log: "EventLog", preset_id: str) -> str | None:
    """Signature of the folded preset, or ``None`` when never defined."""
    preset = get_preset(log, preset_id)
    return None if preset is None else entity_signature("preset", preset_to_json(preset))


# -- per-entity import -------------------------------------------------------


def _import_book(log: "EventLog", raw: Any, meta: dict, result: dict) -> None:
    """Import one book definition: supersede on a changed signature (D33).

    Entries are keyed on the way in (:func:`mint_entry_ids`, W9.23 §3.4.3): this
    function appends ``BOOK_EVENT`` rows DIRECTLY rather than through
    :func:`fabulist.context.books.append_book` (it stamps the import provenance
    in event ``meta``, which that API does not take), so it is the one writer
    that has to mint for itself. The signature is computed over the UNKEYED
    definition on both sides (:func:`_unkeyed_book`), so minting never makes a
    re-import look like a change.
    """
    wire = normalize_book(_as_mapping(raw, "book definition"))
    book_id = wire["book_id"]
    if _existing_book_signature(log, book_id) == entity_signature(
        "book", _unkeyed_book(wire)
    ):
        _record(result["skipped"], "book", book_id, name=wire["name"])
        return
    keyed = dict(wire)
    keyed["entries"] = mint_entry_ids(wire.get("entries"))
    log.append_event(
        BOOKS_ANCHOR, "system", ACTOR, {"event": BOOK_EVENT, **keyed}, meta=meta
    )
    _record(result["imported"], "book", book_id, name=wire["name"])


def _import_persona(log: "EventLog", raw: Any, meta: dict, result: dict) -> None:
    """Import one persona definition: supersede on a changed signature (D33)."""
    wire = normalize_persona(_as_mapping(raw, "persona definition"))
    persona_id = wire["persona_id"]
    if _existing_persona_signature(log, persona_id) == entity_signature("persona", wire):
        _record(result["skipped"], "persona", persona_id, name=wire["name"])
        return
    log.append_event(
        PERSONAS_ANCHOR, "system", ACTOR, {"event": PERSONA_EVENT, **wire}, meta=meta
    )
    _record(result["imported"], "persona", persona_id, name=wire["name"])


def _import_world(log: "EventLog", entry: Any, meta: dict, result: dict) -> None:
    """Import a world bundle: its books, its cast, then the world itself.

    Books and cast are imported first so the world's ``book_ids`` / ``cast``
    references resolve in an otherwise empty log; each level is independently
    signature-checked, so changing one book appends exactly one book row and
    nothing else (D33).
    """
    bundle = _as_mapping(entry, "profile 'world'")
    books = bundle.get("books") or []
    if not isinstance(books, list):
        raise ValueError("world bundle 'books' must be a list")
    for book_def in books:
        _import_book(log, book_def, meta, result)

    cast = bundle.get("cast") or []
    if not isinstance(cast, list):
        raise ValueError("world bundle 'cast' must be a list")
    for persona_def in cast:
        _import_persona(log, persona_def, meta, result)

    world_def = bundle.get("world")
    if world_def is None:
        return
    wire = normalize_world(_as_mapping(world_def, "world bundle 'world'"))
    world_id = wire["world_id"]
    if _existing_world_signature(log, world_id) == entity_signature("world", wire):
        _record(result["skipped"], "world", world_id, name=wire["name"])
        return
    log.append_event(
        WORLDS_ANCHOR, "system", ACTOR, {"event": WORLD_EVENT, **wire}, meta=meta
    )
    _record(result["imported"], "world", world_id, name=wire["name"])


def _import_assistants(log: "EventLog", defs: Any, meta: dict, result: dict) -> None:
    """Import the assistant list: supersede the whole roster on a changed signature.

    The entity is the folded roster itself (:func:`assistant_definitions`), which
    now folds per-id CRUD events too (list snapshot / ``assistant_defined`` /
    ``assistant_deleted``), so the signature covers whatever the CRUD wrote; the
    supersede is still a single whole-roster snapshot append (last-import-wins,
    D33) written to the new anchor. A pre-rename log's roster lives on
    ``__sidekicks__`` (read by :func:`assistant_definitions`).
    """
    normalized = normalize_assistants(defs)
    existing = assistant_definitions(log)
    if entity_signature("assistants", existing) == entity_signature(
        "assistants", normalized
    ):
        _record(result["skipped"], "assistants", ASSISTANTS_ANCHOR, count=len(normalized))
        return
    log.append_event(
        ASSISTANTS_ANCHOR,
        "system",
        ACTOR,
        {"event": ASSISTANTS_EVENT, "definitions": normalized},
        meta=meta,
    )
    _record(result["imported"], "assistants", ASSISTANTS_ANCHOR, count=len(normalized))


# -- preset (+ agent bindings) -----------------------------------------------


def _slug(text: str) -> str:
    """ASCII lowercase, dash-separated slug of ``text`` (``profile`` fallback)."""
    dashed = "".join(c if (c.isascii() and c.isalnum()) else "-" for c in text)
    slug = "-".join(part for part in dashed.split("-") if part).lower()
    return slug or "profile"


def _remap_default_id(profile_name: str) -> str:
    """Deterministic new id for an imported preset that claimed ``"default"``.

    Deriving the id from the profile name (stable per envelope) keeps the import
    idempotent: re-importing the same envelope targets the same new id and finds
    it identical, appending nothing (D33/D26).
    """
    return f"{DEFAULT_PRESET_ID}-{_slug(profile_name)}"


def _rewrite_bindings(
    bindings: Mapping[str, Any], known_agents: set[str], result: dict
) -> dict[str, Any]:
    """Rewrite bindings naming unknown agents to :data:`DEFAULT_BINDINGS`.

    Returns a fresh mapping (the incoming preset is never mutated in place); one
    warning ``{interaction, wanted, fell_back_to}`` is recorded per rewrite. An
    interaction outside :data:`DEFAULT_BINDINGS` (no local fallback) is dropped
    from the result rather than kept pointing at a name this log cannot resolve.
    """
    rewritten: dict[str, Any] = {}
    for interaction, binding in bindings.items():
        agent = binding.get("agent") if isinstance(binding, Mapping) else None
        if isinstance(agent, str) and agent and agent not in known_agents:
            fallback = DEFAULT_BINDINGS.get(str(interaction))
            result["warnings"].append(
                {
                    "type": "agent_binding",
                    "interaction": str(interaction),
                    "wanted": agent,
                    "fell_back_to": fallback["agent"] if fallback else None,
                }
            )
            if fallback is not None:
                rewritten[str(interaction)] = dict(fallback)
            continue
        rewritten[str(interaction)] = binding
    return rewritten


def _resolve_known_agents(
    connections_path: str, known_agents: Collection[str] | None
) -> set[str] | None:
    """Known agent names: the registry's agents UNION the taxonomy's declared slots.

    ``known_agents`` (a test/embedding seam) short-circuits the file read.
    Otherwise the connections document is loaded via
    :func:`fabulist.connections.client.configure` purely to enumerate its
    ``agents`` keys. A missing/unreadable config degrades to ``None`` — the
    importer then leaves bindings untouched (it cannot tell "unknown" from
    "not configured here"), never inventing a rewrite.

    W25.5 (the W25.2 residual): the union with
    :func:`~fabulist.connections.taxonomy.declared_spellings` is the SAME known
    set :func:`~fabulist.prompts.agent_sets.validate_bindings` applies, so a
    preset binding a typed slot (``completion.extraction`` /
    ``completion.narration`` ...) imports VERBATIM even when the connections
    document authors no such key — resolution falls through the taxonomy at call
    time. A dotted spelling outside the declared vocabulary is still an unknown
    agent and still falls back with a warning.
    """
    from fabulist.connections.taxonomy import declared_spellings

    if known_agents is not None:
        return {str(name) for name in known_agents} | declared_spellings()
    try:
        from fabulist.connections.client import configure

        registry = configure(connections_path)
    except Exception:  # noqa: BLE001 - an unreadable config is not a fatal import
        return None
    known = {str(name) for name in (getattr(registry, "agents", {}) or {})}
    return known | declared_spellings()


def _import_preset(
    log: "EventLog",
    raw: Any,
    profile_name: str,
    known_agents: set[str] | None,
    meta: dict,
    result: dict,
) -> None:
    """Import the preset: remap ``default``, rewrite bindings, supersede on diff.

    Operates on the canonical payload dict (:func:`normalize_preset`) so the
    append stores exactly the form it signed; the two rewrites (default-id
    remap, unknown-agent fallback) edit only top-level keys, keeping the payload
    canonical.
    """
    payload = normalize_preset(_as_mapping(raw, "preset definition"))
    if payload["preset_id"] == DEFAULT_PRESET_ID:
        new_id = _remap_default_id(profile_name)
        # A re-imported default is a NEW, ordinary preset (never the persisted
        # default row, D26), so the is_default flag is dropped with the id.
        payload["preset_id"] = new_id
        payload["is_default"] = False
        result["warnings"].append(
            {
                "type": "preset_id",
                "wanted": DEFAULT_PRESET_ID,
                "assigned": new_id,
                "reason": "the default preset cannot be superseded; imported as a new preset",
            }
        )
    if known_agents is not None:
        bindings = payload.get("agent_bindings")
        payload["agent_bindings"] = _rewrite_bindings(
            bindings if isinstance(bindings, Mapping) else {}, known_agents, result
        )
    preset_id = str(payload["preset_id"])
    name = str(payload.get("name") or "")
    if _existing_preset_signature(log, preset_id) == entity_signature("preset", payload):
        _record(result["skipped"], "preset", preset_id, name=name)
        return
    log.append_event(PRESETS_ANCHOR, "system", ACTOR, payload, meta=meta)
    _record(result["imported"], "preset", preset_id, name=name)


# -- entry point -------------------------------------------------------------


def _assigned_definition(bundle: Any) -> dict[str, Any] | None:
    """The world-bundle definition that owns the profile's assigned image.

    Mirrors :func:`fabulist.profiles.image.assigned_image_refs` layer order: the
    world's ``world`` definition first, then each ``cast`` member in
    ``world.cast`` order — the first whose ``image_asset`` is a non-empty string.
    ``None`` when no layer carries a ref (the export route 404s for such a
    profile, so a faithful profile PNG always has one).
    """
    if not isinstance(bundle, Mapping):
        return None
    world = bundle.get("world")
    if _has_image_ref(world):
        return world  # type: ignore[return-value]
    cast = bundle.get("cast")
    if isinstance(cast, list):
        for member in cast:
            if _has_image_ref(member):
                return member  # type: ignore[return-value]
    return None


def _layer_with_ref(bundle: Any, ref: str) -> dict[str, Any] | None:
    """The layer whose ``image_asset`` is exactly ``ref`` (world, then cast).

    Definition refs are content addresses, so the body image's stored id names
    the exact layer that owned the assigned image at export — even when an
    earlier layer's ref dangled there and would otherwise win the positional
    walk.
    """
    if not isinstance(bundle, Mapping):
        return None
    world = bundle.get("world")
    if isinstance(world, dict) and world.get("image_asset") == ref:
        return world
    cast = bundle.get("cast")
    if isinstance(cast, list):
        for member in cast:
            if isinstance(member, dict) and member.get("image_asset") == ref:
                return member
    return None


def _has_image_ref(definition: Any) -> bool:
    """Whether ``definition`` is a mapping carrying a non-empty ``image_asset``."""
    return isinstance(definition, dict) and bool(definition.get("image_asset"))


def persist_profile_image(
    log: "EventLog",
    envelope: Mapping[str, Any],
    data: bytes,
    mime: str,
    *,
    note: str = "",
) -> str | None:
    """Store a profile PNG's body image; repoint the assigned layer's ref (D33/G).

    A profile PNG's body **is** the profile's assigned image
    (:mod:`fabulist.profiles.image`), so an import that decodes only the
    ``fabulist_profile`` chunk would keep the exporting log's content address —
    a ref that does not resolve on the target log. The artwork would then be
    unusable (``GET .../image`` 404) and a re-export would 404 with "no assigned
    image". This stores the body bytes as a sessionless definition asset
    (:func:`~fabulist.media.images.store_definition_image`; content-addressed,
    so re-imports dedupe) and rewrites the assigned layer's ``image_asset`` to
    the local id, in place, so the subsequent :func:`import_profile` persists a
    ref that resolves here.

    Returns the stored id, or ``None`` when the envelope has no assigned layer
    to repoint (``world: null`` carries no image) or ``data`` is empty.
    """
    if not data:
        return None
    bundle = envelope.get("world")
    fallback = _assigned_definition(bundle)
    if fallback is None:
        return None
    asset_id = store_definition_image(log, data, mime, note=note)
    # Prefer the layer whose ref IS this content address (the exact owner); fall
    # back to the positional walk for a non-content-address ref (legacy data).
    target = _layer_with_ref(bundle, asset_id) or fallback
    target["image_asset"] = asset_id
    return asset_id


def _validate_envelope(envelope: Any) -> None:
    """Reject anything that is not a well-formed v1 profile envelope."""
    if not isinstance(envelope, Mapping):
        raise ValueError("profile envelope must be an object")
    if envelope.get("format") != FORMAT:
        raise ValueError(f"not a {FORMAT!r} envelope")
    if envelope.get("format_version") != FORMAT_VERSION:
        raise ValueError(
            f"unsupported profile format_version: {envelope.get('format_version')!r}"
        )
    name = envelope.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("profile envelope requires a non-empty 'name'")


def import_profile(
    log: "EventLog",
    envelope: Mapping[str, Any],
    *,
    connections_path: str = DEFAULT_CONNECTIONS_PATH,
    known_agents: Collection[str] | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    """Import a v1 experience-profile envelope; return ``{imported, skipped, warnings}``.

    ``imported`` / ``skipped`` are ``{kind, id, name?/count?}`` rows (books
    first, then cast, then the world, then the preset, then assistants).
    ``warnings`` carries the agent-binding fallbacks and the default-preset remap
    (see the module docstring). Every append is stamped in event meta with the
    provenance ``{origin: "profile:<name>", imported_at}`` — the definition
    payloads are never modified, so the import is idempotent and round-trippable.

    ``connections_path`` locates the connection registry used to validate agent
    bindings; ``known_agents`` injects the set directly (tests / callers that
    already hold the registry). ``now`` overrides the import timestamp.
    """
    _validate_envelope(envelope)
    name = str(envelope["name"])
    meta = {
        PROVENANCE_META_KEY: {
            "origin": f"profile:{name}",
            "imported_at": now or now_iso(),
        },
        PROFILE_EXTRA_META_KEY: unknown_envelope_keys(envelope),
    }
    result: dict[str, list] = {"imported": [], "skipped": [], "warnings": []}

    world_entry = envelope.get("world")
    if world_entry is not None:
        _import_world(log, world_entry, meta, result)

    preset_entry = envelope.get("preset")
    if preset_entry is not None:
        known = _resolve_known_agents(connections_path, known_agents)
        _import_preset(log, preset_entry, name, known, meta, result)

    # The roster key: the new ``assistants`` key wins; a pre-rename envelope
    # carried the identical roster under ``sidekicks`` (absent ≠ empty for both).
    assistants_entry = envelope.get("assistants")
    if not isinstance(assistants_entry, list):
        assistants_entry = envelope.get("sidekicks")
    if isinstance(assistants_entry, list):
        _import_assistants(log, assistants_entry, meta, result)

    return result
