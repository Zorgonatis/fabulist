"""Issue #67 — the engine tail is an ASSISTANT message before the final user message.

The defect: four engine phases appended their per-turn tail (protocol + palette,
or the reconcile directive) as a TRAILING ``system`` message — the planner seed
(``state/planner_bootstrap.py``), the planner repair, the BTS action pass
(``state/bts.py``) and the reconciliation pass. A stock Jinja chat template (the
Qwen family) ``raise_exception``s with "System message must be at the beginning"
on ANY system message that is not the first, so the payload was a hard 500 at the
provider and the turn died in the seed phase.

The ruling under test (owner-confirmed, "match the narrator so there are no
surprises"; "address it at the central location so this doesn't branch off
again"):

1. the tail is the NARRATOR's carrier — ``prompts.envelope.ENVELOPE_ROLE`` (an
   ``assistant`` message) inserted immediately BEFORE the final user message by
   ``prompts.envelope.insert_engine_envelope``. No new role, no second mechanism,
   never the leading prefix (a preset edit must not be able to drop it), and the
   player's message space is not touched;
2. ``connections/client.build_request`` is the single chokepoint every consumer
   rides, so it REFUSES a request whose ENGINE TAIL is a ``system`` message — a
   system row at or after the final user message
   (:class:`SystemMessageOrderError`, naming the offending index and the slot).
   It never rewrites one (ADR-023: the phase aborts, naming itself). The rule is
   the TAIL rule on purpose: the narrator legitimately emits system rows INSIDE
   history (a ``<validation_notice>``) and before the ask (a card's
   ``post_history_instructions``), and ADR-005 keeps that path unchanged;
3. the narrator path is untouched (ADR-005).

``stock_qwen_render`` below is a faithful miniature of the Qwen3 template rule:
it is what makes "the shape" a real assertion instead of a role-list convention.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from fabulist.connections import Registry, build_request
from fabulist.connections.client import SystemMessageOrderError
from fabulist.graph.nodes import make_planner_node
from fabulist.graph.planner_schema import planner_tool
from fabulist.graph.plans import Plan, save_plan
from fabulist.graph.provider_errors import PhaseCallError, phase_failure
from fabulist.prompts.envelope import ENVELOPE_ROLE
from fabulist.state.bts import (
    BTS_ACTION_PROTOCOL,
    RECONCILE_DIRECTIVE,
    BtsRunner,
    registry_palette,
)
from fabulist.state.entity_tools import RECONCILE_TOOL
from fabulist.state.planner_bootstrap import (
    BOOTSTRAP_DIRECTIVE,
    REPAIR_DIRECTIVE,
)

SID = "s-tail"

#: The planner's resolved Tier A profile (function calling).
PROFILE = {"tools": [planner_tool([])], "tool_choice": "auto"}

PLAN_JSON = json.dumps(
    {
        "strings": ["a", "b", "c", "d"],
        "datetime": "Monday, June 02, 2025 at 19:40",
        "weather": "light rain",
        "forecast": "clearing",
        "established": [],
    }
)


def run(coro):
    return asyncio.run(coro)


class LoopCaller:
    """ModelCaller scripted as ``(tool_name | None, payload)`` steps."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls: list[list[dict]] = []

    async def __call__(self, messages, stream=True, request_profile=None):
        self.calls.append([dict(message) for message in messages])
        name, payload = self.replies.pop(0) if self.replies else (None, "closed.")
        if name is None:
            yield {"type": "content", "delta": payload}
            return
        yield {
            "type": "tool_call",
            "delta": json.dumps(payload),
            "meta": {"name": name},
        }


def stock_qwen_render(messages: list[dict]) -> str:
    """The Qwen3 chat template's rule, verbatim in miniature.

    The real template ``raise_exception``s on ``loop.index0 != 0`` for a system
    role; reproducing that here is what makes the shape assertions mean "a stock
    local GGUF would accept this payload".
    """
    rendered: list[str] = []
    for index, message in enumerate(messages):
        if message.get("role") == "system" and index != 0:
            raise ValueError("System message must be at the beginning.")
        rendered.append(f"<|im_start|>{message['role']}\n{message['content']}<|im_end|>")
    return "\n".join(rendered)


def assert_stock_template_shape(messages: list[dict], tail_marker: str) -> int:
    """The ONE shape every engine tail must have, checked as a template would.

    * exactly one ``system`` message, at index 0;
    * the tail is an ``assistant`` message immediately before the final ``user``
      message, and the last message IS that user message;
    * the tail carries the expected marker and never the player's message bytes.

    Returns the tail's index.
    """
    roles = [message["role"] for message in messages]
    assert roles[0] == "system"
    assert [i for i, role in enumerate(roles) if role == "system"] == [0]
    assert roles[-1] == "user"
    assert roles[-2] == "assistant"
    tail = messages[-2]
    assert tail["role"] == ENVELOPE_ROLE
    assert tail_marker in tail["content"]
    assert tail_marker not in messages[-1]["content"]
    # The payload a stock template would produce — no raise means no 500.
    stock_qwen_render(messages)
    return len(messages) - 2


def _session(session_id: str = SID) -> None:
    from fabulist.agents.debug import CURRENT_SESSION

    CURRENT_SESSION.set(session_id)


def _planner_state(log, **overrides):
    state = {
        "session_id": SID,
        "user_content": "I wait under the awning.",
        "narrative_parts": [],
        "facts": "<state>\ntime: 08:00 | date: Monday, June 02, 2025 | day: 1\n</state>",
        "plan": "",
        "action_class": "dialogue",
    }
    state.update(overrides)
    return state


def _registry(*slots: str) -> Registry:
    """A registry binding every slot under test to one dummy connection."""
    return Registry.from_dict(
        {
            "connections": {"c": {"base_url": "http://host/v1", "model": "m"}},
            "agents": {slot: "c" for slot in slots},
        }
    )


# -- the fixture's own fidelity ---------------------------------------------


def test_the_stock_template_fixture_refuses_the_defect_shape():
    """The old shape was a 500 on a stock template — this is why #67 exists."""
    with pytest.raises(ValueError) as excinfo:
        stock_qwen_render(
            [
                {"role": "system", "content": "prefix"},
                {"role": "user", "content": "ask"},
                {"role": "system", "content": BOOTSTRAP_DIRECTIVE},
            ]
        )

    assert "System message must be at the beginning" in str(excinfo.value)


# -- the planner: seed + repair ----------------------------------------------


def test_the_planner_seed_tail_is_an_assistant_message_before_the_final_user_message(
    log,
):
    caller = LoopCaller((None, "nothing to seed."), (None, PLAN_JSON))
    run(
        make_planner_node(
            log=log, caller=caller, request_profile_fn=lambda: PROFILE
        )(_planner_state(log))
    )

    seed = caller.calls[0]
    assert len(seed) == 3  # the seed phase is ONE tool loop over 3 messages
    tail_at = assert_stock_template_shape(seed, BOOTSTRAP_DIRECTIVE)
    assert tail_at == 1
    # The protocol AND the palette ride the assistant tail — the seeded prefix
    # stays byte-stable (AGENTS 6) and the ask stays the ask.
    assert registry_palette(log, SID) in seed[1]["content"]
    assert "Bootstrap the object stores for turn 0" in seed[2]["content"]
    assert "You are the story planner" in seed[0]["content"]


def test_the_planner_repair_tail_is_an_assistant_message_before_the_final_user_message(
    log,
):
    save_plan(
        log,
        SID,
        Plan(turn_seq=0, created_from="graph", strings=["keep", "me", "s3", "s4"]),
    )
    caller = LoopCaller((None, "nothing to repair."), (None, PLAN_JSON))
    run(
        make_planner_node(
            log=log, caller=caller, request_profile_fn=lambda: PROFILE
        )(_planner_state(log))
    )

    repair = caller.calls[0]
    assert_stock_template_shape(repair, REPAIR_DIRECTIVE)
    assert BOOTSTRAP_DIRECTIVE not in repair[1]["content"]
    assert "Repair the object stores for turn 0" in repair[2]["content"]


def test_every_call_in_the_planner_loop_keeps_the_legal_shape(log):
    """Iteration 2+ appends assistant/tool rows — the lead and the tail hold."""
    caller = LoopCaller(
        ("establish_location", {"name": "hall"}),
        (None, "seeded the hall."),
        (None, PLAN_JSON),
    )
    run(
        make_planner_node(
            log=log, caller=caller, request_profile_fn=lambda: PROFILE
        )(_planner_state(log))
    )

    assert len(caller.calls) == 3
    for messages in caller.calls[:2]:  # the two seed-loop iterations
        assert [i for i, m in enumerate(messages) if m["role"] == "system"] == [0]
        stock_qwen_render(messages)


# -- the BTS pass: action + reconciliation -----------------------------------


def test_the_bts_action_tail_is_an_assistant_message_before_the_final_user_message(
    log,
):
    _session()
    caller = LoopCaller((None, "nothing to record"))
    run(
        BtsRunner(caller, log=log, profile_fn=lambda: {"tools": [], "tool_choice": "auto"})(
            "u", "prose"
        )
    )

    messages = caller.calls[0]
    tail_at = assert_stock_template_shape(messages, BTS_ACTION_PROTOCOL)
    assert tail_at == 1
    assert registry_palette(log, SID) in messages[1]["content"]
    assert messages[2]["content"].startswith("MODE: delta")


def test_the_bts_reconcile_tail_is_an_assistant_message_before_the_final_user_message(
    log,
):
    _session()
    log.append_event(SID, "narrative", "narrator", {"prose": "Yara waits in the hall."})
    caller = LoopCaller(
        (RECONCILE_TOOL, {"beliefs": [{"kind": "thread", "name": "the debt", "status": "active"}]}),
        (None, "stated."),
    )
    runner = BtsRunner(
        caller, log=log, profile_fn=lambda: {"tools": [], "tool_choice": "auto"}
    )
    run(runner.reconcile_now(SID))

    messages = caller.calls[0]
    assert_stock_template_shape(messages, RECONCILE_DIRECTIVE)
    # The directive IS the whole tail here (the action protocol and the palette
    # would both be wrong for a beliefs-only pass).
    assert messages[1]["content"] == RECONCILE_DIRECTIVE
    assert BTS_ACTION_PROTOCOL not in messages[1]["content"]


# -- the central guard (STRICT: only the leading run may be system) ----------


def test_the_central_guard_raises_on_a_bad_list_from_an_arbitrary_caller():
    """ANY caller's list, refused at the chokepoint, naming the index and slot."""
    registry = _registry("narrator")

    with pytest.raises(SystemMessageOrderError) as excinfo:
        build_request(
            "narrator",
            [
                {"role": "system", "content": "prefix"},
                {"role": "user", "content": "ask"},
                {"role": "system", "content": "a trailing system tail"},
            ],
            registry=registry,
        )

    error = excinfo.value
    assert error.index == 2
    assert error.agent == "narrator"
    assert error.total == 3
    message = str(error)
    assert "index 2 of 3" in message and "'narrator'" in message
    assert "System message must be at the beginning" in message
    assert "#67" in message and "#68" in message


def test_the_central_guard_raises_on_any_system_message_after_index_zero():
    """Not just the tail row: index 1 of a mid-list run is equally illegal."""
    registry = _registry("bts")

    for bad in (
        # an interleaved system row (a notice/post-history row left as system)
        [
            {"role": "system", "content": "prefix"},
            {"role": "assistant", "content": "prose"},
            {"role": "system", "content": "an engine row"},
            {"role": "user", "content": "ask"},
        ],
        # a second leading system row (before the #68 imagegen fold)
        [
            {"role": "system", "content": "prefix"},
            {"role": "system", "content": "context"},
            {"role": "user", "content": "ask"},
        ],
    ):
        with pytest.raises(SystemMessageOrderError) as excinfo:
            build_request("bts", bad, registry=registry)
        assert excinfo.value.index == (2 if len(bad) == 4 else 1)


def test_the_central_guard_is_fatal_for_a_pre_commit_phase():
    """ADR-023: the phase ABORTS naming itself — the failure is never contained."""
    error = SystemMessageOrderError("planner", 2, 3)

    fatal = phase_failure("Planning (seed)", error, provider="glm-4.6")

    assert isinstance(fatal, PhaseCallError)
    assert fatal.phase == "Planning (seed)"
    assert str(fatal).startswith("Planning (seed) failed: ")
    assert "index 2 of 3" in str(fatal)


def test_the_central_guard_passes_a_legal_list_and_keeps_the_bytes(log):
    """The engine shape (one leading system + the assistant carrier) rides free."""
    _session()
    caller = LoopCaller((None, "nothing to record"))
    run(
        BtsRunner(caller, log=log, profile_fn=lambda: {"tools": [], "tool_choice": "auto"})(
            "u", "prose"
        )
    )
    messages = caller.calls[0]

    request = build_request("bts", messages, registry=_registry("bts"))

    assert request["messages"] is messages  # untouched, not rewritten
    assert [m["role"] for m in request["messages"]] == ["system", "assistant", "user"]


# -- issue #68: the three shapes the strict rule rejected, fixed in place -----


def test_the_imagegen_shape_cannot_come_back():
    """``[system, system, user]`` was a 500 on a stock template — now ONE system.

    Drives the REAL render path (``imagegen_api._render_prompts``) with a
    template + context, so the assertion is on what a provider would receive.
    """
    from fabulist.api.imagegen_api import _render_prompts

    calls: list[list[dict]] = []

    async def caller(messages, stream=True, request_profile=None):
        calls.append([dict(message) for message in messages])
        yield {"type": "content", "delta": '{"positive": "p", "negative": ""}'}

    run(
        _render_prompts(
            caller=caller,
            template="the request template",
            request_text="a fox",
            context="CAST CONTEXT\nTessa",
        )
    )

    messages = calls[0]
    assert [m["role"] for m in messages] == ["system", "user"]
    # The template still leads byte-verbatim; the context follows the divider.
    assert messages[0]["content"].startswith("the request template\n\n")
    assert "CAST CONTEXT" in messages[0]["content"]
    # …and the strict central guard accepts it.
    build_request("imagegen", messages, registry=_registry("imagegen"))
    # The legacy shape is what the guard now refuses.
    with pytest.raises(SystemMessageOrderError):
        build_request(
            "imagegen",
            [
                {"role": "system", "content": "the request template"},
                {"role": "system", "content": "CAST CONTEXT\nTessa"},
                {"role": "user", "content": "a fox"},
            ],
            registry=_registry("imagegen"),
        )


def test_the_validation_notice_shape_cannot_come_back(log):
    """A notice rides history as an assistant row — never a system one (D-F)."""
    from fabulist.prompts.envelope import ENVELOPE_ROLE
    from fabulist.wiring import history_from_log

    log.append_event(SID, "user", "user", {"text": "I sit by the fire."})
    narrative = log.append_event(
        SID, "narrative", "narrator", {"prose": "Warmth spreads.", "segments": []}
    )
    log.append_event(
        SID,
        "system",
        "validator",
        {
            "event": "validation_notice",
            "seq": narrative.seq,
            "flags": [{"what": "raw markup", "fix": "remove it", "severity": "warn"}],
        },
    )

    history = history_from_log(log, SID)
    notice_at = next(
        index
        for index, message in enumerate(history)
        if str(message["content"]).startswith("<validation_notice>")
    )
    assert history[notice_at]["role"] == ENVELOPE_ROLE
    # The narrator shape around it: prefix, history, the ask.
    messages = [{"role": "system", "content": "the frozen prefix"}, *history]
    messages.append({"role": "user", "content": "the player's ask"})

    build_request("narrator", messages, registry=_registry("narrator"))
    # The legacy shape — the same notice as a SYSTEM row — is what the guard
    # now refuses, so a regression cannot reach a provider as a 500.
    legacy = [dict(message) for message in messages]
    legacy[notice_at + 1] = {
        "role": "system",
        "content": legacy[notice_at + 1]["content"],
    }
    with pytest.raises(SystemMessageOrderError) as excinfo:
        build_request("narrator", legacy, registry=_registry("narrator"))
    assert excinfo.value.index == notice_at + 1


def test_the_post_history_shape_cannot_come_back():
    """A card's post_history row rides the assistant carrier (issue #68).

    Exercised through the REAL preset compile path: the row keeps its authored
    position (immediately before the ask) and loses only its system role, so a
    card edit can never rewrite the frozen prefix for a per-turn-shaped row.
    """
    from fabulist.prompts.envelope import ENVELOPE_ROLE
    from fabulist.prompts.modules import (
        compile_flow,
        from_default_flow,
        splice_session_modules,
    )

    flow = from_default_flow("narrate")
    spliced = splice_session_modules(
        flow,
        [
            {
                "event": "session_module",
                "module_id": "card:x:post_history_instructions",
                "anchor": "post_history",
                "role": "system",
                "content": "CARD POST-HISTORY",
                "enabled": True,
            }
        ],
    )
    messages = compile_flow(
        "narrate",
        spliced,
        {
            "narrator_system": "SYS",
            "library": "LIB",
            "history": [
                {"role": "user", "content": "hi"},
                {"role": "assistant", "content": "hey"},
            ],
            "facts": "FACTS",
            "user_content": "USER",
            "recalled": "RECALLED",
        },
    )

    post_at = [m["content"] for m in messages].index("CARD POST-HISTORY")
    assert messages[post_at]["role"] == ENVELOPE_ROLE
    # Still immediately before the ask (its authored position is preserved).
    assert post_at < [m["content"] for m in messages].index("USER")
    # Exactly one system message, at index 0 — the strict rule, and the guard.
    assert [i for i, m in enumerate(messages) if m["role"] == "system"] == [0]
    build_request("narrator", messages, registry=_registry("narrator"))
