"""The per-turn ENTITY PALETTE block for the BTS tracker's prompt tail (W9.29 §7).

The registry is what prevents conflation (req. 4: the store is dumb, duplicates are
legal) — so the model must SEE every live object, with its id, every turn::

    ENTITY REGISTRY — ids are permanent handles; cite ids exactly. Establish nothing that already exists.
    LOCATIONS: loc_off_screen off-screen (at: —) · loc_9f2c master bedroom · loc_4b7e bathroom
      > WHO IS WHERE — Player (Wanderer): loc_9f2c · cm_yara: loc_4b7e · cm_seraphine: off_screen
    METERS: mt_7d31 hostility cm_seraphine→player = 42 · mt_0b2a stress cm_yara = 55
    TRAITS: mt_51a0 mood cm_yara = wary · mt_4c02 weather scene = rain
    RELATIONSHIPS: rel_7c11 cm_seraphine→player — she has decided the newcomer is a project
    THREADS: th_1c8a [active] the slow courtship — Seraphine tests the newcomer's boundaries
    ITEMS: itm_55aa brass key (player)
    CONDITIONS: cd_2f90 suspicious cm_yara (moderate, expires 21:30) — source: the letter
    BODIES: bd_8c14 cm_yara: kneeling · clothes: housecoat, slippers · surface: faint bruise
    ECONOMY: eco_1d5b 85 credits
    (15 objects · 3 actions last turn)

This is the D85 speaker-palette pattern (:mod:`fabulist.prompts.palette`)
generalized to the tracker: a directive line, one entry per object, and — because
the BTS prompt is rebuilt per turn anyway — a deterministic content order.

Placement is the cache contract (AGENTS 6): the block is the LAST dynamic content,
appended runner-side after ``BtsRunner._messages()`` (today's tail-directive
pattern), never folded into the seeded prefix. The prefix stays byte-identical
turn to turn; a stored preset-flow customisation of the ``bts`` surface therefore
cannot silently drop the registry either.

Token discipline (§7, decision point §14.5)
-------------------------------------------
1. one entry per object, grouped by kind, **id first** (meters and traits are the
   same object kind — a trait is an ``mt_`` object too, so it renders id-first even
   though §7's sketch elides the id of the two traits it happens to show);
2. current values only — history lives in the log, never here;
3. each ENTRY is clipped at :data:`ENTITY_PALETTE_CLIP_CHARS` characters (notes
   elided), never the whole section: a long note must not crowd another object's
   id out of the model's view;
4. stable order = establish (seq) order — cache-friendlier than a name sort;
5. retired objects never appear (the fold tombstones them);
6. a footer counts the objects and last turn's actions;
7. **no cap** (req. 11): growth is observed, not rationed. The named later-ruling
   options are cap-with-LRU-retire, pin-by-reconciliation and summarization — none
   is designed here.

Legacy facts (§11.1, req. 15 — forward-only, no backfill)
--------------------------------------------------------
A pre-regime session carries its world state in the legacy ``world_state`` fold
with **no ids**. Those facts render name-only under a ``LEGACY`` marker so the
model can turn them into real objects instead of re-establishing the world from
scratch (the first ``move_being``/``set_attr`` supersedes per fact).

W9.32 §3.3 (conditions, bodies, the purse)
------------------------------------------
The three object kinds the tracker gained beside the eight of W9.29 render in the
same id-first shape, after the W9.29 sections (a legacy session shows none of them
until it establishes one), the player's assignment line names the persona when the
caller hands one in (``Player (Wanderer)`` — the player is the USER, never a cast
member, so the model is told whose body it is looking at), and an EXPIRED condition
never reaches here at all: the engine's clock sweep tombstones it
(:func:`fabulist.state.entities.expire_conditions`, D9 — the log owns the removal),
so the fold the caller hands in holds live conditions only.

Pure function: no log reads, no clock, no settings — the caller hands in the folded
:class:`~fabulist.state.entities.Registry`, the roster, last turn's action count
and (optionally) the persona's display name.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Sequence

from ..state.entities import OFF_SCREEN_LOCATION_ID, PLAYER, Registry

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fabulist.state.cast import CastMember

__all__ = [
    "ENTITY_PALETTE_CLIP_CHARS",
    "ENTITY_PALETTE_DIRECTIVE",
    "render_entity_palette",
]

#: The block's opening line: the directive the ids exist to enforce.
ENTITY_PALETTE_DIRECTIVE = (
    "ENTITY REGISTRY — ids are permanent handles; cite ids exactly. "
    "Establish nothing that already exists."
)

#: Per-entry clip (chars). A long note is elided rather than pushing the next
#: object's id out of view (decision point §14.5 rule 3).
ENTITY_PALETTE_CLIP_CHARS = 120

#: The marker for facts the legacy ``world_state`` fold holds (name-only, §11.1).
_LEGACY_MARKER = "LEGACY (name-only, no ids yet — establish a real object to track it)"


def _is_number(value: Any) -> bool:
    """Mirror of the registry's number test: a meter is a number, a trait is text."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _clip(text: str) -> str:
    """Clip one entry to :data:`ENTITY_PALETTE_CLIP_CHARS` (tail kept short)."""
    if len(text) <= ENTITY_PALETTE_CLIP_CHARS:
        return text
    return text[: ENTITY_PALETTE_CLIP_CHARS - 1].rstrip() + "…"


def _tail(*parts: str) -> str:
    """``" — a · b"`` for the non-empty parts, else ``""`` (one spelling)."""
    text = " · ".join(part for part in parts if part)
    return f" — {text}" if text else ""


def _section(label: str, entries: Sequence[str]) -> str | None:
    """``LABEL: a · b · c`` (clipped per entry), or ``None`` when empty."""
    if not entries:
        return None
    return f"{label}: " + " · ".join(_clip(entry) for entry in entries)


# -- one entry per object ----------------------------------------------------


def _location_order(registry: Registry) -> list[tuple[str, int]]:
    """Every non-bucket location as ``(id, depth)``, tree order (cycle-safe).

    Roots (no parent, or a parent the fold does not hold) come first in fold
    order; each child follows its parent indented one level. A parent cycle the
    store somehow accepted cannot hang this: a node is emitted once, and a node
    whose parent was never emitted is promoted to a root.
    """
    by_parent: dict[str, list[str]] = {}
    roots: list[str] = []
    for loc_id, location in registry.locations.items():
        if loc_id == OFF_SCREEN_LOCATION_ID:
            continue
        parent = str(location.parent_id or "")
        if parent and parent in registry.locations and parent != loc_id:
            by_parent.setdefault(parent, []).append(loc_id)
        else:
            roots.append(loc_id)

    ordered: list[tuple[str, int]] = []
    seen: set[str] = set()

    def walk(loc_id: str, depth: int) -> None:
        if loc_id in seen:
            return
        seen.add(loc_id)
        ordered.append((loc_id, depth))
        for child in by_parent.get(loc_id, ()):
            walk(child, depth + 1)

    for root in roots:
        walk(root, 0)
    # Anything unreachable from a root (a ring) is still shown, at depth 0.
    for loc_id in registry.locations:
        if loc_id != OFF_SCREEN_LOCATION_ID and loc_id not in seen:
            walk(loc_id, 0)
    return ordered


def _location_entry(reg: Registry, location: Any) -> str:
    if location.id == OFF_SCREEN_LOCATION_ID:
        ref = reg.off_screen_ref or "—"
        note = _tail(location.note)
        return f"{location.id} {location.name} (at: {ref}){note}"
    # A parent the fold no longer holds (retired) is NOT cited: the child renders
    # as a root, so the palette never points the model at a tombstoned id.
    parent = (
        f" (in: {location.parent_id})"
        if location.parent_id and location.parent_id in reg.locations
        else ""
    )
    return f"{location.id} {location.name}{parent}{_tail(location.note)}"


def _meter_entry(meter: Any) -> str:
    toward = f"→{meter.subject}" if meter.subject else ""
    return f"{meter.id} {meter.key} {meter.host}{toward} = {meter.value}"


def _relationship_entry(edge: Any) -> str:
    name = edge.name or f"{edge.source}→{edge.target}"
    detail = _tail(edge.note) if edge.note and edge.note != name else ""
    return f"{edge.id} {edge.source}→{edge.target}{detail}"


def _thread_entry(thread: Any) -> str:
    return f"{thread.id} [{thread.status}] {thread.name}{_tail(thread.detail)}"


def _item_entry(item: Any) -> str:
    holder = f" ({item.holder})" if item.holder else ""
    return f"{item.id} {item.name}{holder}{_tail(item.note)}"


def _condition_entry(condition: Any) -> str:
    """``cd_x suspicious cm_yara (moderate, expires 21:30) — source: ...`` (§3.1).

    ``expires_at`` is a full ``YYYY-MM-DD HH:MM`` instant; the DAY is omitted on
    purpose — the expiry is a beat of the scene the model is in, and the clock
    already carries the date, so the time of day is what it must cite. A permanent
    condition (no instant) shows the severity alone.
    """
    expires_at = str(condition.expires_at or "")
    expires = f", expires {expires_at.split(' ', 1)[-1]}" if expires_at else ""
    meta = f" ({condition.severity}{expires})"
    source = f"source: {condition.source}" if condition.source else ""
    return f"{condition.id} {condition.key} {condition.host}{meta}{_tail(source)}"


def _body_entry(body: Any) -> str:
    """``bd_x cm_yara: kneeling · clothes: a, b · surface: ...`` (§3.1, one body).

    The three fields are independent (R6), so each renders under its own label and
    an empty one is simply absent: a being in a housecoat with nothing showing is
    ``bd_x cm_yara: standing · clothes: housecoat``, not a row of placeholders.
    """
    parts = [
        body.pose,
        f"clothes: {', '.join(body.clothing)}" if body.clothing else "",
        f"surface: {body.surface}" if body.surface else "",
    ]
    detail = " · ".join(part for part in parts if part)
    return f"{body.id} {body.host}: {detail}" if detail else f"{body.id} {body.host}"


def _economy_entry(economy: Any) -> str:
    """``eco_x 85 credits`` (§3.1): the balance first, the denomination after it."""
    denomination = f" {economy.denomination}" if economy.denomination else ""
    return f"{economy.id} {economy.balance}{denomination}"


def _who_is_where(
    reg: Registry,
    roster: Sequence["CastMember"],
    player_display: str | None = None,
) -> str | None:
    """The assignment line, in roster order then assignment order (§7).

    Roster order is the cast pane's order and is stable across turns (a member is
    appended when admitted), which is the cache-friendly choice AGENTS 6 asks for;
    a being the roster does not carry (``player``, or an id whose member retired)
    keeps its assignment order after the cast.

    The player renders as ``Player``, plus the persona's name when the caller hands
    one in: the roster never lists the player (the user's persona is not cast), so
    the name is the one thing that tells the model which body is theirs.
    """
    if not reg.assignments:
        return None
    order = {member.member_id: index for index, member in enumerate(roster)}
    beings = sorted(
        reg.assignments,
        key=lambda who: (
            0 if who in order else 1,
            order.get(who, 0),
        ),
    )
    persona = str(player_display).strip() if player_display else ""
    player_label = f"Player ({persona})" if persona else "Player"
    rendered = " · ".join(
        f"{player_label if who == PLAYER else who}: {reg.assignments[who]}"
        for who in beings
    )
    return f"  > WHO IS WHERE — {rendered}"


def _legacy_line(reg: Registry) -> str | None:
    """Name-only legacy facts, one clipped row per being (§11.1)."""
    entries: list[str] = []
    for name in reg.legacy.state:
        attributes: list[str] = []
        for category, attrs in reg.legacy.state[name].items():
            for key, value in attrs.items():
                attributes.append(f"{category}.{key}={value}")
        if attributes:
            entries.append(f"{name}: " + " ".join(attributes))
    for name, where in reg.legacy.venue.items():
        venue = where.get("venue") or ""
        if venue:
            entries.append(f"{name} at {venue}")
    if not entries:
        return None
    return f"{_LEGACY_MARKER}: " + " · ".join(_clip(entry) for entry in entries)


def render_entity_palette(
    registry: Registry,
    roster: Sequence["CastMember"] = (),
    *,
    actions: int = 0,
    player_display: str | None = None,
) -> str:
    """Render the entity palette for one turn's BTS prompt tail (§7).

    ``registry`` is the folded :class:`~fabulist.state.entities.Registry` (the
    caller folds it once and hands it in — this function reads no log),
    ``roster`` the cast (for the cast-order of the assignment line), ``actions``
    the last turn's accepted action count (:func:`fabulist.state.entities.append_entity_summary`
    carries it) and ``player_display`` the session persona's name, rendered beside
    the player's assignment as ``Player (<name>)`` (no persona: plain ``Player``).
    An empty registry still renders the directive and the off-screen line: the
    absence bucket is ALWAYS a destination the model may use (req. 2).
    """
    lines: list[str] = [ENTITY_PALETTE_DIRECTIVE]

    bucket = registry.locations.get(OFF_SCREEN_LOCATION_ID)
    # ONE PLACE PER LINE, tree order (W9.31 follow-up, live): a run-on
    # ``a · b · c`` line made a room that ALREADY existed under a parent invisible
    # to a small model — it then re-created the place as a new ROOT. Indentation
    # plus ``(in: id)`` makes the hierarchy unmissable, and the line count is
    # bounded by the registry's own size (no cap: owner ruling, W9.29).
    location_lines: list[str] = []
    bucket_line = _clip(_location_entry(registry, bucket)) if bucket is not None else None
    for loc_id, depth in _location_order(registry):
        location = registry.locations[loc_id]
        entry = _clip(_location_entry(registry, location))
        location_lines.append(f"{'  ' * depth}- {entry}")
    sections = ["LOCATIONS:"]
    if bucket_line is not None:
        sections.append(f"- {bucket_line}")
    sections.extend(location_lines)
    # One hint line, at the point of use (owner 2026-10-21): the 4B kept opening a
    # second tree for a room it KNEW was "of house". Text alone never holds, so the
    # store defaults the parent too — this just tells the model where it lands.
    roots = [
        location.name
        for loc_id, location in registry.locations.items()
        if loc_id != OFF_SCREEN_LOCATION_ID and not location.parent_id
    ]
    if roots:
        sections.append(
            "  (new places nest INSIDE this tree — leave parent_id out and they join "
            f"{roots[0]!r}; root=true only for a separate building)"
        )
    lines.append("\n".join(sections) if location_lines or bucket_line else "LOCATIONS: loc_off_screen off-screen")

    where = _who_is_where(registry, roster, player_display)
    if where:
        lines.append(where)

    meters = [m for m in registry.meters.values() if _is_number(m.value)]
    traits = [m for m in registry.meters.values() if not _is_number(m.value)]
    for label, entries in (
        ("METERS", [_meter_entry(meter) for meter in meters]),
        ("TRAITS", [_meter_entry(meter) for meter in traits]),
        ("RELATIONSHIPS", [_relationship_entry(edge) for edge in registry.relationships.values()]),
        ("THREADS", [_thread_entry(thread) for thread in registry.threads.values()]),
        ("ITEMS", [_item_entry(item) for item in registry.items.values()]),
        # W9.32 §3.3: the three kinds that joined the registry after §7 was written
        # ride the same id-first shape, after the W9.29 sections (and before the
        # LEGACY shim, which is not a registry section at all).
        ("CONDITIONS", [_condition_entry(condition) for condition in registry.conditions.values()]),
        ("BODIES", [_body_entry(body) for body in registry.bodies.values()]),
        ("ECONOMY", [_economy_entry(registry.economy)] if registry.economy else []),
    ):
        section = _section(label, entries)
        if section:
            lines.append(section)

    legacy = _legacy_line(registry)
    if legacy:
        lines.append(legacy)

    objects = (
        len(location_lines)
        + (1 if bucket_line is not None else 0)
        + len(registry.meters)
        + len(registry.relationships)
        + len(registry.threads)
        + len(registry.items)
        + len(registry.conditions)
        + len(registry.bodies)
        + (1 if registry.economy else 0)
    )
    count = int(actions) if isinstance(actions, int) and not isinstance(actions, bool) else 0
    noun = "object" if objects == 1 else "objects"
    action_noun = "action" if count == 1 else "actions"
    lines.append(f"({objects} {noun} · {count} {action_noun} last turn)")
    return "\n".join(lines)
