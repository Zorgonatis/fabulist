"""Cast macro for the prompt-module engine (D43 + D51 studio): names, snapshotted
into the STABLE PREFIX, with a live delta notice in the DYNAMIC end region.

The narrator prompt-module vocabulary (D18, :mod:`fabulist.prompts.variables`)
exposes ``{{char}}`` — the character(s) the narrator speaks for. D43 changes its
VALUE, not its mechanism: where ``{{char}}`` used to be the first card's name
only, it now renders **every actor of the session** as an ST-familiar
``[cast: x, y, z]`` string, in declaration order.

Why the value is FROZEN (prompt-cache protection — owner directive, do not thaw)
-------------------------------------------------------------------------------
``{{char}}`` feeds card body text and custom module surfaces, and the
card/persona block sits at the TOP of the prompt as session-stable context
(``static_prefix_parts``). Re-rendering it every turn would rewrite that prefix
and break the provider prompt cache on every turn (the D29/D34 cache-prefix
concern; the same reason all genuinely dynamic context lives at the END of the
prompt). So the cast value is **snapshotted into the session as fact on first
consumption** (:func:`ensure_cast_snapshot`: a ``cast_snapshot`` ``system``
event, latest-wins) and read verbatim thereafter — the static prefix stays
byte-identical for the whole life of the chat. New chats materialize the
*then-current* cast (the "drives new chats" half).

Studio edits stay LIVE via the DYNAMIC delta notice
---------------------------------------------------
Definitions (worlds / casts / personas) are edited in the Design Studio (D51) at
runtime and are expected to be *live* in effect. D32's ``world_ref`` is a
snapshot-at-creation, so an active session's materialized card events do NOT
follow a later world edit ("refresh cast" is deferred). Instead of rewriting the
cache-stable static block, the difference between the session's snapshotted cast
fact and the CURRENT world definition is surfaced as a dynamic notice in the
post-user ``<recalled>`` continuity region (:func:`cast_delta_notice`), so the
narrator sees and uses the change that turn. Keeping the static prefix stable is
exactly what makes the two halves compatible: cache safety *and* live edits.

The cast itself is read from the session's persisted ``system`` card events via
:func:`fabulist.context.roster.extract_cards` — the same source the roster
facts render from (D9: the log owns everything). ``extract_cards`` returns every
card event the session carries; the session model has no per-card tombstone, so
"active" is exactly that set (there is no add/remove-actor surface today).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Sequence

from fabulist.context.roster import extract_cards
from fabulist.log.degradation import append_preset_degraded

logger = logging.getLogger(__name__)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fabulist.log import EventLog

__all__ = [
    "CAST_SNAPSHOT_EVENT",
    "cast_delta_notice",
    "ensure_cast_snapshot",
    "render_cast",
]

#: System-event kind carrying the snapshotted cast string (latest-wins row).
CAST_SNAPSHOT_EVENT = "cast_snapshot"

#: Actor stamped on the memoisation append (engine housekeeping, like the
#: ``session_created`` / card-seed rows).
_SNAPSHOT_ACTOR = "system"


def render_cast(cards: Sequence[dict]) -> str:
    """Render card names as the ST-style ``[cast: a, b, c]`` string.

    Names come from the card dicts in declaration order (the order
    :func:`fabulist.context.roster.extract_cards` returns them); blank or
    missing names are skipped. No cards -> ``""`` (a sandbox chat has no cast).
    """
    names = [str(card.get("name") or "").strip() for card in cards]
    names = [name for name in names if name]
    if not names:
        return ""
    return "[cast: " + ", ".join(names) + "]"


def _last_snapshot(events: Sequence) -> str | None:
    """Latest ``cast_snapshot`` value on ``events`` (latest-wins), or ``None``."""
    stored: str | None = None
    for event in events:
        if event.type != "system" or not isinstance(event.payload, dict):
            continue
        if event.payload.get("event") != CAST_SNAPSHOT_EVENT:
            continue
        value = event.payload.get("cast")
        if isinstance(value, str):
            stored = value
    return stored


def ensure_cast_snapshot(log: "EventLog", session_id: str) -> str:
    """Return the session's frozen ``{{char}}`` cast string, evaluating once.

    Reads the session's latest ``system``/``cast_snapshot`` event and returns
    its stored value verbatim. When no snapshot exists yet it evaluates the
    cast from the session's persisted card events
    (:func:`fabulist.context.roster.extract_cards` -> :func:`render_cast`),
    appends exactly one ``cast_snapshot`` event, and returns that string. The
    write is idempotent: a second call — and every later turn — reads the
    stored value and never appends again.

    The freeze is deliberate prompt-cache protection (see the module docstring):
    the value feeds the STATIC prefix. Studio edits to a session's cast surface
    through :func:`cast_delta_notice` in the dynamic region, never here.

    Fork-aware (bug-wave fix): reads the inherited base chain
    (:meth:`EventLog.get_chain_events`) so a branch sees its parent's cards (and
    an inherited frozen snapshot) instead of freezing an EMPTY cast.
    """
    events = log.get_chain_events(session_id)
    snapshot = _last_snapshot(events)
    if snapshot is not None:
        return snapshot

    cast = render_cast(extract_cards(events))
    log.append_event(
        session_id,
        "system",
        _SNAPSHOT_ACTOR,
        {"event": CAST_SNAPSHOT_EVENT, "cast": cast},
    )
    return cast


def _cast_names(value: str) -> list[str]:
    """Names inside a ``[cast: a, b]`` string (verbatim ``value`` on no match)."""
    if value.startswith("[cast: ") and value.endswith("]"):
        body = value[len("[cast: ") : -1]
    else:
        body = value
    return [name.strip() for name in body.split(",") if name.strip()]


def _delta_notice(stored: str, live: str) -> str:
    """One-line notice naming the actors added/removed since the snapshot."""
    before = _cast_names(stored)
    after = _cast_names(live)
    added = [name for name in after if name not in before]
    removed = [name for name in before if name not in after]
    parts: list[str] = []
    if added:
        parts.append("added: " + ", ".join(added))
    if removed:
        parts.append("removed: " + ", ".join(removed))
    detail = "; ".join(parts) or "cast changed"
    now = live or "no active cast"
    if added and removed:
        action = (
            "Treat the added actor(s) as present and the removed actor(s) as no "
            "longer active this turn."
        )
    elif added:
        action = "Treat the added actor(s) as present this turn."
    else:
        action = "Treat the removed actor(s) as no longer active this turn."
    return f"Cast update — {detail}. The session's cast is now {now}. {action}"


def _world_delta_notice(log: "EventLog", session_id: str) -> str | None:
    """The D51 world-edit diff (existing behaviour), or ``None``.

    Compares the session's snapshotted cast fact (latest ``cast_snapshot`` in its
    chain) against the CURRENT cast derived from the live world definition the
    Design Studio edits: the session's latest ``world_ref`` world id folded from
    the ``__worlds__`` anchor, whose ``cast`` persona ids resolve to names via
    the ``__personas__`` anchor. Studio edits append to those anchors
    (latest-wins), so this reads the *current* definition at build time.

    Returns ``None`` — nothing to flag — when:

    * the session has no ``cast_snapshot`` yet (the first build persists the
      session's materialized cast; there is no fact to be stale against), or
    * the session carries no ``world_ref`` (a plain card chat's own card events
      ARE its definition — there is no separate live definition to diff), or
      the referenced world was tombstoned, or
    * the session's fact already matches the current definition.
    """
    events = log.get_chain_events(session_id)
    stored = _last_snapshot(events)
    if stored is None:
        return None

    world_id: str | None = None
    for event in events:
        if event.type != "system" or not isinstance(event.payload, dict):
            continue
        if event.payload.get("event") != "world_ref":
            continue
        value = event.payload.get("world_id")
        if isinstance(value, str) and value:
            world_id = value
    if world_id is None:
        return None

    from fabulist.personas.registry import list_personas
    from fabulist.worlds.registry import world_by_id

    world = world_by_id(log, world_id)
    if world is None:
        return None
    names = {persona.persona_id: persona.name for persona in list_personas(log)}
    live = render_cast([{"name": names.get(pid, "")} for pid in world.cast])
    if live == stored:
        return None
    return _delta_notice(stored, live)


def cast_delta_notice(log: "EventLog", session_id: str) -> str | None:
    """Dynamic END-of-prompt notice for everything the frozen prefix cannot know.

    Two independent sources, both hash-neutral and turn-local (the caller puts
    the result in the post-user ``<recalled>`` region, D22, so the provenance
    hash and replay stay byte-faithful):

    * **world edits** — the D51 diff between the session's snapshotted cast fact
      and the current world definition (:func:`_world_delta_notice`);
    * **roster growth (W7)** — admissions and renames learned during play
      (:func:`fabulist.state.cast.runtime_notice`). A rename does NOT rewrite
      the stable prefix (D43: "if the roster changes at turn 50, it shouldn't
      update what we told the model of the cast in turn 1"); it rides this
      notice instead, which is the correction channel.

    Returns ``None`` when neither source has anything to say, so a session with
    a frozen cast and no admits is byte-identical to the pre-W7 prompt.
    """
    parts: list[str] = []
    try:
        world_part = _world_delta_notice(log, session_id)
    except Exception as exc:  # noqa: BLE001 - the notice is advisory, never fatal
        # Issue #70: the cast-delta notice is a preset/context read; a failed
        # half must not look like "no change" on the prompt.
        logger.warning("cast delta notice: world diff failed", exc_info=True)
        append_preset_degraded(
            log,
            session_id,
            stage="cast_context",
            exc=exc,
            advice="the narrator's cast-change notice may be incomplete",
        )
        world_part = None
    if world_part:
        parts.append(world_part)
    try:
        from fabulist.state.cast import runtime_notice

        roster_part = runtime_notice(log, session_id)
    except Exception as exc:  # noqa: BLE001 - the notice is advisory, never fatal
        logger.warning("cast delta notice: roster growth failed", exc_info=True)
        append_preset_degraded(
            log,
            session_id,
            stage="cast_context",
            exc=exc,
            advice="the narrator's cast-change notice may be incomplete",
        )
        roster_part = None
    if roster_part:
        parts.append(roster_part)
    if not parts:
        return None
    return "\n\n".join(parts)
