"""The mechanical attribution repair (issue 31) — spans with ZERO model calls.

The palette block teaches the model to wrap each spoken line; the check demands a
span around EVERY quoted line, and a miss used to cost a whole narration repair
round (live: 10+ flags per round, 40-100 s per round trip). But most misses are
not judgements: the prose itself NAMES the speaker in a speech tag beside the
quote — ``"You're late," Aria said.`` / ``Aria set the cup down and said, "You're
late."`` — so the span is deterministic text editing.

What these tests pin:

* the WRAP MATRIX — a named tail frame, a verb-first tail frame and a named
  lead-in closing on a speech verb all wrap; a PRONOUN tag, a lead-in naming two
  palette speakers, an inline citation and a quote the model coloured off-palette
  all resolve nobody (ADR-012: unattributed beats plausibly wrong);
* the EDIT — the quoted run is what gets the span (never the sentence around
  it), offsets survive multiple quotes in one segment, the caller's segments are
  not mutated, and DSL segments are never touched;
* the LOOP — a draft whose only findings are named-but-unwrapped lines lands with
  ZERO repair completions, and a draft carrying one unresolvable line hands the
  model ONLY that line's flag (the mechanical half is already done).
"""

from __future__ import annotations

import asyncio
import json

import pytest

from fabulist.api.cast_api import SPEAKER_PALETTE
from fabulist.graph.nodes import make_narrate_node
from fabulist.state.attribution import (
    PaletteEntry,
    attribution_missing_flags,
    mechanical_attribution_wraps,
    parse_attribution,
    wrap_attributed_quotes,
)

SID = "s-mechanical"
USER = "I wait under the awning."

#: What ``palette_for_turn`` mints for the two seeded cast cards, by slot.
RED = SPEAKER_PALETTE[0]  # #e06c75 — Aria
AMBER = SPEAKER_PALETTE[1]  # #e5c07b — Borin

PALETTE = [
    PaletteEntry("cm_aria", "Aria", RED, "red"),
    PaletteEntry("cm_borin", "Borin", AMBER, "amber"),
]


def _segments(*texts: str) -> list[dict]:
    return [{"kind": "prose", "text": text} for text in texts]


def _wraps(*texts: str) -> list[tuple[str, str]]:
    return [
        (wrap.entry.display_name, wrap.quote)
        for wrap in mechanical_attribution_wraps(_segments(*texts), PALETTE)
    ]


def _wrap_spans(*texts: str) -> list[str]:
    out, _wraps_written = wrap_attributed_quotes(_segments(*texts), PALETTE)
    return [segment["text"] for segment in out]


# -- what the prose names is what gets wrapped -------------------------------


def test_a_named_tail_tag_wraps_the_line():
    """``"You're late," Aria said.`` names its speaker — the span goes on."""
    prose = '"You\'re late," Aria said.'
    assert _wraps(prose) == [("Aria", "You're late,")]
    assert _wrap_spans(prose) == [
        f'<span style="color:{RED}">"You\'re late,"</span> Aria said.'
    ]
    # The wrap is what makes the row exist: the colour IS the attribution.
    assert parse_attribution(_wrap_spans(prose)[0], PALETTE) == [
        {"speaker": "Aria", "quote": "You're late,", "speaker_id": "cm_aria"}
    ]


def test_a_verb_first_tail_tag_wraps_the_line():
    """``said Aria`` is the same tag, other word order."""
    prose = '"You\'re late," said Aria.'
    assert _wraps(prose) == [("Aria", "You're late,")]


def test_a_named_lead_in_closing_on_a_speech_verb_wraps_the_line():
    """The action-beat shape: one name, one closing speech verb, one quote."""
    prose = 'Aria set the cup down and said, "You\'re late."'
    assert _wraps(prose) == [("Aria", "You're late.")]
    wrapped = _wrap_spans(prose)[0]
    assert wrapped.startswith("Aria set the cup down and said, ")
    assert f'<span style="color:{RED}">"You\'re late."</span>' in wrapped
    # The narration carrying the beat is OUTSIDE the span (a span around prose
    # with no quote is ``attribution_unquoted``).
    assert "Aria set the cup down" not in wrapped[
        wrapped.index("<span") : wrapped.index("</span>")
    ]


def test_the_line_tail_wins_over_a_lead_in_name():
    """Two speakers in one paragraph, one of them named right after the quote."""
    prose = (
        "Borin watched the door. "
        '"Step inside," Aria said. '
        '"And shut it," Borin said.'
    )
    assert _wraps(prose) == [("Aria", "Step inside,"), ("Borin", "And shut it,")]


def test_only_the_quoted_run_wears_the_span():
    """The tag and the beat around the quote are never inside the colour."""
    out = _wrap_spans('Aria set the cup down and said, "You\'re late.", then left.')
    assert out == [
        'Aria set the cup down and said, '
        f'<span style="color:{RED}">"You\'re late."</span>'
        ", then left."
    ]


# -- what the prose does NOT name stays unresolved (ADR-012) ------------------


@pytest.mark.parametrize(
    "prose",
    [
        pytest.param('Aria set the cup down. "You\'re late," she says.', id="pronoun"),
        pytest.param('"You\'re late," he said.', id="pronoun-tail"),
        pytest.param('he said, "You\'re late."', id="pronoun-lead-in"),
        pytest.param(
            "Aria looked at Borin and said, \"You're late.\"", id="two-names"
        ),
        pytest.param(
            'Aria said nothing. The letter read "Come home".', id="no-tag-adjacent"
        ),
        pytest.param('The faded neon "OPEN" sign buzzed.', id="citation"),
        pytest.param('"You\'re late." Aria turned away.', id="tag-next-sentence"),
        pytest.param('Aria: "One moment."', id="script-cue"),
    ],
)
def test_an_unattributable_line_is_left_for_the_model(prose):
    """Nothing here names a speaker ADJACENT to the quote — no wrap, no guess."""
    assert _wraps(prose) == []


def test_a_quote_coloured_off_palette_is_recoloured_by_the_model_not_nested():
    """The model DID choose a colour (outside the palette): add no second span."""
    prose = '<span style="color:crimson">"You\'re late,"</span> Aria said.'
    assert _wraps(prose) == []
    assert attribution_missing_flags(_segments(prose), PALETTE)[0]["rule_id"] == (
        "attribution_missing"
    )


def test_no_palette_wraps_nothing():
    assert mechanical_attribution_wraps(_segments('"One," Aria said.'), []) == []
    assert wrap_attributed_quotes(_segments('"One," Aria said.'), []) == (
        [{"kind": "prose", "text": '"One," Aria said.'}],
        [],
    )


# -- the edit itself ---------------------------------------------------------


def test_multiple_quotes_in_one_segment_keep_their_offsets():
    """Spans are written right to left, so no earlier offset shifts."""
    prose = '"One," Aria said. "Two," Borin said. "Three," Aria said.'
    wrapped = _wrap_spans(prose)[0]
    assert wrapped == (
        f'<span style="color:{RED}">"One,"</span> Aria said. '
        f'<span style="color:{AMBER}">"Two,"</span> Borin said. '
        f'<span style="color:{RED}">"Three,"</span> Aria said.'
    )
    assert [(row["speaker"], row["quote"]) for row in parse_attribution(wrapped, PALETTE)] == [
        ("Aria", "One,"),
        ("Borin", "Two,"),
        ("Aria", "Three,"),
    ]


def test_the_callers_segments_are_never_mutated():
    segments = _segments('"One," Aria said.')
    wrapped, wraps = wrap_attributed_quotes(segments, PALETTE)
    assert len(wraps) == 1
    assert segments[0]["text"] == '"One," Aria said.'
    assert wrapped[0]["text"] != segments[0]["text"]


def test_dsl_segments_are_never_touched():
    segments = [
        {"kind": "dsl", "text": 'pane = ScenePane(title="One," Aria said.)'},
        {"kind": "prose", "text": '"One," Aria said.'},
    ]
    wrapped, wraps = wrap_attributed_quotes(segments, PALETTE)
    assert [wrap.segment_index for wrap in wraps] == [1]
    assert wrapped[0] == segments[0]
    assert "<span" not in wrapped[0]["text"]


def test_a_clean_channel_comes_back_untouched():
    segments = _segments(f'<span style="color:{RED}">"One,"</span> Aria said.')
    wrapped, wraps = wrap_attributed_quotes(segments, PALETTE)
    assert wraps == []
    assert wrapped == segments


# -- the repair loop: ZERO model calls for the named half --------------------


class RepairCaller:
    """A streaming caller over scripted replies (the W1.1 event protocol)."""

    def __init__(self, replies: list[dict]) -> None:
        self.replies = list(replies)
        self.calls: list[dict] = []

    async def __call__(self, messages, stream=True, request_profile=None):
        self.calls.append(
            {"messages": [dict(message) for message in messages], "stream": stream}
        )
        reply = self.replies.pop(0)
        if reply.get("content"):
            yield {
                "type": "content",
                "delta": reply["content"],
                "meta": {"channel": "prose"},
            }
        if reply.get("tool_args"):
            yield {
                "type": "tool_call",
                "delta": reply["tool_args"],
                "meta": {"name": reply["tool"]},
            }


def _seed_cards(log, *names: str) -> None:
    """Cast cards are what ``palette_for_turn`` mints the hues from."""
    for name in names:
        log.append_event(
            SID, "system", "system", {"card": {"name": name, "description": name}}
        )


def _state() -> dict:
    return {
        "session_id": SID,
        "user_content": USER,
        "facts": "",
        "plan": "",
        "kind": "in_character",
    }


def _narrate(log, caller):
    return make_narrate_node(
        model=caller, log=log, loop_capabilities={"function_calling": True}
    )


def _prose(out: dict) -> str:
    return "\n".join(
        segment["text"]
        for segment in out["segments"]
        if segment["kind"] == "prose"
    )


def _repair_rows(log, event: str) -> list[dict]:
    return [
        row.payload
        for row in log.get_events(SID)
        if row.type == "system" and row.payload.get("event") == event
    ]


def _agent_loop_rows(log) -> list[dict]:
    return _repair_rows(log, "agent_loop")


#: The whole draft's findings are named-but-unwrapped lines: the engine can
#: settle every one of them, so the turn must cost ONE completion.
NAMED_DRAFT = (
    "Rain worked at the awning. Aria set the cup down and said, \"You're late.\" "
    "\"The road was long,\" Borin said."
)


def test_a_fully_named_repair_round_costs_ZERO_model_calls(tmp_path):
    """Issue 31: the mechanical half resolves the flags — no round trip at all."""
    from fabulist.log import EventLog

    log = EventLog(tmp_path / "mechanical.db")
    try:
        _seed_cards(log, "Aria", "Borin")
        caller = RepairCaller([{"content": NAMED_DRAFT}])
        out = asyncio.run(_narrate(log, caller)(_state()))

        # The DRAFT is the only completion: the repair never reached the model.
        assert len(caller.calls) == 1
        assert _agent_loop_rows(log) == []
        # The turns' spans are in the committed text, in the palette's hues.
        prose = _prose(out)
        assert f'<span style="color:{RED}">"You\'re late."</span>' in prose
        assert f'<span style="color:{AMBER}">"The road was long,"</span>' in prose
        assert [(row["speaker"], row["quote"]) for row in parse_attribution(prose, PALETTE)] == [
            ("Aria", "You're late."),
            ("Borin", "The road was long,"),
        ]
        # D9: the mechanical pass is on the record, with what it cleared.
        row = _repair_rows(log, "narrate_repair_mechanical")[0]
        assert row["applied"] == 2
        assert [flag["code"] for flag in row["flags"]] == [
            "attribution_missing",
            "attribution_missing",
        ]
        # Nothing was handed to a model, so no repair round is recorded.
        assert _repair_rows(log, "narrate_repair_start") == []
    finally:
        log.close()


def test_the_model_round_only_sees_what_the_mechanical_pass_cannot_resolve(tmp_path):
    """A pronoun tag stays a judgement call: one flag in, one patch out."""
    from fabulist.log import EventLog

    log = EventLog(tmp_path / "residual.db")
    try:
        _seed_cards(log, "Aria", "Borin")
        draft = (
            "Rain worked at the awning. Aria set the cup down and said, \"You're late.\" "
            "\"The road was long,\" she adds."
        )
        caller = RepairCaller(
            [
                {"content": draft},
                {
                    "tool": "patch_prose",
                    "tool_args": json.dumps(
                        {
                            "edits": [
                                {
                                    "find": '"The road was long," she adds.',
                                    "replace": (
                                        f'<span style="color:{RED}">'
                                        '"The road was long,"</span> she adds.'
                                    ),
                                }
                            ]
                        }
                    ),
                },
                {"content": "Done."},
            ]
        )
        out = asyncio.run(_narrate(log, caller)(_state()))

        # ONE repair round, and the flags pair it reads carries ONLY the pronoun
        # line: the named line was already wrapped by the engine.
        assert len(caller.calls) == 3
        seeded = caller.calls[1]["messages"][-1]["content"]
        assert "she adds." in seeded
        assert "Aria set the cup down" not in seeded
        # Both lines land attributed, one from each half of the repair.
        assert [(row["speaker"], row["quote"]) for row in parse_attribution(_prose(out), PALETTE)] == [
            ("Aria", "You're late."),
            ("Aria", "The road was long,"),
        ]
        assert _repair_rows(log, "narrate_repair_mechanical")[0]["applied"] == 1
    finally:
        log.close()
