"""Provider failures abort a PRE-COMMIT phase; content failures stay contained.

The owner's live bug: as narrator+planner model, GLM rate-limited the planner.
The planner's containment (``plan_refresh_failed``, "the previous plan keeps
steering") swallowed a 429, the plan silently went stale, and the reader only
met the failure a phase later, at the narrator — with the cause gone.

The rule pinned here (:mod:`fabulist.graph.provider_errors`):

* **provider/transport** — ANY 4xx/5xx status (400 and 422 included: a 400 is the
  provider rejecting OUR request, not a mitigation), an
  ``openai.APIConnectionError``/``APITimeoutError``, a builtin
  ``ConnectionError``/``TimeoutError``, or a bootstrap error flagged
  ``transport=True``. The phase RAISES a
  :class:`~fabulist.graph.provider_errors.PhaseCallError`, so the turn aborts on
  the engine's one error frame and the message names the phase and the status.
* **content/parse** — an unparseable, empty or schema-invalid reply: contained
  exactly as before, with its ``plan_refresh_failed`` row and the previous plan
  still steering.
* **post-commit** — the tail (critic, tracker, HUD, attribution) keeps its
  containment whatever the failure class: the turn is already kept.

Fake models only — no live inference.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from fabulist.connections.registry import Registry
from fabulist.graph.nodes import make_npc_lanes_node, make_planner_node
from fabulist.graph.plans import load_last_plan, save_plan
from fabulist.graph.provider_errors import (
    FATAL_STATUSES,
    PhaseCallError,
    failure_status,
    phase_failure,
)
from fabulist.log import EventLog
from fabulist.state.cast import admit_member
from fabulist.state.planner_bootstrap import PlannerBootstrapError
from fabulist.wiring import _provider_label

SID = "s1"

STRINGS = [
    "the seal goes missing from the vault",
    "Mara's debt comes due",
    "the archivist stalls the search",
    "the duel is postponed, not resolved",
]


def run(coro):
    return asyncio.run(coro)


# -- the two failure shapes --------------------------------------------------


def _rate_limited(
    message: str = "Rate limit reached for requests", retry_after: str = "20"
):
    """A REAL ``openai.RateLimitError`` (429) as the SDK raises it."""
    import httpx
    import openai

    request = httpx.Request("POST", "http://provider.example/v1/chat/completions")
    body = {"error": {"message": message}}
    response = httpx.Response(
        429, request=request, headers={"retry-after": retry_after}, json=body
    )
    return openai.RateLimitError(
        f"Error code: 429 - {body}", response=response, body=body
    )


class _RaisingCaller:
    """A ModelCaller whose call fails before any frame is yielded."""

    def __init__(self, error: BaseException) -> None:
        self.error = error
        self.calls = 0

    async def __call__(self, messages, stream=True, **kwargs):
        self.calls += 1
        raise self.error
        yield  # pragma: no cover - makes this an async generator


class _ReplyCaller:
    """A ModelCaller yielding one canned content payload."""

    def __init__(self, content: str) -> None:
        self.content = content

    async def __call__(self, messages, stream=True, **kwargs):
        yield {"type": "content", "delta": self.content}


def _plan(turn_seq: int = 0):
    from fabulist.graph.plans import Plan

    return Plan(
        turn_seq=turn_seq,
        created_from="graph",
        strings=list(STRINGS),
        datetime="Monday, June 02, 2025 at 19:40",
        weather="light rain",
        forecast="clearing before dawn",
        established=["Mara owes the guild 40 silver"],
    )


def _state(log, **overrides):
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


@pytest.fixture()
def log(tmp_path):
    event_log = EventLog(tmp_path / "events.db")
    try:
        yield event_log
    finally:
        event_log.close()


def _planner_failure_rows(log):
    return [
        e
        for e in log.get_events(SID)
        if (e.payload or {}).get("event") == "plan_refresh_failed"
    ]


# -- classification ----------------------------------------------------------


def test_a_provider_status_is_an_abort_naming_phase_status_and_model():
    fatal = phase_failure("Planning", _rate_limited(), provider="glm-4.6")

    assert isinstance(fatal, PhaseCallError)
    assert fatal.status == 429
    assert fatal.phase == "Planning"
    message = str(fatal)
    assert message.startswith("Planning failed: 429 from glm-4.6")
    # The provider's own words ride along, and so does its Retry-After.
    assert "Rate limit reached for requests" in message
    assert "retry after 20" in message


@pytest.mark.parametrize(
    "error",
    [
        ValueError("structured: empty payload"),
        RuntimeError("tool argument JSON did not parse"),
        KeyError("strings"),
    ],
)
def test_content_failures_stay_contained(error):
    assert phase_failure("Planning", error, provider="glm-4.6") is None


@pytest.mark.parametrize("status", sorted(FATAL_STATUSES) + [500, 502, 503, 529])
def test_provider_statuses_are_aborts(status):
    import httpx
    import openai

    request = httpx.Request("POST", "http://provider.example/v1/chat/completions")
    response = httpx.Response(status, request=request, json={"error": {"message": "nope"}})
    error = openai.APIStatusError("boom", response=response, body={"error": {"message": "nope"}})

    assert failure_status(error) == status
    assert phase_failure("Planning", error) is not None


@pytest.mark.parametrize("status", [400, 422, 402, 451])
def test_a_rejected_request_aborts_too(status):
    """400/422 are the provider rejecting OUR request — fatal pre-commit.

    The owner's rule: "a failed planning phase must abort the turn". A status is
    the provider refusing to serve the call, so the turn must not continue on a
    plan that never refreshed. (The live DeepSeek ``reasoning_content`` 400 that
    suggested an exception is the reasoning round-trip defect, fixed in 7be5782 —
    a bug in flight must not define the policy.)
    """
    import httpx
    import openai

    request = httpx.Request("POST", "http://provider.example/v1/chat/completions")
    response = httpx.Response(status, request=request, json={"error": {"message": "bad shape"}})
    error = openai.APIStatusError("boom", response=response, body={"error": {"message": "bad shape"}})

    fatal = phase_failure("Planning", error, provider="glm-4.6")
    assert fatal is not None
    assert str(fatal).startswith(f"Planning failed: {status} from glm-4.6")


@pytest.mark.parametrize(
    "error",
    [
        ConnectionError("connection reset"),
        TimeoutError("read timed out"),
        PlannerBootstrapError("timed out"),
    ],
)
def test_transport_classes_are_aborts(error):
    if isinstance(error, PlannerBootstrapError):
        error.transport = True  # the bootstrap's transport-timeout marker
        assert phase_failure("Planning (seed)", error) is not None
        return
    fatal = phase_failure("Planning", error, provider="glm-4.6")
    assert fatal is not None
    assert "from glm-4.6" in str(fatal)


# -- the planner node --------------------------------------------------------


def test_planner_rate_limit_aborts_the_turn_with_the_phase_named(log):
    """A provider failure in the PRE-COMMIT planner aborts instead of going stale."""
    caller = _RaisingCaller(_rate_limited())
    node = make_planner_node(
        log=log, caller=caller, provider_label_fn=lambda: "glm-4.6"
    )

    with pytest.raises(PhaseCallError) as caught:
        run(node(_state(log)))

    assert str(caught.value).startswith("Planning failed: 429 from glm-4.6")
    assert caller.calls == 1
    # The abort is not a contained failure: no "keep the previous plan" trace,
    # and nothing was saved.
    assert _planner_failure_rows(log) == []
    assert load_last_plan(log, SID) is None


def test_planner_quality_failure_still_contains_and_keeps_the_previous_plan(log):
    """An unparseable reply is content-class: contained, previous plan steers."""
    save_plan(log, SID, _plan(turn_seq=0))
    node = make_planner_node(
        log=log, caller=_ReplyCaller("not a plan, just prose"), provider_label_fn=lambda: "glm-4.6"
    )

    update = run(node(_state(log, plan="PLAN (turn 0)\nSTRINGS: the seal goes missing")))

    assert "plan" not in update  # the pre_turn render keeps steering
    rows = _planner_failure_rows(log)
    assert len(rows) == 1 and "stage" not in rows[0].payload
    assert load_last_plan(log, SID).strings == STRINGS  # the OLD plan, untouched


def test_planner_seed_transport_timeout_aborts_the_turn(log, monkeypatch):
    """The seed/repair loop's TIMEOUT is a provider failure, not a bad reply."""
    from fabulist.state import planner_bootstrap

    async def timed_out(**kwargs):
        error = PlannerBootstrapError("planner tool phase unusable: timed out")
        error.transport = True
        raise error

    monkeypatch.setattr(planner_bootstrap, "run_planner_tool_phase", timed_out)
    node = make_planner_node(
        log=log,
        caller=_ReplyCaller(json.dumps({"strings": STRINGS})),
        request_profile_fn=lambda: {"tools": []},
        provider_label_fn=lambda: "glm-4.6",
    )

    with pytest.raises(PhaseCallError) as caught:
        run(node(_state(log)))

    assert str(caught.value).startswith("Planning (seed) failed:")
    assert "from glm-4.6" in str(caught.value)


def test_planner_seed_content_failure_does_not_cost_the_plan(log, monkeypatch):
    """A seed that produced nothing usable is still contained (S2)."""
    from fabulist.state import planner_bootstrap

    async def unusable(**kwargs):
        raise PlannerBootstrapError("no action was recorded and no closing summary")

    monkeypatch.setattr(planner_bootstrap, "run_planner_tool_phase", unusable)
    payload = {
        "strings": list(STRINGS),
        "datetime": "Monday, June 02, 2025 at 19:40",
        "weather": "light rain",
        "forecast": "clearing before dawn",
        "established": ["Mara owes the guild 40 silver"],
    }
    node = make_planner_node(
        log=log,
        caller=_ReplyCaller(json.dumps(payload)),
        request_profile_fn=lambda: {"tools": []},
    )

    update = run(node(_state(log)))

    assert update["plan"].startswith("PLAN (turn 0)")
    rows = _planner_failure_rows(log)
    assert len(rows) == 1 and rows[0].payload["stage"] == "bootstrap"


def test_engine_surfaces_the_planner_abort_as_its_error_frame(tmp_path):
    """The abort reaches the client as the engine's ONE error frame shape."""
    from fabulist.graph import GraphTurnEngine, build_graph

    log = EventLog(tmp_path / "events.db")
    try:

        async def _unused(user_msg, prose):
            return {"deltas": {}, "action_class": "dialogue"}

        class _Narrator:
            async def __call__(self, messages, stream=True):
                yield {"type": "content", "delta": "never reached"}

        graph = build_graph(
            model=_Narrator(),
            extractor=_unused,
            log=log,
            planner=make_planner_node(
                log=log,
                caller=_RaisingCaller(_rate_limited()),
                provider_label_fn=lambda: "glm-4.6",
            ),
        )
        engine = GraphTurnEngine(graph=graph, log=log, model=_Narrator())
        frames = run(_collect(engine.run_turn(SID, "I step inside.", "in_character")))

        errors = [frame for frame in frames if frame.get("type") == "error"]
        assert len(errors) == 1
        assert errors[0]["delta"].startswith("Planning failed: 429 from glm-4.6")
        assert errors[0]["meta"]["session_id"] == SID
        # Turn 1's plan junction routes to the planner, so the turn aborted
        # BEFORE the narrator: no prose frame, no `done`, nothing committed.
        assert not [frame for frame in frames if frame.get("type") == "content"]
        assert frames[-1]["type"] == "error"
        narratives = [e for e in log.get_events(SID) if e.type == "narrative"]
        assert narratives == []
    finally:
        log.close()


async def _collect(agen):
    return [frame async for frame in agen]


# -- the NPC lane fan-out ----------------------------------------------------


def _lane_state():
    return {
        "session_id": SID,
        "user_content": "I wait under the awning.",
        "narrative_parts": [],
        "facts": "time: 09:15",
        "plan": "",
        "npc_lanes": {},
        "lane_meta": {},
    }


def _admit(log, name):
    return admit_member(
        log,
        SID,
        name,
        provenance="card",
        admitted_by="test",
        reference_kind="identity",
    )


def _world_state(log, *, venue=()):
    return log.append_event(
        SID,
        "system",
        "bts",
        {
            "event": "world_state",
            "kind": "delta",
            "state": {},
            "venue": {name: {"venue": "tavern", "area": "bar"} for name in venue},
            "off_screen": [],
            "threads": [],
            "tension": {},
        },
    )


def test_a_lane_provider_failure_aborts_the_turn(log):
    _admit(log, "Marcus")
    _world_state(log, venue=["Marcus"])
    fanout = make_npc_lanes_node(
        log=log,
        build_messages=lambda state: [
            {"role": "system", "content": "## PRIVATE — you are Marcus"}
        ],
        caller=_RaisingCaller(_rate_limited()),
        provider_label_fn=lambda: "glm-4.6",
    )

    async def one_lane():
        await fanout(_lane_state())
        rows = _lane_state()
        rows.update(await fanout(rows))
        sends = list(await fanout.route(rows))
        assert len(sends) == 1
        return await fanout.lane(sends[0].arg)

    with pytest.raises(PhaseCallError) as caught:
        run(one_lane())

    assert str(caught.value).startswith("NPC lanes failed: 429 from glm-4.6")


def test_a_lane_content_failure_is_still_a_skipped_lane(log):
    _admit(log, "Marcus")
    _world_state(log, venue=["Marcus"])
    fanout = make_npc_lanes_node(
        log=log,
        build_messages=lambda state: [
            {"role": "system", "content": "## PRIVATE — you are Marcus"}
        ],
        caller=_ReplyCaller("not json at all"),
    )

    async def one_lane():
        await fanout(_lane_state())
        rows = _lane_state()
        rows.update(await fanout(rows))
        sends = list(await fanout.route(rows))
        return await fanout.lane(sends[0].arg)

    update = run(one_lane())

    assert update["lane_meta"]["ran"] == 0
    assert update["lane_meta"]["skipped"][0]["name"] == "Marcus"


# -- the post-commit tail keeps its containment ------------------------------


def test_a_post_commit_phase_contains_a_provider_failure(log):
    """Past the commit barrier the turn is KEPT: housekeeping never aborts it.

    The critic is the model-calling post-commit stage; a 429 there is logged
    (``critic_failed``) and the delivered turn stands — the scope rule in
    :mod:`fabulist.graph.provider_errors`: pre-commit aborts, post-commit
    contains, because a background failure has nothing left to abort (ADR-010).
    """
    from fabulist.graph.nodes import make_critic_node

    narrative = log.append_event(
        SID,
        "narrative",
        "narrator",
        {"prose": "The lamp gutters.", "segments": [{"kind": "prose", "text": "The lamp gutters."}]},
    )

    async def critic(evidence):
        raise _rate_limited()

    node = make_critic_node(log=log, critic=critic)
    update = run(
        node(
            {
                "session_id": SID,
                "user_content": "I step inside.",
                "narrative_parts": ["The lamp gutters."],
                "segments": [{"kind": "prose", "text": "The lamp gutters."}],
                "critic_flags": [],
                "narrative_event": {"id": narrative.id, "seq": narrative.seq, "payload": {}},
            }
        )
    )

    assert isinstance(update, dict)  # no raise: the turn stands
    failures = [
        e
        for e in log.get_events(SID)
        if (e.payload or {}).get("event") == "critic_failed"
    ]
    assert failures and failures[-1].payload["stage"] == "call"


# -- the narrator draft ------------------------------------------------------
#
# The owner's live bug: narration was rebound DeepSeek -> GLM in the connections
# pane (GLM out of credit). The NEXT turn failed at the NARRATOR draft on a 429,
# and the client got the vendor's raw text ("Error code: 429 - {'error': …}") —
# no phase, no model, no classification, because this phase never called the
# classifier. A fresh engine that starts on GLM fails at the planner instead
# (due on turn 1), which IS classified: the two paths disagreed.


def _narrator_node(log, model, label=None):
    """The narrate phase node over a fake model and a live label reader."""
    from fabulist.graph.nodes import make_narrate_node

    return make_narrate_node(
        model=model,
        log=log,
        build_messages=lambda state: [{"role": "user", "content": "continue"}],
        provider_label_fn=label,
    )


def test_narrator_rate_limit_aborts_naming_the_phase_and_the_model(log):
    node = _narrator_node(log, _RaisingCaller(_rate_limited()), lambda: "glm-5.3")

    with pytest.raises(PhaseCallError) as caught:
        run(node(_state(log)))

    assert caught.value.phase == "Narration"
    assert caught.value.status == 429
    assert str(caught.value).startswith("Narration failed: 429 from glm-5.3")
    assert "Rate limit reached for requests" in str(caught.value)
    assert "retry after 20" in str(caught.value)


def test_a_narrator_content_failure_is_not_reclassified(log):
    """Non-provider failures keep their own text (this rule adds no containment)."""

    def broken_builder(state):
        raise ValueError("the message builder could not read the scene")

    from fabulist.graph.nodes import make_narrate_node

    node = make_narrate_node(
        model=_ReplyCaller("prose"),
        log=log,
        build_messages=broken_builder,
        provider_label_fn=lambda: "glm-5.3",
    )

    with pytest.raises(ValueError) as caught:
        run(node(_state(log)))

    assert not isinstance(caught.value, PhaseCallError)


def test_the_narrator_provider_failure_is_phase_named_on_the_turn_wire(tmp_path):
    """The raw exception path can no longer escape the node for a provider status."""
    from fabulist.graph import GraphTurnEngine, build_graph

    log = EventLog(tmp_path / "events.db")
    try:

        async def _extractor(user_msg, prose):
            return {"deltas": {}, "action_class": "dialogue"}

        graph = build_graph(
            model=_RaisingCaller(_rate_limited()),
            extractor=_extractor,
            log=log,
            narrate_provider_label=lambda: "glm-5.3",
        )
        engine = GraphTurnEngine(
            graph=graph, log=log, model=_RaisingCaller(_rate_limited())
        )
        frames = run(_collect(engine.run_turn(SID, "I step inside.", "in_character")))

        errors = [frame for frame in frames if frame.get("type") == "error"]
        assert len(errors) == 1
        delta = errors[0]["delta"]
        assert delta.startswith("Narration failed: 429 from glm-5.3")
        # The vendor's raw rendering is not what the reader meets any more.
        assert "Error code:" not in delta
        assert frames[-1]["type"] == "error"
        # Pre-commit: nothing was kept.
        assert [e for e in log.get_events(SID) if e.type == "narrative"] == []
    finally:
        log.close()


# -- the model label after a LIVE REBIND -------------------------------------


def _two_connection_registry(narration: str) -> Registry:
    """A real registry whose narration slot starts on ``narration``."""
    return Registry.from_dict(
        {
            "connections": {
                "deepseek": {
                    "base_url": "https://api.example/v1",
                    "model": "deepseek-flash",
                },
                "glm": {"base_url": "https://api.example/v1", "model": "glm-5.3"},
            },
            "agents": {
                "completion": "deepseek",
                "completion.extraction": "deepseek",
                "completion.narration": narration,
            },
        }
    )


def test_a_provider_failure_after_a_rebind_names_the_model_the_call_used(log):
    """Turn N fails on the old binding's model, turn N+1 on the NEW one.

    The rebind is the owner's connections-pane PATCH: it swaps the agents map
    under the SAME engine and the same node instance, so the label has to be
    resolved from the binding live at the failure — never captured when the
    graph was built, and never read from a caller object that has not resolved
    this turn's binding yet.
    """
    registry = _two_connection_registry("deepseek")
    node = _narrator_node(
        log,
        _RaisingCaller(_rate_limited()),
        _provider_label(registry, log, "narrate", "narrator"),
    )

    with pytest.raises(PhaseCallError) as before:
        run(node(_state(log)))
    assert str(before.value).startswith("Narration failed: 429 from deepseek-flash")

    registry.agents["completion.narration"] = "glm"  # the live rebind

    with pytest.raises(PhaseCallError) as after:
        run(node(_state(log)))
    assert after.value.phase == "Narration"
    assert str(after.value).startswith("Narration failed: 429 from glm-5.3")


def test_the_planner_names_the_model_of_the_failing_call_after_a_rebind(log):
    registry = _two_connection_registry("deepseek")
    node = make_planner_node(
        log=log,
        caller=_RaisingCaller(_rate_limited()),
        provider_label_fn=_provider_label(registry, log, "planner", "narrator"),
    )

    with pytest.raises(PhaseCallError) as before:
        run(node(_state(log)))
    assert str(before.value).startswith("Planning failed: 429 from deepseek-flash")

    registry.agents["completion.narration"] = "glm"  # the live rebind

    with pytest.raises(PhaseCallError) as after:
        run(node(_state(log)))
    assert str(after.value).startswith("Planning failed: 429 from glm-5.3")


def test_a_successful_plan_reads_no_model_label_at_all(log):
    """The label is read AT the failure: a healthy phase never resolves it."""
    reads: list[str] = []

    def label() -> str:
        reads.append("read")
        return "glm-5.3"

    payload = {
        "strings": list(STRINGS),
        "datetime": "Monday, June 02, 2025 at 19:40",
        "weather": "light rain",
        "forecast": "clearing before dawn",
        "established": ["Mara owes the guild 40 silver"],
    }
    node = make_planner_node(
        log=log, caller=_ReplyCaller(json.dumps(payload)), provider_label_fn=label
    )

    run(node(_state(log)))

    assert reads == []


def test_the_label_follows_the_interaction_not_the_callers_fallback_slot(log):
    """The NPC lanes ride the NARRATION slot, not the caller's ``extractor`` default.

    Reading the caller object's last-resolved agent named the extraction model
    (``deepseek-flash``) for a lane that called GLM — the owner's "wrong model
    printed on 429".
    """
    registry = _two_connection_registry("glm")

    assert _provider_label(registry, log, "npc", "extractor")() == "glm-5.3"
    # …and the extraction interaction still names its own connection.
    assert _provider_label(registry, log, "extract", "extractor")() == "deepseek-flash"
