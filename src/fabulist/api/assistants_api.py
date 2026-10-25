"""Assistant consult router (W2.5 wiring + UI).

Turns the landed :mod:`fabulist.assistants` manager into reachable API surface:
a persona catalog and a consult endpoint. The event log owns everything (D9):
definitions are read from the reserved ``__assistants__`` anchor session (system
events carrying a JSON list); replies are appended as ``system`` events with
``event: "assistant_reply"``. A consult never advances clock, state, or history.

Endpoints
---------
  GET    /api/assistants
         -> {"assistants": [definition, ...], "meta": {count, source, demo, ...}}
  POST   /api/assistants {name, persona, ...}
         -> {"assistant": wire}  (create; mint an id or upsert when one is given)
  PUT    /api/assistants/{assistant_id} {name, persona, ...}
         -> {"assistant": wire} | 404  (latest-wins append)
  DELETE /api/assistants/{assistant_id}
         -> tombstone -> {"assistant_id", "deleted": True} | 404
  GET  /api/sessions/{session_id}/assistants/replies
       -> {"session_id": str, "replies": [{id, seq, ts, assistant, text, agent}]}
  POST /api/sessions/{session_id}/assistants/{assistant_id}/consult {"user_msg": str}
       -> {"assistant": str, "text": str, "agent": str}
  GET  /api/sessions/{session_id}/assistant-panel
       -> {"statement": str | null}  (latest-wins, fork-aware; null until first consult)

Persona-as-assistant (Bug 4)
----------------------------
The enabled slot (``chat_assistant``) may name a **persona id** instead of an
assistant id: a persona can be used directly as the assistant with nothing
cloned. Both consult paths (the per-turn hook and ``POST .../consult``) resolve
the id through :func:`resolve_assistant_definition`, which returns the folded
definition when the id is an assistant and otherwise synthesizes a manual
``narrator`` definition from the persona's own text. Enablement validation
(``chat_config_api``) accepts a persona id via the same seam's catalog check.

Wiring (orchestrator-owned; the ``app.state`` seam needs no ``app.py`` signature
change)::

    from fabulist.api.assistants_api import router as assistants_router
    app.include_router(assistants_router)

    # Give the router a way to build one ``ModelCaller`` per assistant agent tier.
    # Preferred — attach the connections registry; the router lazily builds
    # ``wiring.RegistryModelCaller(agent, registry)`` per consult:
    app.state.registry = registry
    # ...or inject an explicit factory (tests / non-registry callers):
    app.state.assistant_model_caller_factory = lambda agent: my_caller_for(agent)

Without either seam the consult endpoint returns 500 (the catalog still works).

Definitions source
------------------
``GET /api/assistants`` folds the ``__assistants__`` anchor session (then the
legacy ``__sidekicks__`` anchor) in seq order:

* a **list payload** (a bare list, or a dict carrying ``definitions`` /
  ``assistants`` / the legacy ``sidekicks`` key) is a whole-roster snapshot — it
  resets the fold. This is the legacy + profile-import write shape and stays
  read-compatible.
* a ``assistant_defined`` event (D47) upserts ONE definition by its ``id``
  (latest-wins) — the CRUD create/update write shape.
* a ``assistant_deleted`` event (D47) tombstones one ``id`` (D9/D10 — never a row
  mutation; nothing outside the fold changes).

With no definition event at all the fold falls back to two built-in **demo**
personas, returned with ``meta.source == "builtin-demo"`` and ``demo: true`` on
each row — a clearly-marked placeholder, not a port of the preset's Extra
Assistants. Any definition event (including a tombstone) switches the source to
``"log"``. Replace the demos by writing definitions: a whole-list
``assistants_defined`` event, or per-id ``assistant_defined`` events (what the
CRUD endpoints below append)::

    log.append_event("__assistants__", "system", "user", {
        "event": "assistants_defined",
        "definitions": [
            {"id": "segfault", "name": "SEGFAULT", "persona": "...",
             "agent": "narrator", "trigger": "manual", "keywords": []},
        ],
    })

Read shims (zero data migration)
--------------------------------
The pre-rename spelling was ``sidekick``. Existing logs are replayed unchanged:
the definitions fold reads ``__assistants__`` first and falls back to the legacy
``__sidekicks__`` anchor (accepting ``assistants_defined``, the legacy
``sidekicks_defined`` kind, and bare-list payloads), and the replies fold matches
both ``assistant_reply`` and the legacy ``sidekick_reply`` event name. Only the
new spelling is ever written.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any, Callable

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from fabulist.assistants import (
    AssistantContext,
    AssistantDefinition,
    AssistantManager,
)
from fabulist.assistants.panel import (
    latest_assistant_panel,
    scene_facts_line,
    scene_recent_prose,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fabulist.graph.types import ModelCaller
    from fabulist.log import EventLog


router = APIRouter(tags=["assistants"])

#: Reserved session id anchoring assistant definition events (like ``__settings__``).
ASSISTANTS_SESSION_ID = "__assistants__"

#: Legacy reserved session id (pre-rename spelling); read-only fallback.
LEGACY_SIDEKICKS_SESSION_ID = "__sidekicks__"

#: System-event kind carrying an assistant definition list (whole-roster snapshot).
ASSISTANTS_EVENT = "assistants_defined"

#: System-event kind carrying ONE assistant definition (per ``id``, latest wins).
ASSISTANT_DEFINED_EVENT = "assistant_defined"

#: System-event kind carrying a tombstone (removes ``id`` from the fold).
ASSISTANT_DELETED_EVENT = "assistant_deleted"

#: Actor stamped on human-intent definition appends (mirrors personas/worlds).
USER_ACTOR = "user"

#: Legacy system-event kind (read-only; the fold accepts it but never writes it).
LEGACY_SIDEKICKS_EVENT = "sidekicks_defined"

#: System-event kind carrying one appended assistant reply.
ASSISTANT_REPLY_EVENT = "assistant_reply"

#: Legacy system-event kind (read-only; the replies fold accepts it).
LEGACY_SIDEKICK_REPLY_EVENT = "sidekick_reply"

#: Payload keys that may carry the definitions list (first present wins).
_DEFINITION_KEYS = ("definitions", "assistants", "sidekicks")

#: Built-in placeholders, used only until an ``__assistants__`` event defines real
#: personas. Both are explicitly marked DEMO so the UI can badge them.
_DEMO_PERSONAS: tuple[AssistantDefinition, ...] = (
    AssistantDefinition(
        id="demo_oracle",
        name="Demo Oracle (placeholder)",
        persona=(
            "[DEMO PERSONA — not a preset port] A laconic oracle watching the "
            "scene from just off-stage. Replace this placeholder by appending a "
            f"'{ASSISTANTS_EVENT}' system event to the {ASSISTANTS_SESSION_ID} "
            "session with a JSON list of definitions."
        ),
        style_notes="Short, knowing lines; never describes what happens next.",
        agent="narrator",
        trigger="manual",
    ),
    AssistantDefinition(
        id="demo_gremlin",
        name="Demo Gremlin (placeholder)",
        persona=(
            "[DEMO PERSONA — not a preset port] A gleeful gremlin cheering from "
            "the rafters, fond of unsolicited commentary."
        ),
        style_notes="Chaotic, emoji-happy, always at the wrong moment.",
        agent="narrator",
        trigger="manual",
    ),
)


class ConsultRequest(BaseModel):
    """Body of ``POST .../assistants/{id}/consult`` — the trigger message."""

    user_msg: str = ""


class AssistantBody(BaseModel):
    """Body of ``POST/PUT /api/assistants`` — one assistant definition (D47).

    ``name`` and ``persona`` are required; ``agent`` defaults ``narrator`` and
    ``trigger`` ``manual``. An explicit ``id`` updates in place (latest-wins); on
    ``POST`` it may be omitted to mint one. ``actor`` is the optional persona
    reference (id or name) this assistant runs as — absent stays absent (``""``).
    """

    name: str
    persona: str
    id: str | None = None
    style_notes: str = ""
    agent: str = "narrator"
    trigger: str = "manual"
    keywords: list[str] = []
    actor: str = ""


# -- log access --------------------------------------------------------------


def _log_for(request: Request) -> "EventLog":
    """Return the app's event log, or 500 when the app was built without one."""
    log = getattr(request.app.state, "log", None)
    if log is None:
        raise HTTPException(status_code=500, detail="event log unavailable")
    return log


def _caller_factory(request: Request) -> Callable[[str], "ModelCaller"]:
    """Resolve the per-agent ``ModelCaller`` factory from ``app.state``.

    Prefers an injected ``app.state.assistant_model_caller_factory``; otherwise
    derives one from ``app.state.registry`` (the connections registry) via the
    same ``wiring.RegistryModelCaller`` the narrator uses. The import is lazy so
    this module never forms an import cycle with ``fabulist.wiring``.
    """
    state = request.app.state
    injected = getattr(state, "assistant_model_caller_factory", None)
    if callable(injected):
        return injected
    registry = getattr(state, "registry", None)
    if registry is not None:

        def factory(agent: str) -> "ModelCaller":
            from fabulist.wiring import RegistryModelCaller

            return RegistryModelCaller(agent, registry)

        return factory
    raise HTTPException(
        status_code=500,
        detail=(
            "assistant model caller factory unavailable: set app.state.registry "
            "or app.state.assistant_model_caller_factory"
        ),
    )


# -- definition parsing ------------------------------------------------------


def _definition_to_dict(definition: AssistantDefinition, *, demo: bool) -> dict[str, Any]:
    """Wire shape of one assistant definition (the frontend contract).

    ``actor`` (D47) is additive: absent on a legacy definition, it folds to ``""``.
    """
    return {
        "id": definition.id,
        "name": definition.name,
        "persona": definition.persona,
        "style_notes": definition.style_notes,
        "agent": definition.agent,
        "trigger": definition.trigger,
        "keywords": list(definition.keywords),
        "actor": definition.actor,
        "demo": demo,
    }


def _coerce_definition(raw: Any) -> AssistantDefinition | None:
    """Build a definition from a log payload entry; ``None`` when malformed.

    Accepts a legacy entry (no ``actor``) unchanged: the optional actor reference
    folds to ``""`` rather than rejecting the definition.
    """
    if not isinstance(raw, dict):
        return None
    ident = raw.get("id")
    name = raw.get("name")
    persona = raw.get("persona")
    if not all(isinstance(value, str) and value for value in (ident, name, persona)):
        return None
    keywords = raw.get("keywords")
    if not isinstance(keywords, list):
        keywords = []
    try:
        return AssistantDefinition(
            id=ident,
            name=name,
            persona=persona,
            style_notes=str(raw.get("style_notes") or ""),
            agent=str(raw.get("agent") or "narrator"),
            trigger=str(raw.get("trigger") or "manual"),
            keywords=[str(keyword) for keyword in keywords],
            actor=str(raw.get("actor") or ""),
        )
    except ValueError:
        return None


def _definition_list_from_payload(payload: Any) -> list[Any] | None:
    """A whole-roster list carried by one event payload, or ``None``.

    A bare list, or a dict carrying ``definitions`` / ``assistants`` / the legacy
    ``sidekicks`` key. The anchor session is reserved for exactly this purpose,
    so the list is accepted regardless of the event kind name (documented kind:
    ``assistants_defined``).
    """
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in _DEFINITION_KEYS:
            value = payload.get(key)
            if isinstance(value, list):
                return value
    return None


def _roster_from_list(raw: list[Any]) -> dict[str, tuple[AssistantDefinition, bool]]:
    """Parse a whole-roster list into ``{id: (definition, is_demo)}`` (first wins)."""
    roster: dict[str, tuple[AssistantDefinition, bool]] = {}
    for entry in raw:
        definition = _coerce_definition(entry)
        if definition is None or definition.id in roster:
            continue
        demo = bool(entry.get("demo")) if isinstance(entry, dict) else False
        roster[definition.id] = (definition, demo)
    return roster


def _fold_anchor(
    log: "EventLog", anchor: str
) -> tuple[list[tuple[AssistantDefinition, bool]], bool]:
    """Fold one anchor session into ``[(definition, is_demo), ...]`` + defined flag.

    Events are read in seq order (append-only log, D9/D10): a list payload resets
    the roster (whole-roster snapshot); an ``assistant_defined`` event upserts one
    id (latest-wins); an ``assistant_deleted`` event removes one id. ``defined`` is
    ``True`` once any of those is seen, so a tombstoned-to-empty roster never
    falls back to the demo placeholders.
    """
    roster: dict[str, tuple[AssistantDefinition, bool]] | None = None
    defined = False
    for event in log.get_events(anchor):
        if event.type != "system":
            continue
        payload = event.payload
        listed = _definition_list_from_payload(payload)
        if listed is not None:
            roster = _roster_from_list(listed)
            defined = True
            continue
        if not isinstance(payload, dict):
            continue
        kind = payload.get("event")
        if kind == ASSISTANT_DEFINED_EVENT:
            definition = _coerce_definition(payload)
            if definition is not None:
                if roster is None:
                    roster = {}
                roster[definition.id] = (definition, bool(payload.get("demo")))
                defined = True
        elif kind == ASSISTANT_DELETED_EVENT:
            defined = True
            ident = payload.get("id")
            if isinstance(ident, str) and ident and roster is not None:
                roster.pop(ident, None)
    return list((roster or {}).values()), defined


def _load_definitions(log: "EventLog") -> tuple[list[tuple[AssistantDefinition, bool]], bool]:
    """Fold definitions into ``[(definition, is_demo), ...]`` plus source flag.

    ``__assistants__`` is read first, then the legacy ``__sidekicks__`` read-side
    shim (a pre-rename log defined its roster there). Returns the built-in demo
    personas (all flagged ``True``, source ``True``) only when NEITHER anchor
    carries a definition event; an explicitly emptied roster stays empty
    (``source == "log"``).
    """
    for anchor in (ASSISTANTS_SESSION_ID, LEGACY_SIDEKICKS_SESSION_ID):
        entries, defined = _fold_anchor(log, anchor)
        if defined:
            return entries, False
    return [(definition, True) for definition in _DEMO_PERSONAS], True


def assistant_definitions(log: "EventLog") -> list[AssistantDefinition]:
    """Public read of the folded roster (built-in demos when the log defines none).

    Shared seam: the per-turn hook resolves the enabled assistant through this,
    so the hook and the catalog never diverge.
    """
    entries, _ = _load_definitions(log)
    return [definition for definition, _ in entries]


def known_assistant_ids(log: "EventLog") -> set[str]:
    """Ids the catalog exposes (log-defined or demo), for enablement validation."""
    return {definition.id for definition in assistant_definitions(log)}


def _assistant_from_persona(persona: Any) -> AssistantDefinition:
    """Synthesize a manual assistant from a persona definition (Bug 4).

    A persona used directly as an assistant (the enabled slot naming a persona id
    rather than an assistant id) gets its runtime behavior built here — never
    cloned into a stored definition. The persona's own text becomes the assistant
    persona block; the model tier stays the default narrator one.
    """
    text = "\n\n".join(
        part.strip()
        for part in (persona.description, persona.personality, persona.scenario)
        if isinstance(part, str) and part.strip()
    )
    return AssistantDefinition(
        id=persona.persona_id,
        name=persona.name,
        persona=text or persona.name,
        agent="narrator",
        trigger="manual",
    )


def resolve_assistant_definition(
    log: "EventLog",
    assistant_id: str,
    *,
    definitions: list[AssistantDefinition] | None = None,
) -> AssistantDefinition | None:
    """Resolve an enabled id to a definition: an assistant, else a persona (Bug 4).

    The assistant catalog wins (a definition id is never shadowed); an id that
    names no assistant is looked up in the persona catalog and synthesized via
    :func:`_assistant_from_persona`, so a persona can BE the assistant without a
    clone. ``None`` when the id names neither. Both consult paths (the per-turn
    hook and ``POST .../consult``) resolve through this one seam, so they cannot
    disagree on what an enabled id means.
    """
    if definitions is None:
        definitions = assistant_definitions(log)
    for definition in definitions:
        if definition.id == assistant_id:
            return definition
    # Lazy import: the persona fold lives in the personas layer; the API layer
    # already imports it lazily elsewhere (chat_config_api) to stay import-thin.
    from fabulist.personas.registry import list_personas

    for persona in list_personas(log):
        if persona.persona_id == assistant_id:
            return _assistant_from_persona(persona)
    return None


# -- scene context -----------------------------------------------------------

# The scene-digest helpers (:func:`scene_facts_line` / :func:`scene_recent_prose`)
# live in :mod:`fabulist.assistants.panel` so the per-turn hook (engine layer)
# and this router build the SAME digest without the hook importing the API layer.


def _ensure_known_session(request: Request, session_id: str) -> None:
    """404 unless the session exists in-memory or has events in the log."""
    registry = getattr(request.app.state, "sessions", None)
    if isinstance(registry, dict) and session_id in registry:
        return
    if _log_for(request).get_events(session_id):
        return
    raise HTTPException(status_code=404, detail="unknown session")


# -- endpoints ---------------------------------------------------------------


@router.get("/api/assistants")
async def list_assistants(request: Request) -> dict:
    """List assistant personas (log-defined, else built-in demo placeholders)."""
    log = _log_for(request)
    entries, demo_source = _load_definitions(log)
    definitions = [
        _definition_to_dict(definition, demo=is_demo) for definition, is_demo in entries
    ]
    return {
        "assistants": definitions,
        "meta": {
            "count": len(definitions),
            "source": "builtin-demo" if demo_source else "log",
            "session": ASSISTANTS_SESSION_ID,
            "event": ASSISTANTS_EVENT,
            "demo": demo_source,
        },
    }


# -- definition writes (D47 CRUD) --------------------------------------------


def _definition_from_body(
    body: AssistantBody, assistant_id: str
) -> AssistantDefinition:
    """Validate one request body into a definition (``422`` on a bad field)."""
    if not body.name.strip():
        raise HTTPException(
            status_code=422, detail="assistant 'name' must be non-empty"
        )
    if not body.persona.strip():
        raise HTTPException(
            status_code=422, detail="assistant 'persona' must be non-empty"
        )
    try:
        return AssistantDefinition(
            id=assistant_id,
            name=body.name.strip(),
            persona=body.persona,
            style_notes=body.style_notes,
            agent=body.agent or "narrator",
            trigger=body.trigger or "manual",
            keywords=[str(keyword) for keyword in body.keywords],
            actor=body.actor,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _append_definition(log: "EventLog", definition: AssistantDefinition) -> None:
    """Append one ``assistant_defined`` event (create or update, latest-wins).

    The write is a ``system`` event on the ``__assistants__`` anchor (never a
    side-table row, D9); the newest event per id wins the fold.
    """
    log.append_event(
        ASSISTANTS_SESSION_ID,
        "system",
        USER_ACTOR,
        {
            "event": ASSISTANT_DEFINED_EVENT,
            "id": definition.id,
            "name": definition.name,
            "persona": definition.persona,
            "style_notes": definition.style_notes,
            "agent": definition.agent,
            "trigger": definition.trigger,
            "keywords": list(definition.keywords),
            "actor": definition.actor,
        },
    )


def delete_assistant_definition(log: "EventLog", assistant_id: str) -> None:
    """Tombstone one definition (``assistant_deleted``); the fold drops it.

    Non-destructive (D9/D10): definitions stay queryable, only the fold changes.
    """
    log.append_event(
        ASSISTANTS_SESSION_ID,
        "system",
        USER_ACTOR,
        {"event": ASSISTANT_DELETED_EVENT, "id": assistant_id},
    )


def _find_definition(log: "EventLog", assistant_id: str) -> AssistantDefinition | None:
    """One folded definition by id, or ``None`` when absent/tombstoned."""
    for definition in assistant_definitions(log):
        if definition.id == assistant_id:
            return definition
    return None


@router.post("/api/assistants")
async def create_assistant(body: AssistantBody, request: Request) -> dict:
    """Append an assistant definition (create, or upsert when ``id`` is given)."""
    log = _log_for(request)
    assistant_id = body.id or uuid.uuid4().hex
    definition = _definition_from_body(body, assistant_id)
    _append_definition(log, definition)
    return {"assistant": _definition_to_dict(definition, demo=False)}


@router.put("/api/assistants/{assistant_id}")
async def update_assistant(
    assistant_id: str, body: AssistantBody, request: Request
) -> dict:
    """Supersede a definition (latest-wins append); ``404`` when unknown."""
    log = _log_for(request)
    if _find_definition(log, assistant_id) is None:
        raise HTTPException(status_code=404, detail="unknown assistant")
    definition = _definition_from_body(body, assistant_id)
    _append_definition(log, definition)
    return {"assistant": _definition_to_dict(definition, demo=False)}


@router.delete("/api/assistants/{assistant_id}")
async def remove_assistant(assistant_id: str, request: Request) -> dict:
    """Tombstone a definition; ``404`` when unknown. Rows survive (D9)."""
    log = _log_for(request)
    if _find_definition(log, assistant_id) is None:
        raise HTTPException(status_code=404, detail="unknown assistant")
    delete_assistant_definition(log, assistant_id)
    return {"assistant_id": assistant_id, "deleted": True}


@router.get("/api/sessions/{session_id}/assistants/replies")
async def list_assistant_replies(session_id: str, request: Request) -> dict:
    """Read back appended assistant replies for hydration.

    Matches both the new ``assistant_reply`` event name and the legacy
    ``sidekick_reply`` name, so pre-rename sessions hydrate unchanged.
    """
    log = _log_for(request)
    _ensure_known_session(request, session_id)
    replies: list[dict[str, Any]] = []
    for event in log.get_events(session_id):
        if event.type != "system" or event.ephemeral:
            continue
        payload = event.payload
        if not isinstance(payload, dict):
            continue
        if payload.get("event") not in (
            ASSISTANT_REPLY_EVENT,
            LEGACY_SIDEKICK_REPLY_EVENT,
        ):
            continue
        meta = event.meta if isinstance(event.meta, dict) else {}
        # Legacy payloads carried the id under the ``sidekick`` key.
        assistant_id = payload.get("assistant") or payload.get("sidekick") or ""
        replies.append(
            {
                "id": event.id,
                "seq": event.seq,
                "ts": event.ts,
                "assistant": assistant_id,
                "text": payload.get("text", ""),
                # The consult route stamps the agent on ``meta``; the per-turn hook
                # writes it on the payload (D38 event contract). Prefer the
                # payload so hook-appended replies hydrate with their real tier.
                "agent": payload.get("agent") or meta.get("agent", "narrator"),
                "user_msg": meta.get("user_msg", ""),
            }
        )
    return {"session_id": session_id, "replies": replies}


@router.get("/api/sessions/{session_id}/assistant-panel")
async def get_assistant_panel(session_id: str, request: Request) -> dict:
    """Latest panel statement for the session (fork-aware, latest-wins).

    ``{"statement": str | null}`` — ``null`` until the engine has produced a
    statement (a miss is not an error: the frontend keeps the panel quiet).
    ``404`` for an unknown session.
    """
    log = _log_for(request)
    _ensure_known_session(request, session_id)
    return {"statement": latest_assistant_panel(log, session_id)}


@router.post("/api/sessions/{session_id}/assistants/{assistant_id}/consult")
async def consult_assistant(
    session_id: str, assistant_id: str, body: ConsultRequest, request: Request
) -> dict:
    """Consult one assistant; append its reply as a system event (never advances)."""
    log = _log_for(request)
    _ensure_known_session(request, session_id)

    # W5-S2: the consult path is not driven through the engine, so the turn's
    # CURRENT_SESSION contextvar is unset here; set it before the model call so
    # an agent-debug capture lands on THIS session (and so usage recording from
    # the shared caller is attributed correctly).
    from fabulist.agents.debug import CURRENT_SESSION

    CURRENT_SESSION.set(session_id)

    entries, _ = _load_definitions(log)
    definitions = [definition for definition, _ in entries]
    # Bug 4: an enabled id may name a persona; resolve it through the same seam
    # the per-turn hook uses so the two consult paths cannot disagree.
    definition = resolve_assistant_definition(log, assistant_id, definitions=definitions)
    if definition is None:
        raise HTTPException(status_code=404, detail=f"unknown assistant {assistant_id!r}")
    if not any(existing.id == definition.id for existing in definitions):
        definitions = [*definitions, definition]
    manager = AssistantManager(definitions, _caller_factory(request))
    # D18 completion: the assistant interaction's module flow (preset-
    # customized or default), wired by build_app onto app.state.
    assistants_flow = getattr(request.app.state, "assistants_flow", None)
    if assistants_flow is not None:
        manager._messages_builder = assistants_flow
    context = AssistantContext(
        facts=scene_facts_line(log, session_id),
        recent_prose=scene_recent_prose(log, session_id),
        user_msg=body.user_msg,
    )
    try:
        reply = await manager.consult(assistant_id, context)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None

    log.append_event(
        session_id,
        "system",
        "assistant",
        {"event": ASSISTANT_REPLY_EVENT, "assistant": reply.assistant_id, "text": reply.text},
        meta={"agent": reply.agent, "user_msg": body.user_msg},
    )
    return {"assistant": reply.assistant_id, "text": reply.text, "agent": reply.agent}
