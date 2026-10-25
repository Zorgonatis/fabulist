"""Experience-profile exporter (D33): read the anchor folds, build the envelope.

PLAN.md **D33** — the export half of the experience-profile envelope. It reads
**only the five anchor sessions** (``__worlds__`` / ``__personas__`` /
``__books__`` / ``__presets__`` / ``__assistants__``) through their public
registries, so it is structurally incapable of touching a chat session: there is
no code path here that names a non-anchor session, and every log read is
``log.get_events(<anchor>)``.

What a profile carries
----------------------
* ``world`` — either ``null`` (a preset + assistants profile) or a bundle::
      {"world": world_wire,
       "cast":  [persona_wire, ...],   # resolved from world.cast, in order
       "books": [book_wire, ...]}      # world.book_ids + each cast member's
  The cast and book definitions are **inlined** so the bundle is self-contained:
  importing it into an empty log reproduces the world without any other lookup.
* ``preset`` — :func:`~fabulist.prompts.preset_store.preset_to_json`, the full
  preset (modules + per-interaction flows + toggles + agent_bindings + surfaces).
* ``assistants`` — the ``__assistants__`` fold, normalized; OMITTED unless
  requested (``include_assistants`` / ``?assistants=1``) — absent ≠ empty on
  import (owner 2026-10-11): an absent key never clears a local roster.
  Built-in demo placeholders are never exported.

Forward-compat
--------------
When the exported world (or preset, or assistant list) was itself imported from a
profile, the import's unknown top-level envelope keys are recovered from the
entity's event meta (:data:`~fabulist.profiles.schema.PROFILE_EXTRA_META_KEY`)
and laid back onto the new envelope — a future-format profile survives an
import -> export cycle (D33).

Public API
----------
:func:`export_profile`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fabulist.context.books import book_by_id, book_to_wire
from fabulist.personas.registry import list_personas
from fabulist.prompts.preset_store import PRESETS_ANCHOR, get_preset, preset_to_json
from fabulist.worlds.registry import WORLDS_ANCHOR, world_by_id, world_to_wire

from .schema import (
    PROFILE_EXTRA_META_KEY,
    ASSISTANTS_ANCHOR,
    assistant_definitions,
    new_envelope,
    persona_to_wire,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fabulist.log import EventLog
    from fabulist.worlds.registry import World

__all__ = ["export_profile"]


def _carried_extra(
    log: "EventLog",
    world_id: str | None,
    preset_id: str | None,
    include_assistants: bool,
) -> dict[str, Any]:
    """Unknown top-level envelope keys carried by the exported entities.

    Each imported profile stamps its unknown keys into the entity's event meta;
    scanning the matching anchor recovers them so a re-export reproduces a
    future-format envelope losslessly. Newest import wins per key; the merge
    order (world, preset, assistants) only matters when several profiles were
    imported and is otherwise arbitrary.
    """
    extra: dict[str, Any] = {}
    if world_id is not None:
        extra.update(_entity_extra(log, WORLDS_ANCHOR, "world_id", world_id))
    if preset_id is not None:
        extra.update(_entity_extra(log, PRESETS_ANCHOR, "preset_id", preset_id))
    if include_assistants:
        extra.update(_anchor_meta_extra(log, ASSISTANTS_ANCHOR))
    return extra


def _entity_extra(
    log: "EventLog", anchor: str, id_key: str, entity_id: str
) -> dict[str, Any]:
    """Newest import ``profile_extra`` on the ``entity_id`` rows of ``anchor``."""
    extra: dict[str, Any] = {}
    for event in log.get_events(anchor):
        payload = event.payload
        if not isinstance(payload, dict) or payload.get(id_key) != entity_id:
            continue
        found = _meta_extra(event.meta)
        if found:
            extra = found
    return extra


def _anchor_meta_extra(log: "EventLog", anchor: str) -> dict[str, Any]:
    """Newest import ``profile_extra`` anywhere on ``anchor``."""
    extra: dict[str, Any] = {}
    for event in log.get_events(anchor):
        found = _meta_extra(event.meta)
        if found:
            extra = found
    return extra


def _meta_extra(meta: Any) -> dict[str, Any]:
    """The ``profile_extra`` mapping on one event's meta, or ``{}``."""
    if not isinstance(meta, dict):
        return {}
    found = meta.get(PROFILE_EXTRA_META_KEY)
    return dict(found) if isinstance(found, dict) else {}


def _cast_personas(log: "EventLog", world: "World") -> list[Any]:
    """The world's cast, resolved from the persona fold (unknown ids skipped)."""
    by_id = {persona.persona_id: persona for persona in list_personas(log)}
    return [by_id[pid] for pid in world.cast if pid in by_id]


def _world_books(log: "EventLog", world: "World", cast: list[Any]) -> list[dict]:
    """Book wires for ``world.book_ids`` + every cast member's ``book_ids``.

    Deduped by ``book_id`` in first-seen order (world first, then cast order); a
    reference to a missing/tombstoned book is skipped rather than exported as a
    dangling id.
    """
    ordered_ids: list[str] = list(world.book_ids)
    for persona in cast:
        ordered_ids.extend(persona.book_ids)
    books: list[dict] = []
    seen: set[str] = set()
    for book_id in ordered_ids:
        if book_id in seen:
            continue
        seen.add(book_id)
        book = book_by_id(log, book_id)
        if book is not None:
            books.append(book_to_wire(book))
    return books


def _world_bundle(log: "EventLog", world: "World") -> dict[str, Any]:
    """The self-contained world bundle: world wire + inlined cast + books."""
    cast = _cast_personas(log, world)
    return {
        "world": world_to_wire(world),
        "cast": [persona_to_wire(persona) for persona in cast],
        "books": _world_books(log, world, cast),
    }


def _default_name(world: "World | None", preset_name: str) -> str:
    """Profile name when the caller does not supply one: world, else preset."""
    if world is not None and world.name.strip():
        return world.name
    if preset_name.strip():
        return preset_name
    return "profile"


def export_profile(
    log: "EventLog",
    *,
    name: str | None = None,
    world_id: str | None = None,
    preset_id: str | None = None,
    include_assistants: bool = False,
    exported_at: str | None = None,
) -> dict[str, Any]:
    """Build a definitions-only experience-profile envelope (D33).

    ``preset_id`` is required (every envelope carries a preset); ``world_id`` is
    optional (``world: null`` = a preset + assistants profile). An unknown id
    raises :class:`LookupError` with a precise message (the API maps it to a
    404). ``include_assistants`` folds the ``__assistants__`` roster into the
    envelope (opt-in, mirroring the ``?assistants=1`` query); when False the
    key is OMITTED entirely — importing such an envelope never touches the
    local roster. ``name`` defaults to
    the world's name, else the preset's. ``exported_at`` is injectable for
    deterministic round-trip comparisons.

    Reads only ``__worlds__`` / ``__personas__`` / ``__books__`` / ``__presets__``
    / ``__assistants__`` (falling back to the legacy ``__sidekicks__`` roster) —
    never a chat session.
    """
    if preset_id is None:
        raise ValueError("a profile requires a preset")

    world = None
    if world_id is not None:
        world = world_by_id(log, world_id)
        if world is None:
            raise LookupError(f"unknown world: {world_id!r}")

    preset = get_preset(log, preset_id)
    if preset is None:
        raise LookupError(f"unknown preset: {preset_id!r}")

    resolved_name = name or _default_name(world, preset.name)
    return new_envelope(
        resolved_name,
        preset=preset_to_json(preset),
        world=_world_bundle(log, world) if world is not None else None,
        assistants=assistant_definitions(log) if include_assistants else None,
        extra=_carried_extra(log, world_id, preset_id, include_assistants),
        exported_at=exported_at,
    )
