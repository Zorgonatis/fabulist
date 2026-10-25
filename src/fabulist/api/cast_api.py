"""Cast / speaker-colour registry (D25 colour dialogue, B5 backend half).

The colour-dialogue feature (B5) needs a *stable* name -> colour mapping that
survives compaction and replays identically on a fork. Per D25 the mapping is
owned by the engine registry, not the model and not the extractor: the first
time a speaker is seen the engine assigns the next free palette hue and appends
a ``system``/``speaker_color`` event to the log (D9 — the log owns everything).
A picker may later re-assign a name (``assigned_by="user"``); reads are
latest-wins, so the newest event for a name wins.

This module is the Wave-4b backend half (H5's ``CastPane`` consumes it): the
registry helpers plus a tiny read router. The routing engine imports
:func:`assign_speaker_color` at a first-sighting capture site when B5's
post-turn node lands; this lane prepares that seam without editing
``graph/nodes.py`` (see the lane report for the exact prepared patch).

W6-D adds the BTS *read* half here too, because this router already owns the
fork-resolved session-read helper (:func:`_session_events`):
``GET /api/sessions/{id}/world-state`` (latest ``system``/``world_state`` event
+ optional capped history) and ``GET /api/sessions/{id}/attributions`` (the
seq-ordered ``system``/``attribution`` events). Both events are persisted by the
post-turn node regardless of the agent-debug toggle, so the dashboard and the
transcript colouring no longer depend on debug captures.

W3 completes the cast read: ``GET /api/sessions/{id}/cast`` folds the card
roster, the BTS cast (the top-level ``world_state.state`` keys) and the
registry-only names into one speaker list, and assigns a palette hue to every
name at first sight. The pane read is the capture site that makes the registry
complete — a character BTS learned during play but never attributed a quote is
seen here first, so it can no longer stay colourless forever.

Fork fidelity
-------------
Branches copy no events (``EventLog.fork_session`` writes only a pointer), so a
plain ``get_events`` read would miss an inherited assignment and re-assign a
fresh colour. :func:`_session_events` walks the base chain (root-first, each
ancestor bounded by its child's ``base_seq``) exactly like
``EventLog._stream_events`` so a fork replays its parent's colours byte-for-byte.

W9.30 §5.3 — the cast ladder's manual promote door
--------------------------------------------------
``GET /api/sessions/{sid}/cast/{member_id}/promotion`` answers ``{eligible,
suggested_name, reason}`` for the promote dialog (the suggestion is the collision
suffix of the member's name against the live persona catalog), and
``POST`` the same path promotes the member through
:func:`fabulist.state.promotion.apply_promotion` — a studio-class persona write,
returned as the same persona wire ``personas_api`` serves. Every ``cast`` row
carries ``promoted_persona_id`` (one persona fold per read) so the pane can badge
an already-promoted member. The engine's half of the ladder (a promotion
PROPOSAL on the historian cadence) lives in :mod:`fabulist.state.historian`.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from fabulist.api.attachments import _session_exists
from fabulist.context.roster import extract_cards
from fabulist.log.schema_gate import (
    ATTRIBUTION_EVENT,
    SPEAKER_COLOR_EVENT,
    WORLD_STATE_EVENT,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fabulist.log import EventLog
    from fabulist.log.events import Event
    from fabulist.personas.registry import Persona
    from fabulist.state.cast import CastMember

__all__ = [
    "SPEAKER_PALETTE",
    "SPEAKER_PALETTE_NAMES",
    "SPEAKER_USER_COLOR",
    "assign_member_color",
    "assign_speaker_color",
    "color_name",
    "list_member_colors",
    "list_speaker_colors",
    "normalize_hex_color",
    "set_speaker_color",
    "router",
]

#: Fixed 10-hue palette, chosen for legibility on the dark transcript theme
#: (D6). Slot order is the assignment order: the first speaker gets slot 0, the
#: next free slot goes to the next speaker, and so on. Engine-owned constant
#: (not model-facing); HSL-derived hexes are fine here.
SPEAKER_PALETTE: tuple[str, ...] = (
    "#e06c75",  # red
    "#e5c07b",  # amber
    "#98c379",  # green
    "#56b6c2",  # cyan
    "#61afef",  # blue
    "#c678dd",  # purple
    "#d19a66",  # orange
    "#7fbbb3",  # teal
    "#d3869b",  # pink
    "#b8bb26",  # lime
)

#: The name of each :data:`SPEAKER_PALETTE` slot, in slot order (W9.28). The
#: narrator may write a colour as this LABEL instead of the hex, and the palette
#: tail block shows both (``Mara: red #e06c75``) — one spelling per hue that the
#: model can copy. Matching is case-insensitive equality with this label, never a
#: CSS-spec lookup (CSS ``crimson`` is #dc143c and equals no slot).
SPEAKER_PALETTE_NAMES: tuple[str, ...] = (
    "red",
    "amber",
    "green",
    "cyan",
    "blue",
    "purple",
    "orange",
    "teal",
    "pink",
    "lime",
)

#: Reserved near-white for the *user character* (B5: user posts need no
#: attribution and are tinted by role). Never handed out from the palette.
SPEAKER_USER_COLOR = "#f2efe9"

#: A picker-supplied hex colour: ``#rgb`` or ``#rrggbb`` (a leading ``#`` is
#: optional on input; output is always normalised to lowercase ``#rrggbb``).
_HEX_COLOR_RE = re.compile(r"^#?(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


def _normalize_hex_color(color: object) -> str | None:
    """Normalise a ``#rgb``/``#rrggbb`` colour to lowercase ``#rrggbb``.

    Returns ``None`` for anything that is not a valid hex colour, so the
    endpoint can reject it with a precise 422 instead of appending a row the
    schema gate would later choke on. Accepts the value with or without a
    leading ``#`` (a browser ``<input type=color>`` always sends ``#rrggbb``).
    """
    if not isinstance(color, str):
        return None
    cleaned = color.strip()
    if not _HEX_COLOR_RE.match(cleaned):
        return None
    bare = cleaned[1:] if cleaned.startswith("#") else cleaned
    if len(bare) == 3:
        bare = "".join(ch * 2 for ch in bare)
    return "#" + bare.lower()


def normalize_hex_color(color: object) -> str | None:
    """Public form of :func:`_normalize_hex_color` (W9.28 palette compare).

    The attribution parser resolves a colour the narrator wrote (``#rgb``,
    ``#rrggbb``, any case) against the registry, so it needs the same
    normalisation the append path enforces — one spelling, ``#rrggbb``.
    """
    return _normalize_hex_color(color)


def color_name(color: object) -> str | None:
    """The palette LABEL for ``color`` (``red``), or ``None`` when it has none.

    Only the engine's own ten hues carry a name (:data:`SPEAKER_PALETTE_NAMES`).
    A user-picked hex keeps working by hex/tolerance alone: it renders hex-only
    in the palette block and matches by value, which is why this is a label
    lookup rather than a CSS colour-name table (W9.28 non-goal).
    """
    normalized = _normalize_hex_color(color)
    if normalized is None:
        return None
    try:
        return SPEAKER_PALETTE_NAMES[SPEAKER_PALETTE.index(normalized)]
    except ValueError:
        return None


def _session_events(log: "EventLog", session_id: str) -> list["Event"]:
    """Fork-resolved raw events for ``session_id``, ordered by seq.

    Mirrors ``EventLog._stream_events`` (which is private) using only public
    reads: walk the ``base_session`` chain root-first and bound each ancestor at
    its child's ``base_seq``. Revisions and ephemeral rows are not filtered here
    — callers decide; colour events are appended once and never superseded.
    """
    chain: list[str] = []
    seen: set[str] = set()
    current: str | None = session_id
    while current is not None and current not in seen:
        seen.add(current)
        chain.append(current)
        info = log.get_session(current)
        current = info["base_session"] if info is not None else None
    chain.reverse()  # root-first

    events: list["Event"] = []
    for index, sid in enumerate(chain):
        bound: int | None = None
        if index + 1 < len(chain):
            child = log.get_session(chain[index + 1])
            bound = child["base_seq"] if child is not None else None
        for event in log.get_events(sid):
            if bound is not None and event.seq > bound:
                continue
            events.append(event)
    return events


def list_speaker_colors(log: "EventLog", session_id: str) -> dict[str, str]:
    """Fold ``speaker_color`` events latest-wins into ``{name: color}``.

    Fork-aware (see :func:`_session_events`), so a branch reports the colours it
    inherited. Malformed rows (missing name/color) are skipped rather than
    raising; the append gate keeps real producers honest.
    """
    colors: dict[str, str] = {}
    for event in _session_events(log, session_id):
        if event.type != "system" or not isinstance(event.payload, dict):
            continue
        payload = event.payload
        if payload.get("event") != SPEAKER_COLOR_EVENT:
            continue
        name = payload.get("name")
        color = payload.get("color")
        if isinstance(name, str) and name and isinstance(color, str) and color:
            colors[name] = color
    return colors


def assign_speaker_color(
    log: "EventLog",
    session_id: str,
    name: str,
    *,
    is_user: bool = False,
    assigned_by: str = "engine",
) -> str:
    """Return ``name``'s colour, assigning (and logging) one on first sighting.

    Latest-wins: when the name already has an assignment, that colour is
    returned and no new event is appended. Otherwise the first free palette slot
    is chosen (deterministic: same order of names -> same colours, so a fork
    replays identically). ``is_user=True`` flags the user character and returns
    the reserved :data:`SPEAKER_USER_COLOR` instead of consuming a slot.
    ``assigned_by`` is ``engine`` (automatic) or ``user`` (picker re-assignment)
    and is enforced by the schema gate.
    """
    cleaned = str(name).strip()
    if not cleaned:
        raise ValueError("speaker name must be non-empty")

    existing = list_speaker_colors(log, session_id)
    if cleaned in existing:
        return existing[cleaned]

    if is_user:
        color = SPEAKER_USER_COLOR
    else:
        used = set(existing.values())
        color = next((c for c in SPEAKER_PALETTE if c not in used), None)
        if color is None:
            # Palette exhausted (more than 10 speakers): deterministic wrap so
            # the mapping is still stable rather than random.
            color = SPEAKER_PALETTE[len(existing) % len(SPEAKER_PALETTE)]

    log.append_event(
        session_id,
        "system",
        str(assigned_by),
        {
            "event": SPEAKER_COLOR_EVENT,
            "name": cleaned,
            "color": color,
            "assigned_by": assigned_by,
        },
    )
    return color


def set_speaker_color(
    log: "EventLog",
    session_id: str,
    name: str,
    color: str,
    *,
    assigned_by: str = "user",
) -> str:
    """Append an explicit colour assignment, OVERRIDING any prior one (W5C-2).

    Unlike :func:`assign_speaker_color` (first-sighting, latest-wins but
    short-circuits when the name already exists), this always appends a fresh
    ``system``/``speaker_color`` row with ``assigned_by="user"`` so a picker can
    re-tint a name the engine already assigned. Returns the normalised hex.

    Raises :class:`ValueError` for an empty name or a non-hex colour so the
    caller can map it to a precise 422; the append gate stays authoritative.
    """
    cleaned = str(name).strip()
    if not cleaned:
        raise ValueError("speaker name must be non-empty")
    normalized = _normalize_hex_color(color)
    if normalized is None:
        raise ValueError("color must be a hex value like #a1b2c3 or #abc")
    log.append_event(
        session_id,
        "system",
        str(assigned_by),
        {
            "event": SPEAKER_COLOR_EVENT,
            "name": cleaned,
            "color": normalized,
            "assigned_by": assigned_by,
        },
    )
    return normalized


def _member_color_key(log: "EventLog", session_id: str) -> dict[str, list[str]]:
    """Canonical-key index over the session roster (for legacy name rows).

    Uses :func:`ensure_roster` (not the pure fold): a legacy session with colour
    rows but no cast admits bootstraps its seed members here, so its name-keyed
    colours resolve instead of sitting split with ``member_id: None``.
    """
    from fabulist.state.cast import ensure_roster, member_index

    try:
        return member_index(ensure_roster(log, session_id))
    except Exception:  # noqa: BLE001 - colouring is best-effort
        return {}


def _color_rows(log: "EventLog", session_id: str) -> list[dict]:
    """Fork-resolved colour rows, ROSTER-RESOLVED and in roster order (W7.9).

    The ONE fold behind every read of this fact — :func:`list_member_colors`
    and both ``speaker_colors`` responses (the GET and the PATCH reply) — so two
    readers of one fact can never drift apart again. Each row is
    ``{"member_id", "name", "color", "assigned_by"}``:

      * a legacy name-keyed payload (written before W7, no ``member_id``)
        resolves through the roster, so ``clerk`` and ``the clerk`` collapse
        onto the one member owning both spellings and the row carries that
        member's canonical ``display_name``;
      * an explicit ``member_id`` on the payload wins over the name;
      * a name that matches no member — or matches two, which is never merged
        silently — keeps ``member_id: None`` and its literal name, so a colour
        is never dropped.

    Order is roster order, then the unmatched rows in first-assignment order.
    """
    from fabulist.state.cast import canonical_key, ensure_roster

    index = _member_color_key(log, session_id)
    try:
        members = ensure_roster(log, session_id)
    except Exception:  # noqa: BLE001 - colouring is best-effort
        members = []
    by_id = {member.member_id: member for member in members}

    order: list[str] = []
    latest: dict[str, dict] = {}
    for event in _session_events(log, session_id):
        if event.type != "system" or not isinstance(event.payload, dict):
            continue
        payload = event.payload
        if payload.get("event") != SPEAKER_COLOR_EVENT:
            continue
        name = payload.get("name")
        color = payload.get("color")
        if not (isinstance(name, str) and name and isinstance(color, str) and color):
            continue
        member_id = payload.get("member_id")
        if not (isinstance(member_id, str) and member_id):
            ids = index.get(canonical_key(name), [])
            member_id = ids[0] if len(ids) == 1 else None
        member = by_id.get(member_id) if member_id else None
        key = member_id or name
        if key not in latest:
            order.append(key)
        latest[key] = {
            "member_id": member_id,
            "name": member.display_name if member is not None else name,
            "color": color,
            "assigned_by": str(payload.get("assigned_by") or "engine"),
        }

    rows = [latest[key] for key in order]
    by_member_id = {row["member_id"]: row for row in rows if row["member_id"]}
    matched = [m.member_id for m in members if m.member_id in by_member_id]
    ordered = [by_member_id[member_id] for member_id in matched]
    ordered += [
        row for row in rows if not row["member_id"] or row["member_id"] not in by_id
    ]
    return ordered


def list_member_colors(log: "EventLog", session_id: str) -> dict[str, str]:
    """Fold ``speaker_color`` events latest-wins, keyed by ``member_id``.

    W7 re-keys the registry onto the immutable member id so a rename keeps its
    colour ("Woman" -> "Zahra" is the same member, so the same hue). Legacy
    rows written before W7 carry only ``name``: they resolve through the roster
    (so an inherited name-keyed colour attaches to the right member), and a name
    that matches no member is keyed by the literal name as a last resort.

    Derived from :func:`_color_rows` (the shared fold), so this map and the
    ``speaker_colors`` rows are always the same fold of the same events.
    """
    return {
        (row["member_id"] or row["name"]): row["color"]
        for row in _color_rows(log, session_id)
    }


def assign_member_color(
    log: "EventLog",
    session_id: str,
    member_id: str,
    name: str,
    *,
    is_user: bool = False,
    assigned_by: str = "engine",
) -> str:
    """Return ``member_id``'s colour, assigning (and logging) one on first sight.

    Same deterministic first-free-palette-slot rule as
    :func:`assign_speaker_color`, but keyed by the immutable ``member_id`` so a
    patch/rename never changes the colour. ``name`` is stored alongside as a
    display snapshot for the legacy name-keyed read. ``is_user=True`` returns
    the reserved near-white by role and consumes no palette slot.
    """
    existing = list_member_colors(log, session_id)
    if member_id in existing:
        return existing[member_id]
    if is_user:
        color = SPEAKER_USER_COLOR
    else:
        used = set(existing.values()) | set(list_speaker_colors(log, session_id).values())
        color = next((c for c in SPEAKER_PALETTE if c not in used), None)
        if color is None:
            # Palette exhausted: deterministic wrap, never a new hue.
            color = SPEAKER_PALETTE[len(existing) % len(SPEAKER_PALETTE)]
    log.append_event(
        session_id,
        "system",
        str(assigned_by),
        {
            "event": SPEAKER_COLOR_EVENT,
            "name": str(name).strip(),
            "member_id": member_id,
            "color": color,
            "assigned_by": assigned_by,
        },
    )
    return color


from fabulist.state.cast import unaccepted_candidate_names


def _bts_cast(events: list["Event"]) -> list[str]:
    """BTS-discovered character names, in first-appearance order.

    ``world_state.state`` is keyed by character name — the ``:: Name ::``
    headers :func:`fabulist.state.bts.parse_bts` parses are the top-level keys
    (the frontend dashboard reads them the same way). A *delta* event carries
    only the characters whose state changed that turn, so a name learned three
    turns ago is absent from the newest event: the known set is therefore the
    UNION over every fork-resolved world-state event, in first-appearance order
    (stable for a fork, because the chain replays in ``seq`` order).
    """
    names: list[str] = []
    for event in events:
        if event.type != "system" or not isinstance(event.payload, dict):
            continue
        payload = event.payload
        if payload.get("event") != WORLD_STATE_EVENT:
            continue
        state = payload.get("state")
        if not isinstance(state, dict):
            continue
        for name in state:
            cleaned = str(name).strip()
            if cleaned and cleaned not in names:
                names.append(cleaned)
    # User-gated admission (wave close): a name whose cast proposal has not
    # been accepted is BTS-internal — it must not surface as cast.
    hidden = unaccepted_candidate_names(events)
    return [name for name in names if name not in hidden]


def _assign_member_first_sighting_colors(
    log: "EventLog", session_id: str, members: list, name_by_id: dict[str, str]
) -> None:
    """Give every not-yet-coloured member a palette hue, in roster order (W7).

    ``GET /cast`` is the first-sighting capture site for members no other
    capture coloured (a character BTS tracks but never quotes would otherwise
    never get a hue). Keyed by ``member_id`` — so the colour survives a later
    rename — and assigned in roster order, which stays deterministic for a fork.

    Idempotent (a member already coloured is skipped, so the reserved near-white
    the attribution capture gives the user character is never overwritten) and
    best-effort: an append failure leaves that member colourless for this read
    instead of failing the pane (B5 colouring never breaks a turn).
    """
    known = list_member_colors(log, session_id)
    for member in members:
        if not getattr(member, "is_active", True) or member.member_id in known:
            continue
        try:
            assign_member_color(
                log,
                session_id,
                member.member_id,
                name_by_id.get(member.member_id, member.display_name),
            )
        except Exception:  # noqa: BLE001 - colouring is best-effort
            continue


router = APIRouter(tags=["cast"])


@router.get("/api/sessions/{session_id}/speaker_colors")
async def get_speaker_colors(session_id: str, request: Request) -> dict:
    """Return the session's cast colours, roster-resolved (W7.9).

    404 when the session is unknown. Each entry is ``{"member_id", "name",
    "color", "assigned_by"}`` with the latest assignment winning: ``name`` is
    the member's canonical display name and ``member_id`` its immutable handle
    (``null`` for a colour whose name matches no member). Rows follow roster
    order, then the unmatched names. The PATCH reply below returns the very
    same rows through the same fold (:func:`_color_rows`).
    """
    log = getattr(request.app.state, "log", None)
    if log is None:
        raise HTTPException(status_code=500, detail="event log unavailable")
    if not _session_exists(log, session_id):
        raise HTTPException(status_code=404, detail="unknown session")

    return {
        "session_id": session_id,
        "speaker_colors": _color_rows(log, session_id),
    }


class SpeakerColorPatch(BaseModel):
    """Body of ``PATCH /api/sessions/{sid}/speaker_colors`` (W5C-2).

    ``name`` is the speaker to (re)tint, ``color`` a hex string (``#rgb`` or
    ``#rrggbb``); an invalid colour is rejected 422 rather than appended.
    """

    name: str
    color: str


@router.patch("/api/sessions/{session_id}/speaker_colors")
async def patch_speaker_color(
    session_id: str, body: SpeakerColorPatch, request: Request
) -> dict:
    """Re-assign ``name``'s colour as a USER edit (W5C-2).

    Appends a latest-wins ``system``/``speaker_color`` row with
    ``assigned_by="user"``, overriding the engine's first-sighting hue. 404 on
    an unknown session, 422 on an empty name or a non-hex colour. Returns the
    full refreshed registry — roster-resolved rows, the identical shape the GET
    returns (both fold through :func:`_color_rows`), so the picker can re-render
    without a second GET.
    """
    log = getattr(request.app.state, "log", None)
    if log is None:
        raise HTTPException(status_code=500, detail="event log unavailable")
    if not _session_exists(log, session_id):
        raise HTTPException(status_code=404, detail="unknown session")
    try:
        set_speaker_color(log, session_id, body.name, body.color, assigned_by="user")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        "session_id": session_id,
        "speaker_colors": _color_rows(log, session_id),
    }


@router.get("/api/sessions/{session_id}/cast")
async def get_cast(session_id: str, request: Request) -> dict:
    """Return the session's cast: cards + BTS-discovered speakers + registry.

    Three sources make up the merged speaker list, all fork-resolved like every
    other read here:

      * the card roster — names carried by the session's ``system`` events
        (``payload["card"]``, see :func:`fabulist.context.roster.extract_cards`);
      * the BTS cast — the top-level ``state`` keys of the session's
        ``system``/``world_state`` events (:func:`_bts_cast`), i.e. the
        characters learned during play who carry no card;
      * registry-only names — names coloured before any card/BTS sighting.

    Every merged name gets a colour at first sight (:func:`assign_speaker_color`
    — the palette, in merged order), so the pane can show real characters
    instead of blanks. Response shape is unchanged:

      * ``roster``  — card-derived names, in card order;
      * ``colors``  — ``{name: color}`` latest-wins registry map;
      * ``cast``    — canonical member rows ``{member_id, name, color,
                      assigned_by, from_card, provenance, status, provisional,
                      aliases, promoted_persona_id, persona_id, image_asset}``,
                      in roster order (cards first, then BTS, then
                      registry-only names). ``name``/``color``/``assigned_by``/
                      ``from_card`` are kept for wire back-compat. A RETIRED
                      member is omitted: the pane lists who is in the cast, not
                      the tombstones (its colour row still resolves through
                      ``.../speaker_colors``, which is what past transcript
                      quotes are coloured from).

    W7: every row is a canonical :class:`fabulist.state.cast.CastMember`, so a
    rename/two spellings of one name are one row with one colour.

    404 when the session is unknown.
    """
    log = getattr(request.app.state, "log", None)
    if log is None:
        raise HTTPException(status_code=500, detail="event log unavailable")
    if not _session_exists(log, session_id):
        raise HTTPException(status_code=404, detail="unknown session")
    from fabulist.state.cast import (
        admit_member,
        canonical_key,
        ensure_roster,
        member_by_name,
        roster,
    )
    from fabulist.state.promotion import promoted_persona_ids

    events = _session_events(log, session_id)
    cards = extract_cards(events)
    roster_names: list[str] = []
    for card in cards:
        if not isinstance(card, dict):
            continue
        name = str(card.get("name") or "").strip()
        if name and name not in roster_names:
            roster_names.append(name)

    # W7/W9.16: materialise the canonical roster (deterministic seed admits from
    # the card events). The BTS-discovered names are ALREADY roster members —
    # BTS admits them through apply_bts_cast_proposals — so this read does not
    # re-admit them: with resurrection-on-admit, a pane read would undo the
    # cast cap and bring retired members (and role words) back.
    try:
        ensure_roster(log, session_id)
    except Exception:  # noqa: BLE001 - the pane read must not fail
        pass
    # W9.16: colour-registry-only names are DISPLAYED as virtual rows below,
    # never admitted from a read (a read that writes can undo the cast cap and
    # resurrect retired members). The colour registry still resolves them.

    members = roster(log, session_id)
    name_by_id = {member.member_id: member.display_name for member in members}
    _assign_member_first_sighting_colors(log, session_id, members, name_by_id)
    member_colors = list_member_colors(log, session_id)
    color_rows = {row["name"]: row for row in _color_rows(log, session_id)}
    # W9.30 §5.3: the promotion linkage, one persona fold per read (the same
    # fold the dedupe and the promote dialog read).
    promoted = promoted_persona_ids(log)
    # The badge join: a cast row renders the persona's artwork, so the row needs
    # the definition id the image route is keyed by. Read the catalog ONCE per
    # read (definitions are sessionless; a card-admitted member has no
    # promotion, so its definition is found by NAME).
    from fabulist.personas.registry import list_personas

    personas = list_personas(log)
    persona_by_id = {persona.persona_id: persona for persona in personas}
    persona_by_name: dict[str, Persona] = {}
    for persona in personas:
        persona_by_name.setdefault(persona.name.strip().casefold(), persona)

    def _persona_link(name: str, promoted_id: str | None) -> tuple[str | None, str | None]:
        """``(persona_id, image_asset)`` for one cast row (badge art)."""
        if promoted_id:
            definition = persona_by_id.get(promoted_id)
            return promoted_id, definition.image_asset if definition else None
        definition = persona_by_name.get(name.strip().casefold())
        if definition is None:
            return None, None
        return definition.persona_id, definition.image_asset

    cast: list[dict] = []
    colors: dict[str, str] = {}
    for member in members:
        if not member.is_active:
            # A tombstoned member leaves the pane list. The colour registry
            # (list_member_colors / _color_rows) deliberately keeps its row:
            # transcript quotes from earlier turns still resolve it.
            continue
        color = member_colors.get(member.member_id)
        legacy = color_rows.get(member.display_name)
        promoted_id = promoted.get(member.member_id)
        persona_id, image_asset = _persona_link(member.display_name, promoted_id)
        cast.append(
            {
                "member_id": member.member_id,
                "name": member.display_name,
                "color": color,
                "assigned_by": legacy["assigned_by"] if legacy else None,
                "from_card": member.provenance == "card",
                "provenance": member.provenance,
                "status": member.status,
                "provisional": bool(member.provisional),
                "aliases": list(member.aliases),
                # W9.30 §5.3: the persona card this member was promoted to
                # (either door), so the pane can badge it. ``null`` = not
                # promoted; the promote door stays open either way.
                "promoted_persona_id": promoted_id,
                # The badge join: the definition behind this name (a promoted
                # persona, else the name-keyed definition a card import
                # captured), plus whether it carries artwork. ``null`` id =
                # no definition yet -> the badge shows initials.
                "persona_id": persona_id,
                "image_asset": image_asset,
            }
        )
        if color:
            colors[member.display_name] = color

    # W9.16: a READ-ONLY display merge. BTS names with no member (legacy
    # sessions whose admit pass predates W8.6, or a world-state written before
    # the roster existed) still appear as virtual rows — carrying a colour at
    # first sight, exactly as before — but the read never admits: with
    # resurrection-on-admit a pane read would undo the cast cap (and role words
    # like "Narrative" would come back). Role words and pronouns never show.
    from fabulist.state.bts import is_role_surface

    covered = set()
    for member in members:
        covered.add(canonical_key(member.display_name))
        covered.update(canonical_key(alias) for alias in member.aliases)
    virtual_names = list(_bts_cast(events))
    for row in _color_rows(log, session_id):
        if row["name"] not in virtual_names:
            virtual_names.append(row["name"])
    for name in virtual_names:
        if is_role_surface(name) or canonical_key(name) in covered:
            continue
        resolved = member_by_name(members, name)
        if resolved is not None:
            continue
        try:
            color = assign_speaker_color(log, session_id, name)
        except Exception:  # noqa: BLE001 - colouring is best-effort
            color = None
        persona_id, image_asset = _persona_link(name, None)
        cast.append(
            {
                "member_id": None,
                "name": name,
                "color": color,
                "assigned_by": "engine",
                "from_card": False,
                "provenance": "bts",
                "status": "active",
                "provisional": True,
                "aliases": [],
                # A virtual row has no member, so it can carry no promotion.
                "promoted_persona_id": None,
                # It can still name a definition (the badge joins by name).
                "persona_id": persona_id,
                "image_asset": image_asset,
            }
        )
        if color:
            colors[name] = color
    return {
        "session_id": session_id,
        "roster": roster_names,
        "colors": colors,
        "cast": cast,
    }


# -- W9.30 §5.3: the cast ladder's MANUAL promote door -------------------------
#
# The rung above the cast: an owner-accepted cast member becomes a real actor
# persona card (a studio asset that outlives the chat). The ENGINE files a
# promotion PROPOSAL on the historian cadence (state.promotion.promotion_pass);
# this door is the owner's direct one — a studio-class write through
# state.promotion.apply_promotion -> personas.registry.append_persona, so no
# proposal row and no decision row is filed (its reverse is the studio's persona
# delete). The assembly, the collision suffix and the eligibility rule live in
# state/promotion.py; this router only transports them.


class PromotionBody(BaseModel):
    """Body of ``POST /api/sessions/{sid}/cast/{member_id}/promotion``.

    ``name`` omitted (or blank) → the collision suffix of the ladder's §5.4 rule
    (``Tessa`` → ``Tessa1``). ``name`` given → written VERBATIM: never refused,
    never merged (req. 7 — duplicate persona names are legal and an owner who
    types one means it).
    """

    name: str | None = None


def _promotion_member(
    log: "EventLog", session_id: str, member_id: str
) -> "CastMember":
    """Resolve one roster member for the promote door (404 when unknown)."""
    from fabulist.state.cast import member_by_id, roster

    member = member_by_id(roster(log, session_id), member_id)
    if member is None:
        raise HTTPException(
            status_code=404, detail=f"unknown cast member: {member_id}"
        )
    return member


@router.get("/api/sessions/{session_id}/cast/{member_id}/promotion")
async def get_promotion(session_id: str, member_id: str, request: Request) -> dict:
    """What the promote dialog needs BEFORE it opens (req. 7/§5.3).

    ``eligible`` is the manual door's rule (:func:`promotion_eligibility`: an
    active member admitted from an owner-accepted cast proposal).
    ``suggested_name`` is the name the dialog PRE-FILLS — the collision suffix of
    the member's display name against the live persona catalog, so the common case
    is one click. ``reason`` is the owner-readable sentence either way (it becomes
    the 400's text when the door refuses, and it names an existing card when the
    member was already promoted — the door itself stays open).

    404 on an unknown session or member.
    """
    log = getattr(request.app.state, "log", None)
    if log is None:
        raise HTTPException(status_code=500, detail="event log unavailable")
    if not _session_exists(log, session_id):
        raise HTTPException(status_code=404, detail="unknown session")
    from fabulist.personas.registry import list_personas
    from fabulist.state.promotion import (
        promoted_persona_ids,
        promotion_eligibility,
        suffixed_name,
    )

    member = _promotion_member(log, session_id, member_id)
    eligible, reason = promotion_eligibility(member)
    suggested = suffixed_name(
        member.display_name, [persona.name for persona in list_personas(log)]
    )
    existing = promoted_persona_ids(log).get(member.member_id)
    if existing:
        reason = f"{reason}; already promoted to persona {existing}"
    return {
        "eligible": eligible,
        "suggested_name": suggested,
        "reason": reason,
    }


@router.post("/api/sessions/{session_id}/cast/{member_id}/promotion")
async def post_promotion(
    session_id: str,
    member_id: str,
    request: Request,
    body: PromotionBody | None = None,
) -> dict:
    """Promote the member NOW through the manual door; serve the created card.

    The response is the created persona WIRE (the same projection
    ``personas_api`` serves,
    :func:`fabulist.api.personas_api._wire`) plus the ``session_id`` and
    ``member_id`` it came from, so the pane can re-render and badge in one round
    trip.

    404 on an unknown session/member; 400 with the readable reason when the
    member is not on the promotion rung (req. 7's never-refuse is about name
    COLLISIONS, not eligibility).
    """
    log = getattr(request.app.state, "log", None)
    if log is None:
        raise HTTPException(status_code=500, detail="event log unavailable")
    if not _session_exists(log, session_id):
        raise HTTPException(status_code=404, detail="unknown session")
    from fabulist.api.personas_api import _wire
    from fabulist.state.promotion import PromotionError, apply_promotion

    member = _promotion_member(log, session_id, member_id)
    try:
        persona = apply_promotion(
            log, session_id, member, name=body.name if body is not None else None
        )
    except PromotionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {**_wire(persona), "session_id": session_id, "member_id": member.member_id}


# -- W6-D: BTS world-state / attribution reads -------------------------------
#
# The post-turn node persists ``system``/``world_state`` and
# ``system``/``attribution`` events unconditionally (the agent-debug toggle
# gates only DebugCapture rows). These reads expose them so the dashboard and
# the transcript colouring stop depending on the ``bts``/``attribution`` agent
# captures — which are absent whenever agent-debug is off.

#: Cap on the ``?history=1`` world-state list (seq-ordered, most recent wins).
WORLD_STATE_HISTORY_CAP = 50


def _world_state_rows(log: "EventLog", session_id: str) -> list[dict]:
    """Fork-resolved ``system``/``world_state`` events, seq-ordered.

    Each row projects only the keys the dashboard consumes and normalises a
    malformed lane to its empty container, so the reader never hands the UI a
    wrong-typed list. ``event`` and ``seq``/``ts`` envelope metadata ride along.
    """
    rows: list[dict] = []
    # User-gated admission: unaccepted cast candidates are BTS-internal and
    # never surface in the user-facing history either.
    events = _session_events(log, session_id)
    hidden = unaccepted_candidate_names(events)
    for event in events:
        if event.type != "system" or not isinstance(event.payload, dict):
            continue
        payload = event.payload
        if payload.get("event") != WORLD_STATE_EVENT:
            continue
        rows.append(
            {
                "seq": event.seq,
                "ts": event.ts,
                "kind": str(payload.get("kind") or "delta"),
                "state": {
                    key: value
                    for key, value in (
                        payload.get("state")
                        if isinstance(payload.get("state"), dict)
                        else {}
                    ).items()
                    if str(key).strip() not in hidden
                },
                "venue": payload.get("venue")
                if isinstance(payload.get("venue"), dict)
                else {},
                "off_screen": payload.get("off_screen")
                if isinstance(payload.get("off_screen"), list)
                else [],
                "threads": payload.get("threads")
                if isinstance(payload.get("threads"), list)
                else [],
                "tension": payload.get("tension")
                if isinstance(payload.get("tension"), dict)
                else {},
                "raw": str(payload.get("raw") or ""),
            }
        )
    return rows


@router.get("/api/sessions/{session_id}/world-state")
async def get_world_state(
    session_id: str,
    request: Request,
    history: bool = Query(
        False, description="also return the seq-ordered event history (capped)"
    ),
) -> dict:
    """Latest BTS world-state event for a session, or ``null`` when none.

    The post-turn node appends one ``system``/``world_state`` event per BTS
    turn (delta or checkpoint) regardless of the agent-debug toggle; this is the
    player-facing read of that state. ``latest`` carries the newest event's
    structured payload (``{seq, ts, kind, state, venue, off_screen, threads,
    tension, raw}``) so the dashboard never reconstructs it from debug captures.
    With ``?history=1`` the response also carries ``history`` — every event,
    seq-ordered and capped to the most recent :data:`WORLD_STATE_HISTORY_CAP`
    — so a delta turn can be folded onto the last checkpoint client-side.

    404 when the session is unknown (a known session with no world-state event
    returns ``{"latest": null}`` rather than 404).
    """
    log = getattr(request.app.state, "log", None)
    if log is None:
        raise HTTPException(status_code=500, detail="event log unavailable")
    if not _session_exists(log, session_id):
        raise HTTPException(status_code=404, detail="unknown session")

    rows = _world_state_rows(log, session_id)
    body: dict = {
        "session_id": session_id,
        "latest": rows[-1] if rows else None,
    }
    if history:
        body["history"] = rows[-WORLD_STATE_HISTORY_CAP:]
    return body


@router.get("/api/sessions/{session_id}/attributions")
async def get_attributions(session_id: str, request: Request) -> dict:
    """A session's per-turn speaker attributions, seq-ordered.

    Reads the ``system``/``attribution`` events the post-turn node appends
    regardless of the agent-debug toggle. Each row is ``{seq, attributions: [{
    speaker, quote}, ...]}``; a turn with no spoken lines keeps its (empty)
    row so it still pins the turn for the transcript colouring. 404 when the
    session is unknown.
    """
    log = getattr(request.app.state, "log", None)
    if log is None:
        raise HTTPException(status_code=500, detail="event log unavailable")
    if not _session_exists(log, session_id):
        raise HTTPException(status_code=404, detail="unknown session")

    rows: list[dict] = []
    for event in _session_events(log, session_id):
        if event.type != "system" or not isinstance(event.payload, dict):
            continue
        payload = event.payload
        if payload.get("event") != ATTRIBUTION_EVENT:
            continue
        attributions = payload.get("attributions")
        rows.append(
            {
                "seq": event.seq,
                "attributions": attributions
                if isinstance(attributions, list)
                else [],
            }
        )
    return {"session_id": session_id, "attributions": rows}
