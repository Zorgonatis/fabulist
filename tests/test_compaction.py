"""Tests for the W2.9 history keeper / compaction (PLAN W2.9).

The suite has no pytest-asyncio dependency, so async scenarios run via
``asyncio.run`` inside sync tests (same convention as ``test_graph.py``). Each
test uses the real :class:`~fabulist.log.EventLog` on a tmp SQLite file (the
``log`` fixture) plus a fake summarizer seam.
"""

from __future__ import annotations

import asyncio

from fabulist.graph.compaction import (
    RESERVE_TOKENS,
    SUMMARIZER_SYSTEM_PROMPT,
    UNCOVERED_SHARE,
    Compactor,
    build_summarizer_messages,
    derive_threshold_chars,
    recap_texts,
    summaries_payload,
)
from fabulist.prompts.compiler import CHARS_PER_TOKEN


def run(coro):
    return asyncio.run(coro)


# -- helpers / fakes ---------------------------------------------------------


def add_narrative(log, session, prose, *, ephemeral=False, segments=None):
    """Append one engine-shaped narrative event (prose + segments)."""
    if segments is None:
        segments = [{"kind": "prose", "text": prose}] if prose else []
    return log.append_event(
        session,
        "narrative",
        "narrator",
        {"prose": prose, "dsl": "", "segments": segments},
        ephemeral=ephemeral,
    )


def add_user(log, session, text):
    return log.append_event(session, "user", "user", {"text": text})


def add_summary(log, session, text, covers, *, ephemeral=False, absorbs=None):
    """Append one summary row (v2 ``absorbs`` pair optional, as the producer does)."""
    payload = {"text": text, "covers_seqs": list(covers), "reason": "seeded"}
    if absorbs is not None:
        payload["absorbs"] = list(absorbs)
    return log.append_event(
        session, "summary", "summarizer", payload, ephemeral=ephemeral
    )


class FakeSummarizer:
    """An async summarizer seam returning a fixed string; records its messages."""

    def __init__(self, text="SUMMARY TEXT"):
        self.text = text
        self.calls: list[list[dict]] = []

    async def __call__(self, messages):
        self.calls.append(messages)
        return self.text


class FakeModelSummarizer:
    """A ``ModelCaller``-shaped summarizer: streams reasoning then content."""

    def __init__(self, chunks=("SUM", "MARY")):
        self.chunks = list(chunks)
        self.calls: list[list[dict]] = []

    async def __call__(self, messages, stream=True):
        self.calls.append(messages)
        yield {"type": "reasoning", "delta": "this must be ignored"}
        for chunk in self.chunks:
            yield {"type": "content", "delta": chunk}


def _summary_events(log, session):
    return [e for e in log.get_events(session) if e.type == "summary"]


def _failure_events(log, session):
    return [
        e
        for e in log.get_events(session)
        if e.type == "system" and e.payload.get("event") == "compaction_failed"
    ]


# -- no-op paths -------------------------------------------------------------


def test_no_narratives_is_noop(log):
    summarizer = FakeSummarizer()
    result = run(Compactor().maybe_compact("s", log, summarizer))

    assert result is None
    assert summarizer.calls == []
    assert _summary_events(log, "s") == []


def test_below_threshold_does_not_compact(log):
    for i in range(5):
        add_narrative(log, "s", f"turn {i}")

    summarizer = FakeSummarizer()
    result = run(Compactor().maybe_compact("s", log, summarizer, threshold_turns=10))

    assert result is None
    assert summarizer.calls == []
    assert _summary_events(log, "s") == []


def test_at_threshold_does_not_compact(log):
    for i in range(3):
        add_narrative(log, "s", f"turn {i}")

    summarizer = FakeSummarizer()
    result = run(Compactor().maybe_compact("s", log, summarizer, threshold_turns=3))

    assert result is None
    assert summarizer.calls == []


def test_threshold_turns_zero_compacts_everything(log):
    for i in range(3):
        add_narrative(log, "s", f"turn {i}")

    result = run(
        Compactor().maybe_compact("s", log, FakeSummarizer("ALL"), threshold_turns=0)
    )

    assert result is not None
    assert result.payload["covers_seqs"] == [1, 3]


# -- turn-threshold compaction ----------------------------------------------


def test_compacts_oldest_when_turns_exceed_threshold(log):
    for i in range(10):
        add_narrative(log, "s", f"turn {i}")

    summarizer = FakeSummarizer("COMPACTED")
    result = run(Compactor().maybe_compact("s", log, summarizer, threshold_turns=3))

    assert result is not None
    assert result.type == "summary"
    assert result.actor == "summarizer"
    assert result.session_id == "s"
    assert result.payload["text"] == "COMPACTED"
    assert result.payload["covers_seqs"] == [1, 7]
    assert "turns 10>3" in result.payload["reason"]

    # Exactly one summary was appended and it round-trips through the log.
    assert len(_summary_events(log, "s")) == 1
    fetched = log.get_event(result.id)
    assert fetched is not None
    assert fetched.payload == result.payload

    # The summarizer saw only the oldest portion, not the kept tail.
    transcript = summarizer.calls[0][1]["content"]
    assert "turn 0" in transcript
    assert "turn 6" in transcript
    assert "turn 7" not in transcript
    assert "turn 9" not in transcript

    assert summaries_payload(log.get_events("s")) == ["COMPACTED"]


def test_idempotent_second_call(log):
    for i in range(10):
        add_narrative(log, "s", f"turn {i}")

    compactor = Compactor()
    summarizer = FakeSummarizer("COMPACTED")

    first = run(compactor.maybe_compact("s", log, summarizer, threshold_turns=3))
    second = run(compactor.maybe_compact("s", log, summarizer, threshold_turns=3))

    assert first is not None
    assert second is None
    assert len(_summary_events(log, "s")) == 1
    assert len(summarizer.calls) == 1  # the model was not called twice


def test_existing_cover_range_is_not_double_covered(log):
    for i in range(6):
        add_narrative(log, "s", f"turn {i}")
    add_summary(log, "s", "OLD", [1, 3])

    result = run(
        Compactor().maybe_compact("s", log, FakeSummarizer("NEW"), threshold_turns=1)
    )

    # Uncovered = seqs 4..6; keep newest 1 (seq 6); oldest = seqs 4..5.
    assert result is not None
    assert result.payload["covers_seqs"] == [4, 5]
    assert summaries_payload(log.get_events("s")) == ["OLD", "NEW"]


def test_ephemeral_summary_does_not_block_recompaction(log):
    for i in range(4):
        add_narrative(log, "s", f"turn {i}")
    add_summary(log, "s", "GHOST", [1, 2], ephemeral=True)

    result = run(
        Compactor().maybe_compact("s", log, FakeSummarizer("NEW"), threshold_turns=1)
    )

    assert result is not None
    assert result.payload["covers_seqs"] == [1, 3]
    assert summaries_payload(log.get_events("s")) == ["NEW"]


def test_default_threshold_is_twenty(log):
    for i in range(20):
        add_narrative(log, "s", f"turn {i}")

    summarizer = FakeSummarizer("FIRST20")
    assert run(Compactor().maybe_compact("s", log, summarizer)) is None
    assert summarizer.calls == []

    add_narrative(log, "s", "turn 20")
    result = run(Compactor().maybe_compact("s", log, summarizer))

    assert result is not None
    assert result.payload["covers_seqs"] == [1, 1]  # only the overflow turn


# -- char-threshold compaction ----------------------------------------------


def test_threshold_chars_triggers_and_partitions(log):
    for _ in range(5):
        add_narrative(log, "s", "x" * 20)  # 5 * 20 = 100 chars

    result = run(
        Compactor().maybe_compact(
            "s", log, FakeSummarizer("BY CHARS"), threshold_turns=100, threshold_chars=50
        )
    )

    assert result is not None
    # Tail keeps the newest turns under 50 chars (2 * 20); oldest = seqs 1..3.
    assert result.payload["covers_seqs"] == [1, 3]
    assert "prose_chars 100>50" in result.payload["reason"]


def test_threshold_chars_not_exceeded_is_noop(log):
    for _ in range(3):
        add_narrative(log, "s", "x" * 10)  # 30 chars total

    summarizer = FakeSummarizer()
    result = run(
        Compactor().maybe_compact(
            "s", log, summarizer, threshold_turns=100, threshold_chars=50
        )
    )

    assert result is None
    assert summarizer.calls == []


# -- ephemeral / non-narrative exclusion ------------------------------------


def test_ephemeral_narratives_are_excluded(log):
    for i in range(4):
        add_narrative(log, "s", f"turn {i}")
    add_narrative(log, "s", "EPHEMERAL DRAFT", ephemeral=True)
    add_narrative(log, "s", "EPHEMERAL DRAFT 2", ephemeral=True)

    # 4 kept narratives at threshold 4 -> no compaction (ephemeral not counted).
    summarizer = FakeSummarizer()
    assert run(Compactor().maybe_compact("s", log, summarizer, threshold_turns=4)) is None
    assert summarizer.calls == []

    # At threshold 1 the oldest kept turns compact; ephemeral rows are ignored.
    result = run(Compactor().maybe_compact("s", log, summarizer, threshold_turns=1))
    assert result is not None
    assert result.payload["covers_seqs"] == [1, 3]
    transcript = summarizer.calls[0][1]["content"]
    assert "EPHEMERAL" not in transcript


def test_image_narratives_do_not_count_as_turns(log):
    """A generated-image narrative row is a canon image (no prose), not a turn:
    it must not increment the turn count or reach the summarizer."""
    for i in range(4):
        add_narrative(log, "s", f"turn {i}")
    log.append_event(
        "s",
        "narrative",
        "imagegen",
        {
            "prose": "",
            "kind": "image",
            "image_asset_id": "asset-img-1",
            "prompt": "the tavern",
        },
    )

    summarizer = FakeSummarizer()
    # 4 real turns at threshold 4 -> no compaction; the image row is not a turn.
    # Counting it (5 > 4) would wrongly compact here.
    assert run(Compactor().maybe_compact("s", log, summarizer, threshold_turns=4)) is None
    assert summarizer.calls == []

    # At threshold 1 the oldest real turns compact; the image row is ignored.
    result = run(Compactor().maybe_compact("s", log, summarizer, threshold_turns=1))
    assert result is not None
    assert result.payload["covers_seqs"] == [1, 3]
    transcript = summarizer.calls[0][1]["content"]
    assert "the tavern" not in transcript


def test_user_events_do_not_count_but_context_the_transcript(log):
    for i in range(4):
        add_user(log, "s", f"u{i}")
        add_narrative(log, "s", f"turn {i}")

    summarizer = FakeSummarizer("WITH USERS")
    result = run(Compactor().maybe_compact("s", log, summarizer, threshold_turns=1))

    assert result is not None
    # Narrative seqs are 2,4,6,8; keep the newest (seq 8); oldest = seqs 2..6.
    assert result.payload["covers_seqs"] == [2, 6]
    transcript = summarizer.calls[0][1]["content"]
    assert "u1" in transcript  # user message inside the covered span
    assert "u2" in transcript
    assert "u0" not in transcript  # precedes the first covered narrative
    assert "u3" not in transcript  # follows the kept narrative


def test_segments_only_narrative_counts_prose(log):
    for _ in range(4):
        log.append_event(
            "s",
            "narrative",
            "narrator",
            {
                "segments": [
                    {"kind": "prose", "text": "alpha"},
                    {"kind": "dsl", "text": "root = ScenePane()"},
                    {"kind": "prose", "text": "beta"},
                ]
            },
        )

    summarizer = FakeSummarizer("SEGMENTS")
    result = run(Compactor().maybe_compact("s", log, summarizer, threshold_turns=1))

    assert result is not None
    assert result.payload["covers_seqs"] == [1, 3]
    transcript = summarizer.calls[0][1]["content"]
    assert "alpha" in transcript and "beta" in transcript
    assert "ScenePane" not in transcript  # DSL never enters the summary prose


# -- summarizer seam ---------------------------------------------------------


def test_model_caller_summarizer_drains_content(log):
    for i in range(3):
        add_narrative(log, "s", f"turn {i}")

    summarizer = FakeModelSummarizer(("SUM", "MARY"))
    result = run(Compactor().maybe_compact("s", log, summarizer, threshold_turns=0))

    assert result is not None
    assert result.payload["text"] == "SUMMARY"  # reasoning ignored, content joined
    assert len(summarizer.calls) == 1


def test_empty_summary_is_not_logged(log):
    for i in range(3):
        add_narrative(log, "s", f"turn {i}")

    result = run(
        Compactor().maybe_compact("s", log, FakeSummarizer("   "), threshold_turns=0)
    )

    assert result is None
    assert _summary_events(log, "s") == []


def test_empty_summary_appends_compaction_failed_and_retries(log):
    """S4c: an empty summary is a failure to compact, not a silent no-op."""
    for i in range(3):
        add_narrative(log, "s", f"turn {i}")

    compactor = Compactor()
    summarizer = FakeSummarizer("   ")  # whitespace only -> no summary

    first = run(compactor.maybe_compact("s", log, summarizer, threshold_turns=0))
    assert first is None
    failures = _failure_events(log, "s")
    assert len(failures) == 1
    payload = failures[0].payload
    assert payload["stage"] == "summarizer_empty"
    assert payload["covers_seqs"] == [1, 3]
    assert payload["error"]

    # The threshold is still exceeded: nothing was marked covered, so the next
    # turn retries and records another attempt.
    second = run(compactor.maybe_compact("s", log, summarizer, threshold_turns=0))
    assert second is None
    assert len(summarizer.calls) == 2
    assert len(_failure_events(log, "s")) == 2
    assert _summary_events(log, "s") == []


def test_summarizer_prompt_is_bts_shaped():
    messages = build_summarizer_messages("TRANSCRIPT BODY")

    assert messages[0] == {"role": "system", "content": SUMMARIZER_SYSTEM_PROMPT}
    prompt = messages[0]["content"].lower()
    for keyword in ("relationships", "unresolved", "time and place", "promises", "discard"):
        assert keyword in prompt
    assert messages[1]["role"] == "user"
    assert "TRANSCRIPT BODY" in messages[1]["content"]


# -- summaries_payload -------------------------------------------------------


def test_summaries_payload_orders_and_filters(log):
    add_summary(log, "s", "A", [1, 2])
    add_summary(log, "s", "B", [3, 4], ephemeral=True)
    add_summary(log, "s", "   ", [5, 6])
    add_summary(log, "s", "C", [7, 8])

    assert summaries_payload(log.get_events("s")) == ["A", "C"]


def test_summaries_payload_accepts_dicts():
    events = [
        {"type": "summary", "ephemeral": False, "payload": {"text": "D"}},
        {"type": "narrative", "ephemeral": False, "payload": {"text": "ignored"}},
        {"type": "summary", "ephemeral": True, "payload": {"text": "ghost"}},
        {"type": "summary", "ephemeral": False, "payload": {}},
    ]
    assert summaries_payload(events) == ["D"]


# -- legacy card-import seed is not a player turn -----------------------------


def test_format_transcript_refuses_a_legacy_import_seed(log):
    """A seed row must not be summarized as something the player said.

    A card import used to log a synthetic ``user`` event ("[Imported card: X]");
    the row stays in the log (D9) but it was never the player speaking, so the
    summarizer's transcript must not carry a fabricated ``USER:`` line — the
    same refusal ``EventLog``'s projection makes for the live transcript.
    """
    from fabulist.graph.compaction import _format_transcript

    session = "s-seed"
    log.append_event(session, "system", "system", {"event": "session_created"})
    add_user(log, session, "[Imported card: Tessa]")
    add_user(log, session, "Are you sure?")
    add_narrative(log, session, "The kitchen is too bright.")

    events = log.get_events(session)
    transcript = _format_transcript(events, 0, 10_000)

    assert "Are you sure?" in transcript
    assert "Imported card" not in transcript
    assert "USER: [Imported card" not in transcript


def test_run_summarizer_labels_the_capture_summarizer():
    """W9.6: the compaction hook runs outside the pump — its capture must not
    inherit the narrator's label (the flow inspector filters on 'summarizer')."""
    import asyncio

    from fabulist.agents.debug import CURRENT_INTERACTION
    from fabulist.graph.compaction import _run_summarizer

    seen: dict[str, str] = {}

    async def summarizer(messages):
        # The capture happens during the call — assert the label there.
        seen["label"] = CURRENT_INTERACTION.get()
        return "summary text"

    token = CURRENT_INTERACTION.set("narrate")
    try:
        asyncio.run(_run_summarizer(summarizer, [{"role": "user", "content": "x"}]))
    finally:
        CURRENT_INTERACTION.reset(token)
    assert seen["label"] == "summarizer"


# -- the derived character threshold (W9.25 §3.5.2) --------------------------


def test_derive_threshold_chars_formula_is_pinned():
    """(window − response − RESERVE) × chars/token × UNCOVERED_SHARE."""
    window, response = 4096, 1024
    available = window - response - RESERVE_TOKENS
    assert derive_threshold_chars(window, response) == int(
        available * CHARS_PER_TOKEN * UNCOVERED_SHARE
    )
    # The concrete number the plan's example implies: 2560 tokens → 6144 chars.
    assert derive_threshold_chars(4096, 1024) == 6144


def test_derive_threshold_chars_leaves_room_for_the_unshrunkable_parts():
    window, response = 8192, 2048
    full_chars = (window - response - RESERVE_TOKENS) * CHARS_PER_TOKEN
    derived = derive_threshold_chars(window, response)
    assert 0 < derived < full_chars


def test_derive_threshold_chars_floors_at_zero():
    """A declaration too small to hold the response is 0, never negative."""
    assert derive_threshold_chars(1024, 4096) == 0
    assert derive_threshold_chars(0, 0) == 0


def test_derived_threshold_compacts_before_the_turn_default(log):
    """A declared window makes CHAR pressure the trigger, long before 20 turns.

    This is the compact_hook's behavior (W9.25 §3.5.2): the derived character
    threshold rides alongside today's ``threshold_turns=20``, so a session that is
    long in prose but short in turns compacts when the window says it must.
    """
    for _ in range(15):
        add_narrative(log, "s", "x" * 200)  # 3000 uncovered prose chars

    threshold_chars = derive_threshold_chars(2048, 1024)  # 1228 chars
    derived = run(
        Compactor().maybe_compact(
            "s",
            log,
            FakeSummarizer("EARLY"),
            threshold_turns=20,
            threshold_chars=threshold_chars,
        )
    )

    assert derived is not None
    assert "prose_chars 3000>1228" in derived.payload["reason"]
    # The tail protection is the same character budget: newest 6 turns stay raw.
    assert derived.payload["covers_seqs"] == [1, 9]


# -- recap_texts: the PROMPT-side recap reader (W9.26 §2.4) ------------------


def test_recap_texts_drops_absorbed_summaries(log):
    """An absorbing summary replaces its predecessor in the prompt recap."""
    add_summary(log, "s", "ERA ONE", [1, 10])
    add_summary(log, "s", "ERA TWO", [1, 20], absorbs=["s", 1])

    events = log.get_events("s")
    assert recap_texts(events) == ["ERA TWO"]
    # The ALL-rows reader (RecapPane semantics) still lists both eras.
    assert summaries_payload(events) == ["ERA ONE", "ERA TWO"]


def test_recap_texts_never_drops_a_legacy_row_without_absorbs(log):
    """Absence of ``absorbs`` is not evidence of absorption (legacy rows)."""
    add_summary(log, "s", "A", [1, 2])
    add_summary(log, "s", "B", [3, 4])

    assert recap_texts(log.get_events("s")) == ["A", "B"]


def test_recap_texts_names_the_absorbed_row_by_session_and_seq():
    """The pair is ``[session_id, seq]``: a seq alone is ambiguous across forks."""
    events = [
        {"type": "summary", "session_id": "parent", "seq": 7, "ephemeral": False,
         "payload": {"text": "PARENT", "covers_seqs": [1, 2]}},
        {"type": "summary", "session_id": "other", "seq": 7, "ephemeral": False,
         "payload": {"text": "OTHER", "covers_seqs": [1, 2]}},
        {"type": "summary", "session_id": "era-2", "seq": 9, "ephemeral": False,
         "payload": {"text": "CHILD", "covers_seqs": [1, 8], "absorbs": ["parent", 7]}},
    ]

    assert recap_texts(events) == ["OTHER", "CHILD"]


def test_recap_texts_ignores_blank_ghost_and_malformed_rows():
    events = [
        {"type": "summary", "session_id": "s", "seq": 1, "ephemeral": False,
         "payload": {"text": "KEPT"}},
        {"type": "summary", "session_id": "s", "seq": 2, "ephemeral": True,
         "payload": {"text": "GHOST"}},
        {"type": "summary", "session_id": "s", "seq": 3, "ephemeral": False,
         "payload": {"text": "   "}},
        {"type": "summary", "session_id": "s", "seq": 4, "ephemeral": False,
         "payload": {"absorbs": "not-a-pair"}},
        {"type": "narrative", "session_id": "s", "seq": 5, "ephemeral": False,
         "payload": {"prose": "ignored"}},
    ]

    assert recap_texts(events) == ["KEPT"]
