"""Human state-edit router (W2.12 human state-edit door).

The event log owns every fact (D9) and state is append-only (D10): this router
never mutates a side table. It *derives* the current state by reading the log's
own projection and expresses a manual correction as a new ``state-delta``
append.

  GET  /api/sessions/{id}/state                 read the folded state view
  GET  /api/sessions/{id}/tree                  read the whole session tree (W8)
  POST /api/sessions/{id}/state {key, value}    append one ``state-delta`` row
  POST /api/sessions/{id}/state/nested {path, value}   append one ``bts_override`` row
  POST /api/sessions/{id}/entities {op, args}   run ONE registry write action
  POST /api/sessions/{id}/reconcile             run a reconciliation pass NOW

The registry write door (W9.29 §8)
-----------------------------------
``POST .../entities`` is the D45 ✎ pencil extended to the identity-stable entity
objects: the owner edits a location, a meter, a thread, an item or an edge
through the SAME tool code the BTS loop runs
(:mod:`fabulist.state.entity_tools`), so there is exactly one validation path —
an out-of-range value, an unknown catalog key, an ambiguous location name and an
occupied-location retire are refused with the same readable text the model
corrects from, and a refusal appends nothing. The door stamps its appends with
actor ``user`` (its rows are the owner's voice, not the tracker's), and the
legacy nested door above keeps working untouched for legacy lanes.

``POST .../state/nested`` also accepts ``{"path": ["forecast"], "value": <str>}``
(D45): ``forecast`` is the one HUD slot the flat fold does not natively carry
(it rides the BTS result's own field), so the nested door is its write path —
it appends a ``state-delta`` ``{"forecast": value}`` row (the fold the narrator
reads) AND re-emits the ``system``/``env_hud`` statement the HUD chip renders,
so the edit never leaves the narrator's facts and the chip disagreeing.

``POST .../reconcile`` is the manual door onto the W9.29 §12 reconciliation pass,
which otherwise only runs on the BTS cadence (every tenth committed turn). It
drives the SAME pass off-turn — empty user message, the newest committed
narrative, the ``reconcile`` tool as the loop's ONLY tool — so the model states
what it believes and the engine files one ``system``/``validation_notice`` row
(``check: "reconciliation_drift"``) per disagreement. It never writes the
registry: a check pass that "fixed" what it found would be the silent overwrite
req. 12 forbids. A check is ADVISORY, so a model/transport failure answers ``200``
with ``{"flags": [], "checked": 0, "error": ...}`` instead of a 500 — the owner
sees why nothing was checked, and the turn path is untouched either way (no turn
slot is claimed).

Current-state derivation (reused, not reinvented)
-------------------------------------------------
:meth:`fabulist.log.EventLog.get_projection` returns a ``state`` mapping built
by folding ``state-delta`` payloads in seq order, later writes winning per key
(``repo.py`` — ``elif event.type == "state-delta": state.update(payload)``, the
"shallow fold of state-delta payloads, last write wins" documented on
``get_projection``). The graph prefers the same source:
:func:`fabulist.graph.nodes.facts_from_log` renders
:func:`fabulist.state.entities.rendered_state` — that fold overlaid by the
registry-derived ``loc``/``weather``/``tone``/``inv``, ``genre`` and ``conds``
(W9.32 §6). This router reads the same helper, so the manual view can never
drift from what the narrator is fed.

Payload shape (mirrors the extractor exactly)
---------------------------------------------
The extractor's post-turn append is the flat deltas dict, one
``{key: value}`` entry per changed fact (``nodes.py`` — ``log.append_event(
session_id, "state-delta", "extractor", deltas, ...)`` where ``deltas`` is the
``ExtractorResult.deltas`` ``dict[str, str]``). A manual edit therefore appends
``{key: value}`` with exactly the same shape, so the existing fold picks it up
unchanged. The schema gate leaves ``state-delta`` open (``required={}``,
``schema_gate.py``), so any shape would pass — mirroring the extractor keeps the
log homogeneous anyway.

The flat door accepts any non-empty key, so ``date`` and ``genre`` (D44/D45)
are already writable through it — no whitelist is added here (the fold is
deliberately open; a whitelist would reject legal facts).

Nested BTS overrides (D45)
--------------------------
The BTS tracker objects (cast / relationships / threads / off-screen / emotional
/ inventory) fold from the ``system``/``world_state`` lane, so the flat
``state-delta`` write cannot reach them — it would silently not stick. ``POST
/api/sessions/{id}/state/nested`` folds that lane, applies ``{path, value}``
inside the payload, and appends the result as one ``world_state`` event with
``kind="bts_override"``. The dashboard already folds a non-checkpoint
``world_state`` event last (``foldBtsWorldState``, ``frontend/src/lib/api.ts``),
so the override supersedes the agent's value with no read-side change. The fold
mirrors that frontend fold (checkpoint replaces; a delta deep-merges ``state``,
merges ``venue``/``tension`` latest-wins and merges ``threads``/``off_screen``
by ``name``) and is fork-aware like the flat read.

Wiring (orchestrator-owned, one line)::

    from fabulist.api.state_api import router
    app.include_router(router)

Like :mod:`fabulist.api.revisions`, the router reads the live
:class:`~fabulist.log.EventLog` from ``request.app.state.log``.
"""

from __future__ import annotations

import copy
import logging
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from fabulist.api.attachments import _session_exists
from fabulist.log.schema_gate import WORLD_STATE_EVENT
from fabulist.state.cast import unaccepted_candidate_names
from fabulist.state.env_hud import ENV_HUD_EVENT, env_hud_statement
from fabulist.state.entity_tools import (
    USER_WRITABLE_TOOLS,
    EntityTurn,
    entity_action_tools,
)
from fabulist.state.session_tree import player_display, session_tree

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fabulist.log import EventLog


router = APIRouter(tags=["state"])
logger = logging.getLogger(__name__)


@router.get("/api/sessions/{session_id}/theme")
async def session_theme(session_id: str, request: Request) -> dict:
    """Deterministic theme tokens for the session (R4).

    Derived view of the RENDERED state — the projection fold overlaid by the
    registry's own ``tone``/``weather`` (:func:`fabulist.state.entities.
    rendered_state`), so a scene whose tone the tracker set themes on that value
    instead of on a stale flat key that nothing writes any more (W9.32 §6).
    Unknown tones fall back neutral (empty tokens = inherit app.css).
    """
    from fabulist.state.entities import rendered_state
    from fabulist.theme.tokens import style_tokens, token_style

    log = _log_for(request)
    if not _session_exists(log, session_id):
        raise HTTPException(status_code=404, detail="unknown session")
    state = rendered_state(log, session_id)
    tokens = style_tokens(state)
    return {"tokens": tokens, "style": token_style(tokens)}

#: Actor stamped on a human/persona edit. The extractor uses ``"extractor"``;
#: a manual edit is the user's own voice, so the log can distinguish the door.
USER_ACTOR = "user"


class StateEditRequest(BaseModel):
    """Body of ``POST /api/sessions/{id}/state`` — one fact to set."""

    key: str
    value: str


def _log_for(request: Request) -> "EventLog":
    """Return the app's event log, or 500 when the app was built without one."""
    log = getattr(request.app.state, "log", None)
    if log is None:
        raise HTTPException(status_code=500, detail="event log unavailable")
    return log


@router.get("/api/sessions/{session_id}/state")
async def get_state(session_id: str, request: Request) -> dict:
    """Return the merged state: ``{"state": {...}, "registry": {...}}``.

    ``state`` is the RENDERED state (W9.32 §6):
    :func:`fabulist.state.entities.rendered_state` — the projection fold (clock
    keys, the ``forecast`` pin) overlaid by the registry-derived
    ``loc``/``weather``/``tone``/``inv``, plus the derived ``genre`` and
    ``conds`` — so the sidebar's top rows read registry-truth and keep their
    shape. ``registry`` is the identity-stable entity view (W9.29 §8): locations
    with their canonical paths and occupants, assignments, meters with their last
    change, traits, relationships, threads, items, and the legacy ``world_state``
    fold for the read-side shim. ``404`` for an unknown session.
    """
    from fabulist.state.entities import rendered_state

    log = _log_for(request)
    if not _session_exists(log, session_id):
        raise HTTPException(status_code=404, detail="unknown session")
    state = rendered_state(log, session_id)
    return {"state": state, "registry": _registry_view(log, session_id)}


@router.get("/api/sessions/{session_id}/tree")
async def get_tree(session_id: str, request: Request) -> dict:
    """Return the whole session as one tree: ``{"tree": {...}}`` (W8).

    The projection is PURE (:func:`fabulist.state.session_tree.session_tree`):
    it reads the registry fold, the flat state fold, the cast roster and the
    seeded cards and appends nothing, so opening the pane never mints a member
    nor writes a row. ``404`` for an unknown session, like every other read on
    this router.
    """
    log = _log_for(request)
    if not _session_exists(log, session_id):
        raise HTTPException(status_code=404, detail="unknown session")
    return {"tree": session_tree(log, session_id)}


@router.post("/api/sessions/{session_id}/state")
async def edit_state(
    session_id: str, body: StateEditRequest, request: Request
) -> dict:
    """Append one manual ``state-delta`` edit and echo it back.

    The payload mirrors the extractor's flat ``{key: value}`` shape, so the
    existing latest-wins fold (``get_projection``) picks the edit up on the next
    read. ``404`` for an unknown session; ``422`` for an empty key. A rejected
    request appends nothing, so the log is untouched.
    """
    log = _log_for(request)
    if not _session_exists(log, session_id):
        raise HTTPException(status_code=404, detail="unknown session")

    key = body.key.strip()
    if not key:
        raise HTTPException(
            status_code=422,
            detail={"reason": "key must be non-empty", "key": body.key},
        )

    log.append_event(
        session_id,
        "state-delta",
        USER_ACTOR,
        {key: body.value},
    )
    return {"session_id": session_id, "key": key, "value": body.value}


#: Event ``kind`` of a manual nested override appended to the ``world_state``
#: lane (D45). Non-checkpoint, so the dashboard's fold deep-merges it last and
#: the BTS checkpoint cadence (``state.bts.turns_since_checkpoint``) is untouched.
NESTED_OVERRIDE_KIND = "bts_override"

#: The nested path that addresses the turn-level ``forecast`` HUD slot (D45).
#: It is deliberately NOT a ``world_state`` lane — ``forecast`` rides the BTS
#: result, not the per-character state — so it lands on the flat fold and
#: re-emits the HUD statement. ``path`` must be exactly this (a scalar slot has
#: no nested leaves); anything longer is rejected rather than silently lost.
FORECAST_PATH: tuple[str, ...] = ("forecast",)

#: Response ``kind`` tag for a forecast override (the frontend ignores it; it is
#: here so the echo names the lane honestly rather than claiming ``bts_override``).
FORECAST_OVERRIDE_KIND = "forecast_override"

#: The empty world-state lanes, mirroring the dashboard's ``BtsWorldState``.
_EMPTY_WORLD: dict[str, Any] = {
    "state": {},
    "venue": {},
    "off_screen": [],
    "threads": [],
    "tension": {},
}


class StateNestedEditRequest(BaseModel):
    """Body of ``POST /api/sessions/{id}/state/nested`` — one override at a path."""

    path: list[str]
    value: str


def _resolved_events(log: "EventLog", session_id: str) -> list:
    """Fork-resolved raw events for ``session_id``, ordered by seq.

    Same public-read walk as ``api.cast_api._session_events`` and
    ``state.bts._resolve_events``: follow the ``base_session`` chain root-first,
    bounding each ancestor at its child's ``base_seq``.
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

    events: list = []
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


def _merge_by_name(prior: list, incoming: list) -> list:
    """Merge a named list lane (threads/off-screen) latest-wins by ``name``."""
    merged = [dict(entry) for entry in prior if isinstance(entry, dict)]
    for entry in incoming:
        if not isinstance(entry, dict):
            continue
        for index, row in enumerate(merged):
            if row.get("name") == entry.get("name"):
                merged[index] = dict(entry)
                break
        else:
            merged.append(dict(entry))
    return merged


def _fold_world_state(log: "EventLog", session_id: str) -> dict:
    """Fold the session's ``world_state`` lane to its current value (D45).

    Mirrors the dashboard's frontend fold (``foldBtsWorldState``): a checkpoint
    replaces the accumulated payload wholesale; a delta deep-merges ``state``,
    merges ``venue``/``tension`` latest-wins and merges ``threads``/``off_screen``
    by ``name``. Fork-aware, so a branch reads the world state it inherited.
    """
    acc = copy.deepcopy(_EMPTY_WORLD)
    events_folded = list(_resolved_events(log, session_id))
    for event in events_folded:
        if event.type != "system" or not isinstance(event.payload, dict):
            continue
        payload = event.payload
        if payload.get("event") != WORLD_STATE_EVENT:
            continue
        state = payload.get("state")
        state = state if isinstance(state, dict) else {}
        venue = payload.get("venue")
        venue = venue if isinstance(venue, dict) else {}
        off_screen = payload.get("off_screen")
        off_screen = off_screen if isinstance(off_screen, list) else []
        threads = payload.get("threads")
        threads = threads if isinstance(threads, list) else []
        tension = payload.get("tension")
        tension = tension if isinstance(tension, dict) else {}
        if payload.get("kind") == "checkpoint":
            acc = {
                "state": copy.deepcopy(state),
                "venue": copy.deepcopy(venue),
                "off_screen": copy.deepcopy(off_screen),
                "threads": copy.deepcopy(threads),
                "tension": copy.deepcopy(tension),
            }
            continue
        for character, categories in state.items():
            bucket = acc["state"].setdefault(character, {})
            if not isinstance(categories, dict):
                continue
            for category, attrs in categories.items():
                slot = bucket.setdefault(category, {})
                if isinstance(attrs, dict):
                    slot.update(attrs)
        acc["venue"].update(venue)
        acc["tension"].update(tension)
        acc["off_screen"] = _merge_by_name(acc["off_screen"], off_screen)
        acc["threads"] = _merge_by_name(acc["threads"], threads)
    # User-gated admission (wave close): a character whose cast proposal has
    # not been accepted is BTS-internal — never user-facing state.
    hidden = unaccepted_candidate_names(events_folded)
    if hidden:
        for name in hidden:
            acc["state"].pop(name, None)
    return acc


def _set_path(payload: dict, path: list[str], value: str) -> None:
    """Write ``value`` at ``path`` inside ``payload``, creating missing containers.

    A segment whose FOLLOWING segment is all-digits names a list (indexed);
    otherwise it names a dict key. Traversing an existing list requires a valid
    integer index, so an edit can never invent a sparse lane. Raises 422 on a
    malformed or out-of-range path (the caller has not appended yet).
    """
    node: Any = payload
    for position, segment in enumerate(path):
        is_last = position == len(path) - 1
        child_is_list = not is_last and path[position + 1].isdigit()
        if isinstance(node, list):
            if not segment.isdigit():
                raise HTTPException(
                    status_code=422,
                    detail={"reason": f"expected a list index, got {segment!r}", "path": path},
                )
            index = int(segment)
            if index >= len(node):
                raise HTTPException(
                    status_code=422,
                    detail={"reason": f"index {index} out of range for {len(node)} row(s)", "path": path},
                )
            if is_last:
                node[index] = value
                return
            child = node[index]
            if not isinstance(child, (dict, list)):
                child = [] if child_is_list else {}
                node[index] = child
            node = child
        elif isinstance(node, dict):
            if is_last:
                node[segment] = value
                return
            child = node.get(segment)
            if not isinstance(child, (dict, list)):
                child = [] if child_is_list else {}
                node[segment] = child
            node = child
        else:  # pragma: no cover - a malformed stored lane, rejected not guessed
            raise HTTPException(
                status_code=422,
                detail={"reason": "path runs through a scalar value", "path": path},
            )


def _write_forecast_override(log: "EventLog", session_id: str, value: str) -> dict:
    """Append a manual ``forecast`` override and re-emit the HUD (D45).

    Two appends keep the narrator's facts and the HUD chip in agreement, both
    reusing existing lanes (D9/D10 — no new event shape):

    * a ``state-delta`` ``{"forecast": value}`` row, actor ``user`` — the flat
      fold's ``forecast`` key, which is the D45 pin
      (:func:`fabulist.state.entities.rendered_state` carries it and deliberately
      does NOT overlay the scene trait);
    * a fresh ``system``/``env_hud`` statement assembled from the RENDERED state,
      so ``GET /api/sessions/{id}/hud`` (which serves the latest HUD event) shows
      the pin immediately instead of staying on last turn's forecast — and still
      shows the registry's own ``loc``/``weather``, which are derived now and no
      longer ride the flat fold (W9.32 §6).

    ``value`` is the pinned forecast; an explicit empty string clears the pin
    (latest-wins in the flat fold, absent != empty, D33). The cleared statement
    deliberately drops the prior rung (``prior=None``) so the pin cannot
    resurrect itself through the previous statement's baked forecast — it falls
    to the steady weather rung until the next turn's BTS supplies a real one.
    """
    from fabulist.state.entities import rendered_state

    log.append_event(session_id, "state-delta", USER_ACTOR, {"forecast": value})
    fold = rendered_state(log, session_id)
    log.append_event(
        session_id,
        "system",
        USER_ACTOR,
        {
            "event": ENV_HUD_EVENT,
            "statement": env_hud_statement(fold, None, None),
        },
    )
    return {
        "session_id": session_id,
        "kind": FORECAST_OVERRIDE_KIND,
        "path": list(FORECAST_PATH),
        "value": value,
    }


@router.post("/api/sessions/{session_id}/state/nested")
async def edit_state_nested(
    session_id: str, body: StateNestedEditRequest, request: Request
) -> dict:
    """Append one nested BTS OVERRIDE and echo it back (D45).

    Folds the ``world_state`` lane, applies ``{path, value}`` inside it, and
    appends the result as a ``world_state`` event with ``kind="bts_override"``.
    ``path`` addresses the world-state object directly — ``["state", "<name>",
    "<category>", "<key>"]`` for a cast/relationship/emotional/inventory fact,
    ``["venue", "<name>", "venue"]`` for a location, ``["threads", "0",
    "status"]`` for a list lane, ``["tension", "level"]`` for tension.

    ``path == ["forecast"]`` is the one exception (D45): it is not a world-state
    lane, so it writes the flat fold + re-emits the HUD via
    :func:`_write_forecast_override` (see that helper for the contract).
    ``404`` for an unknown session; ``422`` for an empty/malformed path
    (nothing appended).
    """
    log = _log_for(request)
    if not _session_exists(log, session_id):
        raise HTTPException(status_code=404, detail="unknown session")

    path = [segment.strip() for segment in body.path]
    if not path or any(not segment for segment in path):
        raise HTTPException(
            status_code=422,
            detail={
                "reason": "path must be a non-empty list of non-empty segments",
                "path": body.path,
            },
        )

    if path[0] == FORECAST_PATH[0]:
        if tuple(path) != FORECAST_PATH:
            raise HTTPException(
                status_code=422,
                detail={
                    "reason": (
                        "forecast is a scalar HUD slot; path must be exactly "
                        f"{list(FORECAST_PATH)}"
                    ),
                    "path": body.path,
                },
            )
        return _write_forecast_override(log, session_id, body.value)

    payload = _fold_world_state(log, session_id)
    _set_path(payload, path, body.value)

    log.append_event(
        session_id,
        "system",
        USER_ACTOR,
        {
            "event": WORLD_STATE_EVENT,
            "kind": NESTED_OVERRIDE_KIND,
            "state": payload["state"],
            "venue": payload["venue"],
            "off_screen": payload["off_screen"],
            "threads": payload["threads"],
            "tension": payload["tension"],
            "path": path,
            "value": body.value,
        },
    )
    return {
        "session_id": session_id,
        "kind": NESTED_OVERRIDE_KIND,
        "path": path,
        "value": body.value,
    }


# -- the registry view (W9.29 §8) --------------------------------------------


def _is_number(value: Any) -> bool:
    """Mirror of the registry's own meter/trait split (a number vs free text)."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _meter_changes(log: "EventLog", session_id: str) -> dict[str, dict]:
    """``meter_id -> {prior, seq}`` for meters that were SET more than once.

    Identity is the whole point of the registry (§3.4), so the sidebar can show
    what a meter did last: the fold keeps current values only, and history lives
    in the log, so the previous value is read here from the ``entity_set`` rows
    the same way the fold reads them (latest wins per id, in chain order).
    """
    prior: dict[str, Any] = {}
    changes: dict[str, dict] = {}
    for event in _resolved_events(log, session_id):
        if event.type != "system" or not isinstance(event.payload, dict):
            continue
        payload = event.payload
        if payload.get("event") != "entity_set":
            continue
        meter_id = str(payload.get("id") or "")
        if not meter_id:
            continue
        if meter_id in prior:
            changes[meter_id] = {"prior": prior[meter_id], "seq": event.seq}
        prior[meter_id] = payload.get("value")
    return changes


def _being_names(log: "EventLog", session_id: str) -> dict[str, str]:
    """``being key -> display name`` for the occupants the registry stores raw.

    Occupants are keys (``cm_…``/``player``) because identity is the object's
    (W9.29), but a pane has to read them as people. This is a PURE fold: it reads
    the cast roster and the session persona, never minting one — a session whose
    roster was never materialized answers a sparse (or empty) map and the client
    falls back to the raw key, which keeps every occupant reachable.
    """
    from fabulist.state import cast, entities, presence

    names: dict[str, str] = {}
    for member in cast.roster(log, session_id):
        if member.member_id and member.display_name:
            names[member.member_id] = member.display_name
    # ``_player_name`` is the ONE resolution of "who is the player" (W9.29); the
    # tracker and the presence walk read it through the same private helper. The
    # KEY stays the reserved literal (safe to join on); the VALUE presents as
    # ``Player (Name)`` (bare ``Player`` when the chat has no persona, W8), so the
    # pane never renders a raw key as a person either.
    player_name = (presence._player_name(log, session_id) or "").strip()
    names[entities.PLAYER] = player_display(player_name)
    return names


def _registry_view(log: "EventLog", session_id: str) -> dict:
    """The identity-stable registry as a plain, renderable view (W9.29 §8).

    Everything the shakeout sidebar shows (owner ruling: show EVERYTHING), with
    no invention on top of the fold: locations carry their canonical path and
    occupants, the off-screen bucket carries its optional reference, meters
    carry their last change, and the legacy ``world_state`` fold rides along so
    a pre-regime session stays readable through the same response.

    ``beings`` is the name map beside the raw occupant keys (W9.31 §8.1): the
    pane renders people by name without a second cast fetch, and a key the fold
    cannot resolve simply falls back to itself on the client.
    """
    from fabulist.state import entities

    reg = entities.registry(log, session_id)
    changes = _meter_changes(log, session_id)

    locations = [
        {
            "id": loc_id,
            "name": location.name,
            "path": entities.location_path(reg, loc_id),
            "parent_id": location.parent_id,
            "note": location.note,
            "off_screen_ref": location.off_screen_ref,
            "occupants": list(entities.occupants(reg, loc_id)),
        }
        for loc_id, location in reg.locations.items()
    ]

    meters: list[dict] = []
    traits: list[dict] = []
    for meter in reg.meters.values():
        row: dict[str, Any] = {
            "id": meter.id,
            "host": meter.host,
            "subject": meter.subject,
            "key": meter.key,
            "value": meter.value,
        }
        change = changes.get(meter.id)
        if change is not None:
            row["prior"] = change["prior"]
            row["changed_seq"] = change["seq"]
        (meters if _is_number(meter.value) else traits).append(row)

    return {
        "locations": locations,
        "beings": _being_names(log, session_id),
        "assignments": dict(reg.assignments),
        "off_screen_ref": reg.off_screen_ref,
        "meters": meters,
        "traits": traits,
        "relationships": [
            {
                "id": edge.id,
                "from": edge.source,
                "to": edge.target,
                "name": edge.name,
                "note": edge.note,
            }
            for edge in reg.relationships.values()
        ],
        "threads": [
            {
                "id": thread.id,
                "name": thread.name,
                "status": thread.status,
                "detail": thread.detail,
                "note": thread.note,
            }
            for thread in reg.threads.values()
        ],
        "items": [
            {
                "id": item.id,
                "name": item.name,
                "holder": item.holder,
                "note": item.note,
            }
            for item in reg.items.values()
        ],
        "legacy": {
            "state": copy.deepcopy(reg.legacy.state),
            "venue": copy.deepcopy(reg.legacy.venue),
            "off_screen": copy.deepcopy(reg.legacy.off_screen),
            "threads": copy.deepcopy(reg.legacy.threads),
            "tension": copy.deepcopy(reg.legacy.tension),
            "flat": copy.deepcopy(reg.legacy.flat),
        },
    }


# -- the user write door (W9.29 §8 — the ✎ pencil on registry objects) --------


class EntityOpRequest(BaseModel):
    """Body of ``POST /api/sessions/{id}/entities`` — one write action."""

    op: str
    args: dict[str, Any] = {}


class _UserActorLog:
    """Read-through log proxy that stamps this door's appends actor ``user``.

    The registry writers and the tool fns are ONE code path shared with the BTS
    loop, which stamps its own rows ``bts`` (:data:`fabulist.state.entities.DEFAULT_ACTOR`).
    A manual edit is the owner's voice, so the append is re-stamped here instead
    of forking the writers — every read (``get_chain_events``/``get_events``/
    ``get_session``...) passes straight through, and only the actor of a written
    row differs from the tracker's.
    """

    def __init__(self, log: "EventLog") -> None:
        self._log = log

    def __getattr__(self, name: str) -> Any:
        return getattr(self._log, name)

    def append_event(
        self, session_id: str, event_type: str, actor: str, payload: Any, **kwargs: Any
    ) -> Any:
        return self._log.append_event(session_id, event_type, USER_ACTOR, payload, **kwargs)


@router.post("/api/sessions/{session_id}/entities")
async def edit_entity(
    session_id: str, body: EntityOpRequest, request: Request
) -> dict:
    """Run one registry write action as the user; ``{ok: true, ...}`` or 400.

    The op names one of the tool actions
    (:data:`fabulist.state.entity_tools.USER_WRITABLE_TOOLS` — every write action
    EXCEPT ``propose_time``, whose owner door stays the flat ``POST /state
    {key: "time"}`` row, D45) and ``args`` is that tool's own argument object, so
    the validation is literally the tool's: the catalog range check, the
    id-or-name location resolution, the being identity door, the
    occupied-location retire guard. A rejection answers ``400`` with the same
    readable text the model would have been given, and (because the writers
    validate BEFORE the append) writes nothing. ``404`` for an unknown session,
    ``400`` for an unknown op.
    """
    log = _log_for(request)
    if not _session_exists(log, session_id):
        raise HTTPException(status_code=404, detail="unknown session")

    op = body.op.strip()
    if op not in USER_WRITABLE_TOOLS:
        allowed = ", ".join(USER_WRITABLE_TOOLS)
        raise HTTPException(
            status_code=400,
            detail={
                "reason": f"unknown op {op!r} — the registry actions are: {allowed}",
                "op": op,
            },
        )

    turn = EntityTurn(_UserActorLog(log), session_id)
    tools = {tool.name: tool for tool in entity_action_tools(turn)}
    result = await tools[op].fn(dict(body.args))
    if not result.get("ok"):
        raise HTTPException(
            status_code=400,
            detail={
                "reason": str(result.get("error") or "the action was refused"),
                "op": op,
            },
        )
    return {
        "ok": True,
        "op": op,
        **{key: value for key, value in result.items() if key != "ok"},
    }


# -- the manual reconciliation door (W9.29 §12 — "check state now") ----------


def _reconcile_failure(error: str) -> dict:
    """The soft-failure body: 200 with the reason, never a 500.

    A check pass is ADVISORY — it writes nothing but its own flags — so a model
    that is down, a connection without function calling, or a session with no
    narrative is a fact the owner should read, not a server error. The shape is
    the success shape with zero counts plus ``error``, so the client renders one
    line either way.
    """
    return {
        "flags": [],
        "checked": 0,
        "error": (error or "the check did not run")[:300],
    }


@router.post("/api/sessions/{session_id}/reconcile")
async def reconcile_now(session_id: str, request: Request) -> dict:
    """Run one reconciliation pass NOW; ``{"flags": [...], "checked": <int>}``.

    The manual door onto the cadence pass (§12). It drives the wired BTS runner
    off-turn: the synthetic turn state (empty user message + the newest committed
    narrative), the ``reconcile`` tool as the loop's only tool, and the drift rows
    appended through the same ``system``/``validation_notice`` append the tool
    uses — the registry is read, never written. ``checked`` counts the beliefs the
    model stated, ``flags`` carries the disagreements it filed.

    ``404`` for an unknown session (nothing to check). Every other failure is the
    soft ``200`` from :func:`_reconcile_failure`: the pass is advisory, no turn
    slot is claimed, and the ``bts`` runner's own error text is the whole report.
    """
    log = _log_for(request)
    if not _session_exists(log, session_id):
        raise HTTPException(status_code=404, detail="unknown session")
    runner = getattr(request.app.state, "world_state", None)
    if runner is None or not hasattr(runner, "reconcile_now"):
        return _reconcile_failure(
            "reconciliation unavailable: the engine wiring supplied no BTS runner"
        )
    try:
        outcome = await runner.reconcile_now(session_id)
    except Exception as exc:  # noqa: BLE001 - a check never surfaces as a 500
        logger.warning("manual reconcile failed for %s", session_id, exc_info=True)
        return _reconcile_failure(str(exc))
    return {
        "flags": list(outcome.get("flags") or []),
        "checked": int(outcome.get("checked") or 0),
    }
