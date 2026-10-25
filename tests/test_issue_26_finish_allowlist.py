"""Issue #26: a provider FILTER signal is a recognized outcome class, not a discard.

The transport lane found that z.ai's documented STREAMING-side filter signal is
``finish_reason: "sensitive"`` and that compaction's allowlist
(``ALLOWED_FINISH_REASONS = {"stop", "tool_calls"}``) threw it away, so a
platform-filtered completion was indistinguishable from a self-silenced one.

The ruling this pins, in one table (``classify_finish_reason``):

===========  =================================  ============================
class        reasons                            what the phase does
===========  =================================  ============================
trusted      ``stop``, ``tool_calls``, unset    return the text
filter       ``sensitive``, ``content_filter``  fail as ``completion_filtered``
unknown      anything else (``length``, …)     discard, retry once, fail
===========  =================================  ============================

The FILTER class surfaces under the ADR-023 contract (a contained phase failure:
``system``/``compaction_failed`` with the ``completion_filtered`` stage, naming
the phase and the model) rather than as a ``degradation`` notice — the reasoning
is in the ``fabulist.graph.compaction`` module docstring. A filter is NOT
retried: re-sending the identical request into the same filter could only hide
the signal. Everything else — prose streaming, and the discard path for reasons
the engine has not recognized — is untouched.

Every test here is a fake transport: no test touches a model.
"""

from __future__ import annotations

import asyncio

import pytest

from fabulist.agents.loop import run_agent_loop
from fabulist.graph.compaction import (
    ALLOWED_FINISH_REASONS,
    COMPLETION_FILTERED_STAGE,
    FILTER_FINISH_REASONS,
    FINISH_FILTER,
    FINISH_TRUSTED,
    FINISH_UNKNOWN,
    TRUSTED_FINISH_REASONS,
    classify_finish_reason,
    compact,
)
from fabulist.log import EventLog

SESSION = "issue-26"
MODEL = "z-ai/glm-4.6"

#: Marks a scripted reply that emits NO terminal frame at all (the pre-W9.26
#: wire: a missing finish_reason is not evidence of truncation).
ABSENT = object()


# -- fixtures / fakes --------------------------------------------------------


@pytest.fixture
def log(tmp_path):
    event_log = EventLog(tmp_path / "finish-allowlist.db")
    try:
        yield event_log
    finally:
        event_log.close()


class FrameSummarizer:
    """A ``ModelCaller``-shaped summarizer that streams ``(text, finish_reason)``.

    The LAST script entry repeats once the script is exhausted, so a test states
    exactly the replies it means and any extra call is visible.
    """

    def __init__(self, script=None, default=("SUMMARY TEXT", "stop")):
        self.script = list(script or [])
        self.default = default
        self.calls: list[list[dict]] = []

    def __call__(self, messages):
        self.calls.append(messages)
        text, finish = self.script.pop(0) if self.script else self.default
        return self._stream(text, finish)

    async def _stream(self, text, finish):
        if text:
            yield {"type": "content", "delta": text}
        if finish is ABSENT:
            return
        if isinstance(finish, dict):
            yield {"type": "content", "delta": "", "meta": finish}
        else:
            yield {"type": "finish", "finish_reason": finish}


class WireCaller:
    """A narrator-shaped streaming caller over scripted FRAME lists."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.requests: list[list[dict]] = []

    async def __call__(self, messages, stream=True, request_profile=None):
        self.requests.append([dict(message) for message in messages])
        for event in self.replies.pop(0):
            yield dict(event)


def run(coro):
    return asyncio.run(coro)


def add_pair(log, session, prose):
    """One complete turn (user row then narrative row): a single-chunk span."""
    log.append_event(session, "user", "user", {"text": f"player asks for {prose}"})
    log.append_event(
        session,
        "narrative",
        "narrator",
        {"prose": prose, "dsl": "", "segments": [{"kind": "prose", "text": prose}]},
    )


def summary_rows(log, session=SESSION):
    return [event for event in log.get_events(session) if event.type == "summary"]


def failure_rows(log, session=SESSION):
    return [
        event
        for event in log.get_events(session)
        if event.type == "system"
        and (event.payload or {}).get("event") == "compaction_failed"
    ]


def compaction(session=SESSION, **kwargs):
    kwargs.setdefault("budget_tokens", 8192)
    kwargs.setdefault("model", MODEL)
    return kwargs


# -- the classification table ----------------------------------------------


def test_the_three_classes_and_the_trusted_stops():
    """Trusted stops, the two documented filter signals, everything else unknown."""
    for reason in ("stop", "tool_calls", None, ""):
        assert classify_finish_reason(reason) == FINISH_TRUSTED, reason
    for reason in ("sensitive", "content_filter"):
        assert classify_finish_reason(reason) == FINISH_FILTER, reason
    # Unknown keeps ADR-021's discard class — a truncation included: it is
    # REPAIRED once by the retry, never reclassified as a provider filter.
    for reason in ("banana", "length", "STOP", "SENSITIVE", " sensitive", 1301, 0.0):
        assert classify_finish_reason(reason) == FINISH_UNKNOWN, reason


def test_the_classes_are_disjoint_and_the_old_seam_name_still_means_trusted():
    assert TRUSTED_FINISH_REASONS == frozenset({"stop", "tool_calls"})
    assert FILTER_FINISH_REASONS == frozenset({"sensitive", "content_filter"})
    assert not (TRUSTED_FINISH_REASONS & FILTER_FINISH_REASONS)
    # The exported seam name every existing caller reaches for was never widened:
    # a filter is a recognized REFUSAL, not an allowed end.
    assert ALLOWED_FINISH_REASONS is TRUSTED_FINISH_REASONS


# -- a filtered completion fails the phase, named, once --------------------


def test_a_sensitive_completion_fails_the_phase_as_completion_filtered(log):
    """z.ai's STREAMING-side filter signal: contained, named, and NOT retried."""
    add_pair(log, SESSION, "the room is quiet")
    summarizer = FrameSummarizer([("BLOCKED PARTIAL ANSWER", "sensitive")])

    outcome = run(compact(SESSION, log, summarizer=summarizer, **compaction()))

    assert outcome.status == "failed"
    assert outcome.stage == COMPLETION_FILTERED_STAGE == "completion_filtered"
    # ADR-023's shape: the PHASE and the MODEL, so a filtered connection reads
    # differently from a broken one.
    assert "map 1-2" in outcome.error  # the phase that was refused
    assert MODEL in outcome.error  # the model that refused it
    assert "'sensitive'" in outcome.error  # the provider's own signal, verbatim
    # No retry: an identical request into the same filter could only hide it.
    assert len(summarizer.calls) == 1
    # The blocked text is not a summary and never lands in the log (D9/ADR-021).
    assert summary_rows(log) == []
    failures = failure_rows(log)
    assert len(failures) == 1
    assert failures[0].payload["stage"] == COMPLETION_FILTERED_STAGE
    assert failures[0].payload["covers_seqs"] == [1, 2]
    assert MODEL in failures[0].payload["error"]


def test_a_content_filter_completion_is_the_same_recognized_class(log):
    """The OpenAI-compatible spelling classifies identically (one class, two names)."""
    add_pair(log, SESSION, "the room is quiet")
    summarizer = FrameSummarizer([("BLOCKED", "content_filter")])

    outcome = run(compact(SESSION, log, summarizer=summarizer, **compaction()))

    assert outcome.status == "failed"
    assert outcome.stage == COMPLETION_FILTERED_STAGE
    assert "'content_filter'" in outcome.error
    assert len(summarizer.calls) == 1
    assert summary_rows(log) == []
    assert failure_rows(log)[0].payload["stage"] == COMPLETION_FILTERED_STAGE


def test_a_filter_on_the_retry_is_still_named_as_a_filter(log):
    """A truncation earns the one repair; if the repair is FILTERED, that is the verdict."""
    add_pair(log, SESSION, "the room is quiet")
    summarizer = FrameSummarizer([("TRUNCATED", "length"), ("STILL REFUSED", "sensitive")])

    outcome = run(compact(SESSION, log, summarizer=summarizer, **compaction()))

    assert outcome.status == "failed"
    assert outcome.stage == COMPLETION_FILTERED_STAGE
    assert len(summarizer.calls) == 2  # the truncation's single repair, then refusal
    assert summary_rows(log) == []


def test_a_filtered_fold_names_the_fold_phase(log):
    """The phase label follows the call, not the module: a fold failure says fold."""
    # An inherited recap (a seeded summary covering nothing) makes the run fold
    # the mapped note with the earlier recap — so the SECOND call is the reduce.
    log.append_event(
        SESSION,
        "summary",
        "summarizer",
        {"text": "earlier recap", "covers_seqs": [1, 1], "reason": "seeded"},
    )
    add_pair(log, SESSION, "a: " + "x" * 500)
    add_pair(log, SESSION, "b: " + "y" * 500)
    summarizer = FrameSummarizer([("note one", "stop"), ("BLOCKED FOLD", "sensitive")])

    outcome = run(compact(SESSION, log, summarizer=summarizer, **compaction()))

    assert outcome.status == "failed"
    assert outcome.stage == COMPLETION_FILTERED_STAGE
    assert "fold" in outcome.error
    assert MODEL in outcome.error
    assert len(summarizer.calls) == 2  # map, then the refused fold
    assert [row.payload["text"] for row in summary_rows(log)] == ["earlier recap"]


# -- everything the engine does NOT recognize is unchanged (ADR-021) -------


def test_an_unknown_reason_is_discarded_and_repaired_exactly_as_before(log):
    add_pair(log, SESSION, "the room is quiet")
    summarizer = FrameSummarizer([("DISCARDED", "banana"), ("GOOD NOTE", "stop")])

    outcome = run(compact(SESSION, log, summarizer=summarizer, **compaction()))

    assert outcome.status == "done"
    assert len(summarizer.calls) == 2  # one repair, not three attempts
    assert summary_rows(log)[0].payload["text"] == "GOOD NOTE"


def test_an_unknown_reason_twice_fails_the_generic_stage(log):
    """The unknown class keeps its stage name: only the filter class was named."""
    add_pair(log, SESSION, "the room is quiet")
    summarizer = FrameSummarizer([("A", "banana"), ("B", "banana")])

    outcome = run(compact(SESSION, log, summarizer=summarizer, **compaction()))

    assert outcome.status == "failed"
    assert outcome.stage == "finish_reason"
    assert len(summarizer.calls) == 2
    assert summary_rows(log) == []
    assert failure_rows(log)[0].payload["stage"] == "finish_reason"


# -- prose handling is untouched -------------------------------------------


def test_a_filtered_narrator_completion_still_delivers_its_prose(tmp_path):
    """The classification is the STRUCTURED-OUTPUT seam's: prose is never refused.

    The narrator wire records the reason (issue #28) and the loop hands the text
    back unchanged — a filtered narrator completion is a fact the CALLER reads,
    not something the loop raises over.
    """
    event_log = EventLog(tmp_path / "loop.db")
    prose = 'Rain hammers the shutters.\n\n«Lantern?» she asked.'
    caller = WireCaller(
        [
            [
                {"type": "content", "delta": prose[: len(prose) // 2]},
                {"type": "content", "delta": prose[len(prose) // 2 :]},
                {"type": "finish", "finish_reason": "sensitive"},
            ]
        ]
    )
    try:
        result = asyncio.run(
            run_agent_loop(
                caller=caller,
                messages=[
                    {"role": "system", "content": "You are the narrator."},
                    {"role": "user", "content": "Describe the harbour at dawn."},
                ],
                surface="narrate",
                log=event_log,
                session_id=SESSION,
            )
        )
        assert result.text == prose  # byte-identical: nothing was stripped or held
        assert result.finish_reason == "sensitive"  # the caller can classify it
        rows = [
            event.payload
            for event in event_log.get_events(SESSION)
            if event.type == "system" and (event.payload or {}).get("event") == "agent_loop"
        ]
        assert [row["finish_reason"] for row in rows if row["kind"] == "completion"] == [
            "sensitive"
        ]
    finally:
        event_log.close()
