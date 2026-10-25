"""W9.13/W9.17 tests: the NPC fan-out (presence + Send lanes + narrator tail).

Owner spec under test (W9.17 moved the fan-out out of one node and into real
LangGraph topology: a dispatcher node, one ``Send("npc_lane", ...)`` per member,
one ``npc_lane`` node per member, and a join flush at the fan-in):

1. every lane-ELIGIBLE cast member is a CANDIDATE every turn — the LADDER does
   not cap the set (W9.22 §2: eligibility is a TIER question, see the tier
   section at the bottom of this file). W9.24 §3.5 narrows the set per turn by
   the session's DISPATCH POLICY, pinned in the policy section at the bottom;
2. ``max_lanes`` bounds CONCURRENCY only (a worker pool that slides over the
   queue), so actives > 4 all run, in ceil(n/4) batches;
3. lanes return ``{intent, narrative, lines}`` and the narrator synthesises them
   through ONE system message at the very END of its prompt;
4. no stage frame (the turn strip is unchanged);
5. a failed lane is skipped and logged, never fails the turn;
6. zero lanes ⇒ the narrator prompt is byte-identical to a turn without lanes;
7. no forced ``tool_choice`` anywhere (R29 is an open defect).

Fake models only — no live inference.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from fabulist.graph.nodes import (
    make_npc_lanes_node,
    render_lane_block,
    sanitize_lane_line,
    validate_lane_payload,
)
from fabulist.log import EventLog
from fabulist.state.cast import (
    REFERENCE_DESCRIPTOR,
    admit_from_proposal,
    admit_member,
    member_by_id,
    roster,
)
from fabulist.state.presence import (
    DEFAULT_MAX_LANES,
    DISPATCH_STATE_CHARS,
    DISPATCH_STATE_NARRATIVES,
    NPC_DISPATCH_EVENT,
    NpcDispatchPolicy,
    active_members,
    direct_address_members,
    lane_history,
    last_intents,
    npc_dispatch_policy,
    presence_walk,
    select_dispatch_members,
)

SID = "s1"


def _admit(log, name, *, descriptor=False, provenance="card"):
    """Admit one member through the single path; returns its ``member_id``.

    The default is ``card`` — ACTOR tier — because most tests here are about the
    fan-out mechanics (batching, joins, failures), not about the cast gate; the
    tier rules themselves are pinned in the tier section at the bottom.
    """
    return admit_member(
        log,
        SID,
        name,
        provenance=provenance,
        admitted_by="test",
        reference_kind=REFERENCE_DESCRIPTOR if descriptor else "identity",
    )


def _card(log, name):
    """Append a session card event (the seed the roster materialises from)."""
    log.append_event(
        SID,
        "system",
        "system",
        {"card": {"name": name, "description": f"{name} is here."}},
    )


def _attribution(log, speakers, *, key="attributions"):
    """Append one attribution row the way ``_run_attribution`` writes it."""
    log.append_event(
        SID,
        "system",
        "attribution",
        {
            "event": "attribution",
            key: [{"speaker": name, "quote": f'"{name}"'} for name in speakers],
        },
    )


def _narrative(log, prose="Rain drums."):
    return log.append_event(
        SID,
        "narrative",
        "narrator",
        {"prose": prose, "segments": [{"kind": "prose", "text": prose}]},
    )


def _world_state(log, *, venue=(), off_screen=(), state=None, kind="delta"):
    """Append one BTS world-state delta; returns the event (fork cut points)."""
    return log.append_event(
        SID,
        "system",
        "bts",
        {
            "event": "world_state",
            "kind": kind,
            "state": dict(state or {}),
            "venue": {name: {"venue": "tavern", "area": "bar"} for name in venue},
            "off_screen": [{"name": name, "location": "street"} for name in off_screen],
            "threads": [],
            "tension": {},
        },
    )


class FakeLaneCaller:
    """A lane caller: one JSON reply per call, keyed by the lane's name.

    Records every call's message list and kwargs so tests can assert the shared
    prefix, the concurrency pool and the absence of a ``tool_choice`` profile.
    """

    def __init__(self, replies=None, delay=0.01, finish_reason=None):
        self.replies = dict(replies or {})
        self.delay = delay
        self.finish_reason = finish_reason
        self.calls: list[dict] = []
        self.in_flight = 0
        self.max_in_flight = 0

    @staticmethod
    def _name(messages):
        for message in reversed(messages):
            if message.get("role") == "system":
                for line in str(message.get("content") or "").splitlines():
                    if line.startswith("## PRIVATE — you are "):
                        return line.split("## PRIVATE — you are ", 1)[1].strip()
        return ""

    async def __call__(self, messages, stream=True, **kwargs):
        self.calls.append({"messages": messages, "kwargs": kwargs})
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            await asyncio.sleep(self.delay)
            name = self._name(messages)
            reply = self.replies.get(name)
            if isinstance(reply, Exception):
                raise reply
            if reply is None:
                reply = {
                    "intent": f"{name} wants the door shut.",
                    "narrative": f"{name} moves to the door.",
                    "lines": [{"text": f"Not yet, {name}.", "tone": "flat"}],
                }
            payload = reply if isinstance(reply, str) else json.dumps(reply)
            yield {"type": "content", "delta": payload}
            if self.finish_reason:
                yield {
                    "type": "error",
                    "delta": "truncated",
                    "meta": {"finish_reason": self.finish_reason},
                }
        finally:
            self.in_flight -= 1


def _state(log, **overrides):
    state = {
        "session_id": SID,
        "user_content": "I wait under the awning.",
        "narrative_parts": [],
        "facts": "time: 09:15",
        "plan": "",
        "npc_lanes": {},
        "lane_meta": {},
    }
    state.update(overrides)
    return state


async def drive_fanout(fanout, state, *, order=None):
    """Drive a ``NpcLaneFanout`` the way the compiled graph does.

    Applies the dispatcher's update through the SAME reducers the graph uses
    (``merge_npc_lanes`` / ``merge_lane_meta``), runs one ``npc_lane`` per
    ``Send`` and merges each partial return, then runs the fan-in ``join``.
    ``order`` re-sequences the lane tasks so a test can prove that the emitted
    rows do NOT follow completion order.
    """
    from fabulist.graph.types import merge_lane_meta, merge_npc_lanes

    def apply(merged, update):
        if "npc_lanes" in update:
            merged["npc_lanes"] = merge_npc_lanes(
                merged.get("npc_lanes"), update["npc_lanes"]
            )
        if "lane_meta" in update:
            merged["lane_meta"] = merge_lane_meta(
                merged.get("lane_meta"), update["lane_meta"]
            )
        if "lane_plan" in update:
            merged["lane_plan"] = update["lane_plan"]
        return merged

    merged = apply(dict(state), await fanout(state))
    sends = list(await fanout.route(merged))
    payloads = [send.arg for send in sends]
    if order is not None:
        payloads = [payloads[index] for index in order]
    for result in await asyncio.gather(*(fanout.lane(p) for p in payloads)):
        apply(merged, result)
    apply(merged, fanout.join(merged))
    return merged


# -- presence ---------------------------------------------------------------


def test_delta_only_payloads_still_produce_presence(log):
    # A delta payload reports only what CHANGED (the under-reporting read); the
    # seq walk still knows the member is present, three quiet turns later.
    marcus = _admit(log, "Marcus")
    _world_state(log, venue=["Marcus"])
    for _ in range(2):
        _narrative(log)

    walk = presence_walk(log, SID)

    assert walk.is_present("Marcus")
    assert active_members(log, SID) == ([marcus], "presence")


def test_off_screen_after_a_venue_entry_is_absent_by_seq_not_membership(log):
    _admit(log, "Marcus")
    _world_state(log, venue=["Marcus"])
    _world_state(log, off_screen=["Marcus"])

    assert presence_walk(log, SID).is_present("Marcus") is False

    # A later venue entry wins again: the walk is seq-ordered.
    _world_state(log, venue=["Marcus"])
    assert presence_walk(log, SID).is_present("Marcus") is True


def test_a_checkpoint_does_not_resurrect_a_stale_venue(log):
    _admit(log, "Marcus")
    _world_state(log, venue=["Marcus"])
    # The checkpoint replaces the accumulated maps: Marcus is nowhere in it.
    _world_state(log, venue=["Ilse"], state={"Ilse": {"mood": {"state": "wary"}}}, kind="checkpoint")

    walk = presence_walk(log, SID)

    assert walk.is_present("Marcus") is False
    assert walk.is_present("Ilse") is True


def test_provisional_members_are_not_laned(log):
    _admit(log, "the woman", descriptor=True)
    _world_state(log, venue=["the woman"])

    assert active_members(log, SID) == ([], "none")


def test_stale_presence_empties_the_lane_set(log):
    _admit(log, "Marcus")
    _world_state(log, venue=["Marcus"])
    for _ in range(3):
        _narrative(log)

    assert active_members(log, SID) == ([], "none")


# -- the seed floor (fresh chats have no post-turn evidence yet) -------------


def test_a_fresh_chat_lanes_its_card_seeded_roster(log):
    # Turn 1 of a new chat: the cards (and a world actor reference) are on the
    # roster, but BTS has not written ``world_state`` and the attribution pass
    # has not run, so both evidence rungs are empty — the seed roster IS the
    # scene. This is the live session that planned zero lanes.
    _card(log, "Marcus")
    _card(log, "Zahra")
    _admit(log, "Ilse", provenance="world")
    _admit(log, "Barkeep", provenance="bts")  # learned in play: never seed

    members, source = active_members(log, SID)

    cast = roster(log, SID)
    assert source == "seed"
    assert [member_by_id(cast, member_id).display_name for member_id in members] == [
        "Marcus",
        "Zahra",
        "Ilse",
    ]


def test_a_world_state_naming_only_the_player_suppresses_the_seed_floor(log):
    # BTS ran and reported who is present; the player is not a lane owner, so
    # the set is empty — the floor must NOT resurrect the seed roster.
    _admit(log, "Marcus", provenance="card")
    _admit(log, "Zahra", provenance="card")
    _world_state(log, venue=["User"])

    assert active_members(log, SID) == ([], "none")


def test_an_attribution_naming_only_the_player_suppresses_the_seed_floor(log):
    # The second evidence kind gates the floor too: the attribution pass ran, so
    # the chat is not fresh — even though it named nobody who owns a lane.
    _admit(log, "Marcus", provenance="card")
    _narrative(log)
    _attribution(log, ["User"])

    assert active_members(log, SID) == ([], "none")


def test_presence_outranks_the_seed_floor(log):
    _admit(log, "Marcus", provenance="card")
    zahra = _admit(log, "Zahra", provenance="card")
    _world_state(log, venue=["Zahra"])

    assert active_members(log, SID) == ([zahra], "presence")


def test_a_stale_world_state_suppresses_the_seed_floor(log):
    # BTS ran three quiet turns ago: the walk is empty by freshness, but the
    # evidence KIND exists, so there is no seed floor to fall back to.
    _admit(log, "Marcus", provenance="card")
    _world_state(log, venue=["Marcus"])
    for _ in range(3):
        _narrative(log)

    assert active_members(log, SID) == ([], "none")


# -- the attribution rung (reads the key the writer actually emits) ---------


def test_attribution_rows_lane_a_member_on_a_chat_without_world_state(log):
    # Regression: the reader looked for ``rows`` while ``_run_attribution``
    # writes ``attributions`` (schema_gate.ATTRIBUTION_SCHEMA), so this rung
    # could never fire on a real log.
    marcus = _admit(log, "Marcus")
    _narrative(log)
    _attribution(log, ["Marcus"])

    assert active_members(log, SID) == ([marcus], "attribution")


def test_attribution_rows_reader_prefers_the_written_key_and_tolerates_rows():
    from fabulist.state.presence import _attribution_rows

    canonical = [{"speaker": "Marcus", "quote": '"Not yet."'}]
    assert _attribution_rows({"event": "attribution", "attributions": canonical}) == canonical
    # A pre-fix payload carried the rows under the old key; still readable.
    assert _attribution_rows({"event": "attribution", "rows": canonical}) == canonical
    assert _attribution_rows({"event": "attribution"}) == []
    assert _attribution_rows({"event": "attribution", "attributions": "nope"}) == []


# -- lane set: every active member, concurrency separate ---------------------


def test_every_active_member_is_laned_in_one_turn(log):
    ids = [_admit(log, name) for name in ("Marcus", "Ilse", "Zahra")]
    _world_state(log, venue=["Marcus", "Ilse", "Zahra"])

    caller = FakeLaneCaller()
    fanout = make_npc_lanes_node(log=log, build_messages=lambda s: [], caller=caller)
    update = asyncio.run(drive_fanout(fanout, _state(log)))

    assert sorted(update["npc_lanes"]) == sorted(ids)
    assert update["lane_meta"]["ran"] == 3
    assert update["lane_meta"]["skipped"] == []
    assert update["lane_meta"]["batches"] == 1
    # W9.17: one Send per ACTIVE member, and the rows land in the log in the
    # DISPATCHER's member order (the join writes them at the fan-in).
    assert update["lane_meta"]["members"] == ids


def test_concurrency_pool_never_exceeds_max_lanes_and_drops_nobody(log):
    # 9 actives on a pool of 4: all 9 run, in ceil(9/4) = 3 batches, and no
    # request is ever dropped (owner correction: the cap is not on the SET).
    names = [f"N{i}" for i in range(9)]
    for name in names:
        _admit(log, name)
    _world_state(log, venue=names)

    caller = FakeLaneCaller(delay=0.02)
    fanout = make_npc_lanes_node(
        log=log,
        build_messages=lambda s: [],
        caller=caller,
        config={"max_lanes": 4},
    )
    update = asyncio.run(drive_fanout(fanout, _state(log)))

    assert len(update["npc_lanes"]) == 9
    assert update["lane_meta"]["max_lanes"] == DEFAULT_MAX_LANES
    assert update["lane_meta"]["batches"] == 3
    assert caller.max_in_flight <= 4


def test_lane_rows_are_logged_and_readable_back(log):
    marcus = _admit(log, "Marcus")
    _world_state(log, venue=["Marcus"])
    fanout = make_npc_lanes_node(
        log=log, build_messages=lambda s: [], caller=FakeLaneCaller()
    )
    asyncio.run(drive_fanout(fanout, _state(log)))

    assert lane_history(log, SID)[marcus] == 0
    assert last_intents(log, SID)[marcus] == "Marcus wants the door shut."

    summary = [
        e for e in log.get_events(SID) if e.payload.get("event") == "npc_lanes"
    ]
    assert summary and summary[-1].payload["lanes"] == [marcus]
    assert summary[-1].payload["ran"] == 1


# -- lane payload boundary --------------------------------------------------


def test_lane_tail_is_built_from_the_shared_prefix_once(log):
    _admit(log, "Marcus")
    _admit(log, "Ilse")
    _world_state(log, venue=["Marcus", "Ilse"])
    shared = [{"role": "system", "content": "SHARED"}, {"role": "user", "content": "ask"}]
    builds: list[int] = []

    def build_messages(state):
        builds.append(1)
        return [dict(message) for message in shared]

    caller = FakeLaneCaller()
    fanout = make_npc_lanes_node(log=log, build_messages=build_messages, caller=caller)
    asyncio.run(drive_fanout(fanout, _state(log)))

    assert len(builds) == 1  # built ONCE, reused by both lanes
    for call in caller.calls:
        assert call["messages"][: len(shared)] == shared
        assert call["kwargs"] == {}  # no request_profile => no tool_choice (R29)


def test_lane_failure_is_skipped_and_logged_never_fatal(log):
    _admit(log, "Marcus")
    _admit(log, "Ilse")
    _world_state(log, venue=["Marcus", "Ilse"])

    caller = FakeLaneCaller(replies={"Ilse": RuntimeError("boom")})
    fanout = make_npc_lanes_node(log=log, build_messages=lambda s: [], caller=caller)
    update = asyncio.run(drive_fanout(fanout, _state(log)))

    assert list(update["npc_lanes"]) and update["lane_meta"]["ran"] == 1
    assert update["lane_meta"]["skipped"][0]["name"] == "Ilse"
    assert "boom" in update["lane_meta"]["skipped"][0]["reason"]


def test_truncated_reply_skips_the_lane(log):
    _admit(log, "Marcus")
    _world_state(log, venue=["Marcus"])
    caller = FakeLaneCaller(finish_reason="length")
    fanout = make_npc_lanes_node(log=log, build_messages=lambda s: [], caller=caller)
    update = asyncio.run(drive_fanout(fanout, _state(log)))

    assert update["npc_lanes"] == {}
    assert "truncated" in update["lane_meta"]["skipped"][0]["reason"]


def test_a_shape_failed_lane_row_carries_the_clipped_raw_text(log):
    """W25.5 (defect D-A): the raw output survives the failure.

    The live Tessa lane dropped on a shape failure and the row carried only
    ``{ok: false, reason: "shape"}``, so nothing about the model's actual reply
    could be recovered. A shape failure now records the first
    ``LANE_RAW_MAX_CHARS`` of the returned text on the row, and a passing lane's
    row does NOT grow the field.
    """
    from fabulist.graph.nodes import LANE_RAW_MAX_CHARS

    marcus = _admit(log, "Marcus")
    ilse = _admit(log, "Ilse")
    _world_state(log, venue=["Marcus", "Ilse"])

    # Parses as JSON, fails the shape contract (all three fields empty), and is
    # far longer than the clip so truncation is exercised.
    raw = json.dumps({"intent": "", "narrative": "", "lines": [], "junk": "x" * 2000})
    caller = FakeLaneCaller(replies={"Ilse": raw})
    fanout = make_npc_lanes_node(log=log, build_messages=lambda s: [], caller=caller)
    update = asyncio.run(drive_fanout(fanout, _state(log)))

    assert update["lane_meta"]["skipped"][0]["reason"] == "shape"
    rows = [
        event.payload
        for event in log.get_events(SID)
        if event.payload.get("event") == "npc_lane"
    ]
    failed = next(row for row in rows if row["member_id"] == ilse)
    assert failed["ok"] is False
    assert failed["reason"] == "shape"
    assert failed["raw"] == raw[:LANE_RAW_MAX_CHARS]
    assert len(failed["raw"]) == LANE_RAW_MAX_CHARS
    # The passing lane's row is untouched: no new key on the happy path.
    passed = next(row for row in rows if row["member_id"] == marcus)
    assert passed["ok"] is True
    assert "raw" not in passed


def test_zero_lanes_writes_nothing_and_returns_empty_state(log):
    _admit(log, "Marcus")
    _admit(log, "Ilse")
    _world_state(log, venue=["Marcus"])
    _world_state(log, off_screen=["Marcus"])  # nobody present

    caller = FakeLaneCaller()
    fanout = make_npc_lanes_node(log=log, build_messages=lambda s: [], caller=caller)
    update = asyncio.run(drive_fanout(fanout, _state(log)))

    assert caller.calls == []
    assert asyncio.run(fanout.route(_state(log))) == []  # no Send, no empty list of work
    assert update["npc_lanes"] == {}
    assert update["lane_meta"]["source"] == "none"
    assert update["lane_meta"]["members"] == []


# -- shape + sanitisation ---------------------------------------------------


def test_payload_shape_is_bounded_and_normalised():
    payload = validate_lane_payload(
        {
            "intent": "  close   the door ",
            "narrative": "I reach for it.",
            "lines": [
                {"text": '"Not yet."', "tone": "flat"},
                {"text": "```js\nno\n```"},
                {"text": "Panel = Stack([1])"},
                {"text": "second"},
                {"text": "third"},
                {"text": "over the cap"},
            ],
        }
    )

    assert payload["intent"] == "close the door"
    assert [line["text"] for line in payload["lines"]] == ["Not yet.", "second", "third"]
    assert sanitize_lane_line("  ``` x ``` ") == ""
    assert validate_lane_payload({"lines": "nope"}) is None
    assert validate_lane_payload({"intent": "", "lines": []}) is None


# -- the lane's model class (W9.25 §3.4) ------------------------------------


def test_npc_lanes_ride_the_narration_task_not_the_extractor():
    """The rewritten W9.13 binding pin: lanes are narrative-voice work.

    A real turn's Tessa lane — dispatched correctly, d1 chose her at 0.97 —
    rode the extractor-class 4B THINKING model, burned 3222 completion tokens
    and FAILED the lane shape validation, so the lane dropped and the narrator
    never saw her intent. ``npc`` therefore names the narration task: on any
    legacy connections document the lanes ride ``narrator``'s connection, and
    lane cost is the dispatch policy's knob (W9.24-W5), never the model class.
    """
    from fabulist.connections.taxonomy import resolve_interaction_agent
    from fabulist.prompts.agent_sets import DEFAULT_BINDINGS

    assert DEFAULT_BINDINGS["npc"]["agent"] == "completion.narration"

    legacy = {
        "narrator": "narrator-conn",
        "extractor": "extractor-conn",
        "summarizer": "summarizer-conn",
        "captioner": "captioner-conn",
        "decision": "decision-conn",
        "embedder": "embedder-conn",
    }
    # the shipped default resolves through the narration task to the narrator
    # connection on a config the owner never had to touch
    assert resolve_interaction_agent(None, legacy, "npc", "extractor") == "narrator"

    # authoring the typed task key is what the drawer writes; the lanes follow
    typed = {**legacy, "completion.narration": "narration-conn"}
    assert (
        resolve_interaction_agent(None, typed, "npc", "extractor")
        == "completion.narration"
    )

    # per item as today: an explicit preset binding still wins (the escape
    # hatch for a deliberately cheap lane), and a typed extraction key wins
    # over its legacy twin
    assert (
        resolve_interaction_agent(
            {"npc": {"agent": "extractor", "overrides": {}}},
            legacy,
            "npc",
            "extractor",
        )
        == "extractor"
    )
    assert (
        resolve_interaction_agent(
            {"npc": {"agent": "extractor", "overrides": {}}},
            {**legacy, "completion.extraction": "cheap-conn"},
            "npc",
            "extractor",
        )
        == "completion.extraction"
    )


# -- the narrator's bottom block -------------------------------------------


def test_lane_block_carries_intents_and_disappears_with_no_lanes():
    block = render_lane_block(
        {
            "m1": {
                "name": "Marcus",
                "intent": "get the door shut",
                "narrative": "he shoulders it closed",
                "lines": [{"text": "Not yet.", "tone": "flat"}],
            },
            "m2": {"name": "Ilse", "intent": "watch", "narrative": "", "lines": []},
        }
    )

    assert "intentions this turn" in block
    assert "get the door shut" in block
    assert '"Not yet."' in block
    assert "Ilse" in block and "says nothing" in block
    assert render_lane_block({}) == ""
    assert render_lane_block(None) == ""


def test_narrator_prompt_is_byte_identical_without_lanes_and_appends_the_block(log):
    from fabulist.wiring import make_narrator_message_builder

    _narrative(log, "Rain drums.")
    build = make_narrator_message_builder(log)

    without_keys = build(_state(log))
    with_empty_lanes = build(_state(log, npc_lanes={}, lane_meta={}))

    assert with_empty_lanes == without_keys

    lanes = {
        "m1": {
            "name": "Marcus",
            "intent": "get the door shut",
            "narrative": "he shoulders it closed",
            "lines": [{"text": "Not yet.", "tone": "flat"}],
        }
    }
    with_lanes = build(_state(log, npc_lanes=lanes))

    # Everything before the ONE envelope is untouched; the envelope itself
    # (issue #44: an assistant message immediately BEFORE the player's ask)
    # gains the <engine_lane> block, and the player's ask is unchanged.
    assert with_lanes[:-2] == without_keys[:-2]
    assert with_lanes[-1] == without_keys[-1]
    assert with_lanes[-2]["role"] == "assistant"
    assert "<engine_lane>" in with_lanes[-2]["content"]
    assert "get the door shut" in with_lanes[-2]["content"]
    assert with_lanes[-2]["content"].count("<engine_state>") == 1


# -- topology integration (the parity gate) --------------------------------


class _NarratorModel:
    """A ModelCaller that records the messages the narrator actually received."""

    def __init__(self, chunks=("Rain ", "drums.")):
        self.chunks = list(chunks)
        self.calls: list[list[dict]] = []

    async def __call__(self, messages, stream=True):
        self.calls.append([dict(message) for message in messages])
        for chunk in self.chunks:
            yield {"type": "content", "delta": chunk}


class _Extractor:
    async def __call__(self, user_msg, prose):
        return {"deltas": {"tone": "cold"}, "action_class": "dialogue"}


def test_canonical_topology_runs_the_lane_node_and_appends_nothing_without_lanes(log):
    from fabulist.graph.engine import GraphTurnEngine
    from fabulist.graph.graph import CANONICAL_TOPOLOGY, build_graph

    model = _NarratorModel()
    build = make_npc_lanes_recording_builder()

    graph = build_graph(
        model=model,
        extractor=_Extractor(),
        log=log,
        build_messages=build,
        topology=CANONICAL_TOPOLOGY,
    )
    engine = GraphTurnEngine(graph=graph, log=log, model=model)

    async def drive():
        return [event async for event in engine.run_turn(SID, "I wait.")]

    asyncio.run(drive())

    # No active cast, no lanes: the narrator sees today's prompt and the log
    # carries no lane rows (the parity gate).
    assert [m["role"] for m in model.calls[0]] == ["system", "user"]
    assert all("intentions this turn" not in str(m.get("content")) for m in model.calls[0])
    assert not [e for e in log.get_events(SID) if e.payload.get("event") == "npc_lane"]


def make_npc_lanes_recording_builder():
    """The narrator builder the graph will also hand to the lane node."""

    def build(state, out=None):
        return [
            {"role": "system", "content": "You are the narrator."},
            {"role": "user", "content": state["user_content"]},
        ]

    return build


def test_in_graph_lane_run_reaches_the_narrator_state_and_the_log(log):
    from fabulist.graph.engine import GraphTurnEngine
    from fabulist.graph.graph import CANONICAL_TOPOLOGY, build_graph

    marcus = _admit(log, "Marcus")
    _world_state(log, venue=["Marcus"])
    lane_caller = FakeLaneCaller()
    lane_node = make_npc_lanes_node(
        log=log,
        build_messages=lambda state: [
            {"role": "system", "content": "You are the narrator."},
            {"role": "user", "content": state["user_content"]},
        ],
        caller=lane_caller,
    )
    model = _NarratorModel()
    seen: list[dict] = []

    def build_messages(state, out=None):
        seen.append(dict(state))
        return [
            {"role": "system", "content": "You are the narrator."},
            {"role": "user", "content": state["user_content"]},
        ]

    graph = build_graph(
        model=model,
        extractor=_Extractor(),
        log=log,
        build_messages=build_messages,
        topology=CANONICAL_TOPOLOGY,
        npc_lanes=lane_node,
    )
    engine = GraphTurnEngine(graph=graph, log=log, model=model)

    async def drive():
        return [event async for event in engine.run_turn(SID, "I wait.")]

    asyncio.run(drive())

    assert len(lane_caller.calls) == 1  # one lane for the one active member
    # The narrator's builder ran in-graph WITH the lane results in state, which
    # is exactly what makes the real builder append the trailing block.
    assert seen and seen[-1]["npc_lanes"][marcus]["intent"]
    assert seen[-1]["lane_meta"]["ran"] == 1
    assert [e.payload["member_id"] for e in log.get_events(SID)
            if e.payload.get("event") == "npc_lane"] == [marcus]


# -- W9.22 §2: lanes are a TIER question (no bridge) ------------------------
#
# Lane eligibility is a TIER question: the actor provenances
# (``card``/``world``/``actor``/``user``) lane, and the presence provenances
# (``bts``/``extractor``, learned in play) never do. The owner ruled cast
# admission USER-GATED, so D76's transitional "lane until the promotion gate
# assesses you" bridge is gone with the gate: a trained presence has not been
# accepted by anyone and must not be role-played. The accept that re-tiers it is
# ``cast.admit_from_proposal``, pinned here at the read seam the graph calls.


def test_a_learned_bts_member_never_lanes_while_the_owner_has_not_accepted_it(log):
    """The owner's cast gate: detection is not admission.

    BTS detected this character in a turn (``_world_state`` names it in a
    ``venue``) and nothing accepted it, so the engine must not spend a lane on
    it — the engine role-playing an unaccepted character is exactly the leak
    the owner's ruling closes.
    """
    _admit(log, "Marcus", provenance="bts")
    _world_state(log, venue=["Marcus"])

    assert active_members(log, SID) == ([], "none")


def test_a_presence_member_is_still_in_the_scene_but_owns_no_lane(log):
    """Muting a lane is a TIER decision — the member stays a member.

    Presence, colours, attribution and ``member_categories`` all key on members
    (reviewer finding 2), so the walk must keep seeing this character; only the
    lane goes away.
    """
    learned = _admit(log, "Ilse", provenance="bts")
    _world_state(log, venue=["Ilse"])

    members, source = active_members(log, SID)

    assert (members, source) == ([], "none")
    assert presence_walk(log, SID).is_present("Ilse") is True
    assert any(member.member_id == learned for member in roster(log, SID))


def test_a_presence_member_does_not_shadow_a_present_actor(log):
    actor = _admit(log, "Marcus", provenance="card")
    _admit(log, "Ilse", provenance="bts")
    _world_state(log, venue=["Marcus", "Ilse"])

    assert active_members(log, SID) == ([actor], "presence")


def test_the_fanout_dispatches_no_lane_for_an_unaccepted_presence_member(log):
    """The rule at the seam the GRAPH calls, not only in ``active_members``."""
    actor = _admit(log, "Marcus", provenance="card")
    _admit(log, "Ilse", provenance="bts")
    _world_state(log, venue=["Marcus", "Ilse"])

    caller = FakeLaneCaller()
    fanout = make_npc_lanes_node(log=log, build_messages=lambda s: [], caller=caller)
    update = asyncio.run(drive_fanout(fanout, _state(log)))

    assert update["lane_meta"]["members"] == [actor]
    assert list(update["npc_lanes"]) == [actor]
    assert len(caller.calls) == 1


def test_the_member_the_owner_accepted_lanes_as_an_actor(log):
    """The accept is the promotion: ``admit_from_proposal`` re-tiers to ``actor``."""
    from fabulist.state.cast import admit_from_proposal

    accepted = admit_from_proposal(log, SID, "Marisol", ["marisol"], "bts")
    _world_state(log, venue=["Marisol"])

    assert active_members(log, SID) == ([accepted], "presence")


def test_an_actor_provenance_member_lanes_by_tier(log):
    millie = _admit(log, "Millie", provenance="actor")
    _world_state(log, venue=["Millie"])

    assert active_members(log, SID) == ([millie], "presence")


def test_the_user_provenance_is_lane_eligible_as_an_actor(log):
    """User-as-Actor (default OFF): the ``user`` member is ACTOR tier.

    The session SETTING is read by the player seams (the setting is not read
    here — this module stays a pure projection), but the tier contract is this
    module's: a ``user`` member is lane-eligible like any other actor.
    """
    user = admit_member(log, SID, "Nick", provenance="user", admitted_by="user")
    _world_state(log, venue=["Nick"])

    assert active_members(log, SID) == ([user], "presence")


def test_the_user_actor_lanes_when_the_tracker_spells_the_player_another_way(log):
    """User-as-Actor ON (W9.22 req. 4): the reachable row AND the lane.

    The door admits ONE ``user``-provenance member whose display is the persona
    name; the tracker however writes ``User`` in its venue. Both read-side seams
    must honour that: the player subtraction skips that member (the door owns
    the ON/OFF decision) and the presence match counts the player synonyms.
    Without both, the setting would grant the actor tier and then mute it.
    """
    from fabulist.state.cast import admit_member

    user_id = admit_member(log, SID, "Wanderer", provenance="user", admitted_by="bts")
    # The tracker reports the player under the generic surface, not the name.
    log.append_event(
        SID,
        "system",
        "bts",
        {
            "event": "world_state",
            "kind": "delta",
            "state": {"User": {"emotional": {"mood": "watchful"}}},
            "venue": {"User": {"venue": "clearing"}},
            "off_screen": [],
            "threads": [],
            "tension": {},
        },
    )

    assert active_members(log, SID) == ([user_id], "presence")


def test_a_user_provenance_member_with_no_player_evidence_does_not_lane(log):
    """The lane still needs presence: the actor tier is eligibility, not evidence."""
    from fabulist.state.cast import admit_member

    admit_member(log, SID, "Wanderer", provenance="user", admitted_by="bts")
    log.append_event(
        SID,
        "system",
        "bts",
        {
            "event": "world_state",
            "kind": "delta",
            "state": {"Marcus": {"emotional": {"mood": "wary"}}},
            "venue": {"Marcus": {"venue": "shop"}},
            "off_screen": [],
            "threads": [],
            "tension": {},
        },
    )
    members, _source = active_members(log, SID)
    assert members == []


# -- W9.24 §3.5: the dispatch policy ----------------------------------------


class FakeDecision:
    """A d1-3b double: records every ask, answers the dispatch choice question.

    ``probabilities`` is the ``{label: P}`` map the real route returns inside a
    ``choice`` answer; ``none=True`` simulates the real caller's "no usable
    answer" (it returns ``None`` for errors/timeouts rather than raising), and
    ``enabled=False`` a caller with no ``decision`` binding.
    """

    def __init__(self, probabilities=None, *, none=False, enabled=True):
        self.probabilities = dict(probabilities or {})
        self.none = none
        self.enabled = enabled
        self.calls: list[dict] = []

    async def ask(self, state, questions):
        self.calls.append({"state": state, "questions": questions})
        if self.none or not self.enabled:
            return None
        question_id = next(iter(questions))
        return {
            question_id: {
                "choice": next(iter(self.probabilities), ""),
                "probabilities": dict(self.probabilities),
                "confidence": 0.9,
            }
        }


def _dispatch_policy(log, *, strategy=None, max_dispatch=None, session=SID):
    """Append one policy write the way the chat-config PATCH does (both keys)."""
    payload = {"event": NPC_DISPATCH_EVENT}
    if strategy is not None:
        payload["strategy"] = strategy
    if max_dispatch is not None:
        payload["max_dispatch"] = max_dispatch
    log.append_event(session, "system", "user", payload)


def _run_fanout(log, *, decision=None, config=None):
    caller = FakeLaneCaller()
    fanout = make_npc_lanes_node(
        log=log,
        build_messages=lambda s: [],
        caller=caller,
        config=config,
        decision=decision,
    )
    update = asyncio.run(drive_fanout(fanout, _state(log)))
    return caller, update


def _drive(log, state, *, decision=None, config=None):
    """Drive the fan-out with an EXPLICIT turn state (the beat/message seam)."""
    caller = FakeLaneCaller()
    fanout = make_npc_lanes_node(
        log=log,
        build_messages=lambda s: [],
        caller=caller,
        config=config,
        decision=decision,
    )
    return caller, asyncio.run(drive_fanout(fanout, state))


def _prior_lane(log, member_id, name, intent):
    """Append one ``npc_lane`` row with a PRIOR intent (the ratchet's fuel)."""
    log.append_event(
        SID,
        "system",
        "npc",
        {
            "event": "npc_lane",
            "member_id": member_id,
            "name": name,
            "ok": True,
            "intent": intent,
            "narrative": "",
            "lines": [],
        },
    )


def _run_with_message(log, message, decision):
    """Drive one fan-out turn whose player message is ``message``."""
    return _drive(log, _state(log, user_content=message), decision=decision)


def _summaries(log):
    return [
        e.payload for e in log.get_events(SID) if e.payload.get("event") == "npc_lanes"
    ]


def test_the_default_policy_asks_the_dispatch_row_and_lanes_the_clearing_subset(log):
    """Absent policy -> ``decision``: ONE ask over the candidates, subset lanes.

    The ``choice`` question's labels ARE the candidate display names (the
    engine-composed criteria — the pinned difference from the bank's static
    rows), and only characters whose answer probability reaches the row's
    threshold dispatch. Nothing clearing the threshold is pinned separately.
    """
    ids = [_admit(log, name) for name in ("Marcus", "Ilse", "Zahra")]
    _world_state(log, venue=["Marcus", "Ilse", "Zahra"])
    _narrative(log, "Rain drums on the awning.")
    decision = FakeDecision({"Marcus": 0.91, "Ilse": 0.12, "Zahra": 0.31})

    _caller, update = _run_fanout(log, decision=decision)

    assert sorted(update["npc_lanes"]) == sorted([ids[0], ids[2]])
    assert update["lane_meta"]["strategy"] == "decision"
    assert update["lane_meta"]["candidates"] == 3
    assert update["lane_meta"]["dispatched"] == 2
    assert update["lane_meta"]["members"] == [ids[0], ids[2]]  # candidate order

    # ONE ask, on the dispatch row, with the roster as its label set.
    assert len(decision.calls) == 1
    questions = decision.calls[0]["questions"]
    assert list(questions) == ["who_dispatches"]
    assert set(questions["who_dispatches"]["criteria"]) == {"Marcus", "Ilse", "Zahra"}
    # The state line is the scene tail plus the turn's beat.
    assert "Rain drums on the awning." in decision.calls[0]["state"]


def test_the_dispatch_ask_carries_the_turns_beat(log):
    """The plan text is the beat the decision model judges "who acts now" against."""
    _admit(log, "Marcus")
    _admit(log, "Ilse")
    _world_state(log, venue=["Marcus", "Ilse"])
    decision = FakeDecision({"Marcus": 0.9, "Ilse": 0.1})
    state = _state(log, plan="Marcus blocks the door; Ilse watches.")

    fanout = make_npc_lanes_node(
        log=log, build_messages=lambda s: [], caller=FakeLaneCaller(), decision=decision
    )
    asyncio.run(drive_fanout(fanout, state))

    assert "Marcus blocks the door; Ilse watches." in decision.calls[0]["state"]


def test_a_verdict_that_clears_no_threshold_is_a_legal_zero_lane_turn(log):
    """Zero lanes is an ANSWER, not an error: no Send, and the summary says so."""
    _admit(log, "Marcus")
    _admit(log, "Ilse")
    _world_state(log, venue=["Marcus", "Ilse"])
    decision = FakeDecision({"Marcus": 0.04, "Ilse": 0.02})

    caller, update = _run_fanout(log, decision=decision)

    assert caller.calls == []  # no lane call at all
    assert update["npc_lanes"] == {}
    assert update["lane_meta"]["dispatched"] == 0
    assert update["lane_meta"]["candidates"] == 2
    rows = _summaries(log)
    assert len(rows) == 1
    assert rows[0]["lanes"] == []
    assert rows[0]["candidates"] == 2
    assert rows[0]["dispatched"] == 0
    assert rows[0]["ran"] == 0
    assert rows[0]["strategy"] == "decision"


def test_a_failed_ask_falls_back_to_the_ladder_never_to_zero_lanes(log):
    """d1 says nothing (error/timeout/no probabilities) -> today's set, fail-open."""
    ids = [_admit(log, name) for name in ("Marcus", "Ilse", "Zahra")]
    _world_state(log, venue=["Marcus", "Ilse", "Zahra"])
    decision = FakeDecision(none=True)

    _caller, update = _run_fanout(log, decision=decision)

    assert len(decision.calls) == 1  # it DID ask; the answer was unusable
    assert update["lane_meta"]["members"] == ids
    assert update["lane_meta"]["dispatched"] == 3


def test_an_unusable_answer_shape_also_falls_back(log):
    """A present but unreadable answer is "no answer" — never zero lanes."""
    _admit(log, "Marcus")
    _admit(log, "Ilse")
    _world_state(log, venue=["Marcus", "Ilse"])

    class Broken(FakeDecision):
        async def ask(self, state, questions):
            self.calls.append({"state": state, "questions": questions})
            return {"who_dispatches": {"choice": "Marcus", "probabilities": "nope"}}

    decision = Broken()
    _caller, update = _run_fanout(log, decision=decision)

    assert update["lane_meta"]["dispatched"] == 2


def test_a_disabled_or_missing_decision_caller_falls_back_to_the_ladder(log):
    """No binding (feature off) and no caller at all are the same shape: today's set."""
    _admit(log, "Marcus")
    _admit(log, "Ilse")
    _world_state(log, venue=["Marcus", "Ilse"])

    disabled = FakeDecision(enabled=False)
    _caller, update = _run_fanout(log, decision=disabled)
    assert disabled.calls == []  # a disabled caller is never asked
    assert update["lane_meta"]["dispatched"] == 2

    _caller, update = _run_fanout(log, decision=None)
    assert update["lane_meta"]["dispatched"] == 2
    assert update["lane_meta"]["strategy"] == "decision"


def test_the_fallback_still_respects_the_set_bound(log):
    """The bound is the owner's cost knob: fail-open must not blow through it."""
    _admit(log, "Marcus")
    _admit(log, "Ilse")
    _admit(log, "Zahra")
    _world_state(log, venue=["Marcus", "Ilse", "Zahra"])
    _dispatch_policy(log, strategy="decision", max_dispatch=2)

    _caller, update = _run_fanout(log, decision=FakeDecision(none=True))

    assert update["lane_meta"]["dispatched"] == 2
    assert len(update["lane_meta"]["members"]) == 2


def test_a_disabled_dispatch_row_falls_back_to_the_ladder(log):
    """The owner can switch the ask off in the bank: that is "no answer", not zero."""
    _admit(log, "Marcus")
    _admit(log, "Ilse")
    _world_state(log, venue=["Marcus", "Ilse"])
    from fabulist.decision_queries import (
        DECISION_QUERIES_ANCHOR,
        DECISION_QUERIES_EVENT,
        default_questions,
    )

    bank = [
        {**row, "enabled": False} if row["id"] == "who_dispatches" else row
        for row in default_questions()
    ]
    log.append_event(
        DECISION_QUERIES_ANCHOR,
        "system",
        "user",
        {"event": DECISION_QUERIES_EVENT, "questions": bank},
    )
    decision = FakeDecision({"Marcus": 0.99, "Ilse": 0.99})

    _caller, update = _run_fanout(log, decision=decision)

    assert decision.calls == []  # the row is off: no ask at all
    assert update["lane_meta"]["dispatched"] == 2


def test_a_presence_tier_member_is_never_a_dispatch_candidate(log):
    """The policy NARROWS the ladder; it can never lane a presence-tier member.

    The learned ``bts`` member is in the scene (the walk sees it) but the tier
    gate keeps it out of the candidate set, so it is not even a label the
    decision model may choose.
    """
    actor = _admit(log, "Marcus")
    _admit(log, "Barkeep", provenance="bts")
    _world_state(log, venue=["Marcus", "Barkeep"])
    decision = FakeDecision({"Marcus": 0.1, "Barkeep": 0.99})

    _caller, update = _run_fanout(log, decision=decision)

    assert set(decision.calls[0]["questions"]["who_dispatches"]["criteria"]) == {
        "Marcus"
    }
    assert update["lane_meta"]["members"] == []  # Marcus did not clear 0.25
    assert actor not in update["npc_lanes"]


def test_the_dispatch_summary_row_carries_the_policy_and_the_subset(log):
    """The inspector reads the policy + the selected subset off ONE row."""
    ids = [_admit(log, name) for name in ("Marcus", "Ilse", "Zahra")]
    _world_state(log, venue=["Marcus", "Ilse", "Zahra"])
    _dispatch_policy(log, strategy="lru", max_dispatch=2)
    _caller, update = _run_fanout(log)

    rows = _summaries(log)
    assert len(rows) == 1
    row = rows[0]
    assert row["strategy"] == "lru"
    assert row["max_dispatch"] == 2
    assert row["candidates"] == 3
    assert row["dispatched"] == 2
    assert row["lanes"] == ids[:2]
    assert row["ran"] == 2


def test_lru_truncates_the_ladder_order(log):
    """``lru`` is the deterministic sibling: the ladder's order, truncated."""
    ids = [_admit(log, name) for name in ("Marcus", "Ilse", "Zahra")]
    _world_state(log, venue=["Marcus", "Ilse", "Zahra"])

    selected = asyncio.run(
        select_dispatch_members(
            log, SID, ids, None, NpcDispatchPolicy(strategy="lru", max_dispatch=2)
        )
    )
    assert selected == ids[:2]
    # 0 = no bound: the whole ladder, i.e. today's behaviour.
    assert (
        asyncio.run(
            select_dispatch_members(log, SID, ids, None, NpcDispatchPolicy("lru", 0))
        )
        == ids
    )


def test_random_is_seeded_from_the_log_so_a_replay_selects_the_same_members(log):
    """No ambient entropy: the same chain is the same sample, every time."""
    ids = [_admit(log, name) for name in ("Marcus", "Ilse", "Zahra", "Osric")]
    _world_state(log, venue=["Marcus", "Ilse", "Zahra", "Osric"])
    _narrative(log, "Rain drums.")
    policy = NpcDispatchPolicy("random", 2)

    first = asyncio.run(select_dispatch_members(log, SID, ids, None, policy))
    second = asyncio.run(select_dispatch_members(log, SID, ids, None, policy))

    assert first == second
    assert set(first) <= set(ids)
    # Candidate order is preserved in the answer (the row order the join writes).
    assert first == [member_id for member_id in ids if member_id in set(first)]
    # The SEED pin: this chain (last narrative seq + session id) has always
    # selected these two. A change here means the seed formula changed — a
    # replay would then dispatch different members than the log recorded.
    cast = roster(log, SID)
    assert [member_by_id(cast, member_id).display_name for member_id in first] == [
        "Marcus",
        "Zahra",
    ]


def test_random_without_a_bound_keeps_every_candidate(log):
    """Nothing to sample: an unbounded random draw is the whole set."""
    ids = [_admit(log, name) for name in ("Marcus", "Ilse")]
    _world_state(log, venue=["Marcus", "Ilse"])

    assert (
        asyncio.run(
            select_dispatch_members(log, SID, ids, None, NpcDispatchPolicy("random", 0))
        )
        == ids
    )


def test_the_policy_fold_is_latest_wins_per_key(log):
    """Newest valid value per KEY wins; a malformed key never rewrites the other."""
    assert npc_dispatch_policy(log, SID) == NpcDispatchPolicy()  # absent -> defaults
    _dispatch_policy(log, strategy="random", max_dispatch=3)
    assert npc_dispatch_policy(log, SID) == NpcDispatchPolicy("random", 3)
    _dispatch_policy(log, strategy="lru")
    assert npc_dispatch_policy(log, SID) == NpcDispatchPolicy("lru", 3)
    _dispatch_policy(log, strategy="rotating", max_dispatch=-2)
    assert npc_dispatch_policy(log, SID) == NpcDispatchPolicy("lru", 3)


def test_the_policy_is_fork_carried_like_the_other_live_settings(log):
    """A branch lanes by the owner's policy: inherited up to the cut AND carried.

    Presence reads the CHAIN, so a row written before the fork arrives through
    the inherited window; a row written to the parent AFTER the cut is carried
    by ``FORK_CONFIG_EVENTS`` (the ``chat_user_as_actor`` rule) — either way the
    branch never silently reverts to the defaults.
    """
    _admit(log, "Marcus")
    narrative = _narrative(log, "Rain drums.")
    _dispatch_policy(log, strategy="lru", max_dispatch=1)  # after the cut: carried
    log.fork_session(SID, narrative.seq, "branch")

    assert npc_dispatch_policy(log, "branch") == NpcDispatchPolicy("lru", 1)


def test_a_policy_written_before_the_cut_arrives_through_the_chain(log):
    """The inherited half of fork-carrying: rows at/before the cut are the chain's."""
    _admit(log, "Marcus")
    _dispatch_policy(log, strategy="random", max_dispatch=2)
    narrative = _narrative(log, "Rain drums.")
    log.fork_session(SID, narrative.seq, "branch")

    assert npc_dispatch_policy(log, "branch") == NpcDispatchPolicy("random", 2)


def test_a_callerless_fanout_writes_no_summary_row(log):
    """No lane caller = no lane verdict: the dispatcher never reaches the policy.

    The zero-dispatch SUMMARY is a policy answer, so it must not appear for an
    engine that has no ``npc`` binding at all (that is not "the policy chose
    nobody", it is "this engine does not lane").
    """
    _admit(log, "Marcus")
    _world_state(log, venue=["Marcus"])

    fanout = make_npc_lanes_node(log=log, build_messages=lambda s: [], caller=None)
    asyncio.run(drive_fanout(fanout, _state(log)))

    assert _summaries(log) == []


# -- the dispatch ask's facts and the direct-address floor (2026-10-21) ------
#
# Live defect of record: the ``decision`` strategy declined,
# every turn, a character the player had just ADDRESSED BY NAME. The probe matrix
# over the real d1-3b found the cause in what the ask SHOWED it, and these tests
# pin both halves of the fix: the descriptions are presence/venue facts (never
# the member's own last lane intent, which RATCHETS), and a named candidate lanes
# whatever the model answered.


def test_a_named_candidate_dispatches_despite_a_near_zero_verdict(log):
    """The defect itself: the player speaks to her by name, the model says no.

    Same verdict as the live session (0.02 against a 0.25 threshold); the floor
    dispatches her anyway, and the character the model DID pick still lanes.
    """
    yara = _admit(log, "Yara")
    marcus = _admit(log, "Marcus")
    _world_state(log, venue=["Yara", "Marcus"])
    decision = FakeDecision({"Yara": 0.02, "Marcus": 0.91})

    _caller, update = _run_with_message(log, "Yara, are you alright in there?", decision)

    assert update["lane_meta"]["members"] == [yara, marcus]  # candidate order
    assert update["lane_meta"]["dispatched"] == 2


def test_an_alias_in_the_player_message_forces_the_candidate(log):
    """The floor matches aliases too: the player rarely uses the card's spelling."""
    yara = admit_from_proposal(log, SID, "Yara Okafor", ["Min"])
    _world_state(log, venue=["Yara Okafor"])
    decision = FakeDecision({"Yara Okafor": 0.01})

    _caller, update = _run_with_message(log, "Min — wait for me.", decision)

    assert update["lane_meta"]["members"] == [yara]
    assert direct_address_members(log, SID, [yara], "Min — wait for me.") == [yara]


def test_a_name_inside_a_longer_word_does_not_force(log):
    """``\\b`` on the casefolded name: a short alias must not match a longer word."""
    yara = admit_from_proposal(log, SID, "Yara Okafor", ["Min"])
    _world_state(log, venue=["Yara Okafor"])
    decision = FakeDecision({"Yara Okafor": 0.01})

    _caller, update = _run_with_message(log, "Morning, everyone.", decision)

    assert direct_address_members(log, SID, [yara], "Morning, everyone.") == []
    assert update["lane_meta"]["members"] == []  # the model's verdict stands


def test_every_named_candidate_forces_and_the_unnamed_follow_the_verdict(log):
    """The floor is additive: it never overrides the model on a member NOT named."""
    yara = _admit(log, "Yara")
    ilse = _admit(log, "Ilse")
    zahra = _admit(log, "Zahra")
    _world_state(log, venue=["Yara", "Ilse", "Zahra"])
    decision = FakeDecision({"Yara": 0.0, "Ilse": 0.0, "Zahra": 0.02})

    _caller, update = _run_with_message(log, "Yara. Ilse. Both of you, stop.", decision)

    assert update["lane_meta"]["members"] == [yara, ilse]
    assert zahra not in update["npc_lanes"]


def test_a_verdict_of_nobody_still_dispatches_the_named_candidate(log):
    """Zero lanes is the model's ANSWER; the floor is the owner's override on it."""
    yara = _admit(log, "Yara")
    _admit(log, "Marcus")
    _world_state(log, venue=["Yara", "Marcus"])
    decision = FakeDecision({"Yara": 0.04, "Marcus": 0.02})

    _caller, update = _run_with_message(log, "Yara?", decision)

    assert update["lane_meta"]["members"] == [yara]
    assert update["lane_meta"]["dispatched"] == 1


def test_the_floor_holds_for_every_strategy_including_the_bound(log):
    """``lru``/``random`` are narrowed too, and the floor is a union AFTER it.

    The bound (``max_dispatch=1``) is the owner's cost knob; a directly addressed
    character is the one case the ruling puts above it, so the set may exceed it.
    """
    yara = _admit(log, "Yara")
    marcus = _admit(log, "Marcus")
    _world_state(log, venue=["Yara", "Marcus"])

    for strategy in ("lru", "random"):
        _dispatch_policy(log, strategy=strategy, max_dispatch=1)
        _caller, update = _run_with_message(log, "Yara, look at me.", FakeDecision())

        assert yara in update["lane_meta"]["members"], strategy
        assert set(update["lane_meta"]["members"]) <= {yara, marcus}, strategy

    # And the SET the strategy answers with is still the model's when nobody is
    # named: the floor changes nothing on an ordinary turn.
    _dispatch_policy(log, strategy="lru", max_dispatch=1)
    _caller, update = _run_with_message(log, "I wait under the awning.", FakeDecision())
    assert update["lane_meta"]["members"] == [yara]


def test_the_forced_list_lands_in_the_npc_lanes_summary(log):
    """The inspector can explain a near-zero lane from the summary row alone."""
    yara = _admit(log, "Yara")
    marcus = _admit(log, "Marcus")
    _world_state(log, venue=["Yara", "Marcus"])
    decision = FakeDecision({"Yara": 0.03, "Marcus": 0.88})

    _caller, update = _run_with_message(log, "Yara, wait.", decision)

    row = _summaries(log)[-1]
    assert row["forced"] == [yara]
    assert row["lanes"] == [yara, marcus]
    assert set(row["forced"]) <= set(row["lanes"])
    assert update["lane_meta"]["forced"] == [yara]


def test_an_ordinary_turn_writes_an_empty_forced_list(log):
    """Additive and always present: a pre-change payload simply lacked the key."""
    _admit(log, "Marcus")
    _world_state(log, venue=["Marcus"])

    _caller, _update = _run_fanout(log, decision=FakeDecision({"Marcus": 0.9}))

    assert _summaries(log)[-1]["forced"] == []


def test_a_self_effacing_last_intent_is_not_the_description(log):
    """The RATCHET: her own last intent used to argue against her next lane.

    The description is the log's presence fact, so a timid intent cannot leak
    into the ask and the verdict is reachable again.
    """
    yara = _admit(log, "Yara")
    _world_state(log, venue=["Yara"])
    _prior_lane(log, yara, "Yara", "I want to disappear from the conversation.")
    decision = FakeDecision({"Yara": 0.9})

    _caller, _update = _run_fanout(log, decision=decision)

    criteria = decision.calls[0]["questions"]["who_dispatches"]["criteria"]
    assert criteria == {"Yara": "Present in tavern."}
    assert "disappear" not in json.dumps(criteria)


def test_a_candidate_with_no_venue_falls_back_to_in_the_scene(log):
    """The seed floor has no venue fact yet: the description says so, plainly."""
    _admit(log, "Marcus")
    decision = FakeDecision({"Marcus": 0.9})

    _caller, _update = _run_fanout(log, decision=decision)

    criteria = decision.calls[0]["questions"]["who_dispatches"]["criteria"]
    assert criteria == {"Marcus": "In the scene."}


def test_the_dispatch_state_carries_the_players_message(log):
    """The turn's own message is ONE state line, not repeated per candidate."""
    _admit(log, "Yara")
    _world_state(log, venue=["Yara"])
    decision = FakeDecision({"Yara": 0.9})

    _caller, _update = _run_with_message(log, "Yara, are you in there?", decision)

    state = decision.calls[0]["state"]
    assert "The player's message this turn: Yara, are you in there?" in state
    # Not in the descriptions: raw text is not per-candidate evidence.
    criteria = decision.calls[0]["questions"]["who_dispatches"]["criteria"]
    assert criteria == {"Yara": "Present in tavern."}


def test_without_a_plumbed_message_the_state_line_is_unchanged(log):
    """Back-compat: the default ``user_content=""`` adds no line and no floor."""
    ids = [_admit(log, name) for name in ("Yara", "Marcus")]
    _world_state(log, venue=["Yara", "Marcus"])
    decision = FakeDecision({"Yara": 0.9, "Marcus": 0.1})

    selected = asyncio.run(
        select_dispatch_members(
            log, SID, ids, decision, NpcDispatchPolicy("decision", 0), beat="Rain."
        )
    )

    assert selected == [ids[0]]
    state = decision.calls[0]["state"]
    assert "The player's message this turn:" not in state
    assert state.endswith("This beat: Rain.")


def test_the_dispatch_state_caps_are_pinned(log):
    """5 narratives / 4000 chars (probe: 2 and 800 were not the binding constraint).

    Pinned BEHAVIOURALLY as well as by value: the sixth-most-recent narrative is
    out of the window, the first of the last five is in it.
    """
    _admit(log, "Marcus")
    for index in range(6):
        _narrative(log, f"Turn {index} prose.")
    # The presence fact comes AFTER the prose: the freshness window (3 narratives)
    # would otherwise expire Marcus and the turn would have no candidate to ask
    # about.
    _world_state(log, venue=["Marcus"])
    decision = FakeDecision({"Marcus": 0.9})

    _caller, _update = _run_fanout(log, decision=decision)

    state = decision.calls[0]["state"]
    assert "Turn 0 prose." not in state
    assert "Turn 1 prose." in state
    assert DISPATCH_STATE_NARRATIVES == 5
    assert DISPATCH_STATE_CHARS == 4000
