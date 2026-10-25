"""Agent debug capture (D17): the history of an agent call + the context
snapshot it saw for its turn — not always on, but available.

Seams this module builds on (cited, not modified)
--------------------------------------------------
* ``fabulist.wiring.RegistryModelCaller.__call__`` is the streaming caller we
  wrap. Its yield protocol is the shared W1.1 ``StreamEvent`` shape:
  ``{"type": "reasoning"|"content"|"error", "delta": str, "meta": dict}``.
  Reasoning deltas carry the raw provider ``reasoning_content``; content
  deltas carry the DSL-segmented text with ``meta={"channel": "prose"|"dsl"}``;
  a truncated generation is surfaced as one ``{"type": "error", "delta": ...,
  "meta": {"finish_reason": "length"}}`` event at the very end
  (``wiring.RegistryModelCaller.__call__``). :class:`DebugCapture` is a
  drop-in: it forwards every event unchanged and in order.
* ``fabulist.log.repo.EventLog.append_event`` / ``get_events`` — the log owns
  everything (D9). ``system`` payloads pass the schema gate loosely
  (``fabulist.log.schema_gate`` ``"system": Schema(required={}, optional={})``),
  so an ``agent_call`` system payload needs no gate change; ``get_events`` is
  the raw audit view (no fork walk, no revision resolution) used to read the
  captures back.
* ``fabulist.api.inspector_api`` is the existing per-turn view
  (``GET /api/sessions/{id}/inspect/turns``) that pairs ``user`` -> next
  ``narrative`` and surfaces the narrative's config ``facts_hash``/latency.
  The debug capture complements it: the inspector shows the *result* of a turn,
  while ``agent_call`` rows show the *inputs* (the exact messages) and raw
  reasoning/content of any agent call, including non-narrator/nullable agents.

Capture policy
--------------
Append-only (D9): captures are ``system`` events and are never deleted; the API
exposes read endpoints only (see ``fabulist.api.agent_debug_api``).

Cost rationale for the size guard
---------------------------------
A capture is a debugging aid, not always-on, so it must never let one runaway
call bloat the log. ``RegistryModelCaller`` already documents that reasoning
models can emit the entire budget (including full scene drafts) inside
``reasoning_content``, so a single call can be hundreds of KB. Every captured
char is written to the sqlite WAL and re-read by projections, so
:data:`MAX_CAPTURE_CHARS` (200_000) caps the *combined* response+reasoning text
per call — enough for a complete turn snapshot while keeping the row near
200 KB. Individual message contents are capped at
:data:`MESSAGE_CONTENT_LIMIT` (20_000): a context snapshot's bulk is the
injected facts/history suffix, and a per-message cap keeps the snapshot legible
without hiding which messages existed.

Deviations
----------
* ``DebugCapture.__init__`` keeps the contract signature ``(caller, sink)``.
  ``agent`` and ``facts_hash`` are read from the wrapped caller by duck-typing
  (``getattr``) at call time — ``RegistryModelCaller`` exposes ``.agent`` but no
  ``.facts_hash`` (the per-turn hash lives in the graph's provenance outbox and
  reaches the log on the ``narrative`` event, not the caller), so
  ``facts_hash`` defaults to ``""`` unless a caller exposes it.
* ``DebugCapture.__init__`` accepts an OPTIONAL keyword-only ``enabled``
  predicate ``() -> bool`` (W5-S2 runtime toggle). It is evaluated once at the
  start of each call; when it returns false the wrapper is a pure pass-through —
  no accumulation, no truncation, no sink — so a session with debug off pays
  only the predicate. When ``enabled`` is ``None`` the wrapper captures
  unconditionally (the historical behavior; existing callers are unaffected).
* ``calls_for`` returns *all* ``agent_call`` rows including ephemeral ones
  (``get_events`` is the audit view); the API routers that hide ephemeral rows
  (``assistants_api``) are listing conversation surface, not debug surface.
"""

from __future__ import annotations

import logging
import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, AsyncIterator, Callable

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fabulist.log import EventLog

logger = logging.getLogger(__name__)

#: The system-event kind carrying one captured agent call.
AGENT_CALL_EVENT = "agent_call"

#: Per-turn session context. The ModelCaller protocol is ``(messages)``
#: only — no session id rides the call — so the ENGINE sets this contextvar
#: per turn (asyncio tasks inherit context at creation) and the sink reads
#: it to place the capture on the right session's log rows.
CURRENT_SESSION: ContextVar[str] = ContextVar("fabulist_agent_session", default="")

#: Per-call interaction label. OOC rides the same wrapped caller as the
#: narrator (one ``self._model``), so the capture row's ``agent`` would
#: otherwise mislabel OOC turns as narrator calls. The ENGINE sets this
#: contextvar per call path (narrate/ooc); the sink stamps the capture with it.
CURRENT_INTERACTION: ContextVar[str] = ContextVar("fabulist_agent_interaction", default="")

#: Combined cap (chars) on ``response_text`` + ``reasoning_text`` per call.
MAX_CAPTURE_CHARS = 200_000

#: Cap (chars) on a single message's string content in the stored snapshot.
MESSAGE_CONTENT_LIMIT = 20_000

#: Suffix convention marking a field we shortened for the log.
_TRUNCATION_SUFFIX = "[truncated {dropped} chars]"


@dataclass(frozen=True)
class AgentCall:
    """One captured agent call, as read back from the log.

    ``seq`` is the capture event's own sequence number (the append-order id
    within ``session_id``); ``messages`` is the context snapshot the caller
    actually received.
    """

    agent: str
    session_id: str
    seq: int
    messages: list[dict]
    response_text: str
    reasoning_text: str
    finish: str | None
    latency_s: float
    facts_hash: str
    meta: dict = field(default_factory=dict)


def _truncate(text: str, limit: int) -> str:
    """Shorten ``text`` to ``limit`` chars, appending a dropped-count suffix.

    A non-positive ``limit`` keeps the suffix (so the reader knows content was
    dropped) but no original characters; empty text is returned unchanged, so
    "nothing to capture" never fabricates a truncation marker.
    """
    if not text or len(text) <= limit:
        return text
    if limit <= 0:
        return _TRUNCATION_SUFFIX.format(dropped=len(text))
    dropped = len(text) - limit
    return text[:limit] + _TRUNCATION_SUFFIX.format(dropped=dropped)


def _guard_message(message: Any) -> Any:
    """Copy one message, capping over-long string content.

    ``content`` may be a plain ``str`` (the common case) or the multipart list
    used by the native-vision path (``{"type": "text", "text": ...}`` parts);
    text parts are capped too so a vision turn cannot smuggle an unbounded
    snapshot past the guard.
    """
    if not isinstance(message, dict):
        return message
    guarded = dict(message)
    content = guarded.get("content")
    if isinstance(content, str):
        guarded["content"] = _truncate(content, MESSAGE_CONTENT_LIMIT)
    elif isinstance(content, list):
        parts: list[Any] = []
        for part in content:
            if (
                isinstance(part, dict)
                and isinstance(part.get("text"), str)
            ):
                part = {**part, "text": _truncate(part["text"], MESSAGE_CONTENT_LIMIT)}
            parts.append(part)
        guarded["content"] = parts
    return guarded


def _guard(wire: dict) -> dict:
    """Apply the size guard to a capture wire dict, in place.

    Response text has priority for the per-call budget; reasoning takes
    whatever remains (dropped entirely with a suffix when response alone fills
    the cap).
    """
    response = str(wire.get("response_text") or "")
    reasoning = str(wire.get("reasoning_text") or "")
    response = _truncate(response, MAX_CAPTURE_CHARS)
    remaining = MAX_CAPTURE_CHARS - len(response)
    reasoning = _truncate(reasoning, remaining)
    wire["response_text"] = response
    wire["reasoning_text"] = reasoning
    messages = wire.get("messages")
    if isinstance(messages, list):
        wire["messages"] = [_guard_message(m) for m in messages]
    return wire


class DebugCapture:
    """Drop-in :class:`~fabulist.graph.types.ModelCaller` wrapper that records.

    Wrapping a caller preserves its yield protocol byte-for-byte: every event
    is forwarded immediately, in order, unchanged. On normal completion the
    accumulated ``reasoning``/``content`` deltas, provider ``finish`` reason,
    elapsed latency (``time.monotonic``), the context snapshot, and the caller's
    ``agent``/``facts_hash`` are handed to ``sink`` exactly once as a wire dict.

    Capture must never break a turn: a raising ``sink`` is swallowed and logged
    at exception level; the stream has already been forwarded intact.
    """

    def __init__(
        self,
        caller: Any,
        sink: Callable[[dict], None],
        *,
        enabled: Callable[[], bool] | None = None,
    ) -> None:
        self.caller = caller
        self.sink = sink
        self.enabled = enabled

    def _capturing(self) -> bool:
        """Whether this call should be recorded (W5-S2 runtime toggle).

        ``None`` predicate means "always" (historical contract). A raising
        predicate is treated as off and logged: capture is advisory and must
        never break a turn.
        """
        if self.enabled is None:
            return True
        try:
            return bool(self.enabled())
        except Exception:  # noqa: BLE001 - capture is advisory, never fatal
            logger.exception("agent debug toggle check failed; skipping capture")
            return False

    async def __call__(
        self,
        messages: list[dict],
        stream: bool = True,
        request_profile: dict | None = None,
    ) -> AsyncIterator[dict]:
        # W5-S2: a session with capture off is a pure pass-through — forwarded
        # event-for-event, with no delta accumulation or truncation work.
        # ``request_profile`` (W8.4) is forwarded only when set, so a caller
        # that predates the kwarg is unaffected on the narrator/OOC path.
        profile_kw = {} if request_profile is None else {"request_profile": request_profile}
        if not self._capturing():
            async for event in self.caller(messages, stream, **profile_kw):
                yield event
            return
        started = time.monotonic()
        reasoning_parts: list[str] = []
        content_parts: list[str] = []
        tool_call_parts: list[str] = []
        finish: str | None = None
        meta: dict = {}
        async for event in self.caller(messages, stream, **profile_kw):
            kind = event.get("type")
            if kind == "reasoning":
                reasoning_parts.append(event.get("delta", ""))
            elif kind == "content":
                content_parts.append(event.get("delta", ""))
            elif kind == "tool_call":
                # W9.14: a structured call's real output rides here. Without
                # this the capture could not distinguish "called the tool" from
                # "answered nothing" — the blind spot that hid the tool_choice
                # defect (an empty response_text was read as model behaviour).
                tool_call_parts.append(str(event.get("delta", "") or ""))
            elif kind == "error":
                event_meta = event.get("meta")
                if isinstance(event_meta, dict):
                    meta = {**meta, **event_meta}
                    if event_meta.get("finish_reason"):
                        finish = str(event_meta["finish_reason"])
            yield event
        latency_s = time.monotonic() - started
        wire = {
            "event": AGENT_CALL_EVENT,
            "agent": str(getattr(self.caller, "agent", "") or ""),
            "messages": [dict(m) if isinstance(m, dict) else m for m in messages],
            "response_text": "".join(content_parts),
            "reasoning_text": "".join(reasoning_parts),
            "response_tool_calls": "".join(tool_call_parts),
            "finish": finish,
            "latency_s": latency_s,
            "facts_hash": str(getattr(self.caller, "facts_hash", "") or ""),
            "meta": meta,
        }
        _guard(wire)
        try:
            self.sink(wire)
        except Exception:  # noqa: BLE001 - capture is advisory, never fatal
            logger.exception("agent debug capture failed for %s", wire["agent"])


def append_call_event(log: "EventLog", session_id: str, wire_dict: dict) -> None:
    """Append one capture wire dict as a ``system`` event on ``session_id``.

    The log owns the capture (D9); ``system`` payloads are intentionally
    unconstrained by the schema gate, so the wire dict rides through unchanged
    with the actor set to the agent that made the call.
    """
    payload = dict(wire_dict)
    payload.setdefault("event", AGENT_CALL_EVENT)
    actor = str(payload.get("agent") or "system")
    log.append_event(session_id, "system", actor, payload)


def calls_for(
    log: "EventLog", session_id: str, agent: str | None = None
) -> list[AgentCall]:
    """Read every ``agent_call`` system event for a session, seq-ordered.

    Returns ALL captures (not latest-per-agent): the debug view is a history of
    calls. ``agent`` optionally narrows to one caller. Ephemeral rows are
    included because :meth:`EventLog.get_events` is the raw audit view.
    """
    calls: list[AgentCall] = []
    for event in log.get_events(session_id):
        if event.type != "system":
            continue
        payload = event.payload
        if not isinstance(payload, dict) or payload.get("event") != AGENT_CALL_EVENT:
            continue
        call_agent = str(payload.get("agent") or "")
        if agent is not None and call_agent != agent:
            continue
        messages = payload.get("messages")
        meta = payload.get("meta")
        finish = payload.get("finish")
        calls.append(
            AgentCall(
                agent=call_agent,
                session_id=event.session_id,
                seq=event.seq,
                messages=messages if isinstance(messages, list) else [],
                response_text=str(payload.get("response_text") or ""),
                reasoning_text=str(payload.get("reasoning_text") or ""),
                finish=str(finish) if isinstance(finish, str) else None,
                latency_s=float(payload.get("latency_s") or 0.0),
                facts_hash=str(payload.get("facts_hash") or ""),
                meta=meta if isinstance(meta, dict) else {},
            )
        )
    return calls
