"""The narrator's rewrite → browser reconciliation frame.

The node streams its DRAFT and then rewrites the turn in working memory (the
deterministic DSL gate, the optional model re-synthesis, and the W9.24 repair
loop, which can take minutes). Nothing used to carry the rewritten text, so the
chat pane showed the stale, partly-invalid draft until a post-done projection
read replaced it. The fix is ONE full-text ``content`` frame carrying
``meta`` ``{"replace": True}`` on the existing content channel (no new frame
type), emitted after EVERY rewrite is final — one comparison for both fix kinds.

What these tests pin:

* the replacement frame's SHAPE and text (the document-order reconstruction the
  client re-splits), and that the gate path fires it;
* the clean-turn cost contract — a clean draft puts NO replacement on the wire,
  including the case where the reconstruction differs only by the blank lines it
  uses as segment separators (whitespace-normalized comparison);
* the repair-loop path fires it too, after the loop's final segments;
* the replacement text RE-SPLITS to the segments it was built from (the client
  resets its accumulator and re-splits from scratch);
* the engine's own prose accumulator supersedes on a replacement, so a cancel in
  that window writes the right ephemeral partial.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from fabulist.graph import GraphTurnEngine, nodes as nodes_module
from fabulist.graph.nodes import (
    PATCH_PROSE_TOOL,
    _final_narrative_text,
    make_narrator_node,
    segment_narrative,
)
from fabulist.log import EventLog

STATE = {"session_id": "s1", "user_content": "go", "facts": "", "plan": ""}
FUNCTION_CALLING = {"function_calling": True}

#: A draft the deterministic gate repairs with zero model calls: kwargs become
#: positional on a known component.
KWARGS_DRAFT = (
    "The room is cold.\n"
    'card = ArtifactCard(name="Tense", kind="Study", description="08:30")\n'
    'root = ScenePane("Study", [card])\n'
)

#: A clean draft that still SEGMENTS into two pieces: the reconstruction joins
#: them with a blank line, so a byte comparison would fire a pointless
#: replacement here. A clean turn must stay frame-free.
CLEAN_DRAFT = 'Rain pooled in the gutter.\nhook = BeatCard("clean")\n'

#: Prose the validator flags (``slop`` -> ``shivers_down``), so the repair loop
#: engages with no model cost.
SLOP_DRAFT = "The hall was quiet. A shiver down her spine said otherwise."
SLOP_PATCH = {
    "find": "A shiver down her spine said otherwise.",
    "replace": "She held still and listened.",
}
PATCHED_PROSE = "The hall was quiet. She held still and listened."


def run(coro):
    return asyncio.run(coro)


class ScriptedModel:
    """A ``ModelCaller`` returning one whole content response per call."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[list[dict]] = []

    async def __call__(self, messages, stream=True, request_profile=None):
        self.calls.append(messages)
        text = self.responses.pop(0) if self.responses else ""
        yield {"type": "content", "delta": text}


class RepairCaller(ScriptedModel):
    """Scripted caller whose replies may carry a tool call.

    A reply is ``{"content": str, "tool_args": str, "tool": str}`` — ``tool_args``
    makes it a function call named by ``meta`` (the real transport stamps the
    name on every fragment).
    """

    async def __call__(self, messages, stream=True, request_profile=None):
        self.calls.append(messages)
        reply = self.responses.pop(0) if self.responses else {}
        if reply.get("content"):
            yield {"type": "content", "delta": reply["content"]}
        if reply.get("tool_args"):
            yield {
                "type": "tool_call",
                "delta": reply["tool_args"],
                "meta": {"name": reply["tool"]},
            }


class FrameRecorder:
    """Collects every custom-stream frame the node writes (the writer seam)."""

    def __init__(self) -> None:
        self.frames: list[dict] = []

    def __call__(self, frame: dict) -> None:
        self.frames.append(frame)

    def replace_frames(self) -> list[dict]:
        return [
            frame
            for frame in self.frames
            if frame.get("type") == "content" and (frame.get("meta") or {}).get("replace")
        ]


@pytest.fixture
def frames(monkeypatch):
    recorder = FrameRecorder()
    monkeypatch.setattr(nodes_module, "_stream_writer", lambda: recorder)
    return recorder


def _assert_single_replace_frame(recorder: FrameRecorder, final_text: str) -> None:
    replaces = recorder.replace_frames()
    assert len(replaces) == 1, f"expected exactly one replace frame, got {len(replaces)}"
    frame = replaces[0]
    assert frame["delta"] == final_text
    # No new frame type: it rides the content channel the draft used.
    assert frame["type"] == "content"
    # It is the LAST frame of the node's stream — nothing may follow a
    # replacement that the client would append to a reset accumulator.
    assert recorder.frames[-1] is frame


# ---------------------------------------------------------------------------
# the replacement text re-splits to the segments it was built from
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "draft",
    [
        'Rain pooled in the gutter.\nhook = BeatCard("clean")\n',
        KWARGS_DRAFT,
        "First para.\n\nsecond para of prose.\n```openui\n"
        'card = ObjectCard("A", "B")\n```\nafter the fence.\n',
        "Prose only, with no statement at all.",
    ],
)
def test_replacement_text_resegments_to_the_same_segments(draft):
    """The client resets and RE-SPLITS the replacement from scratch.

    ``_final_narrative_text`` separates segments with blank lines; the re-split
    (server: :func:`segment_narrative`, client: its mirror ``splitProseAndDsl``)
    must land on the same pieces, or the pane would redraw a repaired turn with
    the wrong prose/DSL boundaries.
    """
    segments = segment_narrative(draft)
    assert segment_narrative(_final_narrative_text(segments)) == segments


# ---------------------------------------------------------------------------
# deterministic gate path (no function calling, one completion)
# ---------------------------------------------------------------------------


def test_gate_repair_emits_one_replace_frame_with_the_final_text(frames):
    model = ScriptedModel([KWARGS_DRAFT])
    node = make_narrator_node(model=model)
    out = run(node(dict(STATE)))

    assert len(model.calls) == 1  # the gate is model-free
    final_text = "\n\n".join(segment["text"] for segment in out["segments"])
    _assert_single_replace_frame(frames, final_text)
    # The replacement carries the REPAIRED DSL, not the streamed draft.
    assert 'ArtifactCard("Tense", "Study", "08:30")' in final_text
    assert "name=" not in final_text
    # The draft still streamed: the replacement is an addition, never a reroute.
    assert any(
        frame.get("type") == "content" and not (frame.get("meta") or {}).get("replace")
        for frame in frames.frames
    )


# ---------------------------------------------------------------------------
# clean turn: no replacement (the pinned one-completion, no-extra-frame cost)
# ---------------------------------------------------------------------------


def test_clean_draft_emits_no_replace_frame(frames):
    model = ScriptedModel([CLEAN_DRAFT])
    node = make_narrator_node(model=model)
    out = run(node(dict(STATE)))

    assert len(model.calls) == 1
    # The reconstruction differs from the draft in BYTES (the blank lines it uses
    # as separators) — the case a byte comparison would have fired on.
    assert "\n\n".join(segment["text"] for segment in out["segments"]) != CLEAN_DRAFT
    assert frames.replace_frames() == []
    # The only content on the wire is the draft itself, byte-identical.
    content = [frame for frame in frames.frames if frame.get("type") == "content"]
    assert content == [{"type": "content", "delta": CLEAN_DRAFT}]


# ---------------------------------------------------------------------------
# repair-loop path (log + function calling): the replacement follows the loop
# ---------------------------------------------------------------------------


def test_repair_loop_emits_replace_frame_after_the_final_segments(tmp_path):
    log = EventLog(tmp_path / "events.db")
    try:
        model = RepairCaller(
            [
                {"content": SLOP_DRAFT},
                {
                    "tool": PATCH_PROSE_TOOL,
                    "tool_args": json.dumps({"edits": [SLOP_PATCH]}),
                },
                {"content": "Done."},
            ]
        )
        recorder = FrameRecorder()
        original = nodes_module._stream_writer
        nodes_module._stream_writer = lambda: recorder
        try:
            node = make_narrator_node(
                model=model, log=log, loop_capabilities=FUNCTION_CALLING
            )
            out = run(node(dict(STATE)))
        finally:
            nodes_module._stream_writer = original

        assert len(model.calls) == 3  # draft + ONE patch round + the model close-out
        assert out["dsl"] == ""
        assert [segment["text"] for segment in out["segments"]] == [PATCHED_PROSE]
        _assert_single_replace_frame(recorder, PATCHED_PROSE)
        # It fires AFTER the loop: the repair annotation ("(repair N)") is the
        # narrate slot's done frame, and the replacement is the last thing out.
        labels = [
            str((frame.get("meta") or {}).get("label"))
            for frame in recorder.frames
            if frame.get("type") == "stage"
        ]
        assert any("(repair" in label for label in labels)
    finally:
        log.close()


# ---------------------------------------------------------------------------
# the engine's own accumulator: a replace supersedes, never extends
# ---------------------------------------------------------------------------


class StubGraph:
    """A graph whose custom stream yields scripted frames.

    The turn engine drains that stream into the prose accumulator it uses for a
    cancelled turn's ephemeral partial — the second consumer of the content
    channel, which must reset on a replacement exactly like the browser does.
    """

    def __init__(self, frames: list[dict]) -> None:
        self._frames = frames

    def astream(self, *args, **kwargs):  # noqa: ANN002, ANN003 - graph-seam stub
        async def gen():
            for frame in self._frames:
                yield frame

        return gen()


def test_cancelled_after_a_replace_keeps_only_the_final_text(tmp_path):
    log = EventLog(tmp_path / "events.db")
    try:
        draft = [
            {"type": "content", "delta": "draft "},
            {"type": "content", "delta": "text"},
        ]
        final_text = "Rewritten prose."
        graph = StubGraph(
            [
                *draft,
                {"type": "stage", "meta": {"stage": "dsl", "state": "done"}},
                {"type": "content", "delta": final_text, "meta": {"replace": True}},
            ]
        )
        engine = GraphTurnEngine(graph=graph, log=log, model=None)

        async def scenario():
            seen = []
            async for event in engine.run_turn("s1", "I speak", "in_character"):
                seen.append(event)
                if (event.get("meta") or {}).get("replace"):
                    engine.cancel()
            return seen

        run(scenario())
        narratives = [
            event for event in log.get_events("s1") if event.type == "narrative"
        ]
        assert len(narratives) == 1
        assert narratives[0].ephemeral is True
        # Superseded, not appended: the draft does not survive the replacement.
        assert narratives[0].payload["prose"] == final_text
    finally:
        log.close()
