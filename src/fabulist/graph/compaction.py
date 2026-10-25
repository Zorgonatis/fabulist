"""History keeper / compaction: summarize past turns into the log (W2.9, W9.26).

Long sessions outgrow any context window. Compaction folds a stretch of the
session's narrative into ``summary`` events, preserving the facts a reader of
the log needs to keep the story coherent (relationships, unresolved threads,
time/place, promises) while discarding chat noise. Nothing is rewritten or
deleted: the raw ``narrative`` events stay in the log (D9/D10), the summary is an
additional append-only fact.

Two producers live here (W9.26 §2.8 retires the first):

* :meth:`Compactor.maybe_compact` — the W2.9 threshold hook. **Transient** in
  wave 1: ``wiring.py``'s existing call must keep working until Lane B swaps it
  for the service, so its behavior (thresholds, tail protection, in-place
  summary) is frozen. Its span/transcript helpers are reused by the service.
* :class:`CompactService` / :func:`compact` — the W9.26 §2.3 pipeline: turn-pair
  span (no orphan player lines), chunked map-reduce against the summarizer's
  own budget, the D86 finish-reason allowlist, summary v2 (``trigger`` +
  ``stats``, ``absorbs`` omitted-when-absent), the log-recorded era marker and
  the tip fork that starts a new era. Step-10 recovery forks an existing
  summary's era with NO model call.

Which reader to use for the prompt (W9.26 §2.4):

* :func:`summaries_payload` — ALL summary texts (RecapPane semantics).
* :func:`recap_texts` — the PROMPT recap: drops summaries a later one absorbs
  (``absorbs: [session_id, seq]``), keeps every row without an ``absorbs`` key
  (legacy rows are never dropped).

The summarize call is provider-agnostic: ``summarizer`` is any async callable
that accepts the OpenAI chat ``messages`` list and returns the summary text, or
a ``ModelCaller``-shaped async iterator of ``StreamEvent`` dicts (see
:mod:`fabulist.graph.types`). Tests inject a fake; production wiring injects a
real model caller. Ephemeral events (abandoned swipe branches, W2.2) are never
counted, covered, or summarized.

Finish reasons: THREE classes, one surface (issue #26)
------------------------------------------------------
ADR-021 trusts a structured reply only when the completion ENDS in an expected
way. That allowlist is a classification with three classes, not two:

* **trusted** — ``stop``, ``tool_calls``, or a stream that reported no reason at
  all (:data:`TRUSTED_FINISH_REASONS`). The reply is the answer.
* **filter** — ``sensitive`` (z.ai's documented STREAMING-side safety signal:
  the GENERATION, not the request, tripped the platform filter) and
  ``content_filter`` (the OpenAI-compatible spelling of the same fact), together
  :data:`FILTER_FINISH_REASONS`. The provider REFUSED the generation it was
  streaming, so what arrived is a blocked partial answer — never a summary.
* **unknown** — any other value (a provider variant the engine has not seen).
  ADR-021's letter, unchanged: discard the reply even when it parses, repair
  once, then fail the phase.

A filtered completion is NOT retried, and it is recorded under ADR-023 rather
than as a ``fabulist.log.degradation`` notice. Both halves are deliberate:

* no retry — the refusal is about the content of a request THIS engine built, so
  a retry re-sends the identical messages into the same filter. The one outcome
  a retry guarantees to hide is the signal itself: a second attempt that happens
  to pass leaves no row anywhere, and the reader is back to a filtered
  completion that looks like a self-silenced one.
* ADR-023, not a degradation row — a filter is the provider ANSWERING with
  unusable content, so the phase is CONTAINED: :class:`CompactionError` with
  stage :data:`COMPLETION_FILTERED_STAGE`, which :meth:`CompactService._fail`
  writes as ``system``/``compaction_failed`` (the ⚑R3a attempt counter as well as
  the log's own record) and returns as the job's ``stage``/``error`` (rendered
  inline on the UI's context-low row). The ``degradation`` vocabulary was the
  other candidate and was not taken: it means "an optional service was swallowed
  and the turn carried on invisibly", and compaction neither swallows nor
  continues — it fails one phase and says so. One recognized class, one row,
  one surface. The error text names the PHASE (``map 12-30`` / ``fold``) and the
  summarize MODEL, the ADR-023 shape a reader needs to tell a filtered
  summarize connection from a broken one.

Coverage and idempotence
------------------------
A summary records ``covers_seqs = [first_seq, last_seq]`` — the inclusive ``seq``
span it replaces. Compaction only ever considers turn rows outside every
existing (non-ephemeral) summary's span, so a range is never summarized twice.
Spans are TURN PAIRS (W9.26 §2.3 step 3): a covered narrative takes its
preceding user row along, so a player line is never orphaned at a span boundary
(legacy single-seq ``[N, N]`` summaries are extended the same way).

The old hook triggers on ``threshold_turns``/``threshold_chars`` and always
leaves the newest tail raw. The service summarizes EVERYTHING (no raw tail) —
chunking, not truncation, is how it fits the summarizer's budget.

Context-size math (W9.25 §3.5)
------------------------------
This module also owns the ONE context-size formula, because compaction is the
consumer that turns a declared window into a character budget:

    prompt budget (tokens) = Context Size − Response Length − RESERVE_TOKENS

:func:`derive_threshold_chars` converts that budget into the uncovered-prose
character threshold :meth:`Compactor.maybe_compact` keys off (only uncovered
prose is shrinkable; the prefix/recall/facts are not compaction's to spend).
:func:`prompt_budget_tokens` and :func:`estimate_prompt_tokens` are the same math
for the two OTHER consumers — the prompt-budget warn (:func:`warn_prompt_budget`,
used by the streaming caller and the extractor seam) and the agent loop's passive
headroom. Every estimate is the compiler's char/4 heuristic and is labeled an
estimate wherever it appears.
"""

from __future__ import annotations

import inspect
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterable

from ..cards.v2 import is_import_seed_text
from ..log import Event
from ..log.schema_gate import (
    COMPACTION_FAILED_EVENT,
    COMPACTION_MARKER_EVENT,
    PROMPT_BUDGET_EVENT,
)
from ..prompts.compiler import CHARS_PER_TOKEN

logger = logging.getLogger(__name__)

#: Cap on the error text carried by a stage-failure event.
_FAILURE_ERROR_LIMIT = 800

#: Engine reserve between the declared Context Size and the usable prompt
#: (W9.25 §3.5): chat-template wrapping, role markers and framing overhead.
#: Provisional until the live pass measures it (W9.25 R6).
RESERVE_TOKENS = 512

#: How much of the remaining prompt budget compaction may claim. Only the
#: uncovered narrative prose is shrinkable — the stable prefix, the recall block
#: and the facts suffix are not compaction's to spend — so the threshold targets
#: a share, leaving room for the rest (W9.25 §3.5.2; provisional, R6).
UNCOVERED_SHARE = 0.6

__all__ = [
    "ALLOWED_FINISH_REASONS",
    "CHUNK_TRIGGER_SHARE",
    "COMPLETION_FILTERED_STAGE",
    "CompactionError",
    "CompactOutcome",
    "CompactService",
    "Compactor",
    "FILTER_FINISH_REASONS",
    "FINISH_FILTER",
    "FINISH_REASON_FRAME",
    "FINISH_TRUSTED",
    "FINISH_UNKNOWN",
    "RESERVE_TOKENS",
    "SUMMARIZER_SYSTEM_PROMPT",
    "SUMMARY_ACTOR",
    "TRUSTED_FINISH_REASONS",
    "UNCOVERED_SHARE",
    "build_summarizer_messages",
    "classify_finish_reason",
    "compact",
    "covered_turn_seqs",
    "derive_threshold_chars",
    "estimate_prompt_tokens",
    "has_compaction_marker",
    "prompt_budget_tokens",
    "recap_texts",
    "session_title",
    "summaries_payload",
    "turn_groups",
    "turn_rows",
    "warn_prompt_budget",
]


def prompt_budget_tokens(
    context_window: int | None, response_length: int | None
) -> int | None:
    """Usable prompt tokens for a declared Context Size, or ``None`` when unknown.

    The pinned formula (W9.25 §3.5): ``context_window − response_length −
    RESERVE_TOKENS``. ``None`` means the owner declared no window, and every
    consumer keeps today's fixed-budget behavior (no math, no rows). The result
    may be zero or negative — a declaration that small cannot fit a prompt, and
    the consumers report the overshoot rather than clamp it away.
    """
    if context_window is None:
        return None
    return int(context_window) - int(response_length or 0) - RESERVE_TOKENS


def derive_threshold_chars(context_window: int, response_length: int) -> int:
    """Uncovered-prose character threshold for a declared Context Size (W9.25 §3.5.2).

    ``(context_window − response_length − RESERVE_TOKENS) × CHARS_PER_TOKEN ×
    UNCOVERED_SHARE``, floored at zero. Pure: same inputs, same number, and no
    log/model access — the caller decides whether to use it.
    """
    budget_tokens = prompt_budget_tokens(context_window, response_length) or 0
    return max(0, int(budget_tokens * CHARS_PER_TOKEN * UNCOVERED_SHARE))


def _message_text(message: Any) -> str:
    """The text of one chat message (``str`` content, or multipart text parts)."""
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(part.get("text") or "")
            for part in content
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        )
    return ""


def estimate_prompt_tokens(messages: Iterable[Any]) -> int:
    """Char/4 ESTIMATE of one assembled prompt, in tokens (W9.25 §3.5).

    The same heuristic :func:`fabulist.prompts.compiler.count_tokens` uses for
    the directive budget, so the warn and the trim never disagree. Deliberately
    blind to a provider's real tokenizer: the ``tokenizer`` seam on the compiler
    is the upgrade path (W9.25 R4).
    """
    chars = sum(len(_message_text(message)) for message in messages)
    return (chars + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN


def warn_prompt_budget(
    log: Any,
    *,
    session_id: str,
    agent: str,
    context_window: int | None,
    response_length: int | None,
    messages: Iterable[Any],
) -> bool:
    """Append ONE ``system``/``prompt_budget`` row when the prompt overshoots.

    The warn lives at the two seams that build and send a prompt themselves —
    the streaming caller (:class:`fabulist.wiring.RegistryModelCaller`) and the
    extractor's direct call (``LLMExtractor``) — so the owner sees an over-budget
    prompt in the inspector instead of only in the log tail. One call = at most
    one row; an undeclared window produces no math and no row (today's behavior).

    Measurement only (D23): a log hiccup never breaks the call. Returns whether a
    row was recorded.
    """
    available = prompt_budget_tokens(context_window, response_length)
    if available is None:
        return False
    estimate = estimate_prompt_tokens(messages)
    if estimate <= available:
        return False
    over_tokens = estimate - available
    logger.warning(
        "prompt estimate %d exceeds the declared context window for %s "
        "(available %d tokens after response length + reserve; over by %d)",
        estimate,
        agent,
        available,
        over_tokens,
    )
    if log is None or not session_id:
        return False
    payload = {
        "event": PROMPT_BUDGET_EVENT,
        "agent": agent,
        "est_prompt_tokens": int(estimate),
        "context_window": int(context_window),
        "response_length": int(response_length or 0),
        "over_tokens": int(over_tokens),
    }
    try:
        log.append_event(session_id, "system", agent, payload)
    except Exception:  # noqa: BLE001 - measurement must never break a call
        logger.warning("could not record prompt_budget row for %s", session_id, exc_info=True)
        return False
    return True

#: Actor recorded on appended summary events.
SUMMARY_ACTOR = "summarizer"

#: BTS-shaped preservation prompt (PLAN W2.9 / "BTS-shaped preservation"). The
#: summarizer keeps continuity facts and drops chat noise; it never invents.
SUMMARIZER_SYSTEM_PROMPT = """You are the history keeper for a long-form roleplay. You compress one \
stretch of past narrative into continuity notes that later turns read instead of the raw log.

Keep, and state plainly:
- Relationships: who each named character is to the user and to each other, and how
  those bonds changed across the stretch.
- Unresolved threads: open questions, dangling hooks, and goals still in motion.
- Time and place: where and when the story stands at the end of the stretch.
- Promises, debts and obligations: anything owed, sworn, threatened or promised.

Discard:
- Chat noise, greetings, filler, repeated scene description, small talk, and
  out-of-character asides.

Write tight third-person present-tense prose. Invent nothing: summarize only what the
transcript shows. Output the summary text alone, with no preamble, headings or commentary."""

_SUMMARIZER_INSTRUCTION = (
    "Summarize this stretch of roleplay history into continuity notes."
)


def build_summarizer_messages(transcript: str) -> list[dict[str, str]]:
    """Build the chat request for the summarizer over one transcript block."""
    return [
        {"role": "system", "content": SUMMARIZER_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": f"{_SUMMARIZER_INSTRUCTION}\n\n{transcript}",
        },
    ]


# -- event helpers -----------------------------------------------------------


def _append_compaction_failed(
    log: Any,
    session_id: str,
    first_seq: int | None,
    last_seq: int | None,
    *,
    stage: str,
    error: str,
) -> None:
    """Record an attempt that produced no summary as ``system``/``compaction_failed``.

    S4c (audit 2026-10-10, silent-failure class): an empty/refusal summary used
    to ``return None`` silently, so the threshold stayed exceeded, every turn
    retried, and the log (D9 source of truth) never showed why. The covered-span
    attempt rides ``covers_seqs`` when the span is known (a configuration
    failure has no span, so the key is omitted rather than written as null).
    Best-effort: a failure to record must never break compaction (the caller
    still returns ``None`` as before).

    W9.26 §2.3 step 0 also READS these rows: the forced-compaction retry bound
    (⚑R3a, 3 failed attempts per era) is counted from own ``compaction_failed``
    rows, so the row is the restart-safe attempt counter as well as the audit.
    """
    payload: dict[str, Any] = {
        "event": COMPACTION_FAILED_EVENT,
        "stage": stage,
        "error": error[:_FAILURE_ERROR_LIMIT],
    }
    if isinstance(first_seq, int) and isinstance(last_seq, int):
        payload["covers_seqs"] = [first_seq, last_seq]
    try:
        log.append_event(session_id, "system", SUMMARY_ACTOR, payload)
    except Exception:  # noqa: BLE001 - reporting must never break compaction
        logger.exception("could not record compaction failure for %s", session_id)


def _field(event: Any, key: str, default: Any = None) -> Any:
    """Read a field from an :class:`Event` or a projection dict alike."""
    if isinstance(event, dict):
        return event.get(key, default)
    return getattr(event, key, default)


def _is_image_narrative(event: Any) -> bool:
    """Whether ``event`` is a generated-image narrative row.

    A generated image is appended as a ``narrative`` row (imagegen) but is a
    canon image carrying no prose, not a turn. Reuses
    :func:`fabulist.media.images.is_image_narrative`, normalizing an
    :class:`Event` to the projection dict shape it expects. The import is lazy
    because ``fabulist.media.images`` imports back through the graph package.
    """
    from ..media.images import is_image_narrative

    if isinstance(event, dict):
        return is_image_narrative(event)
    return is_image_narrative({"type": event.type, "payload": event.payload})


def _narrative_prose(event: Any) -> str:
    """The prose text of a narrative event.

    The engine commits ``{"prose", "dsl", "segments"}``; a ``segments``-only event
    falls back to re-joining its prose segments (the M1.5 interweave contract is
    preserved, never flattened away for counting).
    """
    payload = _field(event, "payload") or {}
    prose = payload.get("prose")
    if isinstance(prose, str) and prose.strip():
        return prose
    segments = payload.get("segments") or []
    joined = "\n".join(
        str(seg.get("text", ""))
        for seg in segments
        if isinstance(seg, dict) and seg.get("kind") == "prose"
    )
    if joined.strip():
        return joined
    text = payload.get("text")
    return text if isinstance(text, str) else ""


def _covered_seqs(events: Iterable[Any]) -> set[int]:
    """Narrative ``seq`` values already replaced by a non-ephemeral summary.

    ``covers_seqs`` is the inclusive ``[first, last]`` span, so the whole span is
    expanded: any narrative seq inside it is considered covered.
    """
    covered: set[int] = set()
    for event in events:
        if _field(event, "type") != "summary" or _field(event, "ephemeral"):
            continue
        payload = _field(event, "payload") or {}
        raw = payload.get("covers_seqs") or []
        try:
            seqs = [int(seq) for seq in raw]
        except (TypeError, ValueError):
            continue
        if seqs:
            covered.update(range(min(seqs), max(seqs) + 1))
    return covered


def summaries_payload(events: Iterable[Any]) -> list[str]:
    """Collect the summary texts from ``events``, in order (PLAN W2.9 / W2.1).

    Reads :class:`~fabulist.log.Event` objects or projection dicts equally.
    Ephemeral summaries (pruned branches) and blank texts are skipped so the
    returned list is exactly what downstream context assembly may fold in.
    """
    texts: list[str] = []
    for event in events:
        if _field(event, "type") != "summary" or _field(event, "ephemeral"):
            continue
        payload = _field(event, "payload") or {}
        text = payload.get("text")
        if isinstance(text, str) and text.strip():
            texts.append(text)
    return texts


def _format_transcript(events: list[Any], first_seq: int, last_seq: int) -> str:
    """Labeled prose transcript for the covered span (narrative + user context).

    Narrative prose and the user messages interleaved in the span are included so
    the summarizer can preserve intent and thread motion, not just scene description.
    Ephemeral rows are excluded (D10/W2.2), and so is a legacy card-import seed —
    a fabricated ``user`` row that must not be summarized as something the player
    said (``EventLog``'s projection refuses it for the same reason).
    """
    rows: list[Any] = []
    for event in events:
        if _field(event, "ephemeral"):
            continue
        seq = _field(event, "seq")
        if not isinstance(seq, int) or seq < first_seq or seq > last_seq:
            continue
        if _field(event, "type") not in ("narrative", "user"):
            continue
        rows.append(event)
    return _format_rows(rows)


def _row_block(event: Any) -> str | None:
    """One labeled transcript block, or ``None`` when the row carries no prose.

    Shared by every transcript renderer here (the title-pair span, chunk
    splitting, the fold) so what the summarizer reads is byte-identical
    whichever path produced it.
    """
    etype = _field(event, "type")
    seq = _field(event, "seq")
    payload = _field(event, "payload") or {}
    if etype == "narrative":
        text = _narrative_prose(event)
        if text.strip():
            return f"[{seq}] NARRATOR:\n{text}"
        return None
    if etype == "user":
        text = payload.get("text")
        if isinstance(text, str) and text.strip() and not is_import_seed_text(text):
            return f"[{seq}] USER: {text}"
        return None
    return None


def _format_rows(rows: Iterable[Any]) -> str:
    """Join already-selected turn rows into the labeled transcript body."""
    blocks = [block for block in (_row_block(row) for row in rows) if block]
    return "\n\n".join(blocks)


def _reason(
    turn_trigger: bool,
    turn_count: int,
    threshold_turns: int,
    char_trigger: bool,
    char_count: int,
    threshold_chars: int | None,
) -> str:
    parts: list[str] = []
    if turn_trigger:
        parts.append(f"turns {turn_count}>{threshold_turns}")
    if char_trigger:
        parts.append(f"prose_chars {char_count}>{threshold_chars}")
    return "threshold exceeded: " + "; ".join(parts)


async def _run_summarizer(summarizer: Any, messages: list[dict[str, str]]) -> str:
    """Invoke the summarizer seam and normalize its output to plain text.

    Accepts an async function returning ``str``, a ``ModelCaller``-shaped async
    iterator of ``StreamEvent`` dicts (content deltas are concatenated; reasoning
    is ignored), or a plain string-returning callable.
    """
    # W1f/W9.6: the compaction hook runs OUTSIDE the turn pump, so the
    # CURRENT_INTERACTION contextvar still says "narrate". Label the capture
    # truthfully (the flow inspector's summarizer node filters on it).
    from fabulist.agents.debug import CURRENT_INTERACTION

    CURRENT_INTERACTION.set("summarizer")
    result = summarizer(messages)
    if isinstance(result, str):
        return result
    if inspect.isawaitable(result):
        text = await result
        return text if isinstance(text, str) else ("" if text is None else str(text))
    parts: list[str] = []
    async for event in result:  # ModelCaller: async iterator of StreamEvent dicts
        if isinstance(event, dict) and event.get("type") == "content":
            parts.append(str(event.get("delta", "") or ""))
    return "".join(parts)


class Compactor:
    """The W2.9 history keeper: fold the oldest uncovered turns into a summary."""

    def __init__(self, *, actor: str = SUMMARY_ACTOR, messages_builder=None) -> None:
        # D18 completion: an optional flow renderer (transcript) -> messages;
        # None keeps build_summarizer_messages (the fixed default shape).
        self._messages_builder = messages_builder
        self.actor = actor

    async def maybe_compact(
        self,
        session_id: str,
        log: Any,
        summarizer: Any,
        threshold_turns: int = 20,
        threshold_chars: int | None = None,
    ) -> Event | None:
        """Compact ``session_id`` when its raw narrative exceeds a threshold.

        Returns the appended ``summary`` :class:`~fabulist.log.Event`, or ``None``
        when nothing is over threshold, there is nothing left uncovered, or the
        summarizer produced no text. An empty/refusal summary appends a
        ``system``/``compaction_failed`` event (S4c) before returning ``None``,
        so the retry is visible in the log. Never covers a ``seq`` an existing
        summary already covers (idempotent) and never touches ephemeral events.
        """
        threshold_turns = max(int(threshold_turns), 0)

        events = log.get_events(session_id)
        narratives = [
            event
            for event in events
            if _field(event, "type") == "narrative"
            and not _field(event, "ephemeral")
            and not _is_image_narrative(event)
        ]
        if not narratives:
            return None

        covered = _covered_seqs(events)
        uncovered = [event for event in narratives if _field(event, "seq") not in covered]
        if not uncovered:
            return None

        turn_count = len(uncovered)
        char_count = sum(len(_narrative_prose(event)) for event in uncovered)
        turn_trigger = turn_count > threshold_turns
        char_trigger = threshold_chars is not None and char_count > threshold_chars
        if not (turn_trigger or char_trigger):
            return None

        # Keep the newest turns raw for the live window: at most `threshold_turns`
        # of them, and no more than `threshold_chars` characters when set.
        tail: list[Any] = []
        tail_chars = 0
        for event in reversed(uncovered):
            if len(tail) >= threshold_turns:
                break
            prose_len = len(_narrative_prose(event))
            if threshold_chars is not None and tail_chars + prose_len > threshold_chars:
                break
            tail.append(event)
            tail_chars += prose_len

        oldest = uncovered[: len(uncovered) - len(tail)]
        if not oldest:
            return None

        first_seq = _field(oldest[0], "seq")
        last_seq = _field(oldest[-1], "seq")
        transcript = _format_transcript(events, first_seq, last_seq)
        if not transcript.strip():
            return None

        messages = (
            self._messages_builder(transcript)
            if self._messages_builder is not None
            else build_summarizer_messages(transcript)
        )
        text = (await _run_summarizer(summarizer, messages)).strip()
        if not text:
            # S4c: an empty/refusal summary is a *failure to compact*, not
            # "nothing to do" — surface it so the log and the inspector can show
            # why the threshold stays exceeded and every turn retries.
            _append_compaction_failed(
                log,
                session_id,
                first_seq,
                last_seq,
                stage="summarizer_empty",
                error="summarizer returned no text",
            )
            return None

        reason = _reason(
            turn_trigger,
            turn_count,
            threshold_turns,
            char_trigger,
            char_count,
            threshold_chars,
        )
        return log.append_event(
            session_id,
            "summary",
            self.actor,
            {
                "text": text,
                "covers_seqs": [first_seq, last_seq],
                "reason": reason,
            },
        )


# -- W9.26 §2.3: the compact service -----------------------------------------
#
# The era model (W9.26 §2.4): a compaction writes ONE summary of the whole
# history, forks the session from it, and appends a ``system``/``compaction``
# marker to the successor. Every later turn of the new era reads
# ``stable prefix + recap + post-fork turns`` — the covered turns stay in the
# log (D9) and stay readable; they are simply no longer prompt material.
#
# The five moving parts, in order:
#   1. span      — turn PAIRS of the chain, exclusions kept, everything covered
#                  (no raw tail: chunking, not truncation, fits the budget);
#   2. map       — one summarizer call per turn-aligned chunk (chunk outputs
#                  persist in the caller's job object, so a retry reuses the
#                  chunks that already landed);
#   3. fold      — ONE reduce over the chunk notes + the inherited recap
#                  (absorption), hierarchical when even the notes overflow;
#   4. guard     — the three-class finish-reason classification on every
#                  terminal frame (trusted / filter / unknown — see the module
#                  docstring: accept / fail the phase / discard+retry+fail);
#   5. write     — summary v2, tip fork, era marker, title carry row — or, when
#                  nothing is uncovered, the step-10 recovery fork with NO
#                  model call.

#: Terminal ``finish_reason`` values a summarizer call may end with — the TRUSTED
#: class (W9.26 §2.3 step 5 / D86, ADR-021). ``None``/``""`` are ALSO accepted —
#: a caller that surfaces no reason has not evidenced truncation, and a missing
#: reason must not fail a compaction (the pre-W9.26 wire carries none).
TRUSTED_FINISH_REASONS = frozenset({"stop", "tool_calls"})

#: The historic name of :data:`TRUSTED_FINISH_REASONS` (the seam every existing
#: caller and test reaches for). An alias, never widened: a filter signal is not
#: "allowed", it is a recognized REFUSAL.
ALLOWED_FINISH_REASONS = TRUSTED_FINISH_REASONS

#: Provider FILTER signals (issue #26) — a class DISTINCT from a trusted stop.
#: ``sensitive`` is z.ai's documented STREAMING-side signal (the generation was
#: blocked by the platform's safety filter); ``content_filter`` is the
#: OpenAI-compatible spelling of the same fact. Both say the provider refused the
#: generation it was streaming, so the text that arrived is a blocked partial
#: answer — never a summary.
FILTER_FINISH_REASONS = frozenset({"sensitive", "content_filter"})

#: The three classes :func:`classify_finish_reason` returns.
FINISH_TRUSTED = "trusted"
FINISH_FILTER = "filter"
FINISH_UNKNOWN = "unknown"

#: The ``stage`` a filtered completion fails its phase under (ADR-023's contained
#: class: the phase's job is skipped and its ``*_failed`` row says why).
COMPLETION_FILTERED_STAGE = "completion_filtered"

#: The terminal frame type Lane B adds to ``graph/types.py`` (:data:`StreamEventType`)
#: that carries ``finish_reason``. The CONTRACT is the payload, not the name: this
#: reader accepts that frame, any frame carrying a top-level ``finish_reason``, and
#: the ``meta.finish_reason`` an ``error`` frame already carries (the ``length``
#: truncation frame) — so neither side of the seam can silently disagree.
FINISH_REASON_FRAME = "finish"

#: Share of the summarizer's usable prompt budget one request may fill (W9.26
#: §2.3 step 4): above this the transcript splits at turn boundaries and each
#: chunk is sized to stay under it.
CHUNK_TRIGGER_SHARE = 0.8

#: Guard against a fold that cannot make progress (a budget so small that even
#: one note overflows it). Four levels fold ~16+ chunk notes.
_MAX_FOLD_LEVELS = 4


class CompactionError(RuntimeError):
    """A compact attempt failed at a named ``stage`` (the job's ``error``)."""

    def __init__(self, stage: str, error: str) -> None:
        super().__init__(f"{stage}: {error}")
        self.stage = stage
        self.error = error


@dataclass
class CompactOutcome:
    """What one ``compact()`` run did (the job-status DTO's fields).

    ``status`` is ``done`` (an era exists — ``session_id`` is the successor to
    open), ``noop`` (nothing to compact and no era to recover) or ``failed``
    (``stage``/``error`` say where). ``recovered`` marks the step-10 path (a
    fork of an existing summary with no model call).
    """

    status: str
    session_id: str | None = None
    title: str | None = None
    stage: str | None = None
    error: str | None = None
    #: Why a ``noop`` did nothing (rendered inline; a noop is not an error).
    reason: str | None = None
    summary: Any | None = None
    recovered: bool = False
    calls: int = 0
    details: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        """The wire/job shape (``{status, session_id?, title?, error?, stage?}``)."""
        payload: dict[str, Any] = {"status": self.status}
        if self.session_id is not None:
            payload["session_id"] = self.session_id
        if self.title is not None:
            payload["title"] = self.title
        if self.stage is not None:
            payload["stage"] = self.stage
        if self.error is not None:
            payload["error"] = self.error
        if self.reason is not None:
            payload["reason"] = self.reason
        return payload


def _chain_events(log: Any, session_id: str) -> list[Any]:
    """Fork-aware chain rows for ``session_id`` (raw audit view as a fallback)."""
    reader = getattr(log, "get_chain_events", None)
    if callable(reader):
        return list(reader(session_id))
    return list(log.get_events(session_id))


def _own_events(log: Any, session_id: str) -> list[Any]:
    """Rows the session OWNS (marker presence / title / attempt counts)."""
    return list(log.get_events(session_id))


def turn_rows(events: Iterable[Any]) -> list[Any]:
    """The rows compaction treats as history, in chain order.

    A turn row is a ``narrative`` (minus generated-image canon rows, which are
    assets rather than prose) or a ``user`` row a player actually sent (minus
    the legacy card-import seed). Ephemeral rows never qualify (D10/W2.2).
    """
    rows: list[Any] = []
    for event in events:
        if _field(event, "ephemeral"):
            continue
        etype = _field(event, "type")
        if etype == "narrative":
            if _is_image_narrative(event):
                continue
            rows.append(event)
        elif etype == "user":
            payload = _field(event, "payload") or {}
            text = payload.get("text")
            if not isinstance(text, str) or not text.strip():
                continue
            if is_import_seed_text(text):
                continue
            rows.append(event)
    # Chain reads are seq-ordered for tip forks; sort defensively so a carried
    # revision appended after the inherited slice cannot reorder a turn pair.
    if all(isinstance(_field(row, "seq"), int) for row in rows):
        rows.sort(key=lambda row: _field(row, "seq"))
    return rows


def turn_groups(rows: Iterable[Any]) -> list[list[Any]]:
    """Group turn rows into TURN PAIRS (W9.26 §2.3 step 3).

    Each narrative takes the user row that precedes it, so a covered span never
    strands a player line without its reply (the legacy ``[N, N]`` shape did
    exactly that). Two user rows in a row keep the first as its own group (its
    prose still reaches the summarizer); a TRAILING user row with no narrative
    yet is left out — it is not a complete turn, so it stays raw.
    """
    groups: list[list[Any]] = []
    pending: Any | None = None
    for row in rows:
        if _field(row, "type") == "user":
            if pending is not None:
                groups.append([pending])
            pending = row
        else:
            groups.append([pending, row] if pending is not None else [row])
            pending = None
    return groups


def _group_bounds(group: list[Any]) -> tuple[int, int]:
    """Inclusive ``(first_seq, last_seq)`` of one turn group."""
    seqs = [seq for seq in (_field(row, "seq") for row in group) if isinstance(seq, int)]
    return (min(seqs), max(seqs))


def covered_turn_seqs(events: Iterable[Any]) -> set[int]:
    """Seqs already folded into a summary, EXTENDED to whole turn pairs.

    :func:`_covered_seqs` expands each ``covers_seqs`` span (inclusive). A turn
    pair additionally pulls in the user row that precedes a covered narrative:
    a legacy single-seq ``[N, N]`` summary covers the answer but not the player
    line at ``N-1``, and re-summarizing that orphan (or leaving it raw next to a
    compacted history) is the defect step 3 pins.
    """
    events = list(events)
    covered = _covered_seqs(events)
    if not covered:
        return covered
    for group in turn_groups(turn_rows(events)):
        narrative = group[-1]
        seq = _field(narrative, "seq")
        if _field(narrative, "type") != "narrative" or seq not in covered:
            continue
        for row in group[:-1]:
            row_seq = _field(row, "seq")
            if isinstance(row_seq, int):
                covered.add(row_seq)
    return covered


def _summary_text(event: Any) -> str:
    """The text of one summary row (``text`` with a legacy ``content`` fallback)."""
    payload = _field(event, "payload") or {}
    text = payload.get("text")
    if not isinstance(text, str):
        content = payload.get("content")
        text = content if isinstance(content, str) else ""
    return text


def _summary_nodes(events: Iterable[Any]) -> list[Any]:
    """Non-ephemeral summary rows, in chain order (blank texts included)."""
    return [
        event
        for event in events
        if _field(event, "type") == "summary" and not _field(event, "ephemeral")
    ]


def _newest_summary(events: Iterable[Any]) -> Any | None:
    """The newest non-ephemeral summary row on the chain (the inherited recap)."""
    summaries = _summary_nodes(events)
    return summaries[-1] if summaries else None


def recap_texts(events: Iterable[Any]) -> list[str]:
    """Summary texts for the PROMPT, in order, minus absorbed eras (W9.26 §2.4).

    A summary v2 row carries ``absorbs: [session_id, seq]`` naming the summary
    it folded; that predecessor must not be rendered again — the recap would
    double the same history and pay for it on every turn. Rows WITHOUT an
    ``absorbs`` key (every legacy summary, and the first era of a session) are
    never dropped: absence of the key is not evidence of absorption.

    :func:`summaries_payload` stays the ALL-rows reader (the RecapPane lists
    every era); this is the prompt-side reader. Reads
    :class:`~fabulist.log.Event` objects or projection dicts alike.
    """
    events = list(events)
    absorbed: set[tuple[str, int]] = set()
    nodes: list[tuple[tuple[Any, Any], str]] = []
    for event in _summary_nodes(events):
        payload = _field(event, "payload") or {}
        pair = payload.get("absorbs")
        if isinstance(pair, (list, tuple)) and len(pair) >= 2:
            session_id, seq = pair[0], pair[1]
            if (
                isinstance(session_id, str)
                and isinstance(seq, int)
                and not isinstance(seq, bool)
            ):
                absorbed.add((session_id, seq))
        key = (_field(event, "session_id"), _field(event, "seq"))
        nodes.append((key, _summary_text(event)))
    return [text for key, text in nodes if text.strip() and key not in absorbed]


def session_title(rows: Iterable[Any]) -> str:
    """The session's OWN title (latest ``session_created``/``session_renamed``).

    Public because a second writer needs the same carry rule: a lazily created
    branch (the delete-tail fork) inherits the conversation's title by
    re-appending it, exactly as a compaction successor era does. ``rows`` are
    the SOURCE session's own rows (``get_events``), never the chain: the carry
    copies the title onto the new branch, so reading the chain would be
    redundant where it is not stale.
    """
    title = ""
    for event in rows:
        payload = _field(event, "payload") or {}
        if _field(event, "type") == "system":
            kind = payload.get("event")
            value = payload.get("title")
            if kind in ("session_created", "session_renamed") and isinstance(value, str):
                title = value
        elif _field(event, "type") == "revision":
            revised = payload.get("revised")
            if isinstance(revised, dict) and isinstance(revised.get("title"), str):
                title = revised["title"]
    return title


#: Back-compat alias for callers that read the private spelling.
_session_title = session_title


def has_compaction_marker(log: Any, session_id: str) -> bool:
    """Whether a session's OWN rows carry an era marker (the ``kind`` rule).

    W9.26 §2.3 step 9: a session IS a compaction era iff its own rows include a
    ``system``/``compaction`` marker. Own rows, never the chain — every child of
    an era inherits the marker through the fork, so a chain read would label a
    swipe branch "compaction" too.
    """
    for event in _own_events(log, session_id):
        payload = _field(event, "payload") or {}
        if (
            _field(event, "type") == "system"
            and isinstance(payload, dict)
            and payload.get("event") == COMPACTION_MARKER_EVENT
        ):
            return True
    return False


#: Back-compat alias for callers that read the private spelling.
_has_compaction_marker = has_compaction_marker


def _successor_forking_at(log: Any, session_id: str, seq: int) -> str | None:
    """The compaction successor already forked at ``seq``, if one exists.

    Log-derived (never RAM): the duplicate-era race is killed by re-reading the
    fork bookkeeping plus the marker row, so a crash between the fork and the
    marker is repaired by step 10 rather than by a second fork.
    """
    list_branches = getattr(log, "list_branches", None)
    get_session = getattr(log, "get_session", None)
    if not callable(list_branches) or not callable(get_session):
        return None
    for child in list_branches(session_id):
        info = get_session(child) or {}
        if info.get("base_seq") != seq:
            continue
        if _has_compaction_marker(log, child):
            return child
    return None


def _finish_reason_of(event: Any) -> tuple[bool, Any]:
    """``(present, value)`` of a frame's ``finish_reason`` (top level or meta)."""
    if not isinstance(event, dict):
        return False, None
    if "finish_reason" in event:
        return True, event.get("finish_reason")
    meta = event.get("meta")
    if isinstance(meta, dict) and "finish_reason" in meta:
        return True, meta.get("finish_reason")
    return False, None


def classify_finish_reason(reason: Any) -> str:
    """Which of the three classes a terminal ``finish_reason`` falls in (issue #26).

    :data:`FINISH_TRUSTED` (``stop``/``tool_calls`` or no reason at all),
    :data:`FINISH_FILTER` (:data:`FILTER_FINISH_REASONS` — the provider refused
    the generation) or :data:`FINISH_UNKNOWN` (anything else, discarded per
    ADR-021).

    Exact membership, never a case fold and never a prefix read: the values are
    the providers' own lowercase spellings, and a reason the engine does not
    recognize must keep ADR-021's behavior instead of drifting into a class this
    module acts on. Strictness is the point — the FILTER class is a claim about
    what the provider did, so only the documented spellings may make it.
    """
    if reason is None or reason == "":
        return FINISH_TRUSTED
    if not isinstance(reason, str):
        return FINISH_UNKNOWN
    if reason in TRUSTED_FINISH_REASONS:
        return FINISH_TRUSTED
    if reason in FILTER_FINISH_REASONS:
        return FINISH_FILTER
    return FINISH_UNKNOWN


def _model_label(run: dict) -> str:
    """The summarize model as a failure text names it (ADR-023 names the model)."""
    model = str(run.get("model") or "")
    if model:
        return f"model {model!r}"
    return "the summarize connection's model (unresolved)"


def _filtered_error(what: str, finish: Any, run: dict) -> CompactionError:
    """The :class:`CompactionError` a provider-filtered completion fails with.

    Names the PHASE (``what`` — ``map 12-30`` / ``fold``) and the MODEL, and says
    what to check: the reader must be able to tell "the provider filtered this
    content" from "the summarize connection is broken", which is exactly the
    distinction a silent discard erased.
    """
    return CompactionError(
        COMPLETION_FILTERED_STAGE,
        f"{what}: the provider filtered this completion "
        f"(finish_reason={finish!r}) for {_model_label(run)} — the generation "
        "tripped the platform's safety filter, so there is no summary to keep "
        "and the call is not retried; check the summarize connection's model "
        "and the content it was asked to condense",
    )


async def _drain_frames(summarizer: Any, messages: list[dict]) -> tuple[str, Any]:
    """Run the summarizer seam once, returning ``(content_text, finish_reason)``.

    Accepts every shape the codebase's seams use: an async function returning
    ``str``, a ``ModelCaller``-shaped async iterator of ``StreamEvent`` dicts
    (``content`` deltas concatenated, reasoning ignored), or a plain
    string-returning callable. Only the streaming seam can report a
    ``finish_reason``; the plain seams report ``None`` (accepted).
    """
    result = summarizer(messages)
    if isinstance(result, str):
        return result, None
    if inspect.isawaitable(result):
        text = await result
        if isinstance(text, str):
            return text, None
        return ("" if text is None else str(text)), None
    parts: list[str] = []
    finish: Any = None
    async for event in result:  # ModelCaller: async iterator of StreamEvent dicts
        if isinstance(event, dict) and event.get("type") == "content":
            parts.append(str(event.get("delta", "") or ""))
        present, reason = _finish_reason_of(event)
        if present:
            finish = reason
    return "".join(parts), finish


def _cap_output(text: str, cap_chars: int) -> str:
    """Clamp one model output to the per-call output cap (W9.26 §2.3 step 4).

    Applied to every model output — each map note, each intermediate fold note
    and the final reduce — because the note the recap carries must itself fit
    the window it will be re-read in.
    """
    text = text.strip()
    if cap_chars <= 0 or len(text) <= cap_chars:
        return text
    return text[:cap_chars].rstrip()


def _request_char_budget(budget_tokens: int) -> int:
    """Chars one summarizer REQUEST may carry, once the fixed prompt is paid for.

    ``budget × CHUNK_TRIGGER_SHARE`` tokens, minus the system prompt + standing
    instruction the builder always sends, converted at the compiler's char/4
    heuristic (the same estimate every other consumer uses).
    """
    tokens = max(1, int(budget_tokens * CHUNK_TRIGGER_SHARE))
    overhead = estimate_prompt_tokens(build_summarizer_messages(""))
    return max(CHARS_PER_TOKEN, (tokens - overhead) * CHARS_PER_TOKEN)


def _default_output_cap(budget_tokens: int) -> int:
    """Per-call output cap when the caller declares none.

    A quarter of the run's usable budget lands a note small enough for four of
    them (plus the fold's standing instruction) to fit ONE reduce request —
    which is exactly what the reduce needs to keep folding without truncation.
    """
    return max(128, min(4096, int(budget_tokens * CHUNK_TRIGGER_SHARE) // 4))


def _split_groups(
    groups: list[list[Any]], char_budget: int
) -> list[list[list[Any]]]:
    """Split turn groups into chunks that each fit ``char_budget``.

    Boundaries fall between TURN PAIRS only (a chunk never strands a player line
    from its reply). A single turn larger than the whole budget becomes its own
    oversized chunk — a turn is atomic, so there is nothing smaller to split.
    """
    chunks: list[list[list[Any]]] = []
    current: list[list[Any]] = []
    current_chars = 0
    for group in groups:
        size = len(_format_rows(group)) + 2  # + the blank separator
        if current and current_chars + size > char_budget:
            chunks.append(current)
            current = []
            current_chars = 0
        current.append(group)
        current_chars += size
    if current:
        chunks.append(current)
    return chunks


def _fold_text(notes: list[str], inherited: list[str]) -> str:
    """The reduce request body: merge the chunk notes and absorb the recap.

    Passed through the SAME ``messages_builder`` seam the map calls use (the
    builder takes a transcript string), so a customized Studio summarize flow
    styles the fold too — no second builder contract, no drift.
    """
    blocks: list[str] = [
        "MERGE these continuity notes into ONE set of continuity notes for a "
        "long-form roleplay. Keep every relationship, unresolved thread, time "
        "and place, promise and obligation; invent nothing; output the merged "
        "notes alone.",
        "CHUNK NOTES:",
    ]
    for index, note in enumerate(notes, 1):
        blocks.append(f"[note {index}]\n{note}")
    if inherited:
        blocks.append("EARLIER RECAP (already folded history — absorb it, do not repeat it verbatim):")
        blocks.extend(inherited)
    return "\n\n".join(blocks)


class _ChunkStore:
    """Chunk-output persistence/reuse over the caller's job object (§2.3 step 4).

    The job registry is Lane D's, so this adapter accepts the shapes a job can
    honestly offer:

    * ``job.chunks`` as a MAPPING — keyed by the chunk's ``first-last`` seq span,
      which is stable across a retry (the split is greedy from the start, so
      every already-landed chunk keeps its boundaries);
    * ``job.chunks`` as a LIST plus ``job.add_chunk`` — the live ``CompactJob``
      shape, reused positionally in chunk order;
    * no chunk surface at all — nothing is persisted (a plain call).

    A reused chunk is never re-billed; only a missing/blank entry is re-mapped.
    """

    def __init__(self, job: Any | None) -> None:
        self._mapping: dict | None = None
        self._list: list | None = None
        self._add: Any | None = None
        self._index = 0
        if job is None:
            return
        chunks = getattr(job, "chunks", None)
        if isinstance(chunks, dict):
            self._mapping = chunks
        elif isinstance(chunks, list):
            self._list = chunks
        add = getattr(job, "add_chunk", None)
        if callable(add):
            self._add = add
        if self._mapping is None and self._list is None:
            # An opaque job object: keep the keyed store on its own attribute
            # rather than replacing whatever attribute it already uses.
            try:
                setattr(job, "compaction_chunks", {})
                candidate = getattr(job, "compaction_chunks", None)
            except Exception:  # noqa: BLE001 - a read-only job must not break us
                candidate = None
            if isinstance(candidate, dict):
                self._mapping = candidate

    def get(self, key: str) -> str | None:
        """A landed chunk's text, or ``None`` when it must be mapped now."""
        if self._mapping is not None:
            text = self._mapping.get(key)
        elif self._list is not None:
            text = self._list[self._index] if self._index < len(self._list) else None
            self._index += 1
        else:
            text = None
        if isinstance(text, str) and text.strip():
            return text
        return None

    def put(self, key: str, text: str) -> None:
        """Persist one freshly mapped chunk on the job."""
        if self._mapping is not None:
            self._mapping[key] = text
        elif self._add is not None:
            self._add(text)
        elif self._list is not None:
            self._list.append(text)


def _job_set(job: Any, name: str, value: Any) -> None:
    """Best-effort attribute write onto the caller's job object."""
    if job is None:
        return
    try:
        setattr(job, name, value)
    except Exception:  # noqa: BLE001 - job reporting must never break compaction
        pass


class CompactService:
    """The W9.26 §2.3 compact pipeline (one instance per app: ``app.state``).

    Construct with the resolved model seam and the summarizer's OWN budget —
    :meth:`compact` then owns span selection, chunking, the allowlist, the
    summary v2 write, the fork and the era marker. Claims, the job registry and
    the HTTP surface live outside (W9.26 §2.3 step 1 / §2.2); this class is
    happy to be called directly from a test or a job runner.

    Every argument may be overridden per call, so a job runner can pass the
    values it resolved for THIS run (a retry, a per-interaction override).
    """

    def __init__(
        self,
        *,
        summarizer: Any | None = None,
        budget_tokens: int | None = None,
        model: str = "",
        output_cap_tokens: int | None = None,
        messages_builder: Any | None = None,
        actor: str = SUMMARY_ACTOR,
    ) -> None:
        self._summarizer = summarizer
        self._budget_tokens = budget_tokens
        self._model = model
        self._output_cap_tokens = output_cap_tokens
        # D18 completion: an optional flow renderer (transcript) -> messages;
        # None keeps build_summarizer_messages (the fixed default shape).
        self._messages_builder = messages_builder
        self.actor = actor

    # -- the entry point ---------------------------------------------------
    async def compact(
        self,
        session_id: str,
        log: Any | None = None,
        *,
        trigger: str = "manual",
        job: Any | None = None,
        summarizer: Any | None = None,
        budget_tokens: int | None = None,
        model: str | None = None,
        output_cap_tokens: int | None = None,
        new_session_id: str | None = None,
    ) -> CompactOutcome:
        """Compact ``session_id`` into a new era (§2.3 steps 2–10).

        Returns a :class:`CompactOutcome`; a failure is RECORDED in the log
        (``system``/``compaction_failed`` with the producing ``stage``) before it
        is returned, so the forced-path attempt counter (§2.3 step 0, ⚑R3a) and
        the Inspector both read it. Never raises for a model/config failure.
        """
        if log is None:
            raise ValueError("compact() needs an event log")
        summarizer = self._summarizer if summarizer is None else summarizer
        budget = int(
            budget_tokens if budget_tokens is not None else (self._budget_tokens or 0)
        )
        model = self._model if model is None else model
        cap_tokens = int(
            output_cap_tokens
            if output_cap_tokens is not None
            else (self._output_cap_tokens or _default_output_cap(budget))
        )
        cap_chars = max(0, cap_tokens * CHARS_PER_TOKEN)
        # The run ledger. ``model`` rides it too: a filtered completion's ADR-023
        # failure text names the model, and this is the value resolved for THIS
        # run (a per-call override included), not the instance default.
        run = {"calls": 0, "prompt_tokens": 0, "model": str(model)}

        if summarizer is None:
            return self._fail(
                log, session_id, None, None, "config", "no summarizer configured", job
            )
        if budget <= 0:
            # §2.3 step 2: a budget that cannot fit a prompt is a DECLARATION
            # problem (⚑C) — compaction cannot fix it, so say so readably.
            return self._fail(
                log,
                session_id,
                None,
                None,
                "config",
                "summarizer budget is not positive: declare Context Size / "
                "Response Length for the summarize connection (compaction cannot "
                "free a window that is not declared)",
                job,
            )

        events = _chain_events(log, session_id)
        covered = covered_turn_seqs(events)
        groups = turn_groups(turn_rows(events))
        uncovered = [
            group
            for group in groups
            if _field(group[-1], "type") != "narrative"
            or _field(group[-1], "seq") not in covered
        ]
        prior = _newest_summary(events)

        if not uncovered:
            # §2.3 step 10: nothing left to summarize ⇒ fork the newest existing
            # summary's era, NO model call (repairs summary-without-successor
            # crash states and doubles as legacy migration).
            return self._recover(
                log, session_id, events, prior, trigger=trigger, job=job,
                new_session_id=new_session_id, model=model,
            )

        first_seq, _ = _group_bounds(uncovered[0])
        _, last_seq = _group_bounds(uncovered[-1])

        # §2.3 step 6: rows land on the session being compacted, labeled
        # ``summarizer`` (the button path keeps its measurements).
        from fabulist.agents.debug import CURRENT_INTERACTION, CURRENT_SESSION

        session_token = CURRENT_SESSION.set(session_id)
        interaction_token = CURRENT_INTERACTION.set("summarizer")
        try:
            text = await self._summarize_span(
                uncovered,
                inherited=recap_texts(events),
                budget_tokens=budget,
                cap_chars=cap_chars,
                job=job,
                run=run,
                summarizer=summarizer,
            )
        except CompactionError as failure:
            return self._fail(
                log, session_id, first_seq, last_seq, failure.stage, failure.error, job
            )
        finally:
            CURRENT_INTERACTION.reset(interaction_token)
            CURRENT_SESSION.reset(session_token)

        text = text.strip()
        if not text:
            return self._fail(
                log,
                session_id,
                first_seq,
                last_seq,
                "summarizer_empty",
                "the summarizer returned no text",
                job,
            )

        # The run ledger. ``prompt_tokens`` is the char/4 ESTIMATE of every
        # request this run sent (the same heuristic the prompt-budget warn uses);
        # the MEASURED numbers ride the per-call ``usage`` rows step 6 makes land
        # on this session, because a fake/custom caller reports no usage frames.
        stats = {
            "calls": int(run["calls"]),
            "prompt_tokens": int(run["prompt_tokens"]),
            "model": str(model),
        }
        reason = (
            f"compact({trigger}): {len(uncovered)} turn group(s) seq "
            f"{first_seq}-{last_seq}, {run['calls']} call(s)"
        )
        if prior is not None:
            reason += f", absorbs {_field(prior, 'session_id')}:{_field(prior, 'seq')}"

        summary_payload: dict[str, Any] = {
            "text": text,
            "covers_seqs": [first_seq, last_seq],
            "reason": reason,
            "trigger": trigger,
            "stats": stats,
        }
        if prior is not None:
            # §2.3 step 7: the pair is OMITTED when there is no prior summary —
            # a present ``null`` would fail the typed gate.
            summary_payload["absorbs"] = [
                _field(prior, "session_id"),
                _field(prior, "seq"),
            ]

        try:
            summary = log.append_event(
                session_id, "summary", self.actor, summary_payload
            )
            successor, title = self._start_era(
                log,
                session_id,
                summary,
                covers_seqs=[first_seq, last_seq],
                trigger=trigger,
                stats=stats,
                new_session_id=new_session_id,
            )
        except CompactionError as failure:
            # The summary may already have landed: that is exactly the
            # summary-without-successor state step 10 repairs on the next call.
            return self._fail(
                log, session_id, first_seq, last_seq, failure.stage, failure.error, job
            )
        return CompactOutcome(
            status="done",
            session_id=successor,
            title=title,
            summary=summary,
            calls=int(run["calls"]),
            details={"covers_seqs": [first_seq, last_seq], "stats": stats},
        )

    # -- map-reduce --------------------------------------------------------
    async def _summarize_span(
        self,
        groups: list[list[Any]],
        *,
        inherited: list[str],
        budget_tokens: int,
        cap_chars: int,
        job: Any | None,
        run: dict,
        summarizer: Any,
    ) -> str:
        """Map every turn-aligned chunk, then fold the notes + the recap."""
        char_budget = _request_char_budget(budget_tokens)
        chunks = _split_groups(groups, char_budget)
        store = _ChunkStore(job)
        notes: list[str] = []
        for chunk in chunks:
            lo, hi = _group_bounds(chunk[0])[0], _group_bounds(chunk[-1])[1]
            key = f"{lo}-{hi}"
            cached = store.get(key)
            if cached is not None:
                notes.append(cached)  # a landed chunk is reused, never re-billed
                continue
            transcript = _format_rows([row for group in chunk for row in group])
            if not transcript.strip():
                raise CompactionError(
                    "map_exhausted", f"chunk seq {lo}-{hi} rendered no transcript"
                )
            note = _cap_output(
                await self._model_call(
                    self._messages(transcript), run, what=f"map {key}", summarizer=summarizer
                ),
                cap_chars,
            )
            if not note:
                raise CompactionError(
                    "map_exhausted", f"chunk seq {lo}-{hi} produced no summary text"
                )
            notes.append(note)
            store.put(key, note)

        if len(notes) == 1 and not inherited:
            return notes[0]

        pending = notes
        for _ in range(_MAX_FOLD_LEVELS):
            if len(_fold_text(pending, inherited)) <= char_budget:
                break
            if len(pending) > 1:
                shrunk = await self._fold_level(
                    pending, char_budget, cap_chars, run, summarizer=summarizer
                )
                if len(shrunk) < len(pending):
                    pending = shrunk
                    continue
            # No progress possible (one note, or notes too large to pair): clamp
            # them so the final request fits the budget instead of looping.
            share = max(CHARS_PER_TOKEN, char_budget // (len(pending) + 1))
            pending = [note[:share] for note in pending]
            break
        return _cap_output(
            await self._model_call(
                self._messages(_fold_text(pending, inherited)),
                run,
                what="fold",
                summarizer=summarizer,
            ),
            cap_chars,
        )

    async def _fold_level(
        self,
        notes: list[str],
        char_budget: int,
        cap_chars: int,
        run: dict,
        *,
        summarizer: Any,
    ) -> list[str]:
        """One hierarchical fold level: merge adjacent notes that overflow.

        Groups adjacent notes until the merged request would overflow, folds
        that batch into one note, and repeats — so a 40-chunk history folds in
        tree shape instead of losing the tail to a single oversized request.
        """
        batches: list[list[str]] = []
        current: list[str] = []
        for note in notes:
            trial = current + [note]
            # Measure the READY request (header included), so a batch that only
            # overflows once the standing instruction is added is not produced.
            if current and len(_fold_text(trial, [])) > char_budget:
                batches.append(current)
                current = [note]
            else:
                current = trial
        if current:
            batches.append(current)
        if len(batches) <= 1:
            return notes  # no progress possible; the caller clamps
        folded: list[str] = []
        for batch in batches:
            if len(batch) == 1:
                folded.append(batch[0])
                continue
            folded.append(
                _cap_output(
                    await self._model_call(
                        self._messages(_fold_text(batch, [])),
                        run,
                        what="fold",
                        summarizer=summarizer,
                    ),
                    cap_chars,
                )
            )
        return folded

    def _messages(self, transcript: str) -> list[dict]:
        """Render one request through the optional flow builder."""
        if self._messages_builder is not None:
            return self._messages_builder(transcript)
        return build_summarizer_messages(transcript)

    async def _call_once(
        self, messages: list[dict], run: dict, summarizer: Any
    ) -> tuple[str, Any]:
        """One billed attempt: count it on the run ledger, then drain the stream."""
        run["calls"] += 1
        run["prompt_tokens"] += estimate_prompt_tokens(messages)
        return await _drain_frames(summarizer, messages)

    async def _model_call(
        self, messages: list[dict], run: dict, *, what: str, summarizer: Any
    ) -> str:
        """One summarizer call under the three-class finish-reason gate (issue #26).

        TRUSTED (``stop``/``tool_calls``/unset) returns the text. FILTER
        (``sensitive``/``content_filter``) fails the phase immediately as
        :data:`COMPLETION_FILTERED_STAGE`: the provider refused THIS content, so
        re-sending the identical request would only risk hiding the signal. An
        UNKNOWN reason keeps ADR-021's behavior exactly — the reply is discarded
        even when it parses, repaired once, then the stage fails.
        """
        text, finish = await self._call_once(messages, run, summarizer)
        verdict = classify_finish_reason(finish)
        if verdict == FINISH_TRUSTED:
            return text
        if verdict == FINISH_FILTER:
            raise _filtered_error(what, finish, run)
        # UNKNOWN: discard, retry exactly once, then fail the stage.
        text, finish = await self._call_once(messages, run, summarizer)
        verdict = classify_finish_reason(finish)
        if verdict == FINISH_TRUSTED:
            return text
        if verdict == FINISH_FILTER:
            raise _filtered_error(what, finish, run)
        raise CompactionError(
            "finish_reason",
            f"{what}: summarizer finished with {finish!r} after one retry "
            f"(trusted: {sorted(TRUSTED_FINISH_REASONS)} or unset)",
        )

    # -- write / fork / marker --------------------------------------------
    def _start_era(
        self,
        log: Any,
        session_id: str,
        summary: Any,
        *,
        covers_seqs: list[int],
        trigger: str,
        stats: dict,
        new_session_id: str | None,
    ) -> tuple[str, str]:
        """Fork the successor at ``summary.seq`` and mark it (§2.3 steps 8/9).

        The marker is written to the NEW session (its own row, so ``kind``
        derives from marker PRESENCE); the ``session_renamed`` carry row gives
        the era the conversation's title — a lazily registered child would
        otherwise serve ``title: ""`` until someone renames it.
        """
        titled_rows = _own_events(log, session_id)
        title = _session_title(titled_rows)
        successor = new_session_id or uuid.uuid4().hex
        existing = getattr(log, "get_session", None)
        if callable(existing) and existing(successor) is not None:
            raise CompactionError(
                "fork", f"successor session already exists: {successor!r}"
            )
        log.fork_session(session_id, _field(summary, "seq"), successor)
        log.append_event(
            successor,
            "system",
            self.actor,
            {
                "event": COMPACTION_MARKER_EVENT,
                "source_session": session_id,
                "summary_ref": [_field(summary, "session_id"), _field(summary, "seq")],
                "covers_seqs": list(covers_seqs),
                "trigger": trigger,
                "stats": dict(stats),
            },
        )
        if title:
            log.append_event(
                successor, "system", "system", {"event": "session_renamed", "title": title}
            )
        return successor, title

    def _recover(
        self,
        log: Any,
        session_id: str,
        events: list[Any],
        prior: Any | None,
        *,
        trigger: str,
        job: Any | None,
        new_session_id: str | None,
        model: str,
    ) -> CompactOutcome:
        """Step 10: nothing uncovered ⇒ fork the existing summary, no model call."""
        if prior is None:
            return CompactOutcome(
                status="noop",
                reason="nothing to compact: the session has no turns and no summary",
            )
        seq = _field(prior, "seq")
        stats = {"calls": 0, "prompt_tokens": 0, "model": str(model)}
        existing = _successor_forking_at(log, session_id, seq)
        if existing is not None:
            return CompactOutcome(
                status="done",
                session_id=existing,
                title=_session_title(_own_events(log, existing)),
                summary=prior,
                recovered=True,
                reason="already compacted: the era at this summary exists",
                details={"idempotent": True},
            )
        covers = list((_field(prior, "payload") or {}).get("covers_seqs") or [])
        try:
            successor, title = self._start_era(
                log,
                session_id,
                prior,
                covers_seqs=[int(seq) for seq in covers if isinstance(seq, int)],
                trigger=trigger,
                stats=stats,
                new_session_id=new_session_id,
            )
        except CompactionError as failure:
            return self._fail(
                log, session_id, None, None, failure.stage, failure.error, job
            )
        return CompactOutcome(
            status="done",
            session_id=successor,
            title=title,
            summary=prior,
            recovered=True,
            details={"covers_seqs": covers, "stats": stats},
        )

    def _fail(
        self,
        log: Any,
        session_id: str,
        first_seq: int | None,
        last_seq: int | None,
        stage: str,
        error: str,
        job: Any | None,
    ) -> CompactOutcome:
        """Record the failed attempt (the log is the attempt counter) and return."""
        _append_compaction_failed(
            log, session_id, first_seq, last_seq, stage=stage, error=error
        )
        _job_set(job, "stage", stage)
        _job_set(job, "error", error)
        return CompactOutcome(status="failed", stage=stage, error=error)


async def compact(session_id: str, log: Any, **kwargs: Any) -> CompactOutcome:
    """One-shot form of :meth:`CompactService.compact` (tests / job runners)."""
    service_kwargs = {
        key: kwargs.pop(key)
        for key in (
            "summarizer",
            "budget_tokens",
            "model",
            "output_cap_tokens",
            "messages_builder",
            "actor",
        )
        if key in kwargs
    }
    return await CompactService(**service_kwargs).compact(session_id, log, **kwargs)
