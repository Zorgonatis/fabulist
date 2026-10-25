"""TurnEngine implementation bridging the LangGraph graph to `StreamEvent`s.

``GraphTurnEngine.run_turn`` drives the compiled graph with
``astream(stream_mode="custom")``: the narrator node writes each model
`StreamEvent` to the graph's custom stream and this engine forwards them to the
caller. The event log remains the source of truth (D11) — the engine only
writes to the log on the paths the graph does not cover (OOC, cancelled turns).

OOC short-circuit (preset Tier-0 semantics)
-------------------------------------------
``kind == "ooc"`` never enters the graph. The scene must not advance: no clock,
no facts, no state-delta, no post-turn extractor. The engine calls the same
`ModelCaller` directly with an out-of-character system marker, streams the
reply, and appends ``user`` + ``ooc`` events. Because no ``state-delta`` is
written, the next in-character turn sees the same facts it would have.

Cancel semantics
----------------
``cancel()`` sets a per-engine ``asyncio.Event``. ``run_turn`` checks it after
each forwarded event and, when set, closes the graph stream mid-run. That
cancels the narrator task so ``post_turn`` (and therefore the extractor) never
executes. The partial prose already streamed is appended as an **ephemeral**
``narrative`` event (prunable, W2.2) and the turn ends with a ``done`` event
carrying ``meta={"cancelled": True}``. An ephemeral partial is never a kept
turn, so it is never extracted, indexed, or branched on.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from typing import Any, Protocol

from .rolls import rolls_for_turn
from .types import LogPort, ModelCaller, StreamEvent, content_channel, initial_turn_state
from fabulist.memory.recall import RecallResult

__all__ = ["GraphTurnEngine", "OOC_PROMPT_MARKER", "TurnEngine"]

#: System marker for the Tier-0 out-of-character path. The prompt lane may
#: replace this via ``ooc_messages``.
OOC_PROMPT_MARKER = (
    "[out-of-character] Reply directly as the assistant. Do not narrate the "
    "scene and do not advance time or state."
)

_VALID_KINDS = ("in_character", "ooc")


def default_ooc_messages(session_id: str, content: str) -> list[dict]:
    """Bare Tier-0 OOC path (no history/state).

    Production wiring replaces this with a builder that injects recent scene
    history and the folded state facts — the OOC assistant answers WITH
    context, it just never advances the scene.
    """
    return [
        {"role": "system", "content": OOC_PROMPT_MARKER},
        {"role": "user", "content": content},
    ]


class TurnEngine(Protocol):
    """Structural protocol for a turn engine (shared W1.1 contract)."""

    def run_turn(
        self, session_id: str, content: str, kind: str
    ) -> AsyncIterator[StreamEvent]: ...


class GraphTurnEngine:
    """Drive the graph and bridge its custom stream to `StreamEvent`s."""

    def __init__(
        self,
        *,
        graph,
        log: LogPort,
        model: ModelCaller,
        ooc_messages=default_ooc_messages,
        recall_fn: Any | None = None,
        graph_source: Any | None = None,
    ) -> None:
        self._graph = graph
        # W9.5 (R10): ``graph_source`` returns ``(revision, compiled_graph)``
        # for the CURRENT preset topology. The check runs at turn start, so a
        # topology Save takes effect on the next turn without a restart; an
        # unchanged revision costs one string compare.
        self._graph_source = graph_source
        self._graph_rev: str | None = None
        self._log = log
        self._model = model
        self._recall_fn = recall_fn
        self._ooc_messages = ooc_messages
        self._cancel = asyncio.Event()

    # -- control -----------------------------------------------------------
    def cancel(self) -> None:
        """Request cancellation of the in-flight (or next-started) turn."""
        self._cancel.set()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    # -- turn --------------------------------------------------------------
    async def run_turn(
        self,
        session_id: str,
        content: str,
        kind: str = "in_character",
        *,
        image_url: str | None = None,
        image_kind: str = "image",
    ) -> AsyncIterator[StreamEvent]:
        if kind not in _VALID_KINDS:
            raise ValueError(f"unknown turn kind: {kind!r}")
        # D17 agent-debug: the ModelCaller protocol carries no session id, so
        # the turn's session rides this contextvar for any DebugCapture sink
        # downstream (asyncio tasks inherit context at creation).
        from fabulist.agents.debug import CURRENT_INTERACTION, CURRENT_SESSION

        CURRENT_SESSION.set(session_id)
        CURRENT_INTERACTION.set("ooc" if kind == "ooc" else "narrate")
        self._cancel.clear()
        if kind == "ooc":
            async for event in self._run_ooc(session_id, content):
                yield event
            return
        async for event in self._run_in_character(
            session_id, content, image_url, image_kind
        ):
            yield event

    async def _run_in_character(
        self,
        session_id: str,
        content: str,
        image_url: str | None = None,
        image_kind: str = "image",
    ) -> AsyncIterator[StreamEvent]:
        if self._graph_source is not None:
            try:
                revision, compiled = self._graph_source()
            except Exception:  # noqa: BLE001 - a rebuild failure keeps the last graph
                revision, compiled = self._graph_rev, None
            if compiled is not None and revision != self._graph_rev:
                self._graph = compiled
                self._graph_rev = revision
        config = {"configurable": {"thread_id": session_id}}
        initial = initial_turn_state(session_id, content, "in_character")
        if image_url:
            # Native vision: the narrator's final user message carries the
            # image as a content part (VLM path) instead of a caption prefix.
            initial["image_url"] = image_url
            initial["image_kind"] = image_kind
        roll_results = rolls_for_turn(content)
        if roll_results:
            # GM rolls (PLAN §4 pre-turn): dice are dice — the engine commits
            # roll events BEFORE the narrative so replay/fork keep them.
            lines = []
            for r in roll_results:
                self._log.append_event(session_id, "roll", "gm", r.as_payload())
                lines.append(f"{r.dice} -> {r.result} ({r.detail})")
            initial["rolls"] = "### Rolls this turn\n" + "\n".join(lines)
        if self._recall_fn is not None:
            # Pre-turn vector recall (W3.2): embed the user's message and
            # surface the nearest older turns as a facts-suffix block.
            # D25 C4: recall_fn may return a RecallResult (block + exact
            # queries + resolved config) — unpack JSON-safe (checkpointer
            # state must stay JSON) so the inspector RAG panel can show it.
            try:
                outcome = await self._recall_fn(session_id, content)
            except Exception:  # recall must never break a turn
                outcome = None
            if isinstance(outcome, RecallResult):
                initial["recall"] = outcome.block or ""
                initial["recall_queries"] = list(outcome.queries)
                initial["recall_config"] = dict(outcome.config.as_dict())
                # W5C-1: carry the surfaced rows into state (JSON-safe: keep
                # only primitive values) so post_turn stamps them for the
                # inspector RAG panel's per-result view.
                initial["recall_rows"] = [
                    {
                        key: value
                        for key, value in dict(row).items()
                        if value is None or isinstance(value, (str, int, float, bool))
                    }
                    for row in outcome.rows
                ]
            else:
                initial["recall"] = outcome or ""
                initial["recall_queries"] = []
                initial["recall_config"] = {}
                initial["recall_rows"] = []
        prose: list[str] = []
        saw_dsl = False
        cancelled = False
        # W9.19 (A): once the commit barrier passed, the turn is KEPT. A cancel
        # after that point must not roll a kept turn back into an ephemeral
        # partial — the worst a late cancel may do is abandon background work.
        committed = False
        try:
            async with contextlib.aclosing(
                self._graph.astream(initial, config, stream_mode="custom")
            ) as stream:
                async for chunk in stream:
                    if not _is_stream_event(chunk):
                        continue
                    if chunk.get("type") == "committed":
                        committed = True
                    if chunk.get("type") == "content":
                        if (chunk.get("meta") or {}).get("replace"):
                            # A replace frame carries the WHOLE final turn (the
                            # narrator rewrote its draft engine-side): the drained
                            # text is superseded, not extended — mirror of the
                            # client's accumulator reset, so a cancel in this
                            # window still writes the right ephemeral partial.
                            prose = [chunk.get("delta", "")]
                        elif content_channel(chunk) == "prose":
                            prose.append(chunk.get("delta", ""))
                        else:
                            saw_dsl = True
                    yield chunk
                    if self._cancel.is_set() and not committed:
                        cancelled = True
                        break
        except Exception as exc:  # surface node/model failures as an error event
            # Issue #46: a node that refuses the turn (the commit guard's
            # ``EmptyTurnAbort``) carries a stable ``reason`` so the client can
            # say WHY nothing was kept; otherwise the frame shape is the
            # ADR-023 abort frame unchanged.
            meta: dict[str, Any] = {"session_id": session_id}
            reason = getattr(exc, "reason", None)
            if isinstance(reason, str) and reason:
                meta["reason"] = reason
            yield {"type": "error", "delta": str(exc), "meta": meta}
            return

        if cancelled or (self._cancel.is_set() and not committed):
            self._finish_cancelled(session_id, content, "".join(prose))
            yield {"type": "done", "meta": {"cancelled": True}}
            return
        if not prose and not saw_dsl:
            # Reasoning-only turn (observed live): the model deliberated and
            # produced zero content tokens. Never a silent blank bubble.
            yield {
                "type": "error",
                "delta": "turn produced no content (reasoning only — retry, or raise max_tokens)",
                "meta": {"session_id": session_id, "reason": "empty_content"},
            }
        yield {"type": "done", "meta": {}}

    def _finish_cancelled(self, session_id: str, content: str, partial: str) -> None:
        self._log.append_event(session_id, "user", "user", {"text": content})
        if partial:
            self._log.append_event(
                session_id,
                "narrative",
                "narrator",
                {"prose": partial, "dsl": ""},
                ephemeral=True,
                meta={"partial": True, "reason": "cancelled"},
            )

    async def _run_ooc(
        self, session_id: str, content: str
    ) -> AsyncIterator[StreamEvent]:
        prose: list[str] = []
        try:
            async for event in self._model(self._ooc_messages(session_id, content), True):
                if event.get("type") == "content" and content_channel(event) == "prose":
                    prose.append(event.get("delta", ""))
                yield event
        except Exception as exc:
            yield {"type": "error", "delta": str(exc), "meta": {"session_id": session_id}}
            return
        # Echo-dedup (mirrors the in-character post_turn fork-aware check):
        # the API layer already appended the turn-start user event — appending
        # again duplicated every OOC user message in history (found in a D17
        # capture: the OOC turn's text appeared twice in its own context).
        events = self._log.get_events(session_id)
        already = any(
            e.type == "user" and str(e.payload.get("text", "")) == content
            for e in events
        )
        if not already:
            self._log.append_event(session_id, "user", "user", {"text": content})
        self._log.append_event(
            session_id, "ooc", "assistant", {"text": "".join(prose)}
        )
        yield {"type": "done", "meta": {"kind": "ooc"}}


def _is_stream_event(chunk: object) -> bool:
    return isinstance(chunk, dict) and "type" in chunk
