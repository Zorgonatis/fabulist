"""Session tree (W8): ONE render-ready projection of everything a session is.

The tree is the reading surface the whole session reduces to — when the scene
opened and who moved the clock, what the room is doing, who is where, what is
carried, what is unfinished, what is metered, what is wrong with whom, what the
purse holds, and one node per cast member whose identity splits into three
SEPARABLE layers:

* ``participation`` — the roster's own facts (member id, display, aliases,
  provenance, status, tier, the persona card it was promoted to);
* ``definition`` — the card TEXT the session was seeded with (``None`` when the
  member never had a card: a BTS-discovered person has no definition yet);
* ``live`` — where they are right now, their meters, conditions, body and held
  items.

The split is the point: a member's definition must not be re-derived from their
live state, and a live state change must never look like a definition change.

Purity (the whole reason this module exists)
-------------------------------------------
:func:`session_tree` APPENDS NOTHING. It is a read over the same public folds
every other reader uses (``entities.registry``, ``cast.roster``,
``log.get_projection``, ``context.roster.extract_cards``), so calling it never
mints a member, never materialises a roster and never writes a row (D9/D11).
The API door (:func:`fabulist.api.state_api.get_tree`) is a thin wrapper.

Top-level keys, in THIS order
-----------------------------
``clock, scene, places, items, threads, relations, meters, conditions,
economy, cast, Player`` — pinned by :data:`TREE_KEYS` and the tests (the key
order is the render order).

Identity vs. display names
--------------------------
The tree is BOTH presentation and addressing (plan §2: "ids are the object
ids"), so every node carries two kinds of field and they never mix:

* ``id`` (and ``participation.member_id``) is a HANDLE — the real, unmodified
  registry id (``loc_…``/``itm_…``/``th_…``/``rel_…``/``cd_…``/``eco_…``/
  ``cm_…``). It is what the owner's doors take (``update_object``,
  ``retire_object``, ``move_being``, and the cast API's promote/retire/patch),
  so trimming or replacing it would leave the pane unable to act on the objects
  it lists. The UI shows names at rest and only reveals an id while that leaf
  is being edited.
* every DISPLAY field is a name, resolved from the roster plus the session
  persona: occupants, item holders, meter hosts/subjects, condition hosts,
  relation endpoints and labels, and the ``parent`` of a place. The player
  renders as ``Player (Name)`` (:func:`player_display`).

A field a human READS never carries a raw registry id. A key the roster cannot
resolve still renders as a readable, DISTINCT label (:func:`_unresolved_label`)
rather than a raw id, so an unmaterialised roster shows one row per distinct
person without putting a machine handle in a display slot.

The off-screen bucket is the one place node with no ``id``: it is the reserved
absence literal (always live, never retired), so there is nothing to address.

``meters`` carries no ids: a meter is addressed by ``(host, key)`` through
``set_attr``, not by its ``mt_`` handle.

Registry-first, legacy beneath (§11.1)
--------------------------------------
Weather/tone/forecast/irony/genre/tension read the registry's scene objects
first and fall back to the legacy ``world_state`` shim exactly where
:func:`fabulist.state.entities.flat_render` already does — one precedence rule
for every reader. ``forecast`` is the one exception with a pin: the flat fold's
``forecast`` is the D45 MANUAL HUD override, so the tree reports the pin's value
and ``pinned: true`` when it is set, and the model's scene trait only when the
owner has not pinned one (review R2: the pin and the trait never share a slot).

The clock
---------
``time``/``date``/``day`` are the flat fold's clock (the clock's own keys, D21),
``last_writer`` is the actor of the newest ``state-delta`` row that actually
changed ``time``, and ``proposal`` is the newest ``system``/``clock_proposal``
audit row (W9.32 §3.2). ``applied`` is true ONLY when the folded instant equals
the proposal's — a turn that crashed after the accept left an accepted proposal
the clock never took, and reporting that as applied would claim a time the
fiction does not have.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from fabulist.context.roster import extract_cards
from fabulist.state import cast, entities, presence
from fabulist.state.cast import canonical_key

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fabulist.log import EventLog

logger = logging.getLogger(__name__)

__all__ = [
    "CLOCK_PROPOSAL_EVENT",
    "PLAYER_NODE",
    "TREE_KEYS",
    "player_display",
    "session_tree",
]

#: The tree's keys, in render order.
TREE_KEYS: tuple[str, ...] = (
    "clock",
    "scene",
    "places",
    "items",
    "threads",
    "relations",
    "meters",
    "conditions",
    "economy",
    "cast",
    "Player",
)

#: The player's node key. The IDENTITY key inside it stays
#: :data:`fabulist.state.entities.PLAYER` (``"player"``); only the node's own
#: label is capitalised, because the tree is read by humans.
PLAYER_NODE = "Player"

#: The accepted-model-time audit row (W9.32 §3.2). A literal, like the other
#: state-layer event names: this layer never imports ``schema_gate``.
CLOCK_PROPOSAL_EVENT = "clock_proposal"

#: The cast layer's member-id prefix, dropped from the last-resort DISPLAY label
#: of a key the roster cannot resolve (never from ``participation.member_id``).
_MEMBER_PREFIX = "cm_"

#: The card fields the definition layer carries — the labels
#: :func:`fabulist.context.roster.persona_block` renders, name first.
_DEFINITION_FIELDS: tuple[str, ...] = ("name", "description", "personality", "scenario")

#: The card keys a member's ``source_id`` may name it by (a card dict carries no
#: id of its own today, so this is the shape test rather than one spelling).
_CARD_ID_FIELDS: tuple[str, ...] = ("id", "card_id", "source_id")


def player_display(persona_name: object) -> str:
    """``Player (Name)`` — the player's label, bare ``Player`` with no persona.

    ONE spelling of "who is the player" for every display surface (the tree's
    ``Player`` node and the state API's ``beings`` map).
    """
    name = _text(persona_name)
    return f"Player ({name})" if name else "Player"


# -- small helpers -----------------------------------------------------------


def _text(value: object) -> str:
    """Tolerant text coercion (``None`` -> ``""``, everything else stripped)."""
    return str(value).strip() if value is not None else ""


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _unresolved_label(key: object) -> str:
    """The last-resort label for a being key the roster cannot resolve.

    A display field must never carry a raw registry id (``participation.
    member_id`` is the one field that does, on purpose), so an unmaterialised
    roster still renders a readable and DISTINCT word per key: the key itself,
    with the member minting prefix dropped.
    """
    text = _text(key)
    return text[len(_MEMBER_PREFIX) :] if text.startswith(_MEMBER_PREFIX) else text


def _state_fold(log: "EventLog", session_id: str) -> dict:
    """The fork- and revision-aware flat state fold (``get_projection``).

    The same source ``facts_from_log`` and the extractor feed, so the tree's
    clock and scene can never drift from what the narrator is told. An
    unreadable fold reads as empty rather than failing the whole tree.
    """
    try:
        state = log.get_projection(session_id).get("state") or {}
    except Exception:  # noqa: BLE001 - a projection failure is not a 500
        logger.warning(
            "could not fold state for session %r", session_id, exc_info=True
        )
        return {}
    return state if isinstance(state, dict) else {}


def _events(log: "EventLog", session_id: str) -> list[Any]:
    """The fork-aware raw chain, preferring the public walk (no private re-walk)."""
    walk = getattr(log, "get_chain_events", None)
    if callable(walk):
        return list(walk(session_id))
    return list(log.get_events(session_id))


def _instant(state: dict) -> str:
    """The folded clock as one ``YYYY-MM-DD HH:MM`` instant (``""`` when partial)."""
    date_text = _text(state.get("date"))
    time_text = _text(state.get("time"))
    if not date_text or not time_text:
        return ""
    return f"{date_text} {time_text}"


def _persona_name(log: "EventLog", session_id: str) -> str | None:
    """The session persona's display name — ONE resolution, never fatal.

    :func:`fabulist.state.presence._player_name` is that one resolution (the
    tracker, the cast guards and this tree all read it), so the tree renames the
    result instead of re-deriving it.
    """
    return (presence._player_name(log, session_id) or "").strip() or None


def _persona_ref(log: "EventLog", session_id: str) -> str | None:
    """The persona card assigned to this chat, if any (best-effort, never fatal)."""
    try:
        from fabulist.personas.registry import persona_for_chat

        return persona_for_chat(log, session_id) or None
    except Exception:  # noqa: BLE001 - an unreadable persona is "none assigned"
        logger.warning(
            "could not resolve the persona for session %r", session_id, exc_info=True
        )
        return None


def _display_names(log: "EventLog", session_id: str) -> dict[str, str]:
    """``member_id -> display name`` from the roster (a PURE fold: mints nothing)."""
    return {
        member.member_id: member.display_name
        for member in cast.roster(log, session_id)
        if member.member_id and member.display_name
    }


class _Labels:
    """A registry key -> display label resolver for the tree's display fields.

    The player is :func:`player_display`, the scene is its reserved literal, a
    roster member is their display name, and anything else falls back to
    :func:`_unresolved_label` (so an unresolvable ``cm_`` key still renders as a
    distinct, readable word instead of a raw registry id in a name slot).
    """

    def __init__(self, names: dict[str, str], persona_name: str | None) -> None:
        self._names = names
        self._persona = persona_name
        self._by_key: dict[str, str] = {}
        for display in names.values():
            key = canonical_key(display)
            if key:
                self._by_key.setdefault(key, display)

    def __call__(self, key: object) -> str | None:
        text = _text(key)
        if not text:
            return None
        if text == entities.PLAYER:
            return player_display(self._persona)
        if canonical_key(text) == canonical_key(entities.SCENE):
            return entities.SCENE
        found = self._names.get(text)
        if found:
            return found
        return self._by_key.get(canonical_key(text)) or _unresolved_label(text)


# -- clock -------------------------------------------------------------------


def _newest_proposal(events: list[Any]) -> dict | None:
    """The newest ``system``/``clock_proposal`` row as a plain dict, or ``None``."""
    found: dict | None = None
    for event in events:
        if getattr(event, "type", None) != "system":
            continue
        payload = getattr(event, "payload", None)
        if not isinstance(payload, dict):
            continue
        if payload.get("event") != CLOCK_PROPOSAL_EVENT:
            continue
        found = {
            "time": _text(payload.get("time")),
            "date": _text(payload.get("date")),
            "day": payload.get("day"),
            "evidence": _text(payload.get("evidence")),
            "advanced_minutes": payload.get("advanced_minutes"),
        }
    return found


def _last_time_writer(events: list[Any]) -> str | None:
    """The actor of the newest ``state-delta`` row that CHANGED ``time``.

    "Changed" is the fold's own semantics: a row whose ``time`` repeats the
    value already in force wrote nothing new, so it is not the clock's writer.
    """
    current = ""
    writer: str | None = None
    for event in events:
        if getattr(event, "type", None) != "state-delta":
            continue
        payload = getattr(event, "payload", None)
        if not isinstance(payload, dict) or "time" not in payload:
            continue
        value = _text(payload.get("time"))
        if value and value != current:
            current = value
            writer = _text(getattr(event, "actor", "")) or None
    return writer


def _clock(state: dict, events: list[Any]) -> dict:
    """The clock node: the folded instant, who moved it, and the open proposal.

    ``applied`` compares the folded instant with the proposal's. ``day`` is
    compared too when the fold carries one (the accept path stamps all three
    keys, the rules clock stamps date+day) — the same day spelled as text on
    both sides, because one key keeps one spelling.
    """
    time_text = _text(state.get("time"))
    date_text = _text(state.get("date"))
    day_text = _text(state.get("day"))
    proposal = _newest_proposal(events)
    applied = False
    if proposal is not None:
        applied = (
            proposal["time"] == time_text
            and proposal["date"] == date_text
            and (not day_text or _text(proposal["day"]) == day_text)
        )
    return {
        "time": time_text,
        "date": date_text,
        "day": day_text or None,
        "last_writer": _last_time_writer(events),
        "proposal": proposal,
        "applied": applied,
    }


# -- scene -------------------------------------------------------------------


def _scene_number(reg: entities.Registry, key: str) -> int | float | None:
    """The scene's own numeric meter for ``key`` (``tension``), or ``None``."""
    wanted = canonical_key(key)
    for meter in reg.meters.values():
        if meter.host != entities.SCENE or canonical_key(meter.key) != wanted:
            continue
        if _is_number(meter.value):
            return meter.value
    return None


def _scene(reg: entities.Registry, state: dict, conditions: list[dict]) -> dict:
    """What the room is doing: the scene's traits, its tension, its conditions.

    Registry scene objects win; the legacy flat fold fills ``weather``/``tone``
    and the legacy ``tension`` dict fills ``irony``/``tension`` — the same
    precedence :func:`fabulist.state.entities.flat_render` uses (§11.1).
    ``forecast`` honours the D45 manual pin before the model's own trait.
    """
    traits = entities.scene_traits(reg)
    legacy = reg.legacy
    pin = _text(state.get("forecast"))
    tension = _scene_number(reg, "tension")
    if tension is None and _is_number(legacy.tension.get("level")):
        tension = legacy.tension["level"]
    return {
        "weather": traits.get("weather")
        or _text(state.get("weather"))
        or legacy.flat.get("weather", ""),
        "tone": traits.get("tone")
        or _text(state.get("tone"))
        or legacy.flat.get("tone", ""),
        "forecast": {
            "value": pin or traits.get("forecast", ""),
            "pinned": bool(pin),
        },
        "irony": traits.get("irony") or _text(legacy.tension.get("irony")),
        "genre": traits.get("genre") or _text(state.get("genre")),
        "tension": tension,
        "conditions": conditions,
    }


# -- places / inventory / plot -----------------------------------------------


def _occupants(reg: entities.Registry, loc_id: object, labels: _Labels) -> list[str]:
    """Who is in a place, by display name, in assignment order."""
    names = (labels(who) for who in entities.occupants(reg, loc_id))
    return [name for name in names if name]


def _places(reg: entities.Registry, labels: _Labels) -> dict:
    """The location tree (id/name/parent/occupants) plus the off-screen bucket.

    A node carries its ``id`` for ADDRESSING (``update_object``/``retire_object``
    take it) beside the names a human reads: ``parent`` is the parent's NAME
    (``None`` at a root, and the store allows a dangling parent, which renders
    ``None`` rather than an id). The bucket is separate because it is not a room
    — it is the absence of one, with the optional ``at`` reference it hangs off
    (§3.1/req. 2) — and it carries no ``id``: the reserved absence literal is
    always live and never retired, so there is nothing to address.
    """
    tree: list[dict] = []
    for loc_id, location in reg.locations.items():
        if loc_id == entities.OFF_SCREEN_LOCATION_ID:
            continue
        parent = reg.locations.get(_text(location.parent_id))
        tree.append(
            {
                "id": loc_id,
                "name": location.name,
                "parent": parent.name if parent is not None else None,
                "path": entities.location_path(reg, loc_id),
                "note": location.note,
                "occupants": _occupants(reg, loc_id, labels),
            }
        )
    bucket = reg.locations.get(entities.OFF_SCREEN_LOCATION_ID)
    at_ref = _text(reg.off_screen_ref)
    return {
        "tree": tree,
        "off_screen": {
            "name": (bucket.name if bucket is not None else "") or entities.OFF_SCREEN_NAME,
            "at": entities.location_path(reg, at_ref) if at_ref else None,
            "occupants": _occupants(reg, entities.OFF_SCREEN, labels),
        },
    }


def _item_nodes(reg: entities.Registry, labels: _Labels) -> list[dict]:
    """Every item with its id (addressing) and its holder by display name."""
    return [
        {
            "id": item.id,
            "name": item.name,
            "holder": labels(item.holder),
            "note": item.note,
        }
        for item in reg.items.values()
    ]


def _thread_nodes(reg: entities.Registry) -> list[dict]:
    return [
        {
            "id": thread.id,
            "name": thread.name,
            "status": thread.status,
            "detail": thread.detail,
            "note": thread.note,
        }
        for thread in reg.threads.values()
    ]


def _relation_label(edge: entities.Relationship, labels: _Labels) -> str:
    """The edge's label, rebuilt from display names when it is the store's default.

    ``establish_relationship`` synthesizes ``"from→to"`` as the rendering label
    when the writer gives none, and those ends are registry KEYS — rendering that
    stored default verbatim would put raw ids back on a surface that promises
    names. A label the writer actually wrote is prose and renders unchanged.
    """
    stored = _text(edge.name)
    if stored and stored != f"{_text(edge.source)}\u2192{_text(edge.target)}":
        return stored
    return f"{labels(edge.source) or ''}\u2192{labels(edge.target) or ''}"


def _relation_nodes(reg: entities.Registry, labels: _Labels) -> list[dict]:
    """The prose edges with their id, both endpoints by display name, in order."""
    return [
        {
            "id": edge.id,
            "from": labels(edge.source),
            "to": labels(edge.target),
            "label": _relation_label(edge, labels),
            "note": edge.note,
        }
        for edge in reg.relationships.values()
    ]


# -- meters / conditions / economy -------------------------------------------


def _meter_row(meter: entities.Meter, labels: _Labels) -> dict:
    row: dict[str, Any] = {"key": meter.key, "value": meter.value}
    subject = labels(meter.subject)
    if subject:
        row["subject"] = subject
    return row


def _split_meters(meters: list[entities.Meter], labels: _Labels) -> dict:
    """One host's rows split into NUMBERS (meters) and TEXT (traits)."""
    numbers: list[dict] = []
    traits: list[dict] = []
    for meter in meters:
        (numbers if _is_number(meter.value) else traits).append(_meter_row(meter, labels))
    return {"numbers": numbers, "traits": traits}


def _meter_groups(reg: entities.Registry, labels: _Labels) -> list[dict]:
    """Every NON-scene attribute, grouped by host label, first-appearance order.

    The scene's own rows are the ``scene`` branch's (§2: ``meters`` is every
    non-scene attribute) — rendering them here as well would show the owner the
    same ``weather``/``tone``/``tension`` row twice. Grouping is by LABEL, so a
    key the roster resolves and the same host spelled as a name do not render as
    two people (the D38 lesson).
    """
    groups: dict[str, dict] = {}
    for meter in reg.meters.values():
        host = labels(meter.host) or ""
        if host == entities.SCENE:
            continue
        group = groups.get(host)
        if group is None:
            group = groups[host] = {"host": host, "numbers": [], "traits": []}
        bucket = group["numbers"] if _is_number(meter.value) else group["traits"]
        bucket.append(_meter_row(meter, labels))
    return list(groups.values())


def _condition_node(
    condition: entities.Condition, expired_ids: set[str], labels: _Labels
) -> dict:
    """One condition with its id, host label and the fold's own expiry verdict."""
    return {
        "id": condition.id,
        "host": labels(condition.host),
        "key": condition.key,
        "severity": condition.severity,
        "source": condition.source,
        "expires_at": condition.expires_at or None,
        "expired": condition.id in expired_ids,
    }


def _economy_node(reg: entities.Registry) -> dict | None:
    purse = entities.economy_of(reg)
    if purse is None:
        return None
    return {
        "id": purse.id,
        "denomination": purse.denomination,
        "balance": purse.balance,
        "note": purse.note,
    }


# -- beings: the live layer --------------------------------------------------


def _presence(reg: entities.Registry, who: str, assignment: str, off_screen: bool) -> str:
    """Registry-first presence: ``in_scene``/``elsewhere``/``off_screen``/``unknown``.

    In-scene is OBJECT EQUALITY with the player's assignment (§6/req. 13); a
    being with no assignment at all is ``unknown`` — the registry fold has no
    answer, and inventing one here would be a second presence rule (§8's legacy
    walk is the one that answers for those).
    """
    if not assignment:
        return "unknown"
    if off_screen:
        return "off_screen"
    return "in_scene" if entities.same_room(reg, who) else "elsewhere"


def _held_items(reg: entities.Registry, who: str) -> list[str]:
    """The names of the items ``who`` holds (holder compared canonically)."""
    key = canonical_key(who)
    return [
        item.name
        for item in reg.items.values()
        if item.name and key and canonical_key(item.holder) == key
    ]


def _body_node(reg: entities.Registry, who: str) -> dict | None:
    body = entities.body_for(reg, who)
    if body is None:
        return None
    return {
        "pose": body.pose,
        "clothing": list(body.clothing),
        "surface": body.surface,
        "note": body.note,
    }


def _live(
    reg: entities.Registry, who: str, labels: _Labels, expired_ids: set[str]
) -> dict:
    """A being's live layer: where they are, and their meters/conditions/body/items.

    Shared verbatim by the cast nodes and the ``Player`` node — the player is a
    being in the registry, not a special case with its own reads.
    """
    assignment = _text(reg.assignments.get(who))
    off_screen = assignment in (entities.OFF_SCREEN, entities.OFF_SCREEN_LOCATION_ID)
    location = entities.location_path(reg, assignment) if assignment and not off_screen else ""
    meters = [
        meter
        for meter in reg.meters.values()
        if canonical_key(meter.host) == canonical_key(who)
    ]
    return {
        "location": location or None,
        "off_screen": off_screen,
        "presence": _presence(reg, who, assignment, off_screen),
        "meters": _split_meters(meters, labels),
        "conditions": [
            _condition_node(condition, expired_ids, labels)
            for condition in entities.conditions_for(reg, who)
        ],
        "body": _body_node(reg, who),
        "items": _held_items(reg, who),
    }


# -- cast --------------------------------------------------------------------


def _card_for(member: cast.CastMember, cards: list[dict]) -> dict | None:
    """The card this member was seeded from: by ``source_id``, else canonical name.

    ``source_id`` names the card when the admit knew it; a card dict carries no
    id of its own today, so the id is matched against every id-shaped key a card
    may hold. The name fallback is the member's CANONICAL DISPLAY NAME (the seed
    loop's own basis), never an alias: an alias match would hand one person
    another person's definition.
    """
    if member.source_id:
        for card in cards:
            for field in _CARD_ID_FIELDS:
                if _text(card.get(field)) == member.source_id:
                    return card
    wanted = canonical_key(member.display_name)
    if not wanted:
        return None
    for card in cards:
        if canonical_key(card.get("name")) == wanted:
            return card
    return None


def _definition(member: cast.CastMember, cards: list[dict]) -> dict | None:
    """The member's card TEXT, or ``None`` for a person who never had a card."""
    card = _card_for(member, cards)
    if card is None:
        return None
    return {field: _text(card.get(field)) for field in _DEFINITION_FIELDS}


def _participation(
    member: cast.CastMember, promoted: dict[str, str]
) -> dict:
    """The roster's own facts about a member — nothing derived from live state.

    ``member_id`` is the tree's ONE identity field: the real, joinable ``cm_…``
    id the cast API's promote/retire/patch doors take. Every other place this
    person appears is a display name.
    """
    return {
        "member_id": member.member_id,
        "display": member.display_name,
        "aliases": list(member.aliases),
        "provenance": member.provenance,
        "source_id": member.source_id,
        "status": member.status,
        "tier": cast.member_tier(member),
        "promoted_persona_id": promoted.get(member.member_id),
    }


def _cast_nodes(
    log: "EventLog",
    session_id: str,
    reg: entities.Registry,
    cards: list[dict],
    labels: _Labels,
    expired_ids: set[str],
) -> list[dict]:
    """One node per roster member (retired included — ``status`` says so)."""
    members = cast.roster(log, session_id)
    if not members:
        return []
    # Lazy: the promotion lane folds the whole persona catalog, which an empty
    # cast never needs (the router imports this module on every request).
    from fabulist.state.promotion import promoted_persona_ids

    promoted = promoted_persona_ids(log)
    return [
        {
            "participation": _participation(member, promoted),
            "definition": _definition(member, cards),
            "live": _live(reg, member.member_id, labels, expired_ids),
        }
        for member in members
    ]


def _player_node(
    reg: entities.Registry,
    labels: _Labels,
    persona_name: str | None,
    persona_ref: str | None,
    expired_ids: set[str],
) -> dict:
    """The player: its identity (the reserved key + label) and its live layer."""
    node: dict[str, Any] = {
        "identity": {
            "key": entities.PLAYER,
            "display": player_display(persona_name),
        },
        "live": _live(reg, entities.PLAYER, labels, expired_ids),
    }
    if persona_ref:
        node["persona_ref"] = persona_ref
    return node


# -- the projection ----------------------------------------------------------


def session_tree(log: "EventLog", session_id: str) -> dict:
    """The whole session as one render-ready dict (see the module docstring).

    A pure read: the registry fold, the flat state fold, the raw chain, the cast
    roster, the persona and the seeded cards. Nothing is appended and no roster
    is materialised, so this is safe to call on a session no producer has run in
    yet (it answers an empty tree, not an error).
    """
    reg = entities.registry(log, session_id)
    state = _state_fold(log, session_id)
    events = _events(log, session_id)
    persona_name = _persona_name(log, session_id)
    labels = _Labels(_display_names(log, session_id), persona_name)
    expired_ids = {
        condition.id
        for condition in entities.expired_conditions(reg, _instant(state))
    }
    return {
        "clock": _clock(state, events),
        "scene": _scene(
            reg,
            state,
            [
                _condition_node(condition, expired_ids, labels)
                for condition in entities.conditions_for(reg, entities.SCENE)
            ],
        ),
        "places": _places(reg, labels),
        "items": _item_nodes(reg, labels),
        "threads": _thread_nodes(reg),
        "relations": _relation_nodes(reg, labels),
        "meters": _meter_groups(reg, labels),
        "conditions": [
            _condition_node(condition, expired_ids, labels)
            for condition in reg.conditions.values()
        ],
        "economy": _economy_node(reg),
        "cast": _cast_nodes(log, session_id, reg, extract_cards(events), labels, expired_ids),
        "Player": _player_node(
            reg, labels, persona_name, _persona_ref(log, session_id), expired_ids
        ),
    }
