"""Shared types and injectable seams for the engine graph (W1.1).

The graph is deliberately ignorant of *how* models are called and *how* events
are written: those are Protocols, so tests inject fakes and the orchestrator
wires the real narrator prompt (W1.2), extractor (W1.3), and log (W0.2) later.

Nothing in this module imports a concrete model client or the log package.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Annotated, Any, Literal, Protocol, TypedDict

from typing_extensions import NotRequired

#: Event types on the streaming channel, per the shared W1.1 contract. ``tool_call``
#: (W8.4) carries a function-call argument fragment for a structured request.
#: ``finish`` (W9.26 §2.3 step 5) is a TERMINAL side-band frame carrying the
#: provider's ``finish_reason``; it deliberately is NOT ``done`` (that one ends a
#: TURN in ``api/app.py``). It rides BOTH call paths: the summarizer caller has
#: emitted it since W9.26, and the narrator path carries it too (issue #28 — with
#: no frame there, the narrator's ``finish_reason`` is absent on every call, so a
#: clean stop, a ``length`` truncation and a provider content-filter are
#: indistinguishable and ADR-021's allowlist is inert; the caller is built with
#: ``RegistryModelCaller(..., emit_finish=True)``). The frame is SIDE-BAND by
#: contract: it carries no ``delta`` and arrives after the last content delta, so
#: the prose a turn streams is byte-identical whether or not it is emitted, and a
#: reader that drains only ``content``/``reasoning``/``tool_call`` is unaffected.
StreamEventType = Literal[
    "reasoning", "content", "tool_call", "done", "error", "finish"
]

#: One unit on the turn stream. `delta` carries a token/chunk for `reasoning`
#: and `content`; `meta` carries side-band data (cancel flags, OOC marker, ...).
#: ``finish_reason`` rides the terminal ``finish`` frame at the TOP level (the
#: reader also accepts it under ``meta``) and is ``str | None`` — an absent
#: reason is not evidence of truncation (W9.26 D86).
class StreamEvent(TypedDict):
    type: StreamEventType
    delta: NotRequired[str]
    meta: NotRequired[dict]
    finish_reason: NotRequired[str | None]


@dataclass(frozen=True)
class ClockInputs:
    """Everything the rules clock needs to advance one turn."""

    action_class: str
    facts: str


@dataclass(frozen=True)
class ClockOutputs:
    """Result of one clock advance."""

    facts: str
    time: str
    advanced_minutes: int


class ModelCaller(Protocol):
    """A chat model call, streaming.

    ``messages`` is the OpenAI chat shape (``[{"role": ..., "content": ...}]``).
    Yielding happens token-by-token; a caller ends the stream naturally — the
    iterator ending is the signal, so no terminal event is required. A caller MAY
    end with a terminal ``finish`` frame carrying ``finish_reason`` (W9.26 §2.3
    step 5's D86 allowlist): both the summarizer caller and the narrator path emit
    it (issue #28), and it is a SIDE-BAND frame — it adds no ``delta`` and comes
    after the last content delta, so a consumer's prose is byte-identical with or
    without it and a consumer that ignores unknown frame types is unaffected.
    The graph forwards each yielded `StreamEvent` to its consumer.
    """

    def __call__(
        self, messages: list[dict], stream: bool = True
    ) -> AsyncIterator[StreamEvent]: ...


async def collect_text(caller: ModelCaller, messages: list[dict]) -> str:
    """Non-streaming convenience: drain a `ModelCaller` into its content text.

    Reasoning deltas are discarded. Provided so callers that only need the final
    prose (e.g. a future summarizer) do not re-implement the drain loop.
    """
    parts: list[str] = []
    async for event in caller(messages, True):
        if event.get("type") == "content":
            parts.append(event.get("delta", ""))
    return "".join(parts)


def content_channel(event: StreamEvent) -> str:
    """Which stream a content delta belongs to: ``"prose"`` or ``"dsl"``.

    The narrator prompt lane (W1.2/W2.4) tags openui DSL deltas with
    ``meta={"channel": "dsl"}``; everything else is prose. The default keeps
    plain text on the prose channel so the pre-DSL narrator works unchanged.
    """
    return (event.get("meta") or {}).get("channel", "prose")


class ExtractorFn(Protocol):
    """Post-turn state extractor (W1.3 seam).

    Reads the user message and the turn prose; returns
    ``{"deltas": dict, "action_class": str}`` where `deltas` is the pipe-YAML
    state delta as a dict (D2) and `action_class` is the resolved user action.
    """

    async def __call__(self, user_msg: str, prose: str) -> dict: ...


class LogPort(Protocol):
    """The slice of the event log the graph writes through (W0.2 seam).

    Tests pass the real ``fabulist.log.EventLog`` (tmp SQLite) or an in-memory
    fake. The graph never reads the log for anything but rebuilding prior facts.
    """

    def append_event(
        self,
        session_id: str,
        type: str,
        actor: str,
        payload: dict | None = None,
        *,
        ephemeral: bool = False,
        meta: dict | None = None,
    ) -> Any: ...

    def get_projection(self, session_id: str) -> dict: ...


# -- W9.17: the NPC fan-out's concurrent channels -----------------------------
# A LangGraph ``Send`` fans out into INDEPENDENT node invocations, so every
# member's partial return lands on the same channel. A plain channel would let
# the last writer REPLACE its siblings; these reducers merge instead. Both treat
# an EMPTY write as the turn boundary (a reset): the graph input
# (:func:`initial_turn_state`) and the ``npc_lanes`` dispatcher both write empty
# values before any lane runs, which is what keeps a checkpointed thread from
# inheriting the previous turn's lanes.


def merge_npc_lanes(current: dict | None, update: dict | None) -> dict[str, dict]:
    """Reducer for ``TurnState.npc_lanes``: concurrent lane returns MERGE.

    * an EMPTY ``update`` RESETS the channel (the turn boundary — see above);
    * otherwise the dicts merge and a colliding key takes the LATER value (a
      member id is unique inside one turn, so this only matters for a re-run).

    Because an empty write resets, a FAILED lane must return no ``npc_lanes``
    key at all — ``{"npc_lanes": {}}`` would wipe a sibling's merged result.
    """
    if not update:
        return {}
    merged = dict(current or {})
    merged.update(update)
    return merged


#: ``lane_meta`` keys only the dispatcher writes. The reducer keeps the FIRST
#: value it sees for them, so a per-lane return can never move the turn's shape
#: (source/candidates/pool width) out from under the summary written at the
#: fan-in join.
_LANE_META_DISPATCH_KEYS = (
    "source",
    "candidates",
    "max_lanes",
    "batches",
    "members",
)


def merge_lane_meta(current: dict | None, update: dict | None) -> dict:
    """Reducer for ``TurnState.lane_meta``: the fan-out's bookkeeping merges.

    * ``ran`` SUMS (each lane reports 1 on success, 0 on failure — the
      dispatcher seeds it with 0), so the count survives concurrent returns;
    * ``skipped`` CONCATENATES (one row per failed lane, with its reason);
    * the dispatcher-only keys (:data:`_LANE_META_DISPATCH_KEYS`) keep the
      FIRST value (the dispatcher writes them before any lane runs);
    * everything else (``error``, ``emitted``) is last-write-wins.

    An EMPTY ``update`` resets the channel, like ``npc_lanes``.
    """
    if not update:
        return {}
    merged = dict(current or {})
    for key, value in update.items():
        if key == "ran":
            merged["ran"] = int(merged.get("ran") or 0) + int(value or 0)
        elif key == "skipped":
            merged["skipped"] = [*(merged.get("skipped") or []), *(value or [])]
        elif key in _LANE_META_DISPATCH_KEYS and key in merged:
            continue
        else:
            merged[key] = value
    return merged


# -- W9.22 §3: the proposal fan-out's concurrent channels ---------------------
# Same shape as the NPC fan-out above, and the same rule: a ``Send`` fan-out's
# lanes are independent node invocations whose partial returns land on one
# channel, so merging is a reducer's job. The three proposal lanes are DRIVEN by
# a dispatcher that knows the lane order, which is why the fan-in reduces (and
# appends the events) in DISPATCHER order and never in completion order.


def merge_proposal_rows(
    current: dict[str, list[dict]] | None, update: dict[str, list[dict]] | None
) -> dict[str, list[dict]]:
    """Reducer for ``TurnState.proposal_rows``: the lanes' judged rows merge.

    Keyed by LANE id (``world`` / ``actors`` / ``books``), one list per lane, so
    two lanes can never overwrite each other and the fan-in can walk the lanes
    in the dispatcher's order. Rows are CONCATENATED per key: a lane is invoked
    once per turn, so the list only grows when the same lane somehow returns
    twice — and then both batches are real proposals. An EMPTY ``update`` RESETS
    the channel (the turn boundary, exactly like ``npc_lanes``), which is why a
    FAILED lane must return no ``proposal_rows`` key at all.
    """
    if not update:
        return {}
    merged = {lane: list(rows or []) for lane, rows in (current or {}).items()}
    for lane, rows in update.items():
        merged[str(lane)] = [*merged.get(str(lane), []), *(rows or [])]
    return merged


#: ``proposal_meta`` keys only the dispatcher writes: the double gate's reason,
#: the dispatched lane order and the touched-entity count. Kept FIRST-seen for
#: the same reason as :data:`_LANE_META_DISPATCH_KEYS` — a lane's return must
#: never rewrite the turn's shape.
_PROPOSAL_META_DISPATCH_KEYS = ("gate", "lanes", "touched")


def merge_proposal_meta(current: dict | None, update: dict | None) -> dict:
    """Reducer for ``TurnState.proposal_meta``: the proposal bookkeeping merges.

    * ``dropped`` / ``failed`` CONCATENATE (one row per refused proposal, one per
      failed lane) — they are the summary's per-type outcomes;
    * the dispatcher-only keys (:data:`_PROPOSAL_META_DISPATCH_KEYS`) keep the
      FIRST value, so a lane cannot move the gate verdict or the lane order;
    * everything else (``emitted``) is last-write-wins.

    An EMPTY ``update`` resets the channel.
    """
    if not update:
        return {}
    merged = dict(current or {})
    for key, value in update.items():
        if key in ("dropped", "failed"):
            merged[key] = [*(merged.get(key) or []), *(value or [])]
        elif key in _PROPOSAL_META_DISPATCH_KEYS and key in merged:
            continue
        else:
            merged[key] = value
    return merged


#: LangGraph state for one turn. ``facts`` and ``plan`` are the pre-turn
#: injected context (``plan`` is the folded plan's RENDER, written every turn by
#: ``pre_turn`` and refreshed by the planner when it runs — W9.20 §5.5);
#: ``narrative_parts``/``dsl`` accumulate the narrator output;
#: ``segments`` carries the position-preserving interleave (prose/dsl in
#: document order — the M1.5 interweave contract); ``deltas``/``action_class``
#: are the post-turn extraction result. Per D11 this state is checkpoint
#: *cache*: everything here is reconstructible from the log (facts from the
#: folded state-deltas, narrative from narrative events).
class TurnState(TypedDict):
    session_id: str
    user_content: str
    kind: str
    facts: str
    plan: str
    narrative_parts: list[str]
    dsl: str
    segments: list[dict]
    #: The draft completion's ``reasoning_content`` (issue #42): the narrator's
    #: thinking trace, carried to the commit node so the narrative row can store
    #: it for DIAGNOSIS. It is NOT re-sent as text — history assembly re-emits the
    #: field PRESENT but EMPTY for a prior execution (owner ruling on #29).
    #: ``None`` = the provider returned no such field; ``""`` = it returned an
    #: empty one (PRESENCE is the fact — the vendor requires the field itself back,
    #: not non-empty text).
    reasoning: str | None
    validation_errors: list[dict]
    image_url: str
    image_kind: str
    recall: str
    facts_hash: str
    rolls: str
    recall_text: str
    #: D25 C4: exact recall queries + resolved config for the RAG panel.
    recall_queries: list[str]
    recall_config: dict
    #: W5C-1: per-result rows the recall stage surfaced (seq/kind/text/distance,
    #: JSON-safe) so the inspector RAG panel can show what recall actually used.
    recall_rows: list[dict]
    #: Inspector I2 provenance: the injected facts text and per-region sizes.
    facts_text: str
    regions: dict
    deltas: dict
    action_class: str
    interrupt_requested: bool
    # -- W9.6: the turn transaction across the per-stage nodes -----------------
    #: The committed ``narrative`` event ({id, seq, payload}) so downstream
    #: nodes (critic supersede target, BTS) read it from state instead of
    #: recomputing it. State is draft, log is truth (D9/D11).
    narrative_event: dict
    #: The last critic verdict (verdict/attempt/reason/edits — the same fields
    #: its ``critic_verdict`` log row carries); the routing edge reads it.
    critic_verdict: dict
    #: The flags the critic sees: validator flags, then the d1-3b gate's.
    critic_flags: list[dict]
    #: The event a further POST-COMMIT revision would supersede — the narrative
    #: for the first one, the previous revision afterwards (D49's supersede
    #: chain). The narrator's repair loop does not use it: since W9.24 the loop
    #: repairs in working memory BEFORE the commit and lands ONE narrative row,
    #: and the only remaining revision writer is the critic's ``diffs`` patch.
    revision_target: int | None
    #: The d1-3b gate's own advisory flags (answers >= threshold, with ids).
    gate_flags: list[dict]
    #: The world-state pass's structured result and the ``world_state`` row it
    #: committed (``{}`` when the pass failed or was not wired).
    bts_result: dict
    world_state_event: dict
    #: The speaker rows the attribution pass persisted.
    attribution_rows: list[dict]
    #: What the validator produced this pass: flags/score + the DSL findings the
    #: flags were built from.
    validation: dict
    # -- W9.13/W9.17 NPC fan-out ------------------------------------------------
    #: One lane result per active NPC this turn: ``member_id -> {intent,
    #: narrative, lines}`` (see :class:`fabulist.graph.nodes.NpcLaneFanout`).
    #: The narrator's bottom block is RENDERED from this, never stored twice.
    #: Merged by concurrent ``npc_lane`` returns (W9.17).
    npc_lanes: Annotated[dict[str, dict], merge_npc_lanes]
    #: The fan-out's own bookkeeping: which members ran, the concurrency pool
    #: size, the pool batches, and the skipped rows (with reasons). Never read
    #: by a model. Merged across the fan-out's concurrent returns (W9.17).
    lane_meta: Annotated[dict, merge_lane_meta]
    #: The dispatcher's per-member lane rows (name, aliases, categories,
    #: witnessed turns, beat, last intent) in ACTIVE-cast order — what the
    #: fan-out router turns into one ``Send`` per row. Empty means "no lanes".
    lane_plan: list[dict]
    # -- W9.22 §3 asset-proposal fan-out ----------------------------------------
    #: The proposal dispatcher's per-lane scope rows (lane, kind, the touched
    #: entities of that type, the resolved per-item question + threshold) in
    #: DISPATCH order — what the router turns into one ``Send`` per data type.
    #: Empty means "the double gate said no": zero lanes, zero model calls.
    proposal_plan: list[dict]
    #: One judged batch per data type: ``lane -> [asset_proposal payloads + the
    #: lane's evidence line]``. Merged by the concurrent lane returns (W9.22 §3).
    proposal_rows: Annotated[dict[str, list[dict]], merge_proposal_rows]
    #: The proposal fan-out's bookkeeping: the gate verdict, the dispatched lane
    #: order, the touched-entity count, and the dropped/failed rows the fan-in
    #: summary is built from. Never read by a model.
    proposal_meta: Annotated[dict, merge_proposal_meta]


def initial_turn_state(session_id: str, content: str, kind: str) -> TurnState:
    """Blank state for one turn; the graph fills the rest node by node."""
    return {
        "session_id": session_id,
        "user_content": content,
        "kind": kind,
        "facts": "",
        "plan": "",
        "narrative_parts": [],
        "dsl": "",
        "segments": [],
        # Reset per turn: a checkpointed thread must never let a prior turn's
        # reasoning ride the next turn's narrative row (issue #42).
        "reasoning": None,
        "validation_errors": [],
        "image_url": "",
        "image_kind": "image",
        "recall": "",
        "facts_hash": "",
        "rolls": "",
        "recall_text": "",
        "recall_queries": [],
        "recall_config": {},
        "recall_rows": [],
        "facts_text": "",
        "regions": {},
        "deltas": {},
        "action_class": "",
        "interrupt_requested": False,
        # W9.6 turn transaction: every key a stage writes starts empty here so a
        # new turn on the same checkpointed thread never inherits the last one.
        "narrative_event": {},
        "critic_verdict": {},
        "critic_flags": [],
        "revision_target": None,
        "gate_flags": [],
        "bts_result": {},
        "world_state_event": {},
        "attribution_rows": [],
        "validation": {},
        # W9.13: a checkpointed thread must never inherit the last turn's lanes
        # (the W9.6 discipline) — the narrator's bottom block is derived from
        # ``npc_lanes``, so an empty dict means "no block" by construction. The
        # W9.17 reducers read an empty write as the reset, so these three empty
        # values ARE the turn boundary for a fan-out's merged channels.
        "npc_lanes": {},
        "lane_meta": {},
        "lane_plan": [],
        # W9.22 §3: the proposal channels reset at every turn boundary for the
        # same reason — a checkpointed thread must never inherit last turn's
        # proposals (and an empty write IS the reset for the two reduced ones).
        "proposal_plan": [],
        "proposal_rows": {},
        "proposal_meta": {},
    }
