"""Issue #45 — the engine wrapper tag leaked into committed prose.

The defect (deploy experiment run 2, glm-5.3): a turn AFTER the first committed
prose beginning with the engine's own bound — ``'<preset_def>\\nYou turn to ask
— …'`` — 2/3 turns, user-visible garbage on the transcript. The narrator's reply
is parsed whole (#44), so an echoed wrapper tag is ordinary prose to the
segmenter and the commit kept it.

The fix pinned here is a DEFENSIVE commit-seam guard, shape-independent:

* only committed prose that BEGINS with a tag from the engine's closed family is
  touched (the two trust bounds ``<preset_def>`` / ``<preset_runtime>`` from
  ``prompts.wrapper`` plus the envelope's closed tag family from
  ``prompts.envelope`` — the guard reads those constants, it never owns a list);
* the leading open tag is stripped with the whitespace after it, and its
  MATCHING close tag too only when the model wrapped the WHOLE turn;
* the same strip is mirrored onto the committed ``segments`` (the payload
  carries the turn twice);
* ONE warning names the tag with the turn's ``session``/``seq`` — the leak stays
  observable;
* clean prose commits BYTE-IDENTICAL, and a tag merely MENTIONED mid-text is
  never touched (this is a head guard, not a scrubber).

Owner ruling (2026-10-24): verification pivots to DeepSeek — the leak is
reproduced by injecting a synthetic ``<preset_def>``-prefixed completion through
the (fake) streaming transport, which is what these tests do. The GLM live
confirmation is deferred and non-blocking.

The envelope ASSEMBLY (#44) is untouched: the guard reads only the incoming
model text.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from fabulist.graph import GraphTurnEngine, build_graph
from fabulist.graph.nodes import _engine_wrapper_tags, _strip_engine_wrapper_leak
from fabulist.log import EventLog
from fabulist.prompts import envelope, wrapper

SESSION_ID = "s-tag-leak"
USER = "I wait under the awning."

#: The observed leak shape: the engine's own bound echoed at the head of the
#: reply, then the prose the player should have seen on its own.
PREFIX_LEAK = "<preset_def>\nYou turn to ask — the rain answers first."
PREFIX_CLEAN = "You turn to ask — the rain answers first."

#: The whole-turn wrap: the model closes the bound it invented.
WRAPPED_LEAK = "<preset_def>The lantern guttered out.</preset_def>"
WRAPPED_CLEAN = "The lantern guttered out."

#: A turn that never leaked — the guard must leave every byte alone.
CLEAN_DRAFT = "Rain pooled in the gutter and the lantern guttered out."

#: A tag merely MENTIONED inside the prose: scene text, never a wrapper.
MENTION_DRAFT = 'She traced the letters "<preset_def>" on the fogged glass.'


# -- fakes -------------------------------------------------------------------


class _Transport:
    """A streaming transport replaying ONE synthetic completion.

    The W1.1 event shape a DeepSeek-flavoured connection produces: a content
    delta carrying the whole reply with the ``prose`` channel meta.
    """

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.calls: list[dict] = []

    async def __call__(self, messages, stream=True, request_profile=None):
        self.calls.append({
            "messages": [dict(message) for message in messages],
            "stream": stream,
            "request_profile": request_profile,
        })
        yield {"type": "content", "delta": self.reply, "meta": {"channel": "prose"}}


def _extractor(user_msg: str, prose: str) -> dict:
    return {"deltas": {}, "action_class": "dialogue"}


def _run_turn(log: EventLog, reply: str) -> tuple[_Transport, list[dict], object]:
    """Drive ONE real graph turn over the synthetic completion.

    Returns the transport, the stream events and the ONE committed narrative
    event — the seam under test is the graph's own narrate → commit path, not a
    hand-built state.
    """
    model = _Transport(reply)
    graph = build_graph(model=model, extractor=_extractor, log=log)
    engine = GraphTurnEngine(graph=graph, log=log, model=model)

    async def drain() -> list[dict]:
        return [
            event
            async for event in engine.run_turn(SESSION_ID, USER, "in_character")
        ]

    events = asyncio.run(drain())
    narratives = [
        event for event in log.get_events(SESSION_ID) if event.type == "narrative"
    ]
    assert len(narratives) == 1, "the turn must commit exactly one narrative row"
    return model, events, narratives[0]


def _leak_warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [
        record
        for record in caplog.records
        if "leaked engine wrapper" in record.getMessage()
    ]


def _prose_segments(payload: dict) -> list[str]:
    return [
        segment["text"]
        for segment in payload.get("segments") or []
        if segment.get("kind") == "prose"
    ]


# ---------------------------------------------------------------------------
# the commit seam, end to end through the graph
# ---------------------------------------------------------------------------


def test_preset_def_prefix_is_stripped_at_the_commit_seam(tmp_path, caplog):
    """The observed leak: a heading tag + newline, then the real prose."""
    log = EventLog(tmp_path / "prefix.db")
    try:
        with caplog.at_level(logging.WARNING, logger="fabulist.graph.nodes"):
            _model, _events, narrative = _run_turn(log, PREFIX_LEAK)
        rows = log.get_events(SESSION_ID)
    finally:
        log.close()

    payload = narrative.payload
    assert payload["prose"] == PREFIX_CLEAN
    # the payload carries the turn twice; both copies must agree
    assert _prose_segments(payload) == [PREFIX_CLEAN]
    assert "<preset_def>" not in str(payload["segments"])
    # the post-commit validate pass reads the STATE channel: the strip must not
    # leave it flagging markup the transcript no longer carries. (One system
    # row may still record the narrate loop's pre-commit draft flags; what must
    # not survive is a committed-turn prose_markup finding.)
    committed_flags = [
        flag
        for row in rows
        if row.type == "system" and row.payload.get("event") == "validator_flags"
        for flag in row.payload.get("flags") or []
    ]
    assert [flag for flag in committed_flags if flag.get("rule_id") == "prose_markup"] == []

    warnings = _leak_warnings(caplog)
    assert len(warnings) == 1, "ONE warning per leaked turn, never one per copy"
    line = warnings[0].getMessage()
    assert "<preset_def>" in line
    assert SESSION_ID in line
    assert f"seq={narrative.seq}" in line


def test_fully_wrapped_turn_loses_both_tags(tmp_path):
    """The model invented the whole bound: open AND matching close are stripped."""
    log = EventLog(tmp_path / "wrapped.db")
    try:
        _model, _events, narrative = _run_turn(log, WRAPPED_LEAK)
    finally:
        log.close()

    assert narrative.payload["prose"] == WRAPPED_CLEAN
    assert _prose_segments(narrative.payload) == [WRAPPED_CLEAN]


def test_preset_runtime_prefix_is_stripped_too(tmp_path, caplog):
    """The sibling bound leaks the same way — one guard, both bounds."""
    log = EventLog(tmp_path / "runtime.db")
    try:
        with caplog.at_level(logging.WARNING, logger="fabulist.graph.nodes"):
            _model, _events, narrative = _run_turn(
                log, "<preset_runtime>\nThe hall breathed around her."
            )
    finally:
        log.close()

    assert narrative.payload["prose"] == "The hall breathed around her."
    warnings = _leak_warnings(caplog)
    assert len(warnings) == 1
    assert "<preset_runtime>" in warnings[0].getMessage()


def test_clean_prose_commits_byte_identical_and_never_warns(tmp_path, caplog):
    """No leak: the committed text is the model's text, byte for byte."""
    log = EventLog(tmp_path / "clean.db")
    try:
        with caplog.at_level(logging.WARNING, logger="fabulist.graph.nodes"):
            _model, _events, narrative = _run_turn(log, CLEAN_DRAFT)
    finally:
        log.close()

    assert narrative.payload["prose"] == CLEAN_DRAFT
    assert _prose_segments(narrative.payload) == [CLEAN_DRAFT]
    assert _leak_warnings(caplog) == []


def test_a_tag_merely_mentioned_mid_text_is_untouched(tmp_path, caplog):
    """A head guard is not a scrubber: scene text naming a tag stays verbatim."""
    log = EventLog(tmp_path / "mention.db")
    try:
        with caplog.at_level(logging.WARNING, logger="fabulist.graph.nodes"):
            _model, _events, narrative = _run_turn(log, MENTION_DRAFT)
    finally:
        log.close()

    assert narrative.payload["prose"] == MENTION_DRAFT
    assert _prose_segments(narrative.payload) == [MENTION_DRAFT]
    assert _leak_warnings(caplog) == []


# ---------------------------------------------------------------------------
# the rule itself: the closed family, read from the owning modules
# ---------------------------------------------------------------------------


def test_the_closed_family_is_read_from_the_owning_modules():
    """No second tag list: every envelope tag pair and both bounds are covered."""
    tags = _engine_wrapper_tags()
    assert tags[wrapper.DEF_OPEN] == wrapper.DEF_CLOSE
    assert tags[wrapper.RUNTIME_OPEN] == wrapper.RUNTIME_CLOSE
    for name in dir(envelope):
        if not name.endswith("_OPEN"):
            continue
        close = getattr(envelope, name[: -len("_OPEN")] + "_CLOSE", None)
        if close is not None:
            assert tags[getattr(envelope, name)] == close, name


@pytest.mark.parametrize("open_tag", sorted(_engine_wrapper_tags()))
def test_every_closed_family_tag_is_guarded_at_the_head(open_tag):
    close_tag = _engine_wrapper_tags()[open_tag]
    body = "The corridor was empty."

    assert _strip_engine_wrapper_leak(f"{open_tag}\n{body}") == (body, open_tag)
    assert _strip_engine_wrapper_leak(f"{open_tag}{body}{close_tag}") == (
        body,
        open_tag,
    )


def test_clean_prose_passes_through_unchanged_with_no_tag():
    text = "  The rain kept its own counsel.\n"
    stripped, leaked = _strip_engine_wrapper_leak(text)
    assert leaked is None
    assert stripped is text, "a clean turn is returned as the same object, not rebuilt"


def test_an_unclosed_leading_tag_strips_only_the_open_tag():
    """The close tag is the MATCHING one, and only when it closes the turn."""
    stripped, leaked = _strip_engine_wrapper_leak(
        "<preset_def>The rain came down hard."
    )
    assert leaked == wrapper.DEF_OPEN
    assert stripped == "The rain came down hard."


def test_a_close_tag_that_does_not_end_the_turn_is_left_alone():
    stripped, leaked = _strip_engine_wrapper_leak(
        "<preset_def>One beat.</preset_def> Then the door opened."
    )
    assert leaked == wrapper.DEF_OPEN
    # the OPEN tag is the leak; the mid-text close tag is prose, not a wrapper
    assert stripped == "One beat.</preset_def> Then the door opened."


def test_a_close_tag_alone_is_not_a_wrapper_head():
    text = "</preset_def>\nThe hallway lights failed."
    stripped, leaked = _strip_engine_wrapper_leak(text)
    assert leaked is None
    assert stripped == text


def test_the_tag_match_is_case_insensitive():
    """A leak does not reliably reproduce the engine's casing."""
    stripped, leaked = _strip_engine_wrapper_leak(
        "<PRESET_DEF>\nYou turn to ask.</PRESET_DEF>"
    )
    assert leaked == wrapper.DEF_OPEN
    assert stripped == "You turn to ask."
