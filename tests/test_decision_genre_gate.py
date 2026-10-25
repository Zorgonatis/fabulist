"""d1-3b decision model at the post-turn call site (graph/nodes.py).

History: a critic SKIP gate was built first and removed the same day. The live
spike proved d1-3B cannot separate clean prose from subtle defects (P(ok)=0.77
on a turn with a POV slip vs 0.73-0.76 on clean turns), so no cutoff is safe
and the critic ALWAYS runs. The decision caller's sound, evidence-backed use is
the genre fact: rubric v2 read 'fantasy' 0.72 on a secondary-world scene,
repeatable, and the state bar + VTK palette already consume a ``genre`` fact.

These tests pin: the critic still runs in every case, and the genre fact is
written only from a confident non-other answer, never clobbering the extractor.
"""

from __future__ import annotations

import asyncio
import json

from fabulist.graph.nodes import make_post_turn_node


def run(coro):
    return asyncio.run(coro)


PROSE = "The tide-readers came up the shale path at dusk, cowled against the salt."


class FakeDecision:
    """Records the state it sees; returns a genre or None (or raises)."""

    def __init__(self, genre=None, *, raises=None):
        self.genre = genre
        self.raises = raises
        self.calls: list[str] = []

    async def classify_genre(self, state):
        self.calls.append(state)
        if self.raises is not None:
            raise self.raises
        return self.genre


class FakeCritic:
    """Critic stand-in; counts invocations and returns a canned reply."""

    def __init__(self):
        self.calls = 0

    async def __call__(self, evidence):
        self.calls += 1
        return json.dumps({"verdict": "ok", "reason": "fine", "edits": []})


def _state(**overrides):
    state = {
        "session_id": "s1",
        "user_content": "I light the lamp early.",
        "narrative_parts": [PROSE],
        "dsl": "",
        "segments": [{"kind": "prose", "text": PROSE}],
        "facts": "time: 09:15",
        "action_class": "dialogue",
    }
    state.update(overrides)
    return state


def _deltas(log):
    for event in log.get_events("s1"):
        if event.type == "state-delta":
            return event.payload
    return None


def _node(log, decision=None, extractor=None):
    return make_post_turn_node(
        # The extractor owns the flat facts the genre state reads (loc/tone).
        extractor=extractor
        or (
            lambda u, p: {
                "deltas": {"loc": "shale", "tone": "eerie"},
                "action_class": "dialogue",
            }
        ),
        log=log,
        critic=FakeCritic(),
        decision=decision,
    )


def test_critic_always_runs_even_with_a_decision_caller(log):
    """The removed skip is unforgivable: a decision caller must not gate the critic."""
    critic = FakeCritic()
    node = make_post_turn_node(
        extractor=lambda u, p: {"deltas": {}, "action_class": "dialogue"},
        log=log,
        critic=critic,
        decision=FakeDecision("fantasy"),
    )
    run(node(_state()))

    assert critic.calls == 1


def test_genre_fact_is_written_from_a_confident_answer(log):
    decision = FakeDecision("fantasy")
    run(_node(log, decision)(_state()))

    assert decision.calls and PROSE in decision.calls[0]
    # Genre is a scene property: the state carries the scene line (from this
    # turn's own extracted facts), the prior tail, the exchange and the prose.
    state = decision.calls[0]
    assert "<scene_line>" in state and "tone: eerie" in state
    assert f"<user_message>I light the lamp early.</user_message>" in state
    # W9.32 R8/W11: the derived genre is a SCENE TRAIT and the trait is its only
    # home — the flat ``genre`` key this path used to stamp is retired, and
    # ``rendered_state`` derives it from the trait for every reader.
    assert _deltas(log).get("genre") is None
    from fabulist.state import entities

    assert entities.scene_traits(entities.registry(log, "s1"))["genre"] == "fantasy"


def test_no_genre_fact_when_the_caller_returns_none(log):
    decision = FakeDecision(None)  # disabled / error / 'other' / below confidence
    run(_node(log, decision)(_state()))

    assert "genre" not in _deltas(log)


def test_no_genre_fact_when_the_caller_raises(log):
    decision = FakeDecision(raises=RuntimeError("router down"))
    run(_node(log, decision)(_state()))

    assert "genre" not in _deltas(log)


def test_extractors_genre_is_never_clobbered(log):
    """Two owners of one fact = clobber: the extractor's answer wins, d1 is not asked."""
    decision = FakeDecision("sci-fi")
    extractor = lambda u, p: {"deltas": {"genre": "horror"}, "action_class": "dialogue"}
    run(_node(log, decision, extractor)(_state()))

    assert _deltas(log).get("genre") == "horror"
    assert decision.calls == []


def test_gate_off_writes_no_genre_and_no_decision_call(log):
    run(_node(log, None)(_state()))

    assert "genre" not in _deltas(log)
