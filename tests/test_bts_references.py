"""W8.6 — BTS as the typed cast producer (identity / descriptor / rename).

The BTS interaction is the producer side of W7's cast identity. These tests pin
the five acceptance behaviours of the lane:

* ``references`` identity -> the member is admitted through the single admit path;
* a rename proposal **patches the same member** (member_id unchanged, colour kept,
  old name demoted to an alias) and rides the dynamic end notice;
* a descriptor splits on ambiguity — two members, never a silent merge;
* a descriptor entry's ``rename_to`` patches the member the phrase resolved to
  (W8.11), and a first-sight descriptor whose canonical key names exactly one
  IDENTITY member LINKS to that member instead of minting a provisional beside it;
* ``references`` absent -> the legacy ``bts_name_set`` admit, event for event;
* ``time`` is never set by BTS (D3) — on the structured path or the notation path;
* a narrator-class role word ("Narrative") is classified as a DESCRIPTOR, so it can
  never be admitted as an identity — and the engine retires the member such a
  word already produced (once), never a card-captured or renamed one.
* a DETERMINER PHRASE ("the soldier") is a DESCRIPTOR whatever kind the model
  typed (owner rule: "anything that is not a specific identity"), so it enters
  as a scene handle; the only escape is a surface the roster ALREADY carries
  exactly (a real name, or an alias a rename demoted), which stays an identity
  so "the soldier" resolves to the existing member instead of a "(2)" copy.

It also covers the structured transport itself: the Tier A request profile, the
``state/structured`` boundary feeding the BTS error path, and the payload
byte-compatibility of the world-state event.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from fabulist.api.cast_api import (
    SPEAKER_PALETTE,
    assign_member_color,
    list_member_colors,
)
from fabulist.graph.nodes import make_post_turn_node
from fabulist.prompts.entity_palette import ENTITY_PALETTE_DIRECTIVE
from fabulist.log import EventLog
from fabulist.state.bts import (
    BTS_ACTION_PROTOCOL,
    BTS_REPAIR_INSTRUCTION,
    BTS_STRUCTURED_REPAIR_INSTRUCTION,
    BTS_SYSTEM_PROMPT,
    BTS_TOOL_NAME,
    BtsExtractionError,
    BtsRunner,
    apply_bts_cast_proposals,
    build_bts_request_profile,
    bts_name_set,
    bts_tool,
    classify_reference_kind,
    is_determiner_phrase,
    is_role_surface,
    parse_bts_structured,
)
from fabulist.state.entity_tools import (
    ESTABLISH_LOCATION_TOOL,
    WRITE_TOOL_NAMES,
)
from fabulist.state.cast import (
    CAST_ADMIT_EVENT,
    CAST_ALIAS_EVENT,
    CAST_AMBIGUOUS_EVENT,
    CAST_LINK_EVENT,
    CAST_PATCH_EVENT,
    CAST_RETIRE_EVENT,
    REFERENCE_DESCRIPTOR,
    REFERENCE_IDENTITY,
    admit_member,
    ensure_roster,
    member_by_id,
    patch_member,
    roster,
    runtime_notice,
)

ARIA = "Aria"


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def build_log(tmp_path: Path):
    built: list[EventLog] = []

    def _build(name: str = "events.db") -> EventLog:
        log = EventLog(tmp_path / name)
        built.append(log)
        return log

    yield _build
    for log in built:
        log.close()


@pytest.fixture
def cast_proposals(monkeypatch) -> list[tuple[str, str, list[str], str]]:
    """Capture the cast proposals the BTS door files.

    The emitter is Lane L4's pinned ``state.proposals.emit_cast_proposal(
    session_id, name, aliases, origin)``; this lane codes against that signature,
    so the assertions here read the arguments the door hands it — name, aliases
    and origin — without owning the store.
    """
    filed: list[tuple[str, str, list[str], str]] = []

    def _emit(log, session_id, name, aliases, origin):
        filed.append((session_id, name, list(aliases), origin))

    from fabulist.state import proposals as proposal_store

    monkeypatch.setattr(proposal_store, "emit_cast_proposal", _emit, raising=False)
    return filed


def _seed_card(log: EventLog, sid: str, name: str) -> None:
    log.append_event(sid, "system", "system", {"card": {"name": name}, "first_mes": ""})


def _events(log: EventLog, sid: str, kind: str) -> list:
    return [e for e in log.get_events(sid) if e.payload.get("event") == kind]


def _admitted_names(log: EventLog, sid: str) -> list[str]:
    """Every name a ``cast_admit`` row put on the roster, in log order."""
    return [e.payload["name"] for e in _events(log, sid, CAST_ADMIT_EVENT)]


# -- the classifier: role words are descriptors -----------------------------


def test_role_words_are_classified_as_descriptors():
    for word in ("Narrative", "narrator", "SCENE", "story", "off-screen"):
        assert is_role_surface(word)
        assert classify_reference_kind(word) == REFERENCE_DESCRIPTOR
    # A role word can never be forced back into an identity by the model's own kind.
    assert classify_reference_kind("Narrative", "identity") == REFERENCE_DESCRIPTOR


def test_specific_names_keep_the_identity_default():
    assert classify_reference_kind("Zahra") == REFERENCE_IDENTITY
    # W7 ADDENDUM 3 A16 withdrew the role LEXICON: a bare noun is never
    # reclassified by spelling — a character can legitimately be named Clerk,
    # Mage or Kai, so the model's own kind (else the identity default) decides.
    for bare in ("Clerk", "mage", "Kai"):
        assert classify_reference_kind(bare) == REFERENCE_IDENTITY
        assert classify_reference_kind(bare, "identity") == REFERENCE_IDENTITY
    assert classify_reference_kind("Clerk", "descriptor") == REFERENCE_DESCRIPTOR


def test_determiner_phrases_are_classified_as_descriptors():
    """Owner rule (W7 ADDENDUM 3 Q1): anything not a specific identity.

    A leading article is the one spelling signal that always reads as a role
    phrase, whatever kind the model typed — the live roster carried
    ``the soldier`` / ``the soldier (2)`` because an identity claim was honoured.
    """
    for phrase in ("the soldier", "a guard", "an old woman", "the courier"):
        assert is_determiner_phrase(phrase)
        assert classify_reference_kind(phrase) == REFERENCE_DESCRIPTOR
        assert classify_reference_kind(phrase, "identity") == REFERENCE_DESCRIPTOR
        assert classify_reference_kind(phrase, "descriptor") == REFERENCE_DESCRIPTOR
    # Separators are spelling: an underscore surface is the same phrase.
    assert is_determiner_phrase("the_courier")
    assert classify_reference_kind("the_courier", "identity") == REFERENCE_DESCRIPTOR
    assert classify_reference_kind("the-courier", "identity") == REFERENCE_DESCRIPTOR
    assert classify_reference_kind("  The  Soldier ", "identity") == REFERENCE_DESCRIPTOR
    # A bare word that merely LOOKS like an article stem is not a phrase.
    assert not is_determiner_phrase("Theo")
    assert not is_determiner_phrase("The")
    assert classify_reference_kind("The", "identity") == REFERENCE_IDENTITY


# -- the structured contract -------------------------------------------------


def test_report_world_state_tool_declares_the_existing_fields_plus_references():
    tool = bts_tool()
    assert tool["type"] == "function"
    fn = tool["function"]
    assert fn["name"] == BTS_TOOL_NAME == "report_world_state"
    props = fn["parameters"]["properties"]
    for field in (
        "kind",
        "state",
        "deltas",
        "venue",
        "off_screen",
        "threads",
        "tension",
        "forecast",
        "references",
    ):
        assert field in props
    refs = props["references"]["items"]
    assert refs["properties"]["kind"]["enum"] == [REFERENCE_IDENTITY, REFERENCE_DESCRIPTOR]
    assert refs["required"] == ["kind", "name"]
    assert "rename_to" in refs["properties"] and "member_id" in refs["properties"]


def test_request_profile_is_none_without_function_calling_and_forced_when_measured():
    assert build_bts_request_profile({"function_calling": False}) is None
    assert build_bts_request_profile({}) is None
    forced = build_bts_request_profile(
        {"function_calling": True, "tool_choice_forced": True}
    )
    # W9.14: "required" is the only shape this llama.cpp build enforces as a
    # grammar (the named form is a hint — measured ~1/3 compliance).
    assert forced["tool_choice"] == "required"
    degraded = build_bts_request_profile(
        {"function_calling": True, "tool_choice_forced": False}
    )
    assert degraded["tool_choice"] == "auto"


def test_prompt_seed_states_the_names_contract_and_the_action_protocol():
    """The References section SURVIVES the rewrite (W9.29 §6); the notation does not.

    The identity/descriptor rules and the rename rule are the cast lane's
    producer-side contract, so they stay in the seeded prompt. The ``references:``
    LINE and ``report_world_state`` are gone with the retired write path — the
    successor spellings are the actions (``rename_being``, the proposal door).
    """
    assert "descriptor" in BTS_SYSTEM_PROMPT
    assert "identity" in BTS_SYSTEM_PROMPT
    assert "rename_being" in BTS_SYSTEM_PROMPT
    assert "get_roster" in BTS_SYSTEM_PROMPT
    assert "references:" not in BTS_SYSTEM_PROMPT
    assert "rename_to" not in BTS_SYSTEM_PROMPT
    assert "report_world_state" not in BTS_SYSTEM_PROMPT


def test_structured_payload_maps_into_the_canonical_result_and_drops_time():
    result = parse_bts_structured(
        {
            "kind": "checkpoint",
            "state": {"Zahra": {"physical": {"hp": 78}, "mood": "wary"}},
            "deltas": {"loc": "gallery", "weather": "light rain", "time": "09:15"},
            "venue": {"Zahra": {"venue": "@gallery", "area": "west wing"}},
            "off_screen": [{"name": "Kesh", "location": "@keep", "mood": "restless"}],
            "threads": [{"status": "weird", "name": "seal", "detail": "cracking"}],
            "tension": {"level": 6, "irony": "calm sky"},
            "references": [{"kind": "identity", "name": "Zahra"}],
        }
    )
    assert result.kind == "checkpoint"
    assert result.state == {
        "Zahra": {"physical": {"hp": "78"}, "emotional": {"mood": "wary"}}
    }
    assert result.venue == {"Zahra": {"venue": "gallery", "area": "west wing"}}
    assert result.off_screen[0]["location"] == "keep"
    assert result.threads[0]["status"] == "active"  # unknown status normalised
    assert result.tension == {"level": 6, "irony": "calm sky"}
    assert result.references[0].name == "Zahra"
    # D3: the clock owns time; BTS never sets it.
    assert result.deltas["loc"] == "gallery"
    assert "time" not in result.deltas


# -- the runner's action transport (W9.29 §6) --------------------------------
#
# The tracker no longer REPORTS a payload: it records the turn's changes as one
# validated write action per call. These tests pin the transport-level facts the
# cast lane depends on (the served tool set, the tail protocol, the identity
# door), not the action semantics — those live in ``tests/test_entity_tools.py``.


class _StructuredCaller:
    """A ModelCaller that answers with the given events and records its profile.

    When the scripted replies run out the caller answers with a plain content
    completion: the loop's natural stop, so a fixture can never spin (the runtime
    has no iteration cap by design).
    """

    def __init__(self, replies: list[list[dict]]) -> None:
        self._replies = list(replies)
        self.profiles: list[dict | None] = []
        self.calls: list[list[dict]] = []

    async def __call__(self, messages, stream=True, request_profile=None):
        self.calls.append([dict(m) for m in messages])
        self.profiles.append(request_profile)
        index = min(len(self.calls) - 1, len(self._replies) - 1)
        events = self._replies[index] if index >= 0 else []
        for event in events:
            yield event
        if len(self.calls) > len(self._replies):
            yield {"type": "content", "delta": "recorded."}


def _tool_reply(payload: dict, name: str) -> list[dict]:
    """A function-call reply the way the transport surfaces it: NAMED.

    ``RegistryModelCaller`` stamps ``meta.name`` on the argument fragments (W9.24
    added it so a multi-tool loop can attribute a call), and the BTS action loop
    resolves its tool by that name — so the fixture carries exactly what the wire
    carries.
    """
    return [
        {"type": "tool_call", "delta": json.dumps(payload), "meta": {"name": name}}
    ]


def _profiled():
    return {"tools": [bts_tool()]}


def test_the_structured_runner_serves_the_reads_and_writes_with_auto_choice(build_log):
    caller = _StructuredCaller(
        [
            _tool_reply({"name": "bathroom"}, ESTABLISH_LOCATION_TOOL),
            [{"type": "content", "delta": "Recorded the bathroom."}],
        ]
    )
    runner = BtsRunner(caller, log=build_log(), profile_fn=_profiled)
    result = run(runner("hello", "Zahra narrows her eyes."))

    assert result.actions == 1
    profile = caller.profiles[0]
    names = [tool["function"]["name"] for tool in profile["tools"]]
    assert names[:3] == ["get_folded_state", "get_roster", "get_recent_deltas"]
    assert names[3:] == list(WRITE_TOOL_NAMES)
    # ``report_world_state`` is not served: the loop's close-out replaced it.
    assert BTS_TOOL_NAME not in names
    assert profile["tool_choice"] == "auto"


def test_the_prose_reply_is_a_close_out_and_never_a_payload():
    """§11.2: a model that writes the retired notation records NOTHING."""
    caller = _StructuredCaller([[{"type": "content", "delta": "[BTS] :: Zahra ::\n@gate\n[/BTS]"}]])
    result = run(BtsRunner(caller, profile_fn=_profiled)("u", "n"))

    assert result.state == {} and result.venue == {}
    assert result.actions == 0
    assert result.raw == "[BTS] :: Zahra ::\n@gate\n[/BTS]"


def test_an_empty_run_raises_the_existing_error():
    caller = _StructuredCaller([[{"type": "content", "delta": ""}]])
    runner = BtsRunner(caller, profile_fn=_profiled)
    with pytest.raises(BtsExtractionError) as excinfo:
        run(runner("u", "n"))
    assert "no action was recorded" in str(excinfo.value)
    # No repair re-ask: a rejected ACTION is corrected in-loop by its tool result,
    # and an empty completion is a failure, not drift.
    assert len(caller.calls) == 1


def test_a_truncated_completion_with_no_action_is_refused():
    # finish_reason=length (surfaced by RegistryModelCaller as an error event with
    # meta) is refused once: truncated output is untrustworthy even when it closes,
    # and re-running it on the same budget cannot help (D86).
    truncated = _StructuredCaller(
        [
            [
                {"type": "tool_call", "delta": '{"name": "bath', "meta": {"name": ESTABLISH_LOCATION_TOOL}},
                {
                    "type": "error",
                    "delta": "generation truncated",
                    "meta": {"finish_reason": "length"},
                },
            ],
            [{"type": "content", "delta": ""}],
        ]
    )
    with pytest.raises(BtsExtractionError) as excinfo:
        run(BtsRunner(truncated, profile_fn=_profiled)("u", "n"))
    assert "truncated" in str(excinfo.value)


def test_a_malformed_call_is_answered_in_loop_and_the_model_re_calls(build_log):
    # A cut/malformed argument payload is not a dead turn: the runtime hands the
    # parse failure back as the tool result and the model re-calls.
    caller = _StructuredCaller(
        [
            [{"type": "tool_call", "delta": '{"name": "bath', "meta": {"name": ESTABLISH_LOCATION_TOOL}}],
            _tool_reply({"name": "bathroom"}, ESTABLISH_LOCATION_TOOL),
            [{"type": "content", "delta": "recorded"}],
        ]
    )
    result = run(BtsRunner(caller, log=build_log(), profile_fn=_profiled)("u", "n"))

    assert result.actions == 1
    assert len(caller.calls) == 3


def test_a_connection_without_function_calling_records_no_state():
    """§11.2 (a doctrine amendment to D69): Tier D can no longer write BTS state."""
    caller = _StructuredCaller([[{"type": "content", "delta": "[BTS] :: Zahra :: [/BTS]"}]])
    runner = BtsRunner(caller, profile_fn=lambda: None)
    with pytest.raises(BtsExtractionError) as excinfo:
        run(runner("u", "n"))
    assert excinfo.value.stage == "tier_d"
    # The model was never even called: nothing to parse, nothing to record.
    assert caller.calls == [] and caller.profiles == []


# -- the cast producer -------------------------------------------------------


def test_identity_reference_is_admitted_through_the_single_admit_path(build_log, cast_proposals):
    """Cast admission is USER-GATED: a new identity is a proposal, not a member.

    The BTS door still DETECTS the person (the reference is applied and returns),
    but nothing is admitted — the review home shows the proposal and the owner's
    accept is the one admission (``cast.admit_from_proposal``).
    """
    log = build_log()
    sid = "chat"
    _seed_card(log, sid, ARIA)
    ensure_roster(log, sid)

    applied = apply_bts_cast_proposals(
        log,
        sid,
        {
            "state": {"Zahra": {"physical": {"hp": "80"}}},
            "venue": {"Zahra": {"venue": "gate"}},
            "references": [{"kind": "identity", "name": "Zahra"}],
        },
    )
    assert [ref.name for ref in applied] == ["Zahra"]
    assert [m.display_name for m in roster(log, sid)] == [ARIA]
    assert _admitted_names(log, sid) == [ARIA]  # the seed, nobody else
    assert cast_proposals == [(sid, "Zahra", [], "bts")]


def test_rename_reference_patches_the_member_and_keeps_the_colour(build_log):
    log = build_log()
    sid = "chat"
    _seed_card(log, sid, ARIA)
    ensure_roster(log, sid)
    assign_member_color(log, sid, roster(log, sid)[0].member_id, ARIA)

    woman_id = admit_member(log, sid, "Woman", provenance="bts", admitted_by="bts")
    color_before = assign_member_color(log, sid, woman_id, "Woman")

    apply_bts_cast_proposals(
        log,
        sid,
        {
            "state": {"Woman": {"emotional": {"mood": "wary"}}},
            "references": [
                {
                    "kind": "identity",
                    "name": "Woman",
                    "rename_to": "Zahra",
                    "reason": "she gives her name",
                }
            ],
        },
    )
    member = member_by_id(roster(log, sid), woman_id)
    assert member is not None
    assert member.member_id == woman_id  # member_id is IMMUTABLE
    assert member.display_name == "Zahra"
    assert "Woman" in member.aliases
    assert list_member_colors(log, sid)[woman_id] == color_before
    patch = _events(log, sid, CAST_PATCH_EVENT)[-1]
    assert patch.payload["member_id"] == woman_id
    assert patch.payload["display_name"] == "Zahra"
    assert patch.payload["reason"] == "she gives her name"
    # The rename reaches the model through the dynamic END notice.
    notice = runtime_notice(log, sid)
    assert notice is not None and "Zahra" in notice and '"Woman"' in notice
    assert len(roster(log, sid)) == 2  # never a second member for the new name


def test_rename_of_an_unknown_surface_names_the_proposal(build_log, cast_proposals):
    """A determinised identity ("the clerk") is a DESCRIPTOR handle, not a person.

    The typed ``rename_to`` therefore reaches the owner as the proposal's name,
    with the phrase the prose used as its alias — and mints no member.
    """
    log = build_log()
    sid = "chat"
    _seed_card(log, sid, ARIA)
    ensure_roster(log, sid)

    apply_bts_cast_proposals(
        log,
        sid,
        {
            "references": [
                {"kind": "identity", "name": "the clerk", "rename_to": "Moses"}
            ],
        },
    )
    names = [m.display_name for m in roster(log, sid)]
    assert names == [ARIA, "the clerk"]  # the seed + the scene HANDLE, no person
    assert [m for m in roster(log, sid) if m.provisional] != []
    assert cast_proposals == [(sid, "Moses", ["the clerk"], "bts")]


def test_descriptor_reference_state_is_scene_scoped_and_splits_on_ambiguity(build_log):
    log = build_log()
    sid = "chat"
    _seed_card(log, sid, ARIA)
    ensure_roster(log, sid)

    # Turn 1: "the woman" with nobody known under that phrase -> a provisional
    # member with a scene link, and NO alias (a descriptor is never a name).
    turn = {
        "state": {"the woman": {"appearance": {"hair": "red"}}},
        "venue": {"the woman": {"venue": "gate"}},
        "references": [{"kind": "descriptor", "name": "the woman"}],
    }
    apply_bts_cast_proposals(log, sid, turn)
    provisional = [m for m in roster(log, sid) if m.provisional]
    assert len(provisional) == 1
    first_id = provisional[0].member_id
    assert provisional[0].display_name == "the woman"
    assert "the woman" not in provisional[0].aliases
    assert _events(log, sid, CAST_LINK_EVENT)
    first_color = assign_member_color(log, sid, first_id, "the woman")

    # Turn 2: same phrase, still no competing candidate -> REUSE (rule 1).
    apply_bts_cast_proposals(log, sid, turn)
    assert [m.member_id for m in roster(log, sid) if m.provisional] == [first_id]
    assert list_member_colors(log, sid)[first_id] == first_color

    # Turn 3: the typed conflict signal ("a different woman") -> SPLIT (rule 5).
    apply_bts_cast_proposals(
        log,
        sid,
        {**turn, "references": [{"kind": "descriptor", "name": "the woman", "conflict": True}]},
    )
    second = [m for m in roster(log, sid) if m.provisional and m.member_id != first_id]
    assert len(second) == 1
    second_id = second[0].member_id
    assert second[0].display_name == "the woman (2)"

    # Turn 4: TWO live candidates for the phrase are reported present at once ->
    # REFUSE (log ambiguous) and SPLIT again. Never a merge.
    apply_bts_cast_proposals(
        log,
        sid,
        {
            "state": {
                "the woman": {"appearance": {"hair": "red"}},
                "the woman (2)": {"appearance": {"hair": "black"}},
            },
            "venue": {"the woman": {"venue": "gate"}},
            "references": [{"kind": "descriptor", "name": "the woman"}],
        },
    )
    third_id = [
        m.member_id for m in roster(log, sid) if m.provisional and m.member_id not in {first_id, second_id}
    ]
    assert len(third_id) == 1
    ambiguous = _events(log, sid, CAST_AMBIGUOUS_EVENT)
    assert ambiguous
    assert set(ambiguous[-1].payload["candidates"]) == {first_id, second_id}
    # Three distinct members, each with its own id and colour; no alias absorbs any.
    ids = {m.member_id for m in roster(log, sid)}
    assert len(ids) == 4  # Aria + the three women
    assert "the woman" not in member_by_id(roster(log, sid), second_id).aliases
    assert list_member_colors(log, sid)[first_id] == first_color


def test_descriptor_conflict_signal_forces_a_new_member(build_log):
    log = build_log()
    sid = "chat"
    _seed_card(log, sid, ARIA)
    ensure_roster(log, sid)

    payload = {
        "state": {"the woman": {"appearance": {"hair": "red"}}},
        "venue": {"the woman": {"venue": "gate"}},
        "references": [{"kind": "descriptor", "name": "the woman"}],
    }
    apply_bts_cast_proposals(log, sid, payload)
    first = [m for m in roster(log, sid) if m.provisional][0].member_id

    conflicted = {
        **payload,
        "references": [{"kind": "descriptor", "name": "the woman", "conflict": True}],
    }
    apply_bts_cast_proposals(log, sid, conflicted)
    provisional = [m for m in roster(log, sid) if m.provisional and m.member_id != first]
    assert len(provisional) == 1  # "a different woman" splits


def test_role_word_reference_is_never_admitted_as_an_identity(build_log):
    log = build_log()
    sid = "chat"
    _seed_card(log, sid, ARIA)
    ensure_roster(log, sid)

    apply_bts_cast_proposals(
        log,
        sid,
        {
            "state": {"Narrative": {"narrative": {"tension": "6/10"}}},
            "references": [{"kind": "narrative", "name": "Narrative"}],
        },
    )
    narrative = [m for m in roster(log, sid) if m.display_name == "Narrative"]
    assert len(narrative) == 1
    # The role word went down the descriptor path: provisional + a scene link,
    # and (crucially) NOT admitted as an identity member.
    assert narrative[0].provisional
    assert narrative[0].descriptor_key
    assert any(
        e.payload.get("member_id") == narrative[0].member_id
        for e in _events(log, sid, CAST_LINK_EVENT)
    )


# -- determiner phrases: a role phrase, never a second person -----------------


def test_identity_claim_on_a_determiner_phrase_admits_a_descriptor(build_log):
    """The model typing ``identity`` cannot make "the soldier" a name."""
    log = build_log()
    sid = "chat"
    _seed_card(log, sid, ARIA)
    ensure_roster(log, sid)

    apply_bts_cast_proposals(
        log,
        sid,
        {
            "state": {"the soldier": {"physical": {"hp": 90}}},
            "references": [{"kind": "identity", "name": "the soldier"}],
        },
    )
    soldier = [m for m in roster(log, sid) if m.display_name == "the soldier"]
    assert len(soldier) == 1
    # The descriptor path: provisional handle + a scene link, never an identity.
    assert soldier[0].provisional
    assert soldier[0].descriptor_key == "soldier"
    assert [e.payload["member_id"] for e in _events(log, sid, CAST_LINK_EVENT)] == [
        soldier[0].member_id
    ]


def test_bare_common_noun_still_follows_the_model(build_log, cast_proposals):
    """No role lexicon (A16): "mage" is a NAME when the model says identity.

    The typing decides the DOOR (identity, not a scene handle); the user-gated
    policy decides what the door does with a person nobody has accepted.
    """
    log = build_log()
    sid = "chat"
    _seed_card(log, sid, ARIA)
    ensure_roster(log, sid)

    apply_bts_cast_proposals(
        log,
        sid,
        {
            "state": {"mage": {"emotional": {"mood": "calm"}}},
            "references": [{"kind": "identity", "name": "mage"}],
        },
    )
    assert [m.display_name for m in roster(log, sid)] == [ARIA]
    assert cast_proposals == [(sid, "mage", [], "bts")]
    assert not _events(log, sid, CAST_LINK_EVENT)


def test_descriptor_reference_to_an_existing_member_mints_no_duplicate(build_log):
    """The existing "the soldier" member absorbs the phrase: no "(2)" copy.

    The live roster grew a (2)/(3)/(4) family out of this exact surface. An exact
    roster surface outranks the determiner coercion (the only escape, and it is
    the apply path's because the classifier is pure), so the second turn reuses
    the member and writes no second admit.
    """
    log = build_log()
    sid = "chat"
    _seed_card(log, sid, "the soldier")
    ensure_roster(log, sid)
    soldier_id = roster(log, sid)[0].member_id

    applied = apply_bts_cast_proposals(
        log,
        sid,
        {
            "state": {"the soldier": {"physical": {"hp": 60}}},
            "references": [{"kind": "descriptor", "name": "the soldier"}],
        },
    )
    assert [ref.kind for ref in applied] == [REFERENCE_IDENTITY]  # as applied
    members = roster(log, sid)
    assert [m.display_name for m in members] == ["the soldier"]
    assert members[0].member_id == soldier_id
    assert not any(name.endswith("(2)") for name in (m.display_name for m in members))
    assert len(_events(log, sid, CAST_ADMIT_EVENT)) == 1
    # A real name is not turned into a scene handle: no descriptor link.
    assert not _events(log, sid, CAST_LINK_EVENT)


def test_rename_demoted_alias_survives_the_determiner_coercion(build_log):
    """A rename's old surface keeps resolving backward to its member (A9)."""
    log = build_log()
    sid = "chat"
    _seed_card(log, sid, ARIA)
    ensure_roster(log, sid)
    woman_id = admit_member(log, sid, "the woman", provenance="bts", admitted_by="bts")
    patch_member(log, sid, woman_id, display_name="Mara", reason="she gives her name")

    apply_bts_cast_proposals(
        log,
        sid,
        {
            "state": {"Mara": {"emotional": {"mood": "wary"}}},
            "references": [{"kind": "descriptor", "name": "the woman"}],
        },
    )
    members = roster(log, sid)
    assert [m.display_name for m in members] == [ARIA, "Mara"]
    assert "the woman" in member_by_id(members, woman_id).aliases
    assert not _events(log, sid, CAST_LINK_EVENT)


# -- the retire guard: the engine corrects only its own mistakes -------------


def _narrative_ref() -> dict:
    return {"kind": "descriptor", "name": "Narrative"}


def _narrative_result() -> dict:
    return {
        "state": {"Narrative": {"narrative": {"tension": "6/10"}}},
        "references": [_narrative_ref()],
    }


def test_role_word_retires_the_provisional_member_it_minted(build_log):
    """A role word's provisional scene handle is the engine's mistake: retire."""
    log = build_log()
    sid = "chat"
    _seed_card(log, sid, ARIA)
    ensure_roster(log, sid)

    apply_bts_cast_proposals(log, sid, _narrative_result())  # mints the handle
    before = [m for m in roster(log, sid) if m.display_name == "Narrative"]
    assert len(before) == 1 and before[0].provisional

    apply_bts_cast_proposals(log, sid, _narrative_result())  # the guard owns it

    member = member_by_id(roster(log, sid), before[0].member_id)
    assert member is not None and not member.is_active
    (retire,) = _events(log, sid, CAST_RETIRE_EVENT)
    assert retire.payload["member_id"] == before[0].member_id
    assert "Narrative" in retire.payload["reason"]
    # Retired, and NOT re-minted: no fresh handle, no "Narrative (2)".
    assert [m.display_name for m in roster(log, sid)] == [ARIA, "Narrative"]

    # Idempotent: a third mention appends no second tombstone.
    apply_bts_cast_proposals(log, sid, _narrative_result())
    assert len(_events(log, sid, CAST_RETIRE_EVENT)) == 1


def test_role_word_never_retires_a_card_captured_character(build_log):
    """A card member named like a role word is a deliberate character."""
    log = build_log()
    sid = "chat"
    _seed_card(log, sid, "Narrative")
    seed = ensure_roster(log, sid)[0]
    assert seed.provenance == "card"

    apply_bts_cast_proposals(log, sid, _narrative_result())

    member = member_by_id(roster(log, sid), seed.member_id)
    assert member is not None and member.is_active
    assert _events(log, sid, CAST_RETIRE_EVENT) == []


def test_role_word_never_retires_a_member_a_reference_has_named(build_log):
    """Once a reference names the member it is deliberate: the rename keeps it."""
    log = build_log()
    sid = "chat"
    _seed_card(log, sid, ARIA)
    ensure_roster(log, sid)
    apply_bts_cast_proposals(log, sid, _narrative_result())
    (narrative,) = [m for m in roster(log, sid) if m.display_name == "Narrative"]
    patch_member(log, sid, narrative.member_id, display_name="Nora", reason="named")

    apply_bts_cast_proposals(log, sid, _narrative_result())

    member = member_by_id(roster(log, sid), narrative.member_id)
    assert member is not None and member.is_active
    assert member.display_name == "Nora"
    assert _events(log, sid, CAST_RETIRE_EVENT) == []


def test_role_word_retires_a_legacy_engine_admitted_member(build_log):
    """The pre-typing failure class: an identity admit for the role word itself.

    A session that ran before W8.6 carries a plain ``cast_admit`` for the role
    word (no provisional flag, no rename). It is the engine's own mistake, so
    the next role-word reference tombstones it — once.
    """
    log = build_log()
    sid = "chat"
    _seed_card(log, sid, ARIA)
    ensure_roster(log, sid)
    legacy_id = admit_member(log, sid, "Narrative", provenance="bts", admitted_by="bts")

    apply_bts_cast_proposals(log, sid, _narrative_result())

    (retire,) = _events(log, sid, CAST_RETIRE_EVENT)
    assert retire.payload["member_id"] == legacy_id
    assert "Narrative" in retire.payload["reason"]
    member = member_by_id(roster(log, sid), legacy_id)
    assert member is not None and not member.is_active

    apply_bts_cast_proposals(log, sid, _narrative_result())
    assert len(_events(log, sid, CAST_RETIRE_EVENT)) == 1


def test_role_word_retires_an_attribution_minted_member_but_not_a_seed(build_log):
    """The guard covers every runtime admitter, and no seeded member."""
    log = build_log()
    sid = "chat"
    _seed_card(log, sid, ARIA)
    world_id = admit_member(
        log, sid, "Narrator", provenance="world", admitted_by="world"
    )
    extracted_id = admit_member(
        log, sid, "Story", provenance="extractor", admitted_by="attribution"
    )

    apply_bts_cast_proposals(
        log,
        sid,
        {
            "references": [
                {"kind": "descriptor", "name": "Narrator"},
                {"kind": "descriptor", "name": "Story"},
            ]
        },
    )

    assert not member_by_id(roster(log, sid), extracted_id).is_active
    assert member_by_id(roster(log, sid), world_id).is_active
    assert [e.payload["member_id"] for e in _events(log, sid, CAST_RETIRE_EVENT)] == [
        extracted_id
    ]


def test_references_absent_files_proposals_from_the_name_set(build_log, cast_proposals):
    """The pre-W8.6 path keeps its payload handling but not its admission policy.

    Same names, same order — every surface ``bts_name_set`` yields goes through
    the ONE user-gated door, so the log gains no cast rows and the proposals
    carry the tracker's own spelling.
    """
    sid = "chat"
    result = {
        "state": {"Zahra": {"physical": {"hp": "80"}}, "Kesh": {"mood": "wary"}},
        "venue": {"Zahra": {"venue": "gate"}},
        "off_screen": [{"name": "Moses", "location": "keep"}],
    }

    log = build_log()
    _seed_card(log, sid, ARIA)
    ensure_roster(log, sid)
    apply_bts_cast_proposals(log, sid, result)

    assert [m.display_name for m in roster(log, sid)] == [ARIA]
    assert _admitted_names(log, sid) == [ARIA]  # the seed, nobody else
    assert [name for (_sid, name, _aliases, _origin) in cast_proposals] == list(
        bts_name_set(result)
    )
    assert set(bts_name_set(result)) == {"Zahra", "Kesh", "Moses"}
    # A legacy result with no references applies nothing typed.
    assert apply_bts_cast_proposals(log, sid, result) == []


def test_empty_references_list_is_the_legacy_path(build_log, cast_proposals):
    log = build_log()
    sid = "chat"
    _seed_card(log, sid, ARIA)
    ensure_roster(log, sid)
    apply_bts_cast_proposals(log, sid, {"state": {"Kesh": {}}, "references": []})
    assert [m.display_name for m in roster(log, sid)] == [ARIA]
    assert cast_proposals == [(sid, "Kesh", [], "bts")]


# -- the node path: world-state payload byte-compatibility + time -----------


class FakeWorldState:
    def __init__(self, result):
        self.result = result

    async def __call__(self, user_msg, narrative, prior_state=None, *, checkpoint=False):
        return self.result


def _state(**overrides):
    state = {
        "session_id": "s1",
        "user_content": "I speak",
        "narrative_parts": ["Zahra speaks."],
        "dsl": "",
        "segments": [{"kind": "prose", "text": "Zahra speaks."}],
        "facts": "time: 09:15",
        "action_class": "dialogue",
    }
    state.update(overrides)
    return state


def test_the_node_feeds_the_cast_door_from_the_mention_set(log, cast_proposals):
    """W9.29 §6/W4: the door's input is the turn's MENTION SET.

    FLIPPED: this test used to assert the compat ``world_state`` payload row and
    that its ``references[].rename_to`` reached the owner. Both are retired — the
    payload row is gone (§6: the compat render retires in W4) and a rename arrives
    as a ``rename_being`` ACTION (which patches the member in-loop) instead of a
    report-path channel. What survives is the door itself: every being name the
    turn's actions dealt with is filed for the owner, and the retired payload's own
    name set is no longer a feed.
    """
    node = make_post_turn_node(
        extractor=None,
        log=log,
        world_state=FakeWorldState(
            {
                "kind": "delta",
                "state": {"Moody": {"emotional": {"mood": "wary"}}},
                "deltas": {"loc": "gate", "time": "23:59"},
                "venue": {"Moody": {"venue": "gate"}},
                # The runner's ledger: the beings its write actions resolved.
                "mentioned": ["Woman"],
            }
        ),
    )
    run(node(_state()))

    assert [
        e for e in log.get_events("s1") if e.payload.get("event") == "world_state"
    ] == []
    delta = [e for e in log.get_events("s1") if e.type == "state-delta"][0]
    assert delta.payload["loc"] == "gate"
    # The clock's time, never the model's.
    assert delta.payload["time"] == "09:15"
    # The mention set reaches the cast door — nobody is admitted (L4: user-gated).
    assert roster(log, "s1") == []
    assert cast_proposals == [("s1", "Woman", [], "bts")]


def test_a_retired_payloads_name_set_no_longer_feeds_the_cast_door(log, cast_proposals):
    """FLIPPED: the payload's ``state``/``venue`` headers are not a feed any more.

    They were the compat render's contribution to the door (Review R1). With the
    render retired the ONLY feed is the turn's mention set, so a legacy-shaped
    result that mentions nobody files nothing — silently admitting whatever a
    payload happened to header would be exactly the parallel identity path §6
    closes.
    """
    node = make_post_turn_node(
        extractor=None,
        log=log,
        world_state=FakeWorldState(
            {
                "kind": "delta",
                "state": {"Kesh": {"physical": {"hp": "40"}}},
                "deltas": {"loc": "keep"},
                "venue": {"Kesh": {"venue": "keep"}},
            }
        ),
    )
    run(node(_state()))
    assert roster(log, "s1") == []
    assert cast_proposals == []


# -- the wired engine's BTS profile (the live-enable point) -------------------


def test_build_engine_wires_the_bts_profile_from_the_bound_connection(
    monkeypatch, tmp_path
):
    """The engine's BTS pass is Tier A on the local llama.cpp deployment.

    Pins the wiring decision: the profile the runner resolves per call is the
    capability-resolved one for the interaction's bound agent (forced tool on
    llama.cpp), and it is re-resolved, not frozen at boot.
    """
    import fabulist.wiring as wiring
    from fabulist.connections.registry import Registry

    captured: dict = {}
    monkeypatch.setattr(
        wiring, "build_graph", lambda **kwargs: captured.update(kwargs) or None
    )
    monkeypatch.setattr(wiring, "BtsRunner", lambda *args, **kwargs: captured.update(kwargs))

    log = EventLog(tmp_path / "events.db")
    try:
        wiring.build_engine(
            registry=Registry.from_dict(
                {
                    "connections": {
                        "c": {"base_url": "http://localhost:4444/v1", "model": "your-model-id"}
                    },
                    "agents": {
                        "narrator": "c",
                        "bts": "c",
                        "extractor": "c",
                    },
                }
            ),
            log=log,
            bts_enabled=True,
            attribution_enabled=False,
        )
        profile_fn = captured["profile_fn"]
        assert profile_fn()["tool_choice"] == "required"
    finally:
        log.close()


def test_descriptor_never_resolves_through_the_alias_index(build_log):
    """A descriptor may not absorb a specific person through their aliases.

    Aliases resolve BACKWARD (old text maps to the member it referred to); a
    descriptor resolves FORWARD through a live scene link. ``Zahra`` carrying
    "the woman" as an alias (from an earlier confirmation) must therefore not be
    merged into by a later, genuinely different "the woman" (ADDENDUM 2 A9).
    """
    log = build_log()
    sid = "chat"
    _seed_card(log, sid, ARIA)
    ensure_roster(log, sid)

    zahra_id = admit_member(log, sid, "the woman", provenance="bts", admitted_by="bts")
    from fabulist.state.cast import patch_member

    patch_member(log, sid, zahra_id, display_name="Zahra", reason="she gives her name")
    assert "the woman" in member_by_id(roster(log, sid), zahra_id).aliases

    apply_bts_cast_proposals(
        log,
        sid,
        {
            "state": {"Zahra": {"emotional": {"mood": "wary"}}},
            "references": [{"kind": "descriptor", "name": "the woman", "conflict": True}],
        },
    )
    provisional = [m for m in roster(log, sid) if m.provisional]
    assert len(provisional) == 1
    assert provisional[0].member_id != zahra_id
    assert member_by_id(roster(log, sid), zahra_id).display_name == "Zahra"


# -- W8.11: descriptor rename + the first-sight identity bridge --------------


def test_descriptor_rename_patches_the_member_the_phrase_named(build_log):
    """W8.11 FIX 1: a descriptor entry's ``rename_to`` is honoured.

    The observed live turn (BTS notation path) was
    ``references: descriptor | the woman | rename_to: Mara`` while the identity
    member "Woman" sat in the same scene. The rename loop only walked the
    identity admits, so the proposal was dropped and a provisional "the woman"
    was left beside "Woman". Now the admit returns the member the phrase
    resolved to (here: the identity member, W8.11 FIX 2) and the patch lands on
    it, keeping the id and the colour (ADDENDUM 1 A12 / A2).
    """
    log = build_log()
    sid = "chat"
    _seed_card(log, sid, ARIA)
    ensure_roster(log, sid)

    turn = {
        "state": {"Woman": {"emotional": {"mood": "wary"}}},
        "venue": {"Woman": {"venue": "gate"}},
        "references": [
            {
                "kind": "descriptor",
                "name": "the woman",
                "rename_to": "Mara",
                "reason": "narrator identifies her via reactions",
            }
        ],
    }
    woman_id = admit_member(log, sid, "Woman", provenance="bts", admitted_by="bts")
    color_before = assign_member_color(log, sid, woman_id, "Woman")

    apply_bts_cast_proposals(log, sid, turn)

    member = member_by_id(roster(log, sid), woman_id)
    assert member.member_id == woman_id  # member_id is IMMUTABLE
    assert member.display_name == "Mara"
    assert "Woman" in member.aliases  # the old name is demoted to an alias
    assert list_member_colors(log, sid)[woman_id] == color_before
    patch = _events(log, sid, CAST_PATCH_EVENT)[-1]
    assert patch.payload["member_id"] == woman_id
    assert patch.payload["display_name"] == "Mara"
    assert patch.payload["reason"] == "narrator identifies her via reactions"
    # No stray provisional was minted beside the member the phrase named, and
    # the phrase itself never became an alias.
    assert not [m for m in roster(log, sid) if m.provisional]
    assert len(roster(log, sid)) == 2  # Aria + Mara
    assert _events(log, sid, CAST_LINK_EVENT)[-1].payload["member_id"] == woman_id
    assert not [
        e
        for e in _events(log, sid, CAST_ALIAS_EVENT)
        if e.payload.get("alias") == "the woman"
    ]


def test_descriptor_bridges_when_the_turn_headers_use_the_new_name(build_log):
    """The live W8.11 shape: the rename target already exists as a member.

    A live session had the model switch its own state header from "Woman" to
    "Mara" mid-scene (before the typed path existed), so the roster carried both
    members. The bridge links the phrase to the member it names; the rename then
    collides with the existing "Mara" and is REFUSED, logged ambiguous — never a
    silent merge (merging two existing identities is a different lane).
    """
    log = build_log()
    sid = "chat"
    _seed_card(log, sid, ARIA)
    ensure_roster(log, sid)
    woman_id = admit_member(log, sid, "Woman", provenance="bts", admitted_by="bts")
    mara_id = admit_member(log, sid, "Mara", provenance="bts", admitted_by="bts")

    apply_bts_cast_proposals(
        log,
        sid,
        {
            "state": {"Mara": {"emotional": {"moodlets": "[predatory-2t]"}}},
            "venue": {"Mara": {"venue": "guild"}},
            "references": [
                {
                    "kind": "descriptor",
                    "name": "the woman",
                    "rename_to": "Mara",
                    "reason": "narrator identifies her via reactions",
                }
            ],
        },
    )

    assert [e.payload["member_id"] for e in _events(log, sid, CAST_LINK_EVENT)] == [
        woman_id
    ]
    assert not [m for m in roster(log, sid) if m.provisional]
    assert member_by_id(roster(log, sid), woman_id).display_name == "Woman"
    assert member_by_id(roster(log, sid), mara_id).display_name == "Mara"
    assert not _events(log, sid, CAST_PATCH_EVENT)
    refused = _events(log, sid, CAST_AMBIGUOUS_EVENT)[-1]
    assert refused.payload["proposed"] == "Mara"
    assert refused.payload["candidates"] == [mara_id]

    # The next turn resolves the phrase through the link — no third member.
    apply_bts_cast_proposals(
        log,
        sid,
        {
            "state": {"Woman": {"emotional": {"mood": "wary"}}},
            "references": [{"kind": "descriptor", "name": "the woman"}],
        },
    )
    assert len(_events(log, sid, CAST_LINK_EVENT)) == 1
    assert not [m for m in roster(log, sid) if m.provisional]
    assert len(roster(log, sid)) == 3  # Aria + Woman + Mara


def test_descriptor_rename_of_a_fresh_provisional_files_a_proposal_instead(
    build_log, cast_proposals
):
    """Naming an engine-minted handle would be an admission, so it does not happen.

    Live turn: the door correctly refused to admit "Marisol" and the
    descriptor's ``rename_to`` then renamed the ``clerk`` handle to her one row
    later, putting an unaccepted member in the cast pane. The handle keeps the
    scene state under the phrase the prose used; the NAME is a proposal.
    """
    log = build_log()
    sid = "chat"
    _seed_card(log, sid, ARIA)
    ensure_roster(log, sid)

    apply_bts_cast_proposals(
        log,
        sid,
        {
            "state": {"the woman": {"appearance": {"hair": "red"}}},
            "references": [
                {"kind": "descriptor", "name": "the woman", "rename_to": "Mara"}
            ],
        },
    )
    handle = [m for m in roster(log, sid) if m.admitted_by == "bts"]
    assert len(handle) == 1  # the scene handle, still a handle
    assert handle[0].display_name == "the woman"
    assert handle[0].provisional is True
    assert not _events(log, sid, CAST_PATCH_EVENT)
    assert cast_proposals == [(sid, "Mara", ["the woman"], "bts")]
    assert len(roster(log, sid)) == 2


def test_descriptor_rename_to_its_own_surface_applies_no_patch(build_log):
    log = build_log()
    sid = "chat"
    _seed_card(log, sid, ARIA)
    ensure_roster(log, sid)
    woman_id = admit_member(log, sid, "Woman", provenance="bts", admitted_by="bts")

    apply_bts_cast_proposals(
        log,
        sid,
        {
            "state": {"Woman": {"emotional": {"mood": "wary"}}},
            "references": [
                {"kind": "descriptor", "name": "the woman", "rename_to": "the woman"}
            ],
        },
    )
    assert not _events(log, sid, CAST_PATCH_EVENT)
    assert member_by_id(roster(log, sid), woman_id).display_name == "Woman"


def test_first_sight_descriptor_links_the_present_identity_match(build_log):
    """W8.11 FIX 2: the typed path is not WORSE than the legacy canonical match.

    A reference for "the woman" while "Woman" is present used to resolve through
    the alias index; the descriptor path alone minted a second, provisional
    member beside it. The bridge writes the relation the design allows — a
    ``cast_link``, never an alias (ADDENDUM 2 A9) — and mints nothing.
    """
    log = build_log()
    sid = "chat"
    _seed_card(log, sid, ARIA)
    ensure_roster(log, sid)
    woman_id = admit_member(log, sid, "Woman", provenance="bts", admitted_by="bts")
    color_before = assign_member_color(log, sid, woman_id, "Woman")
    admits_before = len(_events(log, sid, CAST_ADMIT_EVENT))

    apply_bts_cast_proposals(
        log,
        sid,
        {
            "state": {"Woman": {"emotional": {"mood": "wary"}}},
            "venue": {"Woman": {"venue": "gate"}},
            "references": [{"kind": "descriptor", "name": "the woman"}],
        },
    )

    links = _events(log, sid, CAST_LINK_EVENT)
    assert [e.payload["member_id"] for e in links] == [woman_id]
    assert links[0].payload["descriptor_key"] == "woman"
    assert len(_events(log, sid, CAST_ADMIT_EVENT)) == admits_before
    assert not [m for m in roster(log, sid) if m.provisional]
    assert member_by_id(roster(log, sid), woman_id).display_name == "Woman"
    assert list_member_colors(log, sid)[woman_id] == color_before
    # The phrase is a scene handle, never an alias of the member (A9).
    assert not _events(log, sid, CAST_ALIAS_EVENT)


def test_descriptor_splits_when_two_identity_members_share_the_key(build_log):
    """Two canonical-key identity matches -> the ambiguity path, untouched."""
    log = build_log()
    sid = "chat"
    _seed_card(log, sid, ARIA)
    ensure_roster(log, sid)
    woman_id = admit_member(log, sid, "Woman", provenance="bts", admitted_by="bts")
    # A second person whose surface canonicalises identically — the roster state
    # the SPLIT rule exists for (a world seed arrives with its own id).
    log.append_event(
        sid,
        "system",
        "system",
        {
            "event": CAST_ADMIT_EVENT,
            "member_id": "cm_world_woman",
            "name": "woman",
            "provenance": "world",
            "admitted_by": "system",
        },
    )

    apply_bts_cast_proposals(
        log,
        sid,
        {
            "state": {"Woman": {}, "woman": {}},
            "references": [{"kind": "descriptor", "name": "the woman"}],
        },
    )

    provisional = [m for m in roster(log, sid) if m.provisional]
    assert len(provisional) == 1
    assert provisional[0].display_name == "the woman"
    # Neither identity member was captured by the phrase, and nothing merged.
    assert {e.payload["member_id"] for e in _events(log, sid, CAST_LINK_EVENT)} == {
        provisional[0].member_id
    }
    assert not _events(log, sid, CAST_ALIAS_EVENT)
    members = roster(log, sid)
    assert len(members) == 4  # Aria + Woman + woman + the woman
    assert member_by_id(members, woman_id).display_name == "Woman"


def test_first_sight_descriptor_without_an_identity_match_still_mints(build_log):
    """Regression pin: no identity match -> today's provisional mint + link."""
    log = build_log()
    sid = "chat"
    _seed_card(log, sid, ARIA)
    ensure_roster(log, sid)

    apply_bts_cast_proposals(
        log,
        sid,
        {
            "state": {"the woman": {"appearance": {"hair": "red"}}},
            "venue": {"the woman": {"venue": "gate"}},
            "references": [{"kind": "descriptor", "name": "the woman"}],
        },
    )

    provisional = [m for m in roster(log, sid) if m.provisional]
    assert len(provisional) == 1
    assert provisional[0].display_name == "the woman"
    assert provisional[0].descriptor_key == "woman"
    assert "the woman" not in provisional[0].aliases
    link = _events(log, sid, CAST_LINK_EVENT)
    assert [e.payload["member_id"] for e in link] == [provisional[0].member_id]
    # One admit, one link: the two surfaces carrying the same canonical key
    # ("the woman" as the state header, "the woman" as the reference name) are
    # admitted once — a second pass used to mint a duplicate, disambiguated one.
    assert len(_events(log, sid, CAST_ADMIT_EVENT)) == 2  # Aria + the woman
    assert len(link) == 1


def test_the_tail_message_overrides_a_stored_notation_prompt(build_log):
    """§7 (review N5): the protocol + palette ride the TAIL, after the flow.

    A stored preset customisation of the bts surface can therefore never drop
    either of them — the seeded prefix stays byte-stable (AGENTS 6) and the
    action protocol plus the registry arrive last, whatever the prefix says.
    """
    caller = _StructuredCaller([[{"type": "content", "delta": "recorded"}]])
    runner = BtsRunner(
        caller, log=build_log(), profile_fn=lambda: {"tools": [bts_tool()], "tool_choice": "required"}
    )
    run(runner("hello", "Zahra narrows her eyes."))
    last = caller.calls[0][-1]
    assert last["role"] == "system"
    assert last["content"].startswith(BTS_ACTION_PROTOCOL)
    assert ENTITY_PALETTE_DIRECTIVE in last["content"]
    # The seeded system prompt above it is untouched by the swap.
    assert caller.calls[0][0]["content"] == BTS_SYSTEM_PROMPT


def test_legacy_path_skips_role_words_and_pronouns(cast_proposals):
    """W9.16: the no-references bridge must not propose 'Narrative' or 'you'."""
    from fabulist.state import cast as cast_module
    from fabulist.state.bts import BtsResult, apply_bts_cast_proposals

    log = EventLog(":memory:")
    for index in range(7):
        cast_module.admit_member(log, "s-legacy", f"NPC {index}", provenance="bts", admitted_by="bts")
    result = {"kind": "delta", "state": {"Narrative": {}, "you": {}, "Bram": {}}, "references": []}
    apply_bts_cast_proposals(log, "s-legacy", result)
    names = {member.display_name for member in cast_module.roster(log, "s-legacy") if member.is_active}
    assert "Narrative" not in names
    assert "you" not in names
    assert "Bram" not in names
    assert [name for (_sid, name, _aliases, _origin) in cast_proposals] == ["Bram"]
    log.close()


def test_legacy_path_enforces_the_cast_cap():
    """W9.16: the cap used to be skipped on the early-return (no references)."""
    from fabulist.state import cast as cast_module
    from fabulist.state.bts import BtsResult, apply_bts_cast_proposals

    log = EventLog(":memory:")
    for index in range(9):
        cast_module.admit_member(log, "s-legacy", f"NPC {index}", provenance="bts", admitted_by="bts")
    result = {"kind": "delta", "state": {"Bram": {}}, "references": []}
    apply_bts_cast_proposals(log, "s-legacy", result)
    active = [member for member in cast_module.roster(log, "s-legacy") if member.is_active]
    assert len(active) <= 8, [m.display_name for m in active]
    log.close()


# -- the live RCA: admit hygiene ---------------------------------------------
#
# The fork's five BTS turns are reproduced from the log: the same card roster
# ("Hobbs", "Tessa"), the same player persona ("Wanderer"), the same drifted
# surfaces, the same attribution rows naming the card character on every turn.
# What the engine did with them is what four rules now stop:
#
# * the drifted header "TheHeron" minted a member of its own BESIDE the
#   descriptor reference ("the heron") for the same person;
# * the descriptor minted a fresh provisional every turn — "the heron",
#   "(2)", "(3)", "(4)", and the same chain for "the new guy" — re-pointing the
#   ``cast_link`` at each new member (seq 27 -> 52 -> 80 -> 111);
# * the player persona "Wanderer" was admitted as cast;
# * the cap's recency clock (world-state presence only) retired BOTH card
#   characters mid-scene ("Hobbs" at seq 86, "Tessa" at seq 117).

LIVE_TURNS: tuple[dict, ...] = (
    {  # seq 23 — header spelled run-together, reference spelled as the phrase
        "state": {
            "TheHeron": {"appearance": {"features": "grey wing, one clouded eye"}},
            "Wanderer": {"emotional": {"mood": "neutral"}},
        },
        "venue": {
            "TheHeron": {"venue": "Hobbs & Sons", "area": "counter"},
            "Wanderer": {"venue": "Hobbs & Sons", "area": "entrance"},
        },
        "references": [
            {"kind": "identity", "name": "Wanderer", "reason": "arriving for first day"},
            {
                "kind": "descriptor",
                "name": "the heron",
                "reason": "shopkeeper behind the counter",
            },
        ],
    },
    {  # seq 50 — the model confirms the phrase into the name, and repeats both
        "state": {
            "TheHeron": {"emotional": {"mood": "weary"}},
            "Wanderer": {"emotional": {"mood": "neutral"}},
        },
        "venue": {
            "TheHeron": {"venue": "Hobbs & Sons", "area": "counter"},
            "Wanderer": {"venue": "Hobbs & Sons", "area": "entrance"},
        },
        "references": [
            {"kind": "identity", "name": "TheHeron", "reason": "the shop owner"},
            {"kind": "identity", "name": "Wanderer", "reason": "the new employee"},
            {
                "kind": "descriptor",
                "name": "the heron",
                "rename_to": "TheHeron",
                "reason": "refers to the shop owner",
            },
            {
                "kind": "descriptor",
                "name": "the new guy",
                "rename_to": "Wanderer",
                "reason": "refers to the employee",
            },
        ],
    },
    {  # seq 78 — the model echoes the engine's own "(2)" suffix back at it
        "state": {
            "TheHeron": {"physical": {"pose": "hunched"}},
            "Wanderer": {"emotional": {"mood": "neutral"}},
        },
        "venue": {
            "TheHeron": {"venue": "Hobbs & Sons", "area": "counter"},
            "Wanderer": {"venue": "Hobbs & Sons", "area": "entrance"},
        },
        "references": [
            {
                "kind": "descriptor",
                "name": "the heron",
                "rename_to": "TheHeron",
                "reason": "referent",
            },
            {
                "kind": "descriptor",
                "name": "the heron (2)",
                "rename_to": "TheHeron",
                "reason": "referent",
            },
            {
                "kind": "descriptor",
                "name": "the new guy",
                "rename_to": "Wanderer",
                "reason": "referent",
            },
        ],
    },
    {  # seq 109 — the model finally names the card character
        "state": {
            "TheHeron": {"emotional": {"mood": "amused"}},
            "Wanderer": {"emotional": {"mood": "alert"}},
        },
        "venue": {
            "TheHeron": {"venue": "Hobbs & Sons", "area": "counter"},
            "Wanderer": {"venue": "Hobbs & Sons", "area": "entrance"},
        },
        "references": [
            {
                "kind": "identity",
                "name": "TheHeron",
                "rename_to": "Hobbs",
                "reason": "character card mapping",
            },
            {"kind": "identity", "name": "Wanderer", "rename_to": "Wanderer"},
            {
                "kind": "descriptor",
                "name": "the heron",
                "rename_to": "Hobbs",
                "reason": "referent",
            },
            {
                "kind": "descriptor",
                "name": "the new guy",
                "rename_to": "Wanderer",
                "reason": "referent",
            },
        ],
    },
    {  # seq 139 — and then inverts it
        "state": {"TheHeron": {}, "Wanderer": {}},
        "venue": {
            "TheHeron": {"venue": "Hobbs & Sons", "area": "counter"},
            "Wanderer": {"venue": "Hobbs & Sons", "area": "entrance"},
        },
        "references": [
            {"kind": "identity", "name": "Hobbs", "rename_to": "TheHeron"},
        ],
    },
)


def _live_card_chat(log: EventLog, sid: str = "chat") -> dict[str, str]:
    """The live session's seed: two cards, and the Wanderer player persona."""
    from fabulist.personas.registry import append_persona, assign_persona

    _seed_card(log, sid, "Hobbs")
    _seed_card(log, sid, "Tessa")
    assign_persona(log, sid, append_persona(log, {"name": "Wanderer"}))
    return {member.display_name: member.member_id for member in ensure_roster(log, sid)}


def _live_turn(
    log: EventLog,
    sid: str,
    turn: dict,
    *,
    speaker: str,
    speaker_id: str,
) -> None:
    """Append one turn's evidence, then run the BTS apply path on its payload.

    The persisted ``world_state`` payload is exactly what the node writes — the
    ``references`` field is consumed engine-side and never stored (W8.6) — so the
    log gets the persisted slice while the apply call gets the model payload.
    """
    log.append_event(
        sid,
        "narrative",
        "narrator",
        {"prose": "The shopkeeper squints at the counter.", "segments": []},
    )
    log.append_event(
        sid,
        "system",
        "bts",
        {
            "event": "world_state",
            "kind": "delta",
            "state": turn["state"],
            "venue": turn["venue"],
            "off_screen": [],
            "threads": [],
            "tension": {},
            "raw": "",
        },
    )
    log.append_event(
        sid,
        "system",
        "attribution",
        {
            "event": "attribution",
            "attributions": [
                {"speaker": speaker, "quote": "Fast, ain't ya?", "speaker_id": speaker_id}
            ],
        },
    )
    apply_bts_cast_proposals(
        log,
        sid,
        {
            "kind": "delta",
            "state": turn["state"],
            "venue": turn["venue"],
            "off_screen": [],
            "references": turn["references"],
        },
    )


def test_live_rca_chain_is_one_member_per_entity_and_never_the_player(build_log):
    """Invariants 1+2: re-link, no "(N)" churn, and the player is never cast."""
    import re

    from fabulist.state.cast import canonical_key

    log = build_log()
    sid = "chat"
    seeds = _live_card_chat(log, sid)
    entity_ids: list[str] = []
    for turn in LIVE_TURNS:
        _live_turn(log, sid, turn, speaker="Hobbs", speaker_id=seeds["Hobbs"])
        members = roster(log, sid)
        created = [member for member in members if member.admitted_by == "bts"]
        # ONE member for the scene entity, on every turn of the chain.
        assert len(created) == 1, [member.display_name for member in members]
        entity_ids.append(created[0].member_id)
        assert not [m for m in members if re.search(r"\(\d+\)$", m.display_name)]
        # The player persona never enters the cast, under any spelling.
        assert not [m for m in members if canonical_key(m.display_name) == "zero shot"]
    # The engine re-links the same member every turn instead of re-minting.
    assert len(set(entity_ids)) == 1
    # The card members are untouched and still active; the descriptor surfaces
    # are not aliases of them (ADDENDUM 2 A9: a phrase is never an alias).
    active = {member.display_name for member in roster(log, sid) if member.is_active}
    assert {"Hobbs", "Tessa"} <= active
    assert not [e for e in _events(log, sid, CAST_RETIRE_EVENT)]
    # Every spelling the model used for the entity still lands on ONE member.
    from fabulist.state.cast import member_by_name

    entity_member = member_by_id(roster(log, sid), entity_ids[0])
    for surface in ("the heron", "TheHeron"):
        assert member_by_name(roster(log, sid), surface).member_id == entity_member.member_id
    assert {canonical_key(s) for s in entity_member.surface_forms()} == {"heron"}
    assert "the heron" not in member_by_id(roster(log, sid), seeds["Hobbs"]).aliases


def test_live_rca_cap_keeps_the_card_members_and_retires_play_not_the_seed(build_log):
    """Invariants 3+4: the cap spends its budget on what play invented."""
    from fabulist.state.cast import DEFAULT_CAST_CAP

    log = build_log()
    sid = "chat"
    seeds = _live_card_chat(log, sid)
    junk = [
        admit_member(log, sid, f"Scrap {index}", provenance="bts", admitted_by="bts")
        for index in range(6)
    ]
    for turn in LIVE_TURNS:
        _live_turn(log, sid, turn, speaker="Hobbs", speaker_id=seeds["Hobbs"])

    members = roster(log, sid)
    active = [member for member in members if member.is_active]
    assert len(active) <= DEFAULT_CAST_CAP, [m.display_name for m in active]
    active_ids = {member.member_id for member in active}
    # The two CARD characters survive — the live session retired both.
    assert seeds["Hobbs"] in active_ids and seeds["Tessa"] in active_ids
    # The scene entity survives too: it is present in the world state every turn.
    entity = [m for m in active if m.admitted_by == "bts" and m.member_id not in junk]
    assert len(entity) == 1
    # The budget is paid by the members nothing ever named (never by the seed).
    retired = [e.payload["member_id"] for e in _events(log, sid, CAST_RETIRE_EVENT)]
    assert retired and set(retired) <= set(junk)


def test_cap_clock_counts_attribution_rows_not_just_world_state_presence(build_log):
    """Invariant 4: the speaker rows are the strongest recency evidence there is.

    The clock used to be the presence walk alone, and the presence walk only sees
    BTS headers — so a character the model stopped re-headering ranked as never
    seen while the turn's own attribution rows named them with a ``member_id``.
    """
    from fabulist.state.bts import _enforce_cap
    from fabulist.state.cast import member_by_id

    log = build_log()
    sid = "chat"
    _seed_card(log, sid, ARIA)
    ensure_roster(log, sid)
    silent = admit_member(log, sid, "Silent", provenance="bts", admitted_by="bts")
    speaker = admit_member(log, sid, "Speaker", provenance="bts", admitted_by="bts")
    junk = [
        admit_member(log, sid, f"Scrap {index}", provenance="bts", admitted_by="bts")
        for index in range(6)
    ]
    log.append_event(
        sid, "narrative", "narrator", {"prose": "Fast, ain't ya?", "segments": []}
    )
    log.append_event(
        sid,
        "system",
        "attribution",
        {
            "event": "attribution",
            "attributions": [
                {"speaker": "Speaker", "quote": "Fast, ain't ya?", "speaker_id": speaker}
            ],
        },
    )

    _enforce_cap(log, sid)

    members = roster(log, sid)
    assert member_by_id(members, speaker).is_active
    # The member with no evidence at all is the one that pays.
    assert not member_by_id(members, silent).is_active
    retired = [e.payload["member_id"] for e in _events(log, sid, CAST_RETIRE_EVENT)]
    assert retired == [silent]
    assert set(retired) & set(junk) == set()


def test_polluted_descriptor_churn_self_heals_to_the_current_binding(build_log):
    """A roster the OLD churn already polluted re-links, it does not mint "(4)".

    A live session's log holds exactly this shape: three provisional
    members the engine minted for "the heron" on consecutive turns, each
    linked to the same ``descriptor_key`` (seq 27 -> 52 -> 80). The newest link is
    where the phrase currently points, so a drifted header re-links to it — the
    ADDENDUM 3 rule 3 SPLIT stays for the case it exists for (two candidates the
    turn itself reports present, i.e. two names, two people).
    """
    log = build_log()
    sid = "chat"
    _seed_card(log, sid, ARIA)
    ensure_roster(log, sid)
    churn: list[str] = []
    for index in range(3):
        # Exactly how the old churn built it: the SAME phrase, a presence set that
        # did not contain the member it minted last turn, so each turn got a
        # disambiguated copy and the link for "heron" was re-pointed.
        churn.append(
            admit_member(
                log,
                sid,
                "the heron",
                provenance="bts",
                admitted_by="bts",
                reference_kind=REFERENCE_DESCRIPTOR,
                presence=[],
            )
        )
        log.append_event(
            sid, "narrative", "narrator", {"prose": f"turn {index}", "segments": []}
        )
    # The drifted header names the entity under a spelling no member carries; the
    # reference is the phrase the engine owns three provisionals for.
    apply_bts_cast_proposals(
        log,
        sid,
        {
            "state": {"TheHeron": {"emotional": {"mood": "weary"}}},
            "venue": {"TheHeron": {"venue": "Hobbs & Sons"}},
            "references": [{"kind": "descriptor", "name": "the heron"}],
        },
    )
    assert [
        member.member_id for member in roster(log, sid) if member.admitted_by == "bts"
    ] == churn
    assert not _events(log, sid, CAST_AMBIGUOUS_EVENT)

    # Two candidates the turn ITSELF reports present still split (rule 3).
    apply_bts_cast_proposals(
        log,
        sid,
        {
            "state": {"the heron": {}, "the heron (2)": {}},
            "venue": {"the heron": {"venue": "gate"}},
            "references": [{"kind": "descriptor", "name": "the heron"}],
        },
    )
    assert len([m for m in roster(log, sid) if m.admitted_by == "bts"]) == 4
    assert _events(log, sid, CAST_AMBIGUOUS_EVENT)
