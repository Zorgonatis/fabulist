"""Tests for W2.5 assistants (Extra Assistant personas).

No pytest-asyncio dependency in this repo, so async scenarios run via
``asyncio.run`` inside sync tests — matching ``tests/test_graph.py``. Callers
are fakes: the manager never touches the log or the graph.
"""

from __future__ import annotations

import asyncio

import pytest

from fabulist.assistants import (
    AssistantContext,
    AssistantDefinition,
    AssistantManager,
    build_assistant_messages,
    keyword_matches,
)


# -- helpers / fakes ---------------------------------------------------------


def run(coro):
    return asyncio.run(coro)


class FakeCaller:
    """A ``ModelCaller``: yields reasoning (optional) then content chunks."""

    def __init__(self, chunks=("SEGFAULT ", "online."), reasoning=None):
        self.chunks = list(chunks)
        self.reasoning = reasoning
        self.calls: list[list[dict]] = []

    async def __call__(self, messages, stream=True):
        self.calls.append(messages)
        if self.reasoning is not None:
            yield {"type": "reasoning", "delta": self.reasoning}
        for chunk in self.chunks:
            await asyncio.sleep(0)
            yield {"type": "content", "delta": chunk}


class FakeFactory:
    """A ``model_caller_factory`` recording agents and handing back fakes."""

    def __init__(self, callers=None):
        self.agents: list[str] = []
        self.callers: dict[str, FakeCaller] = dict(callers or {})

    def __call__(self, agent):
        self.agents.append(agent)
        if agent not in self.callers:
            self.callers[agent] = FakeCaller()
        return self.callers[agent]


def segfault(**overrides):
    base = dict(
        id="segfault",
        name="SEGFAULT",
        persona="A hacked-in assistant that monitors for CRITICAL ERRORS.",
        style_notes="Fun facts and emoji, always at the wrong moment.",
        agent="narrator",
        trigger="manual",
        keywords=["segfault", "error"],
    )
    base.update(overrides)
    return AssistantDefinition(**base)


# -- consult: persona-first context + draining -------------------------------


def test_consult_builds_persona_first_context_and_drains_caller():
    caller = FakeCaller(chunks=("Hello ", "there, ", "traveler."), reasoning="hmm")
    factory = FakeFactory({"narrator": caller})
    manager = AssistantManager([segfault()], factory)
    context = AssistantContext(
        facts="time: 14:45 | loc: night market",
        recent_prose="The market hums under lamplight.",
        user_msg="What now?",
    )

    reply = run(manager.consult("segfault", context))

    assert reply.text == "Hello there, traveler."
    assert reply.assistant_id == "segfault"
    assert reply.agent == "narrator"
    assert factory.agents == ["narrator"]

    (messages,) = caller.calls
    assert [m["role"] for m in messages] == ["system", "user"]
    system = messages[0]["content"]
    user = messages[1]["content"]
    assert "SEGFAULT" in system
    assert "A hacked-in assistant" in system
    assert "Fun facts and emoji" in system
    # digest carries facts / prose / user message into the final user turn
    assert "time: 14:45 | loc: night market" in user
    assert "The market hums under lamplight." in user
    assert "What now?" in user


def test_consult_uses_each_assistants_own_agent_tier():
    small = FakeCaller(chunks=("small",))
    big = FakeCaller(chunks=("big",))
    factory = FakeFactory({"small-model": small, "narrator": big})
    manager = AssistantManager(
        [
            segfault(id="segfault", agent="small-model"),
            segfault(id="muse", name="Muse", agent="narrator"),
        ],
        factory,
    )

    first = run(manager.consult("segfault", AssistantContext(user_msg="hi")))
    second = run(manager.consult("muse", AssistantContext(user_msg="hi")))

    assert factory.agents == ["small-model", "narrator"]
    assert first.agent == "small-model" and first.text == "small"
    assert second.agent == "narrator" and second.text == "big"
    # each caller received its own definition's persona
    assert "SEGFAULT" in small.calls[0][0]["content"]
    assert "Muse" in big.calls[0][0]["content"]


def test_empty_reply_is_tolerated():
    caller = FakeCaller(chunks=(), reasoning="only thinking")  # no content events
    manager = AssistantManager([segfault()], FakeFactory({"narrator": caller}))

    reply = run(manager.consult("segfault", AssistantContext(user_msg="hi")))

    assert reply.text == ""
    assert reply.assistant_id == "segfault"


def test_unknown_assistant_raises_keyerror():
    manager = AssistantManager([segfault()], FakeFactory())

    with pytest.raises(KeyError):
        run(manager.consult("nope", AssistantContext()))


def test_duplicate_assistant_ids_rejected():
    with pytest.raises(ValueError):
        AssistantManager([segfault(), segfault()], FakeFactory())


def test_bad_trigger_rejected():
    with pytest.raises(ValueError):
        segfault(trigger="sometimes")


# -- scan_triggers -----------------------------------------------------------


def test_keyword_scan_is_word_boundary_and_case_insensitive():
    assert keyword_matches("segfault", "SEGFAULT DETECTED")
    assert keyword_matches("error", "a critical error!")
    assert keyword_matches("error", "errors") is False
    assert not keyword_matches("segfault", "segfaulted the process")
    assert not keyword_matches("cat", "concatenate")
    assert not keyword_matches("term", "terminal")


def test_scan_triggers_matches_keyword_definitions_only():
    factory = FakeFactory()
    manager = AssistantManager(
        [
            segfault(id="segfault", trigger="keyword", keywords=["segfault"]),
            segfault(
                id="muse",
                name="Muse",
                trigger="manual",
                keywords=["muse"],
            ),
        ],
        factory,
    )

    assert manager.scan_triggers("Is there a SEGFAULT in here?") == ["segfault"]
    # manual-only definitions never scan, even when their keyword appears
    assert manager.scan_triggers("wake up, muse") == []


def test_scan_triggers_returns_all_matches_in_declaration_order():
    manager = AssistantManager(
        [
            segfault(id="a", trigger="keyword", keywords=["alpha"]),
            segfault(id="b", trigger="keyword", keywords=["beta", "alpha"]),
            segfault(id="c", trigger="keyword", keywords=["gamma"]),
        ],
        FakeFactory(),
    )

    assert manager.scan_triggers("alpha and beta walk in") == ["a", "b"]
    assert manager.scan_triggers("nothing here") == []
    # blank keywords are ignored
    manager_none = AssistantManager(
        [segfault(id="d", trigger="keyword", keywords=["  "])], FakeFactory()
    )
    assert manager_none.scan_triggers("anything") == []


# -- registry surface --------------------------------------------------------


def test_available_lists_definitions_in_order():
    a, b = segfault(id="a"), segfault(id="b")
    manager = AssistantManager([a, b], FakeFactory())

    listed = manager.available()

    assert [d.id for d in listed] == ["a", "b"]
    listed.clear()  # callers cannot mutate the manager's registry
    assert [d.id for d in manager.available()] == ["a", "b"]


def test_build_assistant_messages_is_persona_first_without_style():
    definition = segfault(style_notes="")
    messages = build_assistant_messages(definition, AssistantContext(user_msg="hey"))

    assert messages[0]["role"] == "system"
    assert "<style>" not in messages[0]["content"]
    assert messages[-1]["role"] == "user"
    assert "hey" in messages[-1]["content"]
