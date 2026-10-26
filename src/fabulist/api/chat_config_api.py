"""Per-chat configuration router (D16: chat memory opt-out toggle).

Two orthogonal per-session knobs live on the append-only event log (D9): the
*persona* assigned to a chat and whether long-term *memory* is enabled for it.
This router owns only the memory door; persona assignment events
(``{event: "chat_persona", persona_id}``) are written by the personas lane and
are read here, never produced.

  PATCH /api/sessions/{sid}/memory         {"enabled": bool}  -> append + echo
  GET   /api/sessions/{sid}/memory          -> {"enabled": bool}
  GET   /api/sessions/{sid}/memory-config   -> {"enabled", "k", "block_chars",
                                                 "rerank_enabled", "query_mode",
                                                 "overfetch"}
  PATCH /api/sessions/{sid}/memory-config   -> partial knobs, append + echo
  GET   /api/sessions/{sid}/agent-debug      -> {"enabled": bool}
  PATCH /api/sessions/{sid}/agent-debug      {"enabled": bool}  -> append + echo
  GET   /api/sessions/{sid}/assistant        -> {"assistant_id": str | null}
  PATCH /api/sessions/{sid}/assistant        {"assistant_id": str | null}  -> append + echo
  GET   /api/sessions/{sid}/chat-config     -> persona + memory + user_as_actor
                                              + npc_strategy/npc_max_dispatch
  PATCH /api/sessions/{sid}/chat-config     -> per-key optional session settings

Event contract (fixed, shared with the memory-scope lane)
---------------------------------------------------------
* Memory opt-out is a **system** event appended *on the chat session*::

      {"event": "chat_memory", "enabled": <bool>}

  Folded latest-wins in seq order; **absent means default True** (memory on).
* Recall knobs (D25 C2) are a **system** event appended on the chat session::

      {"event": "chat_memory_config", "k": int, "block_chars": int,
       "rerank_enabled": bool, "query_mode": "message"|"message+scene",
       "overfetch": int}

  Each PATCH appends a FULL snapshot; folded latest-wins. Defaults and the
  clamp policy live in :mod:`fabulist.memory.recall`.
* Persona assignment is ``{"event": "chat_persona", "persona_id": <str>}``,
  also a system event on the chat session, folded latest-wins.
* Agent-debug capture (W5-S2, D20) is a **system** event appended on the chat
  session::

      {"event": "chat_agent_debug", "enabled": <bool>}

  Folded latest-wins; **absent means the boot default** (the ``--agent-debug``
  flag: True -> sessions default ON, False -> default OFF, and a per-session
  write can still enable/disable either way). :func:`resolve_agent_debug` is the
  seam the capture sink (``wiring.make_agent_caller``) reads per call, so the
  toggle takes effect on the NEXT agent call with no restart.
* Assistant enablement (D38 lane 2c) is a **system** event appended on the chat
  session::

      {"event": "chat_assistant", "assistant_id": <str> | null}

  Folded latest-wins (the fold lives in :mod:`fabulist.assistants.panel`);
  **absent or an explicit ``null`` means no assistant enabled**. Unlike the
  persona/memory folds above this one is fork-aware (a branch inherits its
  parent's enabled assistant up to the fork point) so the read agrees with the
  per-turn consult hook. Enabling an id the catalog does not expose is a 404;
  a **persona id** is accepted too (Bug 4) and the engine synthesizes the
  assistant from the persona at consult time.
* User-as-Actor (W9.22 plan §2, the cast model) is a **system** event appended
  on the chat session::

      {"event": "chat_user_as_actor", "user_as_actor": <bool>}

  Folded latest-wins; **absent means OFF** (the plan's default). The setting
  is SESSION-scoped by design: impersonation is opt-in per chat, so turning it
  ON makes the player a first-class presence AND a lane candidate, and OFF
  keeps today's behaviour (the player is never a cast member). The read is
  published here in two places because two layers need it: the ``PATCH``/``GET
  /chat-config`` pair for the UI's one config read, and
  :func:`resolve_user_as_actor` — the in-engine seam the presence/admit seams
  call per turn (lane 1b), which defaults to ``False`` for a session that never
  wrote a row (defensive: an unknown session is never impersonated).
* The NPC dispatch policy (W9.24 §3.5) is a **system** event appended on the
  chat session::

      {"event": "chat_npc_dispatch", "strategy": "decision"|"random"|"lru",
       "max_dispatch": <int >= 0>}

  Folded latest-wins (per key) by
  :func:`fabulist.state.presence.npc_dispatch_policy`; **absent means
  ``decision`` + ``0``** (no bound). It is a SETTING, never an asset rewrite:
  the lane ladder's tier eligibility is untouched, and no strategy can lane a
  presence-tier member. Unlike the folds above this one is FORK-AWARE (a branch
  inherits its parent's policy up to the fork point), because the LADDER it
  shapes is itself chain-walked. :func:`resolve_npc_dispatch` is the in-engine
  seam.

**The PATCH body is PER-KEY OPTIONAL** (W9.24 §4, the R1 review fold): each key
ABSENT from the request leaves that setting unchanged, at least ONE key must be
present (an empty body is a 422), and ``null`` is never a value (absent ≠
null). A ``chat_npc_dispatch`` write always appends a FULL snapshot of both its
keys, merged onto the folded policy — the recall-knob pattern, so the read stays
unambiguous.

:func:`resolve_recall_config` is the public seam the recall path reads: it
combines the knob fold with the canonical ``chat_memory`` toggle (single source
for ``enabled``).

Why a raw fold instead of a scope import
----------------------------------------
The canonical resolver for a chat's effective memory scope is
:func:`fabulist.memory.scope.resolve_scope` (memory lane). This router is a
*thin read of the same events*, deliberately free of any import from the
personas / memory-scope modules so the parallel lanes can land independently.
Both surfaces fold the same ``chat_memory`` / ``chat_persona`` rows, so they
cannot diverge as long as the event contract above is honored.

Wiring (orchestrator-owned, one line)::

    from fabulist.api.chat_config_api import router as chat_config_router
    app.include_router(chat_config_router)

Like :mod:`fabulist.api.state_api`, this router reads the live
:class:`~fabulist.log.EventLog` from ``request.app.state.log`` and reuses
:func:`fabulist.api.attachments._session_exists` for the 404 door, so no
``app.py`` signature change is needed.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from fabulist.api.attachments import _session_exists
from fabulist.assistants.panel import CHAT_ASSISTANT_EVENT, latest_chat_assistant
from fabulist.memory.recall import RecallConfig
from fabulist.state.presence import (
    NPC_DISPATCH_EVENT,
    NpcDispatchPolicy,
    npc_dispatch_policy,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fabulist.log import EventLog


router = APIRouter(tags=["chat-config"])

#: System-event kind carrying a chat's memory toggle (latest wins per session).
CHAT_MEMORY_EVENT = "chat_memory"

#: System-event kind carrying a chat's recall knobs (D25 C2): ``k``, block char
#: cap, ``rerank_enabled``, ``query_mode`` and ``overfetch``. Each PATCH appends
#: a FULL snapshot; latest-wins folding keeps the read cheap and unambiguous.
CHAT_MEMORY_CONFIG_EVENT = "chat_memory_config"

#: System-event kind carrying a chat's persona assignment (written by the
#: personas lane; read-only here so this router never duplicates that write).
CHAT_PERSONA_EVENT = "chat_persona"

#: System-event kind carrying a chat's runtime agent-debug toggle (W5-S2).
#: Latest-wins; absent means the boot-flag default (see
#: :func:`resolve_agent_debug`). One ``system`` event per PATCH — append-only,
#: no restart, no side table.
CHAT_AGENT_DEBUG_EVENT = "chat_agent_debug"

#: System-event kind carrying a chat's User-as-Actor setting (W9.22 plan §2).
#: Latest-wins with ONE row per PATCH; **absent means OFF**. The payload key is
#: the setting's name (``user_as_actor``) so a hand-read of the log names the
#: setting the same way the API does — this is the key lane 1b is contracted on.
CHAT_USER_AS_ACTOR_EVENT = "chat_user_as_actor"

#: Actor stamped on a human toggle write. Matches :mod:`fabulist.api.sessions`
#: / :mod:`fabulist.api.settings`, whose user-intent appends use ``"user"``.
USER_ACTOR = "user"


class MemoryPatch(BaseModel):
    """Body of ``PATCH /api/sessions/{sid}/memory`` — the desired memory state.

    ``enabled`` is a strict JSON boolean shape: a missing key or a value Pydantic
    cannot coerce to ``bool`` fails validation with a 422 before any append.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool


class AgentDebugPatch(BaseModel):
    """Body of ``PATCH /api/sessions/{sid}/agent-debug`` — the debug toggle.

    ``enabled`` is a strict JSON boolean; a missing/coercible value is a 422
    before any append (mirrors :class:`MemoryPatch`).
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool


class MemoryConfigPatch(BaseModel):
    """Body of ``PATCH /api/sessions/{sid}/memory-config`` (D25 C2).

    Every field is optional; only the keys present are applied (merged onto the
    currently folded config) and a full snapshot is appended. Ranges mirror the
    clamps in :mod:`fabulist.memory.recall`, so a bad value is a 422 before any
    append rather than a silently clamped surprise.
    """

    model_config = ConfigDict(extra="forbid")

    k: int | None = Field(default=None, ge=1, le=50)
    block_chars: int | None = Field(default=None, ge=100, le=20000)
    rerank_enabled: bool | None = None
    query_mode: Literal["message", "message+scene"] | None = None
    overfetch: int | None = Field(default=None, ge=1, le=100)


class ChatConfigPatch(BaseModel):
    """Body of ``PATCH /api/sessions/{sid}/chat-config`` (W9.22 §2, W9.24 §4).

    PER-KEY OPTIONAL: a key that is ABSENT leaves that setting unchanged; at
    least one key must be present (the route 422s an empty body). An explicit
    JSON ``null`` is not a value for any of these keys — absent means "leave
    it", so the route refuses a null rather than guessing (the recall-knob
    pattern, without its nullable knobs). ``extra="forbid"`` keeps the route
    from becoming a grab-bag of session settings the UI thinks it can write
    here.

    ``npc_strategy``/``npc_max_dispatch`` are the W9.24 NPC dispatch policy: a
    SETTING (the lane set per turn), never a cast/asset rewrite.
    """

    model_config = ConfigDict(extra="forbid")

    user_as_actor: bool | None = None
    npc_strategy: Literal["decision", "random", "lru"] | None = None
    # ``strict``: a JSON ``true``/``"3"`` is not this setting's value type —
    # the sibling recall knobs are laxer, but a silently coerced bound would
    # change how many lanes a turn runs.
    npc_max_dispatch: int | None = Field(default=None, ge=0, strict=True)


class AssistantPatch(BaseModel):
    """Body of ``PATCH /api/sessions/{sid}/assistant`` — the enabled slot (D38).

    ``assistant_id`` is a required key (an explicit JSON ``null`` disables the
    slot); a missing key fails validation with a 422 before any append. An id the
    catalog does not expose is a 404 (checked in the route).
    """

    model_config = ConfigDict(extra="forbid")

    assistant_id: str | None


def _log_for(request: Request) -> "EventLog":
    """Return the app's event log, or 500 when the app was built without one."""
    log = getattr(request.app.state, "log", None)
    if log is None:
        raise HTTPException(status_code=500, detail="event log unavailable")
    return log


def _is_persona_id(log: "EventLog", candidate: str) -> bool:
    """True when ``candidate`` names a persona in the catalog (Bug 4).

    The enabled-assistant slot also accepts a persona id: the engine resolves it
    through :func:`fabulist.api.assistants_api.resolve_assistant_definition` and
    synthesizes the assistant at consult time. Lazy import keeps this module's
    import graph thin, the same way sibling routes reach the assistants fold.
    """
    from fabulist.personas.registry import list_personas

    return any(persona.persona_id == candidate for persona in list_personas(log))


def _memory_enabled(log: "EventLog", session_id: str) -> bool:
    """Fold ``chat_memory`` system events latest-wins; absent -> ``True``.

    Only boolean ``enabled`` values are honored (a malformed row is skipped, so
    the gate's permissive ``system`` shape can never flip the toggle by accident).
    Ordering comes from :meth:`EventLog.get_chain_events` (seq order), so the
    last valid write wins — and, issue #86, a branch inherits the toggle its
    parent held at the cut (``chat_memory`` is carried CONFIG), exactly like the
    engine's readability gate does.
    """
    enabled = True
    for event in log.get_chain_events(session_id):
        if event.type != "system":
            continue
        if event.payload.get("event") != CHAT_MEMORY_EVENT:
            continue
        value = event.payload.get("enabled")
        if isinstance(value, bool):
            enabled = value
    return enabled


def _agent_debug(log: "EventLog", session_id: str, default: bool) -> bool:
    """Fold ``chat_agent_debug`` system events latest-wins; absent -> ``default``.

    Only boolean ``enabled`` values are honored (malformed rows are skipped), so
    the gate's permissive ``system`` shape can never flip capture by accident.
    Ordering comes from :meth:`EventLog.get_events` (seq order).

    DELIBERATELY own-row, not chain-walked (issue #86): ``chat_agent_debug`` is
    operational capture, not session-constant narrative config, so it is NOT in
    :data:`fabulist.log.repo.FORK_CONFIG_EVENTS` and a branch reads the boot
    default until someone flips it there. Reading the chain here would silently
    re-inherit exactly what the carry whitelist excludes.
    """
    enabled = default
    for event in log.get_events(session_id):
        if event.type != "system":
            continue
        if event.payload.get("event") != CHAT_AGENT_DEBUG_EVENT:
            continue
        value = event.payload.get("enabled")
        if isinstance(value, bool):
            enabled = value
    return enabled


def _user_as_actor(log: "EventLog", session_id: str) -> bool:
    """Fold ``chat_user_as_actor`` system events latest-wins; absent -> ``False``.

    Only boolean ``user_as_actor`` values are honored (a malformed row is
    skipped), so the gate's permissive ``system`` shape can never switch
    impersonation on by accident — the safe direction for this setting. Ordering
    comes from :meth:`EventLog.get_chain_events` (seq order), so the last valid
    write wins.

    Chain-walked (issue #86): the setting is carried CONFIG, and the engine's
    BTS seam (:func:`fabulist.state.bts.user_as_actor`) reads through here —
    reading own rows made a branch flip User-as-Actor OFF while the source chat
    was ON.
    """
    enabled = False
    for event in log.get_chain_events(session_id):
        if event.type != "system":
            continue
        if event.payload.get("event") != CHAT_USER_AS_ACTOR_EVENT:
            continue
        value = event.payload.get("user_as_actor")
        if isinstance(value, bool):
            enabled = value
    return enabled


def resolve_user_as_actor(log: "EventLog", session_id: str) -> bool:
    """The effective per-session User-as-Actor policy (W9.22 plan §2).

    Public seam for the cast/presence lane: when ON the player is an actor — a
    first-class presence AND a lane candidate — and OFF keeps today's read-side
    subtraction. Folds the CHAIN's ``chat_user_as_actor`` writes latest-wins
    (a branch inherits the value in force at its cut, issue #86) and defaults to
    ``False`` (the plan's default OFF), so a session that never
    wrote a row, a blank ``session_id`` and a legacy log all read as OFF.

    This router never imports the presence/cast modules on purpose: the event
    contract above is the whole coupling, exactly like the memory folds, so the
    lanes can land independently.
    """
    if not session_id:
        return False
    return _user_as_actor(log, session_id)


def resolve_npc_dispatch(log: "EventLog", session_id: str) -> NpcDispatchPolicy:
    """The effective per-session NPC dispatch policy (W9.24 §3.5).

    Public seam for the lane dispatcher (and anything else that must explain a
    turn's lane set): folds this session's ``chat_npc_dispatch`` writes
    latest-wins through :func:`fabulist.state.presence.npc_dispatch_policy` and
    defaults to ``decision`` / ``0`` (no bound) for a session that never wrote a
    row, a blank ``session_id`` and a legacy log alike. The fold itself lives
    with the presence ladder it shapes — this function only guards the blank
    session, so the two surfaces can never disagree about a VALID row.
    """
    if not session_id:
        return NpcDispatchPolicy()
    return npc_dispatch_policy(log, session_id)


def resolve_agent_debug(
    log: "EventLog", session_id: str, default: bool = False
) -> bool:
    """The effective per-session agent-debug capture policy (W5-S2).

    Public seam for the capture sink (``wiring.make_agent_caller``): folds this
    session's ``chat_agent_debug`` writes latest-wins, falling back to the boot
    default (``--agent-debug``) when the session never wrote one. A blank
    ``session_id`` (no active session on the call path) resolves to the default
    so the caller is never captured onto the wrong session.
    """
    if not session_id:
        return default
    return _agent_debug(log, session_id, default)


def _agent_debug_default(request: Request) -> bool:
    """The boot-flag default for agent debug (set by ``build_app``).

    ``build_app(agent_debug=True)`` makes every session default ON (today's
    behavior); without the flag this is ``False`` and a session must opt in.
    Tests that mount only this router get the safe default OFF.
    """
    return bool(getattr(request.app.state, "agent_debug_default", False))


def _recall_config(log: "EventLog", session_id: str) -> RecallConfig:
    """Fold ``chat_memory_config`` system events latest-wins onto the defaults.

    Each valid row applies as a full snapshot (PATCH always appends one), but
    :meth:`RecallConfig.from_payload` is *partial-tolerant*: a hand-edited row
    carrying only ``k`` keeps the remaining knobs from the prior fold rather
    than resetting them. The ``enabled`` key (if present) is ignored here — the
    canonical toggle lives on ``chat_memory`` (see :func:`resolve_recall_config`).

    Chain-walked (issue #86): ``chat_memory_config`` is carried CONFIG, so a
    branch folds its parent's knobs (at/below the cut) under its own rows — the
    own-row read returned the defaults for every knob last written before the
    fork.
    """
    config = RecallConfig()
    for event in log.get_chain_events(session_id):
        if event.type != "system":
            continue
        if event.payload.get("event") != CHAT_MEMORY_CONFIG_EVENT:
            continue
        config = RecallConfig.from_payload(event.payload, base=config)
    return config


def resolve_recall_config(log: "EventLog", session_id: str) -> RecallConfig:
    """The effective per-session recall policy (knobs + canonical ``enabled``).

    Public seam for the recall path (``wiring.Memorizer``): the knob fold here
    plus the canonical ``chat_memory`` toggle from :func:`_memory_enabled`, so
    the two memory surfaces can never diverge.
    """
    return replace(_recall_config(log, session_id), enabled=_memory_enabled(log, session_id))


def _persona_id(log: "EventLog", session_id: str) -> str | None:
    """Fold ``chat_persona`` system events latest-wins; absent -> ``None``.

    A row carrying a non-``str`` ``persona_id`` (e.g. an explicit ``null``
    clearing the assignment) folds to ``None``. Reads only — this router never
    writes persona events.

    Chain-walked (issue #86): a branch must echo the persona its parent held at
    the cut, the value the narrator and ``_summarize`` already resolve; an
    own-row read made a fresh branch report ``persona_id: None``.
    """
    persona_id: str | None = None
    for event in log.get_chain_events(session_id):
        if event.type != "system":
            continue
        if event.payload.get("event") != CHAT_PERSONA_EVENT:
            continue
        if "persona_id" not in event.payload:
            continue
        value = event.payload.get("persona_id")
        persona_id = value if isinstance(value, str) else None
    return persona_id


def _chat_config_view(log: "EventLog", session_id: str) -> dict:
    """The one chat-config response body (the GET and the PATCH share it).

    Shared so a PATCH reply is byte-identical to the next GET: the pane applies
    the engine's echo instead of re-reading, and the two surfaces cannot drift
    apart the way two hand-written dicts would.

    Every key is session-constant config, so every fold is chain-walked (issue
    #86): a branch echoes the settings its parent held at the cut. The one
    exception in this module is ``chat_agent_debug`` (own-row by contract; see
    :func:`_agent_debug`).
    """
    dispatch = resolve_npc_dispatch(log, session_id)
    return {
        "persona_id": _persona_id(log, session_id),
        "memory_enabled": _memory_enabled(log, session_id),
        "user_as_actor": resolve_user_as_actor(log, session_id),
        "npc_strategy": dispatch.strategy,
        "npc_max_dispatch": dispatch.max_dispatch,
    }


@router.patch("/api/sessions/{session_id}/memory")
async def set_memory(
    session_id: str, body: MemoryPatch, request: Request
) -> dict:
    """Toggle memory by appending one ``chat_memory`` system event (D16).

    Immediate and non-destructive (D9): the event lands on the chat session and
    the newest fold takes effect on the next read — no restart. ``404`` for an
    unknown session; ``422`` when ``enabled`` is missing or not a bool. A
    rejected request appends nothing.
    """
    log = _log_for(request)
    if not _session_exists(log, session_id):
        raise HTTPException(status_code=404, detail="unknown session")

    log.append_event(
        session_id,
        "system",
        USER_ACTOR,
        {"event": CHAT_MEMORY_EVENT, "enabled": body.enabled},
    )
    return {"session_id": session_id, "enabled": body.enabled}


@router.get("/api/sessions/{session_id}/memory")
async def get_memory(session_id: str, request: Request) -> dict:
    """Return the folded memory toggle: ``{"enabled": bool}`` (default True)."""
    log = _log_for(request)
    if not _session_exists(log, session_id):
        raise HTTPException(status_code=404, detail="unknown session")
    return {"enabled": _memory_enabled(log, session_id)}


@router.get("/api/sessions/{session_id}/memory-config")
async def get_memory_config(session_id: str, request: Request) -> dict:
    """Return the folded recall policy (D25 C2): ``{enabled, k, ...}``.

    ``enabled`` is the canonical ``chat_memory`` toggle; the remaining knobs are
    the latest ``chat_memory_config`` snapshot, defaulted when never set.
    """
    log = _log_for(request)
    if not _session_exists(log, session_id):
        raise HTTPException(status_code=404, detail="unknown session")
    return {"session_id": session_id, **resolve_recall_config(log, session_id).as_dict()}


@router.patch("/api/sessions/{session_id}/memory-config")
async def set_memory_config(
    session_id: str, body: MemoryConfigPatch, request: Request
) -> dict:
    """Patch one or more recall knobs by appending a full config snapshot (D25 C2).

    Merge semantics: absent fields keep their current folded value. Immediate
    and non-destructive (D9): the newest snapshot wins on the next read — no
    restart. ``404`` unknown session; ``422`` for an empty body or an
    out-of-range/unknown field (validation happens before any append).
    """
    log = _log_for(request)
    if not _session_exists(log, session_id):
        raise HTTPException(status_code=404, detail="unknown session")

    patch = body.model_dump(exclude_unset=True)
    if not patch:
        raise HTTPException(status_code=422, detail="no recall knobs supplied")

    merged = resolve_recall_config(log, session_id).with_knobs(**patch)
    log.append_event(
        session_id,
        "system",
        USER_ACTOR,
        {"event": CHAT_MEMORY_CONFIG_EVENT, **merged.knobs()},
    )
    return {
        "session_id": session_id,
        **resolve_recall_config(log, session_id).as_dict(),
    }


@router.get("/api/sessions/{session_id}/agent-debug")
async def get_agent_debug(session_id: str, request: Request) -> dict:
    """Return the folded agent-debug toggle for this session (W5-S2).

    ``enabled`` is the latest ``chat_agent_debug`` write, or the boot-flag
    default when the session never wrote one. ``404`` for an unknown session.
    """
    log = _log_for(request)
    if not _session_exists(log, session_id):
        raise HTTPException(status_code=404, detail="unknown session")
    return {
        "session_id": session_id,
        "enabled": resolve_agent_debug(log, session_id, _agent_debug_default(request)),
    }


@router.patch("/api/sessions/{session_id}/agent-debug")
async def set_agent_debug(
    session_id: str, body: AgentDebugPatch, request: Request
) -> dict:
    """Toggle agent-debug capture by appending one ``chat_agent_debug`` event.

    Immediate and non-destructive (D9): the newest fold takes effect on the next
    agent call — no restart. ``404`` unknown session; ``422`` when ``enabled`` is
    missing or not a bool. A rejected request appends nothing.
    """
    log = _log_for(request)
    if not _session_exists(log, session_id):
        raise HTTPException(status_code=404, detail="unknown session")

    log.append_event(
        session_id,
        "system",
        USER_ACTOR,
        {"event": CHAT_AGENT_DEBUG_EVENT, "enabled": body.enabled},
    )
    return {"session_id": session_id, "enabled": body.enabled}


@router.get("/api/sessions/{session_id}/chat-config")
async def get_chat_config(session_id: str, request: Request) -> dict:
    """Return the chat's config: persona, memory, actor and NPC dispatch policy.

    Every field is a latest-wins fold of this session's own system events
    (``chat_persona`` / ``chat_memory`` / ``chat_user_as_actor`` /
    ``chat_npc_dispatch``): ``persona_id`` is ``str | None``, ``memory_enabled``
    is ``bool`` (default True), ``user_as_actor`` is ``bool`` (default False,
    W9.22 §2), ``npc_strategy`` is ``decision``|``random``|``lru`` (default
    ``decision``) and ``npc_max_dispatch`` is an ``int >= 0`` SET bound (default
    ``0`` = unbounded, W9.24 §3.5). ``404`` for an unknown session.

    W9.22 deliberately WIDENS this shape instead of adding a third session-config
    read: the Cast pane needs the persona (player label) and the User-as-Actor
    setting in ONE read, and the presence lane reads the same setting in-engine
    through :func:`resolve_user_as_actor`. W9.24 adds the dispatch policy for the
    same reason (one pane, one read, one write). D25 C2 recall knobs still live
    on the dedicated ``/memory-config`` endpoints — they are a different object
    (a numeric policy with clamps), not a session flag.

    NOTE: the canonical memory-scope resolver is
    :func:`fabulist.memory.scope.resolve_scope`; this route reads the same
    events and exists so the UI need not import the memory lane.
    """
    log = _log_for(request)
    if not _session_exists(log, session_id):
        raise HTTPException(status_code=404, detail="unknown session")
    return _chat_config_view(log, session_id)


@router.patch("/api/sessions/{session_id}/chat-config")
async def set_chat_config(
    session_id: str, body: ChatConfigPatch, request: Request
) -> dict:
    """Write the PER-KEY-OPTIONAL chat settings (W9.22 §2, W9.24 §4).

    Each key PRESENT appends its own setting event (append-only, D9: the newest
    fold wins on the next read — no restart, no side table); a key ABSENT
    changes nothing. Returns the full config view (the same body the GET
    returns) so the pane confirms what the engine folded rather than assuming
    its own write.

    ``user_as_actor`` is one ``chat_user_as_actor`` row as before. The NPC
    dispatch pair writes ONE ``chat_npc_dispatch`` row carrying BOTH keys merged
    onto the folded policy (the recall-knob full-snapshot rule), so a bound-only
    PATCH cannot reset the strategy — or the other way round.

    ``404`` unknown session; ``422`` for an empty body, an explicit ``null``, an
    unknown key or an out-of-range value. Validation happens before any append,
    so a rejected request writes nothing (not even the keys that were valid).
    """
    log = _log_for(request)
    if not _session_exists(log, session_id):
        raise HTTPException(status_code=404, detail="unknown session")

    patch = body.model_dump(exclude_unset=True)
    if not patch:
        raise HTTPException(status_code=422, detail="no chat settings supplied")
    # An explicit ``null`` is not a value here: absent means "leave it alone",
    # so a null is refused rather than silently treated as unchanged.
    nulls = sorted(key for key, value in patch.items() if value is None)
    if nulls:
        raise HTTPException(
            status_code=422, detail=f"null is not a value for {', '.join(nulls)}"
        )

    if "user_as_actor" in patch:
        log.append_event(
            session_id,
            "system",
            USER_ACTOR,
            {"event": CHAT_USER_AS_ACTOR_EVENT, "user_as_actor": patch["user_as_actor"]},
        )

    if "npc_strategy" in patch or "npc_max_dispatch" in patch:
        current = resolve_npc_dispatch(log, session_id)
        log.append_event(
            session_id,
            "system",
            USER_ACTOR,
            {
                "event": NPC_DISPATCH_EVENT,
                "strategy": patch.get("npc_strategy", current.strategy),
                "max_dispatch": patch.get("npc_max_dispatch", current.max_dispatch),
            },
        )

    return _chat_config_view(log, session_id)


@router.get("/api/sessions/{session_id}/assistant")
async def get_session_assistant(session_id: str, request: Request) -> dict:
    """Return the session's enabled assistant: ``{"assistant_id": str | null}``.

    Fork-aware latest-wins fold of ``chat_assistant`` writes (see
    :func:`fabulist.assistants.panel.latest_chat_assistant`); an absent event
    and an explicit ``null`` both read as ``null``. ``404`` unknown session.
    """
    log = _log_for(request)
    if not _session_exists(log, session_id):
        raise HTTPException(status_code=404, detail="unknown session")
    return {"assistant_id": latest_chat_assistant(log, session_id)}


@router.patch("/api/sessions/{session_id}/assistant")
async def set_session_assistant(
    session_id: str, body: AssistantPatch, request: Request
) -> dict:
    """Set (or clear, with ``null``) the single enabled assistant (D38 lane 2c).

    Appends one ``chat_assistant`` system event and echoes the resulting slot
    (``{"assistant_id": str | null}``). ``null`` disables; an id the catalog does
    not expose is a ``404`` (nothing appended). A **persona id** is also accepted
    (Bug 4): the engine synthesizes an assistant from the persona, so a persona
    can be used directly as the assistant with nothing cloned. ``422`` for a
    missing key/unknown field; ``404`` for an unknown session.
    """
    log = _log_for(request)
    if not _session_exists(log, session_id):
        raise HTTPException(status_code=404, detail="unknown session")

    if body.assistant_id is not None:
        # Lazy import: the definition fold lives with the assistants router; this
        # keeps the config module's import graph thin (mirrors its style).
        from fabulist.api.assistants_api import known_assistant_ids

        if body.assistant_id not in known_assistant_ids(log) and not _is_persona_id(
            log, body.assistant_id
        ):
            raise HTTPException(
                status_code=404, detail=f"unknown assistant {body.assistant_id!r}"
            )

    log.append_event(
        session_id,
        "system",
        USER_ACTOR,
        {"event": CHAT_ASSISTANT_EVENT, "assistant_id": body.assistant_id},
    )
    return {"assistant_id": body.assistant_id}
