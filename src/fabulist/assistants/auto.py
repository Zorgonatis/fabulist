"""Per-turn assistant consult hook (D38 lane 2c).

When a chat has an assistant enabled (``chat_assistant`` latest-wins fold), the
engine consults that assistant once per kept turn — fire-and-forget, after the
narrator's turn commits — and appends its card as log events. This is the same
flow the ``POST .../consult`` route runs, minus the request: the enabled id and
the scene digest both come from the event log.

Event sequence the hook appends (all ``system`` actor ``assistant``, D9)::

    {"event": "assistant_reply", "assistant": "<id>", "text": "<prose>", "agent": "<agent>"}
    {"event": "assistant_panel", "assistant": "<id>", "statement": "asst = AssistantCard(...)"}

The ``text`` on the reply event is PROSE ONLY — the ``asst = AssistantCard(...)``
statement is split off via :func:`~fabulist.assistants.panel.split_assistant_reply`
and travels solely on the panel event, so the history rows never render raw DSL.

The panel event is appended only when :func:`parse_assistant_panel` lifts a
well-formed card out of the reply; a reply without a card (or a malformed one)
still records ``assistant_reply`` and nothing else. A consult failure appends a
non-fatal ``{"event": "assistant_failed", "error": ...}`` event — the hook never
raises out to the turn, and never advances clock/state/history.

Wiring (orchestrator-owned): the returned callable takes only ``session_id``
(``Callable[[str], Awaitable[None]]``) so it drops into the ``_schedule_post_turn``
hook tuple exactly like the compact/memory hooks; the log and the per-agent
model-caller factory are closed over at wiring time::

    from fabulist.assistants.auto import make_assistant_turn_hook

    assistant_hook = make_assistant_turn_hook(log, model_caller_factory)
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from fabulist.assistants.manager import AssistantContext, AssistantManager
from fabulist.assistants.panel import (
    ASSISTANT_PANEL_EVENT,
    latest_chat_assistant,
    parse_assistant_panel,
    scene_facts_line,
    scene_recent_prose,
    split_assistant_reply,
)
from fabulist.log.schema_gate import ASSISTANT_REPLY_EVENT

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fabulist.graph.types import ModelCaller
    from fabulist.log import EventLog

__all__ = ["ASSISTANT_FAILED_EVENT", "make_assistant_turn_hook"]

logger = logging.getLogger(__name__)

#: System-event kind appended when a per-turn consult fails (non-fatal, open shape).
ASSISTANT_FAILED_EVENT = "assistant_failed"

#: Cap on the error text stored on a failure event (mirrors the post-turn hooks).
_FAILURE_ERROR_LIMIT = 800


def _latest_user_message(log: "EventLog", session_id: str) -> str:
    """The newest user turn's text ('' when the session has none)."""
    messages = log.get_projection(session_id).get("user_messages") or []
    if not messages:
        return ""
    payload = messages[-1].get("payload") or {}
    text = payload.get("text")
    return text if isinstance(text, str) else ""


def _append_failed(
    log: "EventLog", session_id: str, assistant_id: str, error: str
) -> None:
    """Record a non-fatal consult failure as a system event (best-effort)."""
    try:
        log.append_event(
            session_id,
            "system",
            "assistant",
            {
                "event": ASSISTANT_FAILED_EVENT,
                "assistant": assistant_id,
                "error": error[:_FAILURE_ERROR_LIMIT],
            },
        )
    except Exception:  # noqa: BLE001 - recording a failure is best-effort
        logger.exception("could not record assistant failure for %s", session_id)


async def _run_assistant_turn(
    log: "EventLog",
    session_id: str,
    model_caller_factory: Callable[[str], "ModelCaller"],
    flow: Callable[[Any, Any], list[dict]] | None,
) -> None:
    """Consult the session's enabled assistant and append its events."""
    assistant_id = latest_chat_assistant(log, session_id)
    if not assistant_id:
        return  # disabled (or never enabled) -> silent

    # W5-S2 parity with the consult route: the hook runs off-request, so the
    # turn's CURRENT_SESSION contextvar is unset here; set it before the model
    # call so an agent-debug capture lands on THIS session.
    from fabulist.agents.debug import CURRENT_SESSION

    CURRENT_SESSION.set(session_id)

    # Lazy import keeps the API layer out of module load (engine->api is an
    # established pattern in this repo, kept here to the one shared fold).
    from fabulist.api.assistants_api import (
        assistant_definitions,
        resolve_assistant_definition,
    )

    # Bug 4: the enabled id may name a persona (used directly as the assistant).
    # Resolve through the shared seam so this hook and the consult route agree;
    # a persona yields a synthesized manual narrator definition, nothing cloned.
    definitions = assistant_definitions(log)
    definition = resolve_assistant_definition(log, assistant_id, definitions=definitions)
    if definition is None:
        _append_failed(log, session_id, assistant_id, f"unknown assistant {assistant_id!r}")
        return
    if not any(existing.id == definition.id for existing in definitions):
        definitions = [*definitions, definition]

    manager = AssistantManager(definitions, model_caller_factory)
    if flow is not None:
        # D18 completion: the assistant interaction's module flow, when the
        # orchestrator supplies one (preset-customized); None keeps the default.
        # Issue #70: bind THIS consult's session id into the flow call, so a
        # swallowed preset read leaves its ``preset_degraded`` row on the turn.
        manager._messages_builder = lambda definition, context: flow(
            definition, context, session_id
        )
    context = AssistantContext(
        facts=scene_facts_line(log, session_id),
        recent_prose=scene_recent_prose(log, session_id),
        user_msg=_latest_user_message(log, session_id),
    )

    try:
        reply = await manager.consult(assistant_id, context)
    except KeyError as exc:  # enabled id no longer resolves (definition retracted)
        _append_failed(log, session_id, assistant_id, str(exc))
        return
    except Exception as exc:  # noqa: BLE001 - a consult failure is never fatal
        logger.exception("assistant turn consult failed for %s", session_id)
        _append_failed(log, session_id, assistant_id, f"{type(exc).__name__}: {exc}")
        return

    # Log PROSE ONLY: the raw reply ends in the ``asst = AssistantCard(...)``
    # statement, which is not prose and must never reach the history rows. The
    # statement itself stays on the separate ``assistant_panel`` event below.
    prose, _statement = split_assistant_reply(reply.text)
    log.append_event(
        session_id,
        "system",
        "assistant",
        {
            "event": ASSISTANT_REPLY_EVENT,
            "assistant": reply.assistant_id,
            "text": prose,
            "agent": reply.agent,
        },
    )

    card = parse_assistant_panel(reply.text, manager.get(reply.assistant_id))
    if card is not None:
        log.append_event(
            session_id,
            "system",
            "assistant",
            {
                "event": ASSISTANT_PANEL_EVENT,
                "assistant": reply.assistant_id,
                "statement": card.to_statement(),
            },
        )


def make_assistant_turn_hook(
    log: "EventLog",
    model_caller_factory: Callable[[str], "ModelCaller"],
    *,
    flow: Callable[[Any, Any], list[dict]] | None = None,
) -> Callable[[str], Awaitable[None]]:
    """Build the per-turn assistant consult hook.

    The returned callable takes only ``session_id`` so it drops into the
    ``_schedule_post_turn`` hook tuple; ``log`` and ``model_caller_factory`` are
    closed over at wiring time (mirroring the compact/memory hook closures).
    ``flow`` is the optional preset-customized assistant message builder
    (``app.state.assistants_flow``); ``None`` keeps the fixed persona-first shape.
    """

    async def assistant_turn_hook(session_id: str) -> None:
        await _run_assistant_turn(log, session_id, model_caller_factory, flow)

    return assistant_turn_hook
