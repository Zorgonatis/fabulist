"""Tests for the W9.26 §2.3 compact service (Lane A).

The service is the successor to ``Compactor.maybe_compact``: turn-pair spans (no
orphan player lines), chunked map-reduce against the summarizer's OWN budget, the
D86 finish-reason allowlist, summary v2 (``trigger``/``stats`` + ``absorbs``
omitted-when-absent), the era marker in the successor session, step-10 recovery
(nothing uncovered ⇒ fork an existing summary, NO model call) and the projection
transport keys (``context_lows``/``compactions``).

The suite has no pytest-asyncio dependency, so async scenarios run via
``asyncio.run`` inside sync tests (same convention as ``test_compaction.py``).
Every test uses the real :class:`~fabulist.log.EventLog` on a tmp SQLite file,
so payloads are validated by the real schema gate at the append door.
"""

from __future__ import annotations

import asyncio

from fabulist.graph.compaction import (
    ALLOWED_FINISH_REASONS,
    CHUNK_TRIGGER_SHARE,
    _request_char_budget,
    compact,
    covered_turn_seqs,
    recap_texts,
    turn_groups,
    turn_rows,
)
from fabulist.prompts.compiler import CHARS_PER_TOKEN

#: Marks a scripted reply that emits NO terminal frame at all (the pre-W9.26
#: wire: a missing finish_reason is not evidence of truncation).
ABSENT = object()


def run(coro):
    return asyncio.run(coro)


# -- fixtures / fakes --------------------------------------------------------


def add_user(log, session, text):
    return log.append_event(session, "user", "user", {"text": text})


def add_narrative(log, session, prose):
    return log.append_event(
        session,
        "narrative",
        "narrator",
        {"prose": prose, "dsl": "", "segments": [{"kind": "prose", "text": prose}]},
    )


def add_pair(log, session, prose, *, user=None):
    """Append one complete turn (user row then narrative row)."""
    add_user(log, session, user if user is not None else f"player asks for {prose}")
    return add_narrative(log, session, prose)


def add_summary(log, session, text, covers, *, absorbs=None, ephemeral=False):
    payload = {"text": text, "covers_seqs": list(covers), "reason": "seeded"}
    if absorbs is not None:
        payload["absorbs"] = list(absorbs)
    return log.append_event(
        session, "summary", "summarizer", payload, ephemeral=ephemeral
    )


class FrameSummarizer:
    """A ``ModelCaller``-shaped summarizer that streams (text, finish_reason).

    ``script`` entries are ``(text, finish)``; ``finish`` may be a string, a
    ``meta``-carried reason via ``{"meta": ...}``, or :data:`ABSENT` for a stream
    with no terminal frame. The last entry repeats once the script is exhausted.
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


def summary_rows(log, session):
    return [e for e in log.get_events(session) if e.type == "summary"]


def marker_rows(log, session):
    return [
        e
        for e in log.get_events(session)
        if e.type == "system" and (e.payload or {}).get("event") == "compaction"
    ]


def failure_rows(log, session):
    return [
        e
        for e in log.get_events(session)
        if e.type == "system" and (e.payload or {}).get("event") == "compaction_failed"
    ]


def chunks_of(messages):
    """The transcript body a summarizer call carried (user message content)."""
    return messages[-1]["content"]


def is_fold(messages):
    return "MERGE these continuity notes" in chunks_of(messages)


def block_kinds(messages):
    """The USER/NARRATOR block order a transcript body carries."""
    import re

    return re.findall(r"^\[\d+\] (USER|NARRATOR):", chunks_of(messages), flags=re.M)


def user_bodies(messages):
    return len(block_kinds(messages))


# -- turn-pair span (§2.3 step 3) -------------------------------------------


def test_turn_groups_pair_a_narrative_with_its_preceding_user_row(log):
    for index in range(3):
        add_user(log, "s", f"u{index}")
        add_narrative(log, "s", f"n{index}")

    groups = turn_groups(turn_rows(log.get_chain_events("s")))

    assert [len(group) for group in groups] == [2, 2, 2]
    assert all(group[0].type == "user" and group[1].type == "narrative" for group in groups)


def test_a_trailing_user_row_without_a_reply_is_not_a_turn(log):
    add_user(log, "s", "u0")
    add_narrative(log, "s", "n0")
    add_user(log, "s", "pending question")

    rows = turn_rows(log.get_chain_events("s"))
    groups = turn_groups(rows)

    assert len(groups) == 1  # the pending player line stays RAW (no orphan pair)


def test_span_starts_at_the_user_row_not_the_first_narrative(log):
    """covers_seqs is a turn PAIR: the player line at the head is covered too."""
    for index in range(3):
        add_user(log, "s", f"u{index}")
        add_narrative(log, "s", f"n{index}")

    summarizer = FrameSummarizer()
    outcome = run(compact("s", log, summarizer=summarizer, budget_tokens=8192))

    assert outcome.status == "done"
    summary = summary_rows(log, "s")[0]
    # rows: user@1, narrative@2, user@3, narrative@4, user@5, narrative@6
    assert summary.payload["covers_seqs"] == [1, 6]
    assert "u0" in chunks_of(summarizer.calls[0])


def test_legacy_single_seq_span_is_extended_over_its_user_row(log):
    """A legacy ``[N, N]`` summary must not orphan the player line at ``N-1``.

    The covered set extends each covered narrative down over its user partner,
    so the new span starts AFTER that whole turn and the legacy summary's text is
    absorbed (folded forward) instead of being re-summarized as raw turns.
    """
    for index in range(3):
        add_user(log, "s", f"u{index}")
        add_narrative(log, "s", f"n{index}")  # seqs 1..6
    prior = add_summary(log, "s", "LEGACY NOTE", [2, 2])  # narrative-only span

    covered = covered_turn_seqs(log.get_chain_events("s"))
    assert {1, 2} <= covered  # the legacy narrative AND its player line

    summarizer = FrameSummarizer()
    outcome = run(compact("s", log, summarizer=summarizer, budget_tokens=8192))

    assert outcome.status == "done"
    summary = summary_rows(log, "s")[1]
    assert summary.payload["covers_seqs"] == [3, 6]
    assert summary.payload["absorbs"] == ["s", prior.seq]
    # The legacy turn is excluded from the map transcript, and its note is
    # folded forward (absorption) instead.
    assert "u0" not in chunks_of(summarizer.calls[0])
    assert "LEGACY NOTE" in chunks_of(summarizer.calls[-1])


# -- chunked map-reduce (§2.3 step 4) --------------------------------------


def test_chunk_split_respects_the_budget_and_turn_boundaries(log):
    for index in range(12):
        add_user(log, "s", f"u{index}")
        add_narrative(log, "s", f"{index}:" + "x" * 400)

    budget = 1024
    summarizer = FrameSummarizer(default=("note", "stop"))
    outcome = run(compact("s", log, summarizer=summarizer, budget_tokens=budget))

    assert outcome.status == "done"
    char_budget = _request_char_budget(budget)
    map_calls = [call for call in summarizer.calls if not is_fold(call)]
    fold_calls = [call for call in summarizer.calls if is_fold(call)]
    assert len(map_calls) > 1, "a transcript over 80% of the budget must split"
    assert len(fold_calls) == 1, "one reduce over the chunk notes"
    for messages in map_calls:
        body = chunks_of(messages)
        assert len(body) <= char_budget
        # Chunk boundaries fall on TURN PAIRS: every chunk is a whole sequence of
        # USER/NARRATOR blocks — never a stranded player line.
        kinds = block_kinds(messages)
        assert kinds[0] == "USER" and kinds[-1] == "NARRATOR"
        assert kinds == ["USER", "NARRATOR"] * (len(kinds) // 2)
    # Every turn was summarized exactly once across the chunks.
    bodies = "\n".join(chunks_of(call) for call in map_calls)
    for index in range(12):
        assert f"u{index}" in bodies
    assert sum(user_bodies(call) for call in map_calls) == 24  # 12 pairs


def test_chunk_triggers_only_above_eighty_percent_of_the_budget(log):
    for index in range(3):
        add_user(log, "s", f"u{index}")
        add_narrative(log, "s", f"n{index}" + "x" * 40)

    summarizer = FrameSummarizer(default=("note", "stop"))
    run(compact("s", log, summarizer=summarizer, budget_tokens=8192))

    assert len(summarizer.calls) == 1  # one request, no fold, no chunking
    assert CHUNK_TRIGGER_SHARE == 0.8


def test_per_chunk_output_is_capped(log):
    for index in range(6):
        add_user(log, "s", f"u{index}")
        add_narrative(log, "s", f"{index}:" + "x" * 400)

    summarizer = FrameSummarizer(default=("y" * 5000, "stop"))
    outcome = run(
        compact(
            "s",
            log,
            summarizer=summarizer,
            budget_tokens=1024,
            output_cap_tokens=100,
        )
    )

    assert outcome.status == "done"
    summary = summary_rows(log, "s")[0]
    cap_chars = 100 * CHARS_PER_TOKEN
    assert len(summary.payload["text"]) <= cap_chars


def test_fold_overflows_hierarchically(log):
    """A fold input that overflows the budget folds in tree shape, not by loss."""
    for index in range(10):
        add_user(log, "s", f"u{index}")
        add_narrative(log, "s", f"{index}:" + "x" * 600)

    summarizer = FrameSummarizer(default=("z" * 1200, "stop"))
    outcome = run(compact("s", log, summarizer=summarizer, budget_tokens=1024))

    assert outcome.status == "done"
    # The notes themselves overflow the budget, so the fold runs in tree shape:
    # chunk notes first fold into intermediate notes, THEN the final reduce.
    fold_calls = [call for call in summarizer.calls if is_fold(call)]
    assert len(fold_calls) >= 2
    assert all(len(chunks_of(call)) <= _request_char_budget(1024) for call in fold_calls)
    assert outcome.details["stats"]["calls"] == len(summarizer.calls)
    assert summary_rows(log, "s")[0].payload["text"]  # a merged summary landed


class FoldFailsSummarizer:
    """Maps chunks normally but always violates the allowlist on a fold call."""

    def __init__(self):
        self.calls: list[list[dict]] = []

    def __call__(self, messages):
        self.calls.append(messages)
        if "MERGE these continuity notes" in chunks_of(messages):
            return self._stream("TRUNCATED FOLD", "length")
        return self._stream(f"note {len(self.calls)}", "stop")

    async def _stream(self, text, finish):
        yield {"type": "content", "delta": text}
        yield {"type": "finish", "finish_reason": finish}


def test_landed_chunks_are_reused_on_a_retry(log):
    """A retry after a mid-run failure reuses the chunks that already cost money."""
    for index in range(8):
        add_user(log, "s", f"u{index}")
        add_narrative(log, "s", f"{index}:" + "x" * 500)

    class Job:
        chunks: dict = {}

    job = Job()
    failing = FoldFailsSummarizer()
    failed = run(compact("s", log, summarizer=failing, budget_tokens=1024, job=job))

    assert failed.status == "failed"
    assert failed.stage == "finish_reason"  # the fold exhausts its one retry
    assert len(job.chunks) > 1  # every map chunk landed before the fold failed
    landed = dict(job.chunks)

    # Retry with a working summarizer: the maps are reused, only folds run.
    working = FrameSummarizer(default=("note", "stop"))
    again = run(compact("s", log, summarizer=working, budget_tokens=1024, job=job))

    assert again.status == "done"
    assert working.calls, "the fold still had to run"
    assert all(
        "MERGE these continuity notes" in chunks_of(call) for call in working.calls
    ), "a landed map chunk must never be re-billed"
    assert job.chunks == landed  # nothing new was mapped
    assert summary_rows(log, "s")[0].payload["text"] == "note"


# -- finish-reason allowlist (§2.3 step 5 / D86) ---------------------------


def test_allowed_finish_reasons_accept_stop_tool_calls_and_absent(log):
    assert ALLOWED_FINISH_REASONS == frozenset({"stop", "tool_calls"})
    for finish in ("stop", "tool_calls", None, "", ABSENT):
        add_pair(log, f"s-{finish}", "the room is quiet")
        summarizer = FrameSummarizer([("NOTE", finish)])
        outcome = run(compact(f"s-{finish}", log, summarizer=summarizer, budget_tokens=8192))
        assert outcome.status == "done", finish
        assert summary_rows(log, f"s-{finish}")[0].payload["text"] == "NOTE"


def test_a_violating_finish_reason_is_discarded_and_retried_once(log):
    add_pair(log, "s", "the room is quiet")
    summarizer = FrameSummarizer([("TRUNCATED", "length"), ("GOOD NOTE", "stop")])

    outcome = run(compact("s", log, summarizer=summarizer, budget_tokens=8192))

    assert outcome.status == "done"
    assert len(summarizer.calls) == 2  # one retry, not three attempts
    assert summary_rows(log, "s")[0].payload["text"] == "GOOD NOTE"
    assert summary_rows(log, "s")[0].payload["stats"]["calls"] == 2


def test_two_violations_fail_the_stage_and_file_the_row(log):
    add_pair(log, "s", "the room is quiet")
    # An UNKNOWN reason is ADR-021's discard class (issue #26: only the recognized
    # FILTER class is classified and named — see test_issue_26_finish_allowlist).
    summarizer = FrameSummarizer([("A", "length"), ("B", "banana")])

    outcome = run(compact("s", log, summarizer=summarizer, budget_tokens=8192))

    assert outcome.status == "failed"
    assert outcome.stage == "finish_reason"
    assert len(summarizer.calls) == 2
    assert summary_rows(log, "s") == []
    failures = failure_rows(log, "s")
    assert len(failures) == 1
    assert failures[0].payload["stage"] == "finish_reason"
    assert failures[0].payload["covers_seqs"] == [1, 2]


def test_partial_blank_text_after_a_violation_is_never_written(log):
    """A discarded attempt's text must not leak into the log."""
    add_pair(log, "s", "the room is quiet")
    summarizer = FrameSummarizer([("DISCARDED", "length"), ("", "stop")])

    outcome = run(compact("s", log, summarizer=summarizer, budget_tokens=8192))

    assert outcome.status == "failed"
    # A single-chunk run IS the summary: an empty map output is exhausted map.
    assert outcome.stage == "map_exhausted"
    assert summary_rows(log, "s") == []
    assert failure_rows(log, "s")[0].payload["stage"] == "map_exhausted"


# -- summary v2 / absorbs / era marker ------------------------------------


def test_first_summary_omits_absorbs_entirely(log):
    add_pair(log, "s", "the room is quiet")
    outcome = run(compact("s", log, summarizer=FrameSummarizer(), budget_tokens=8192))

    assert outcome.status == "done"
    payload = summary_rows(log, "s")[0].payload
    assert "absorbs" not in payload  # never written as null
    assert payload["trigger"] == "manual"
    assert set(payload["stats"]) == {"calls", "prompt_tokens", "model"}
    assert payload["covers_seqs"] == [1, 2]
    assert payload["reason"]


def test_the_fork_marker_and_title_land_on_the_successor(log):
    log.append_event("s", "system", "system", {"event": "session_renamed", "title": "The Tavern"})
    add_pair(log, "s", "the room is quiet")
    outcome = run(
        compact("s", log, summarizer=FrameSummarizer(), budget_tokens=8192, new_session_id="era-2")
    )

    assert outcome.status == "done"
    assert outcome.session_id == "era-2"
    assert outcome.title == "The Tavern"

    summary = summary_rows(log, "s")[0]
    markers = marker_rows(log, "era-2")
    assert len(markers) == 1
    marker = markers[0].payload
    assert marker["event"] == "compaction"
    assert marker["source_session"] == "s"
    assert marker["summary_ref"] == ["s", summary.seq]
    # The rename row occupies seq 1, so the turn pair is 2-3.
    assert marker["covers_seqs"] == [2, 3]
    assert marker["trigger"] == "manual"
    assert set(marker["stats"]) == {"calls", "prompt_tokens", "model"}

    # The title carry row is the era's OWN row (a lazy child would serve "").
    titles = [
        e.payload["title"]
        for e in log.get_events("era-2")
        if e.type == "system" and e.payload.get("event") == "session_renamed"
    ]
    assert titles == ["The Tavern"]
    # The marker is NOT fork-carried config: it exists only because step 9 wrote it.
    assert len(marker_rows(log, "era-2")) == 1
    assert log.get_session("era-2")["base_seq"] == summary.seq


def test_a_second_era_absorbs_the_first_summary(log):
    add_pair(log, "s", "the room is quiet")
    first = run(
        compact("s", log, summarizer=FrameSummarizer(), budget_tokens=8192, new_session_id="era-2")
    )
    assert first.status == "done"
    first_summary = summary_rows(log, "s")[0]

    # New-era turns live on the successor; compacting it absorbs era 1.
    add_pair(log, "era-2", "a door opens")
    summarizer = FrameSummarizer()
    second = run(
        compact("era-2", log, summarizer=summarizer, budget_tokens=8192, new_session_id="era-3")
    )

    assert second.status == "done"
    new_summary = [
        e for e in log.get_events("era-2") if e.type == "summary"
    ][0]
    assert new_summary.payload["absorbs"] == ["s", first_summary.seq]
    # The recap the fold saw carried the inherited summary (absorption), and the
    # prompt-side reader drops the absorbed era.
    assert "SUMMARY TEXT" in chunks_of(summarizer.calls[-1])
    assert recap_texts(log.get_chain_events("era-3")) == [new_summary.payload["text"]]
    # ALL-rows reader (RecapPane semantics) still lists both eras.
    from fabulist.graph.compaction import summaries_payload

    assert len(summaries_payload(log.get_chain_events("era-3"))) == 2


# -- step-10 recovery ------------------------------------------------------


def test_recovery_forks_an_existing_summary_with_no_model_call(log):
    add_pair(log, "s", "the room is quiet")
    add_summary(log, "s", "OLD ERA", [1, 2])

    summarizer = FrameSummarizer()
    outcome = run(
        compact("s", log, summarizer=summarizer, budget_tokens=8192, new_session_id="era-2")
    )

    assert outcome.status == "done"
    assert outcome.recovered is True
    assert outcome.session_id == "era-2"
    assert summarizer.calls == []  # NO model call
    assert len(summary_rows(log, "s")) == 1  # nothing new written
    marker = marker_rows(log, "era-2")[0].payload
    assert marker["summary_ref"] == ["s", summary_rows(log, "s")[0].seq]
    assert marker["covers_seqs"] == [1, 2]
    assert marker["stats"]["calls"] == 0


def test_recovery_is_idempotent_and_returns_the_same_successor(log):
    add_pair(log, "s", "the room is quiet")
    add_summary(log, "s", "OLD ERA", [1, 2])

    first = run(
        compact("s", log, summarizer=FrameSummarizer(), budget_tokens=8192, new_session_id="era-2")
    )
    second = run(
        compact("s", log, summarizer=FrameSummarizer(), budget_tokens=8192, new_session_id="era-9")
    )

    assert first.session_id == second.session_id == "era-2"
    assert second.recovered is True
    assert second.details == {"idempotent": True}
    assert log.get_session("era-9") is None  # no second fork was created
    assert len(marker_rows(log, "era-2")) == 1


def test_nothing_to_compact_at_all_is_a_noop(log):
    outcome = run(compact("s", log, summarizer=FrameSummarizer(), budget_tokens=8192))

    assert outcome.status == "noop"
    assert outcome.session_id is None
    assert log.list_branches("s") == []


def test_a_successful_compaction_then_a_second_call_is_the_recovery_path(log):
    add_pair(log, "s", "the room is quiet")
    first = run(
        compact("s", log, summarizer=FrameSummarizer(), budget_tokens=8192, new_session_id="era-2")
    )
    second = run(compact("s", log, summarizer=FrameSummarizer(), budget_tokens=8192))

    assert first.status == second.status == "done"
    assert second.session_id == "era-2"
    assert second.recovered is True
    assert len(summary_rows(log, "s")) == 1  # no duplicate era


# -- configuration failures (§2.3 step 2) ---------------------------------


def test_an_undeclarable_budget_fails_with_a_readable_config_stage(log):
    add_pair(log, "s", "the room is quiet")

    outcome = run(compact("s", log, summarizer=FrameSummarizer(), budget_tokens=0))

    assert outcome.status == "failed"
    assert outcome.stage == "config"
    assert "Context Size" in outcome.error
    assert failure_rows(log, "s")[0].payload["stage"] == "config"


def test_a_missing_summarizer_fails_the_config_stage(log):
    add_pair(log, "s", "the room is quiet")

    outcome = run(compact("s", log, budget_tokens=8192))

    assert outcome.status == "failed"
    assert outcome.stage == "config"


# -- projection transport keys (§2.2) -------------------------------------


def test_projection_carries_chain_walked_context_lows_and_compactions(log):
    from fabulist.log import EventLog  # noqa: F401 - the real gate validates

    add_pair(log, "s", "the room is quiet")
    log.append_event(
        "s",
        "system",
        "monitor",
        {
            "event": "context_low",
            "agent": "narrator",
            "ratio": 0.82,
            "prompt_tokens": 21400,
            "budget_tokens": 26112,
            "basis": "measured",
        },
    )
    first = run(
        compact("s", log, summarizer=FrameSummarizer(), budget_tokens=8192, new_session_id="era-2")
    )
    assert first.status == "done"

    parent = log.get_projection("s")
    assert [item["seq"] for item in parent["context_lows"]] == [3]
    assert parent["context_lows"][0]["session_id"] == "s"
    assert parent["context_lows"][0]["payload"]["ratio"] == 0.82
    assert parent["compactions"] == []  # the marker lives on the CHILD's own rows

    child = log.get_projection("era-2")
    # Chain-walked: the inherited ancestor row rides along with ITS OWN session.
    assert [item["session_id"] for item in child["context_lows"]] == ["s"]
    assert [item["payload"]["event"] for item in child["compactions"]] == ["compaction"]
    marker = child["compactions"][-1]
    assert marker["session_id"] == "era-2"
    assert marker["payload"]["summary_ref"][0] == "s"

    # Additive keys: every pre-existing projection key is still there.
    assert set(parent) >= {
        "narratives",
        "state",
        "user_messages",
        "rolls",
        "assets",
        "notices",
        "context_lows",
        "compactions",
    }


def test_a_second_era_sees_two_markers_in_seq_order(log):
    add_pair(log, "s", "the room is quiet")
    run(compact("s", log, summarizer=FrameSummarizer(), budget_tokens=8192, new_session_id="era-2"))
    add_pair(log, "era-2", "a door opens")
    run(
        compact(
            "era-2",
            log,
            summarizer=FrameSummarizer(),
            budget_tokens=8192,
            new_session_id="era-3",
        )
    )

    compactions = log.get_projection("era-3")["compactions"]
    assert len(compactions) == 2
    assert [item["payload"]["source_session"] for item in compactions] == ["s", "era-2"]
    assert compactions == sorted(compactions, key=lambda item: item["seq"])
    # The LAST marker is the current era's (§2.2).
    assert compactions[-1]["session_id"] == "era-3"


def test_kind_derives_from_marker_presence_among_own_rows(log):
    """A session is a compaction era iff ITS OWN rows carry the marker."""
    from fabulist.graph.compaction import has_compaction_marker

    add_pair(log, "s", "the room is quiet")
    assert has_compaction_marker(log, "s") is False

    run(compact("s", log, summarizer=FrameSummarizer(), budget_tokens=8192, new_session_id="era-2"))
    assert has_compaction_marker(log, "era-2") is True
    # A plain swipe child inherits the marker through the chain but owns none.
    log.fork_session("era-2", 2, "swipe")
    assert has_compaction_marker(log, "swipe") is False


# -- the caller contract (Lane D's job / Lane B's construction) -------------


def test_the_live_job_shape_lands_chunks_and_reports_stage(log):
    """Lane D's ``CompactJob`` is ``chunks: list`` + ``add_chunk`` (§2.3 step 4)."""

    class CompactJobLike:
        def __init__(self):
            self.chunks: list[str] = []
            self.stage = None
            self.error = None

        def add_chunk(self, text):
            self.chunks.append(text)

    for index in range(8):
        add_user(log, "s", f"u{index}")
        add_narrative(log, "s", f"{index}:" + "x" * 500)

    job = CompactJobLike()
    ok = FrameSummarizer(default=("note", "stop"))
    outcome = run(compact("s", log, summarizer=ok, budget_tokens=1024, job=job))
    assert outcome.status == "done"
    mapped = len([call for call in ok.calls if not is_fold(call)])
    assert len(job.chunks) == mapped  # one landed chunk per map call

    # A failure reports its stage onto the job as well as onto the outcome.
    add_pair(log, "era-x", "a door opens")
    failing = FrameSummarizer([("t", "length"), ("t", "length")])
    job2 = CompactJobLike()
    failed = run(compact("era-x", log, summarizer=failing, budget_tokens=8192, job=job2))
    assert failed.status == "failed"
    assert (job2.stage, job2.error) == (failed.stage, failed.error)


def test_the_constructor_carries_the_resolved_seam_and_the_call_can_override_it(log):
    """B constructs the service once; a job may still override per call."""
    from fabulist.graph.compaction import CompactService

    service = CompactService(budget_tokens=8192, model="your-model-id")
    add_pair(log, "s", "the room is quiet")

    # No seam at construction ⇒ a config failure, never a crash.
    no_seam = run(service.compact("s", log))
    assert no_seam.status == "failed" and no_seam.stage == "config"

    outcome = run(service.compact("s", log, summarizer=FrameSummarizer(), trigger="forced_90"))
    assert outcome.status == "done"
    summary = summary_rows(log, "s")[0]
    assert summary.payload["trigger"] == "forced_90"
    assert summary.payload["stats"]["model"] == "your-model-id"


def test_a_fork_failure_leaves_a_summary_step_10_can_repair(log):
    """A crash between the summary write and the fork is repaired, not repeated."""
    add_pair(log, "s", "the room is quiet")
    log.append_event("s", "system", "system", {"event": "session_created", "title": "x"})
    # The id the caller chose is already taken ⇒ the fork cannot happen.
    log.fork_session("s", 1, "taken")

    failing = FrameSummarizer()
    failed = run(
        compact("s", log, summarizer=failing, budget_tokens=8192, new_session_id="taken")
    )

    assert failed.status == "failed"
    assert failed.stage == "fork"
    assert len(summary_rows(log, "s")) == 1  # the summary landed
    assert failure_rows(log, "s")[0].payload["stage"] == "fork"

    # Step 10: nothing is uncovered now, so the next call forks the summary with
    # NO model call instead of summarizing a second time.
    repair = FrameSummarizer()
    healed = run(compact("s", log, summarizer=repair, budget_tokens=8192))
    assert healed.status == "done"
    assert healed.recovered is True
    assert repair.calls == []
    assert len(summary_rows(log, "s")) == 1
    assert len(marker_rows(log, healed.session_id)) == 1


def test_compacting_a_swipe_branch_summarizes_its_inherited_chain(log):
    """W9.26 §2.3 step 3 ruling: a branch summarizes the WHOLE inherited chain.

    ``covers_seqs`` are chain seqs and the parent is untouched — forking readable
    history is reading, and compaction never writes into an ancestor.
    """
    add_pair(log, "s", "the first turn")  # rows 1 (user), 2 (narrative)
    log.fork_session("s", 2, "b")
    # issue #62: the branch's first own seq (3) is its state snapshot, so its own
    # turn lands at 4/5 instead of 3/4 -- covers_seqs stays "the whole chain".
    add_user(log, "b", "a different question")  # seq 4
    add_narrative(log, "b", "a different answer")  # seq 5

    outcome = run(
        compact("b", log, summarizer=FrameSummarizer(), budget_tokens=8192, new_session_id="b-era")
    )

    assert outcome.status == "done"
    summary = summary_rows(log, "b")[0]
    assert summary.payload["covers_seqs"] == [1, 5]  # chain seqs, user row included
    assert summary_rows(log, "s") == []  # the parent is untouched
    assert marker_rows(log, "s") == []
    assert log.get_session("b-era")["base_session"] == "b"
    # The successor inherits the whole chain plus the branch's summary.
    child = log.get_projection("b-era")
    assert [item["payload"]["source_session"] for item in child["compactions"]] == ["b"]


def test_ephemeral_image_and_import_seed_rows_never_reach_the_summarizer(log):
    """The step-3 exclusions the old hook kept are kept by the service too."""
    add_user(log, "s", "[Imported card: Tessa]")  # a fabricated player line
    add_narrative(log, "s", "first scene")
    log.append_event(
        "s",
        "narrative",
        "imagegen",
        {"prose": "", "kind": "image", "image_asset_id": "a1", "prompt": "the tavern"},
    )
    log.append_event(
        "s", "narrative", "narrator", {"prose": "ghost draft"}, ephemeral=True
    )
    add_user(log, "s", "the player's line")
    add_narrative(log, "s", "second scene")

    summarizer = FrameSummarizer()
    outcome = run(compact("s", log, summarizer=summarizer, budget_tokens=8192))

    assert outcome.status == "done"
    body = chunks_of(summarizer.calls[0])
    assert "first scene" in body and "second scene" in body
    assert "the player's line" in body
    assert "Imported card" not in body
    assert "the tavern" not in body
    assert "ghost draft" not in body


def test_every_call_rides_the_injected_messages_builder(log):
    """§2.3 step 4: map AND fold ride ``compile_preset_flow(preset, "summarize")``."""
    from fabulist.graph.compaction import CompactService

    built: list[str] = []

    def builder(transcript: str) -> list[dict]:
        built.append(transcript)
        return [
            {"role": "system", "content": "PRESET SYSTEM"},
            {"role": "user", "content": transcript},
        ]

    for index in range(6):
        add_user(log, "s", f"u{index}")
        add_narrative(log, "s", f"{index}:" + "x" * 400)

    service = CompactService(
        summarizer=FrameSummarizer(default=("note", "stop")),
        budget_tokens=1024,
        messages_builder=builder,
    )
    outcome = run(service.compact("s", log))

    assert outcome.status == "done"
    # One builder call per model call (map chunks + the reduce).
    assert len(built) == 3
    assert any("MERGE these continuity notes" in text for text in built)
    assert any("NARRATOR:" in text for text in built)
    assert summary_rows(log, "s")[0].payload["text"] == "note"
