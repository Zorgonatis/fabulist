"""BTS world-state interaction (PLAN D21 / Wave 6 lane W6-A).

Covers the notation parser (block -> structured state + flat fold keys), the
delta/checkpoint cadence, the declarative narrator renderer, the ``bts`` module
flow shape, and the :class:`BtsRunner` call/repair/fail contract against a fake
``ModelCaller``.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from fabulist.prompts.modules import DEFAULT_FLOWS, INTERACTIONS, compile_flow
from fabulist.prompts.entity_palette import ENTITY_PALETTE_DIRECTIVE
from fabulist.state.bts import (
    BTS_ACTION_PROTOCOL,
    BTS_CHECKPOINT_TURNS,
    BTS_STRUCTURED_REPAIR_INSTRUCTION,
    BTS_SYSTEM_PROMPT,
    BTS_TOOL_NAME,
    FOLDED_STATE_TOOL,
    RECENT_DELTAS_TOOL,
    ROSTER_TOOL,
    BtsExtractionError,
    BtsParseError,
    BtsResult,
    BtsRunner,
    apply_bts_cast_proposals,
    build_bts_messages,
    bts_loop_tools,
    bts_tool,
    checkpoint_requested,
    fold_flat,
    latest_world_state,
    parse_bts,
    render_bts_section,
    session_bts_block,
    should_checkpoint,
    turns_since_checkpoint,
    user_as_actor,
)
from fabulist.state.entity_tools import (
    ENTITY_BUDGET_ERROR,
    ESTABLISH_LOCATION_TOOL,
    ESTABLISH_THREAD_TOOL,
    MOVE_BEING_TOOL,
    SET_ATTR_TOOL,
    WRITE_TOOL_NAMES,
)

# A compact but notation-complete delta block.
DELTA_BLOCK = """[BTS]
:: Elara ::
  hp: 78→62 | wound: +gash(L.thigh, moderate, bleeding)
  fatigue: 4→6 | mood: focused ↑
  moodlets: [+battle-haze-2t, pain-3t]
  @courtyard → @gallery_wing
  weather: light rain | tone: tense
  carrying: longsword, 1 potion
  rel→Kael: trust 52→58 (saved her from flanking skeleton)

:: User ::
  @gallery_wing > north arcade

:: OFF ::
  Maren | @infirmary(treating wounded) | mood: harried ↑ | finishes in ~3t
  Thane | @gate(standing guard) | mood: tense | heard distant fighting

  Threads: ~curse_investigation(40%) | +mayor_secret(letter found) | -goblin_threat(resolved)
  Tension: 7/10 | irony: mayor=necromancer
[/BTS]"""

CHECKPOINT_BLOCK = """[BTS|CP]
:: Elara ::
  HP: 62/100 | Fatigue: 6/10
  @mayor's_office
  rel→User: friend 65 | trust 72
[/BTS]"""


def run(coro):
    return asyncio.run(coro)


class FakeCaller:
    """ModelCaller: yields each queued reply as one content delta; records calls."""

    def __init__(self, *replies: str) -> None:
        self.replies = list(replies)
        self.calls: list[list[dict]] = []

    async def __call__(self, messages, stream=True):
        self.calls.append(messages)
        reply = self.replies.pop(0) if self.replies else "[BTS]\n[/BTS]"
        yield {"type": "content", "delta": reply}


# --- parser -----------------------------------------------------------------


def test_parse_delta_block_structured_state_and_fold_keys():
    result = parse_bts(DELTA_BLOCK)
    assert isinstance(result, BtsResult)
    assert result.kind == "delta"
    # Flat fold keys the existing consumers read (D21/B4).
    assert result.deltas == {
        "loc": "gallery_wing",
        "weather": "light rain",
        "tone": "tense",
    }
    elara = result.state["Elara"]
    assert elara["physical"]["hp"] == "78→62"
    assert elara["emotional"]["moodlets"] == "[+battle-haze-2t, pain-3t]"
    assert elara["inventory"]["carrying"] == "longsword, 1 potion"
    assert elara["relationships"]["rel→Kael"] == "trust 52→58 (saved her from flanking skeleton)"
    # Environment keys are turn-level, not per-character attributes.
    assert all("weather" not in inner for inner in elara.values())
    # Venue map carries @venue > area for present characters.
    assert result.venue["Elara"] == {"venue": "gallery_wing"}
    assert result.venue["User"] == {"venue": "gallery_wing", "area": "north arcade"}
    # Off-screen sim.
    assert result.off_screen[0]["name"] == "Maren"
    assert result.off_screen[0]["location"] == "infirmary"
    assert result.off_screen[0]["activity"] == "treating wounded"
    # Threads + tension.
    assert {t["status"] for t in result.threads} == {"active", "new", "resolved"}
    assert result.tension == {"level": 7, "irony": "mayor=necromancer"}


def test_parse_checkpoint_block_kind_and_relationship_fragments():
    result = parse_bts(CHECKPOINT_BLOCK)
    assert result.kind == "checkpoint"
    rel = result.state["Elara"]["relationships"]["rel→User"]
    # The bare "| trust 72" fragment merges onto its parent relationship.
    assert rel == "friend 65 | trust 72"


def test_parse_missing_block_raises():
    with pytest.raises(BtsParseError):
        parse_bts("no block here")


def test_parse_bare_thread_drift_not_folded_as_inventory():
    # Live-observed: the model wrote a label-less thread ``+name | detail``.
    r = parse_bts(
        "[BTS]\n:: Elara ::\n@docks\n+letter_hunt | sold to grey cloak\n-3g | +lantern\n[/BTS]"
    )
    assert r.threads == [
        {"status": "new", "name": "letter_hunt", "detail": "sold to grey cloak"}
    ]
    assert r.deltas["inv"] == "-3g +lantern"


def test_parse_tolerates_fenced_block():
    fenced = "```\n" + DELTA_BLOCK + "\n```"
    assert parse_bts(fenced).kind == "delta"


def test_fold_flat_inventory_signed_tokens_from_block():
    block = "[BTS]\n:: Elara ::\n  -torch(used) | +lantern\n[/BTS]"
    assert parse_bts(block).deltas["inv"] == "-torch +lantern"


def test_fold_flat_explicit_location_attribute():
    flat = fold_flat({}, "loc: night market\nweather: rain")
    assert flat == {"loc": "night market", "weather": "rain"}


def test_fold_flat_never_emits_time():
    # BTS may echo time in prose but the parser must never fold it (D3).
    assert "time" not in fold_flat({}, "time: 14:35\nloc: docks")


# --- forecast (+6h weather outlook) ----------------------------------------


def test_parse_forecast_present_beside_weather():
    block = (
        "[BTS]\n:: Elara ::\n@docks\n"
        "weather: light rain | forecast: heavier toward nightfall | tone: grim\n[/BTS]"
    )
    result = parse_bts(block)
    assert result.forecast == "heavier toward nightfall"
    # Existing behavior untouched: weather still folds; forecast is turn-level,
    # never a per-character attribute nor a flat fold key.
    assert result.deltas["weather"] == "light rain"
    assert "forecast" not in result.deltas
    assert all(
        "forecast" not in attrs
        for categories in result.state.values()
        for attrs in categories.values()
    )


def test_parse_forecast_on_its_own_line_and_case_insensitive():
    block = "[BTS]\n:: Elara ::\n@docks\nweather: clear\nForecast: storm by nightfall\n[/BTS]"
    assert parse_bts(block).forecast == "storm by nightfall"


def test_parse_forecast_tolerates_live_comma_join_drift():
    # Live-observed: the model comma-joined instead of piping. The forecast
    # field stays clean regardless of separator drift.
    block = "[BTS]\n:: PC ::\n@harbor_quarter\nweather: leaden, forecast: storm\n[/BTS]"
    assert parse_bts(block).forecast == "storm"


def test_parse_forecast_absent_defaults_empty():
    # The canonical block carries weather but no forecast: omission is natural.
    assert parse_bts(DELTA_BLOCK).forecast == ""
    assert BtsResult(kind="delta").forecast == ""


def test_parse_forecast_malformed_or_multiline_yields_empty():
    # Empty value.
    assert parse_bts("[BTS]\n:: Elara ::\n@docks\nforecast:\n[/BTS]").forecast == ""
    # Whitespace-only value.
    assert parse_bts("[BTS]\n:: Elara ::\n@docks\nforecast:   \n[/BTS]").forecast == ""
    # Value continued onto the next line (multi-line garbage) never bleeds in.
    assert (
        parse_bts("[BTS]\n:: Elara ::\n@docks\nforecast:\nheavy rain by evening\n[/BTS]").forecast == ""
    )
    # A bare pipe-terminated empty slot.
    assert parse_bts("[BTS]\n:: Elara ::\n@docks\nweather: clear | forecast: | tone: calm\n[/BTS]").forecast == ""
    # Malformed forecast never breaks the rest of the parse.
    result = parse_bts("[BTS]\n:: Elara ::\n@docks\nforecast:\ntension: 4/10\n[/BTS]")
    assert result.forecast == ""
    assert result.tension == {"level": 4}


# --- checkpoint cadence -----------------------------------------------------


def test_should_checkpoint_threshold_scene_and_request():
    assert should_checkpoint(turns_since_checkpoint=BTS_CHECKPOINT_TURNS - 1) is False
    assert should_checkpoint(turns_since_checkpoint=BTS_CHECKPOINT_TURNS) is True
    assert should_checkpoint(turns_since_checkpoint=0, scene_changed=True) is True
    assert should_checkpoint(turns_since_checkpoint=0, requested=True) is True


def test_checkpoint_requested_matches_ooc_directive():
    assert checkpoint_requested("please [OOC: BTS checkpoint] now") is True
    assert checkpoint_requested("[ooc:bts checkpoint]") is True
    assert checkpoint_requested("just talking") is False


# --- narrator renderer ------------------------------------------------------


def test_render_section_is_declarative_and_has_priority_content():
    result = parse_bts(DELTA_BLOCK)
    section = render_bts_section(
        {
            "state": result.state,
            "venue": result.venue,
            "threads": result.threads,
            "tension": result.tension,
            "off_screen": result.off_screen,
        }
    )
    assert section is not None
    assert "@gallery_wing" in section
    assert "Kael" in section
    assert "tension is 7/10" in section
    assert "Off-screen" in section
    # Declarative only — never an imperative to the narrator.
    assert not section.lower().startswith("emit")


def test_render_section_budget_truncates():
    state = {"A": {"physical": {f"w{i}": "x" * 50 for i in range(80)}}}
    section = render_bts_section({"state": state}, budget=200)
    assert section is not None
    assert len(section) <= 240
    assert "truncated" in section


# --- module flow ------------------------------------------------------------


def test_bts_interaction_registered_with_flow():
    assert "bts" in INTERACTIONS
    assert [m.type for m in DEFAULT_FLOWS["bts"]] == ["system", "payload"]
    messages = compile_flow(
        "bts",
        DEFAULT_FLOWS["bts"],
        {"system": BTS_SYSTEM_PROMPT, "payload": "P", "variables": {}},
    )
    assert messages == [
        {"role": "system", "content": BTS_SYSTEM_PROMPT},
        {"role": "user", "content": "P"},
    ]


def test_build_bts_messages_carries_mode_and_prior_state():
    messages = build_bts_messages("hi", "prose", {"Elara": {"physical": {"hp": "5"}}}, checkpoint=True)
    assert messages[0]["content"] == BTS_SYSTEM_PROMPT
    payload = messages[1]["content"]
    assert "MODE: checkpoint" in payload
    assert ":: Elara ::" in payload
    assert "hp: 5" in payload


def test_bts_system_prompt_is_tight_and_notation_free():
    """W9.29 §6/G7: distilled, and it teaches the ACTION protocol, not the DSL.

    The retired text carried the ``[BTS]`` notation grammar (envelope, character
    headers, the P1-P4 compression ladder). A prompt that teaches a format the
    engine no longer parses is worse than no prompt: the write path is tool
    calls now, and the notation must not come back through the seed.
    """
    assert len(BTS_SYSTEM_PROMPT) < 6000
    assert "[BTS]" not in BTS_SYSTEM_PROMPT
    assert ":: Name ::" not in BTS_SYSTEM_PROMPT
    assert "P1" not in BTS_SYSTEM_PROMPT
    # The names contract and the action checklist are what it does teach.
    assert "## References" in BTS_SYSTEM_PROMPT
    assert "rename_being" in BTS_SYSTEM_PROMPT
    assert "## Actions (checklist)" in BTS_SYSTEM_PROMPT
    assert "## Worked example" in BTS_SYSTEM_PROMPT
    assert "Ten write actions per turn" in BTS_SYSTEM_PROMPT


# --- runner -----------------------------------------------------------------


def test_runner_never_parses_a_notation_reply_into_state():
    """§11.2: the notation WRITE path is retired; the parser is a read-side shim.

    The runner used to end at ``parse_bts`` — a well-formed block became world
    state. Requirement 9 deleted that path: on a connection with no function
    calling the pass records nothing at all (the node logs ``world_state_failed``),
    and even a perfectly formatted block is not state. ``parse_bts`` itself stays
    covered by the parser tests above (the sidebar's legacy reads still use it).
    """
    for reply in (DELTA_BLOCK, CHECKPOINT_BLOCK, "garbage, no block"):
        caller = FakeCaller(reply)
        with pytest.raises(BtsExtractionError) as excinfo:
            run(BtsRunner(caller)("u", "prose"))
        assert excinfo.value.stage == "tier_d"
        assert caller.calls == []


# --- log-backed reads -------------------------------------------------------


def _seed_world_state(log, sid, payload, *, kind="delta"):
    log.append_event(
        sid,
        "system",
        "bts",
        {"event": "world_state", "kind": kind, "state": payload, "venue": {}, "threads": [], "tension": {}},
    )


def test_latest_world_state_and_section_from_log(log):
    _seed_world_state(
        log,
        "s1",
        {"Elara": {"physical": {"hp": "62"}}},
    )
    latest = latest_world_state(log, "s1")
    assert latest["state"]["Elara"]["physical"]["hp"] == "62"
    assert session_bts_block(log, "s1") is not None
    assert session_bts_block(log, "empty") is None


def test_world_state_and_failure_payloads_pass_schema_gate():
    from fabulist.log.schema_gate import validate_event

    assert validate_event(
        "system", {"event": "world_state", "kind": "delta", "state": {"A": {}}}
    ) == []
    assert validate_event(
        "system", {"event": "world_state_failed", "stage": "parse", "error": "x"}
    ) == []
    # A missing required key is rejected.
    assert validate_event("system", {"event": "world_state", "kind": "delta"}) != []


def test_turns_since_checkpoint_counts_after_last_checkpoint(log):
    log.append_event("s1", "narrative", "narrator", {"prose": "a", "segments": []})
    assert turns_since_checkpoint(log, "s1") == 1
    _seed_world_state(log, "s1", {}, kind="checkpoint")
    log.append_event("s1", "narrative", "narrator", {"prose": "b", "segments": []})
    assert turns_since_checkpoint(log, "s1") == 1


# --- User-as-Actor doors (W9.22 §2) -----------------------------------------
#
# The player is REFUSED at the admit door by default (the W9.21 hygiene rule).
# The per-session ``user_as_actor`` setting (default OFF, folded by the
# chat-config router — the row these tests append is exactly what its PATCH
# writes) is the one key that turns the player into an ACTOR: admitted as the ONE
# ``user`` member, never retired as a mistake. OFF must stay byte-identical to
# today, so every ON test below is paired with the OFF behaviour it changes.

USER_AS_ACTOR_ON = {"event": "chat_user_as_actor", "user_as_actor": True}
USER_AS_ACTOR_OFF = {"event": "chat_user_as_actor", "user_as_actor": False}


def _set_user_as_actor(log, sid, enabled, *, value=None):
    """Append the setting row the chat-config PATCH writes (the one contract shape)."""
    log.append_event(
        sid, "system", "user", value if value is not None else (
            USER_AS_ACTOR_ON if enabled else USER_AS_ACTOR_OFF
        )
    )


def _identity_turn(*names):
    """A BTS payload carrying one ``identity`` reference per name (+ its state)."""
    return {
        "kind": "delta",
        "state": {name: {"emotional": {"mood": "watchful"}} for name in names},
        "venue": {name: {"venue": "tavern"} for name in names},
        "off_screen": [],
        "threads": [],
        "tension": {},
        "raw": "",
        "references": [{"kind": "identity", "name": name} for name in names],
    }


def _members(log, sid="s1"):
    from fabulist.state.cast import roster

    return {member.display_name: member for member in roster(log, sid)}


@pytest.fixture
def cast_proposals(monkeypatch):
    """Capture the ``cast`` proposals the BTS door files (Lane L4's emitter).

    Cast admission is USER-GATED, so an identity the owner has not accepted is a
    proposal — that is the door's output for every name these tests report.
    """
    filed: list[tuple[str, str, list[str], str]] = []

    def _emit(log, session_id, name, aliases, origin):
        filed.append((session_id, name, list(aliases), origin))

    from fabulist.state import proposals as proposal_store

    monkeypatch.setattr(proposal_store, "emit_cast_proposal", _emit, raising=False)
    return filed


def test_the_player_is_refused_at_the_admit_door_when_user_as_actor_is_off(
    log, cast_proposals
):
    """Default OFF keeps the W9.21 rule: a header spelling the player is not a person."""
    apply_bts_cast_proposals(log, "s1", _identity_turn("User", "Elara"))

    members = _members(log)
    assert "User" not in members
    # The non-player person is DETECTED (a proposal), never admitted: admission
    # is the owner's decision now.
    assert "Elara" not in members
    assert cast_proposals == [("s1", "Elara", [], "bts")]


def test_user_as_actor_off_tombstones_the_players_engine_minted_member(log):
    """OFF is also today's CLEANUP: a junk player member is retired at the door."""
    from fabulist.state.cast import admit_member

    junk = admit_member(log, "s1", "User", provenance="bts", admitted_by="bts")
    apply_bts_cast_proposals(log, "s1", _identity_turn("User"))

    assert _members(log)["User"].is_active is False
    assert _members(log)["User"].member_id == junk


def test_user_as_actor_on_admits_the_player_as_one_user_member(log, cast_proposals):
    """ON opens the admit door: the player becomes an ACTOR-tier ``user`` member.

    The setting is the one admission the owner pre-approved; the OTHER people the
    turn reports still go to the proposal queue like everywhere else.
    """
    _set_user_as_actor(log, "s1", True)

    apply_bts_cast_proposals(log, "s1", _identity_turn("User", "Elara"))

    members = _members(log)
    assert members["User"].provenance == "user"
    assert members["User"].is_active is True
    assert "Elara" not in members
    assert cast_proposals == [("s1", "Elara", [], "bts")]


def test_user_as_actor_on_maps_every_player_surface_to_the_same_member(log):
    """ONE user member: ``User`` now and ``Player`` later are the same person.

    The generic player words are synonyms; the setting makes the player one actor,
    so a second spelling must not mint a second member (the persona-less case keys
    the identity on the member already bearing the ``user`` provenance).
    """
    _set_user_as_actor(log, "s1", True)

    apply_bts_cast_proposals(log, "s1", _identity_turn("User"))
    apply_bts_cast_proposals(log, "s1", _identity_turn("Player"))

    user_members = [m for m in _members(log).values() if m.provenance == "user"]
    assert len(user_members) == 1


def test_user_as_actor_on_names_the_user_member_after_the_persona(log):
    """The display name is the PERSONA's, which is what keeps the near-white role tint.

    ``_assign_speaker_colors`` (graph/nodes.py) reserves the near-white for the
    player by matching the persona's name and skips that speaker's own rows, so a
    member carrying a generic ``User`` label would be tinted like an NPC.
    """
    from fabulist.personas.registry import append_persona, assign_persona

    assign_persona(log, "s1", append_persona(log, {"name": "Wanderer"}))
    _set_user_as_actor(log, "s1", True)

    apply_bts_cast_proposals(log, "s1", _identity_turn("User"))

    members = _members(log)
    assert members["Wanderer"].provenance == "user"
    assert "User" not in members


def test_user_as_actor_on_keeps_a_previously_tombstoned_player_member(log):
    """The door re-tiers and resurrects the SAME member instead of minting a twin."""
    from fabulist.state.cast import admit_member

    junk = admit_member(log, "s1", "User", provenance="bts", admitted_by="bts")
    apply_bts_cast_proposals(log, "s1", _identity_turn("User"))  # OFF: tombstoned
    assert _members(log)["User"].is_active is False

    _set_user_as_actor(log, "s1", True)
    apply_bts_cast_proposals(log, "s1", _identity_turn("User"))

    member = _members(log)["User"]
    assert member.member_id == junk
    assert member.is_active is True
    assert member.provenance == "user"


def test_user_as_actor_on_covers_the_legacy_admit_path_too(log):
    """No ``references`` (a weaker transport tier) still reaches the same door."""
    _set_user_as_actor(log, "s1", True)
    legacy = {
        "kind": "delta",
        "state": {"User": {"emotional": {"mood": "calm"}}},
        "venue": {"User": {"venue": "tavern"}},
        "off_screen": [],
        "threads": [],
        "tension": {},
        "raw": "",
    }

    apply_bts_cast_proposals(log, "s1", legacy)

    assert _members(log)["User"].provenance == "user"


def test_a_malformed_user_as_actor_row_never_opens_the_door(log):
    """The safe direction: only a real JSON boolean counts (absent/malformed = OFF)."""
    _set_user_as_actor(
        log,
        "s1",
        False,
        value={"event": "chat_user_as_actor", "user_as_actor": "yes"},
    )

    apply_bts_cast_proposals(log, "s1", _identity_turn("User"))

    assert "User" not in _members(log)


def test_user_as_actor_on_does_not_widen_the_role_word_rule(log, cast_proposals):
    """The setting is about the PLAYER only: a section word never becomes an actor.

    A role word with no member carrying it still enters as a scene handle
    (provisional descriptor, the pre-existing rule) — what this pins is that the
    ON door grants it nothing: it is not a ``user`` actor and not an identity.
    """
    _set_user_as_actor(log, "s1", True)

    apply_bts_cast_proposals(log, "s1", _identity_turn("Narrative", "Elara"))

    members = _members(log)
    assert members["Narrative"].provisional is True
    assert members["Narrative"].provenance == "bts"
    assert "Elara" not in members
    assert cast_proposals == [("s1", "Elara", [], "bts")]


def test_user_as_actor_reads_the_session_setting_and_defaults_off(log):
    """The in-engine seam folds the setting rows latest-wins; absent means OFF."""
    assert user_as_actor(log, "s1") is False
    _set_user_as_actor(log, "s1", True)
    assert user_as_actor(log, "s1") is True
    _set_user_as_actor(log, "s1", False)
    assert user_as_actor(log, "s1") is False
    # A different chat is its own decision (session-scoped, never global).
    _set_user_as_actor(log, "s1", True)
    assert user_as_actor(log, "s2") is False


def test_user_as_actor_on_never_renames_the_user_member_from_a_reference(
    log, cast_proposals
):
    """The player's name is the persona's: a ``rename_to`` on a player surface is ignored."""
    _set_user_as_actor(log, "s1", True)
    turn = {
        "kind": "delta",
        "state": {"User": {"emotional": {"mood": "calm"}}, "Zahra": {}},
        "venue": {"User": {"venue": "tavern"}, "Zahra": {"venue": "tavern"}},
        "off_screen": [],
        "threads": [],
        "tension": {},
        "raw": "",
        "references": [
            {"kind": "identity", "name": "User", "rename_to": "Zahra"},
            {"kind": "identity", "name": "Zahra"},
        ],
    }

    apply_bts_cast_proposals(log, "s1", turn)

    members = _members(log)
    assert members["User"].provenance == "user"
    assert members["User"].display_name == "User"
    # Zahra is proposed (the name is not the player's, so it is not merged into
    # the user member either) and not admitted.
    assert "Zahra" not in members
    assert cast_proposals == [("s1", "Zahra", [], "bts")]


def test_user_as_actor_is_eager_the_toggle_alone_populates_the_cast(log):
    """Requirement 4: ON = a cast-bar entry, not a lazy maybe.

    The door used to admit only when the tracker happened to name a player
    surface, so a session whose BTS never spells the persona out showed an ON
    toggle and no actor. With the setting ON the apply path admits the user
    member on every pass, whatever the turn reported.
    """
    from fabulist.state.bts import apply_bts_cast_proposals
    from fabulist.state.cast import member_tier, roster

    sid = "uaa-eager"
    _set_user_as_actor(log, sid, True)
    # A turn that mentions nobody player-shaped at all.
    apply_bts_cast_proposals(
        log,
        sid,
        {"state": {"Marcus": {"emotional": {"mood": "wary"}}}, "references": []},
    )

    users = [m for m in roster(log, sid) if m.provenance == "user"]
    assert len(users) == 1
    assert member_tier(users[0]) == "actor"
    assert users[0].is_active


def test_user_as_actor_off_admits_nothing_for_the_player(log):
    """OFF = byte-identical to today: no user member appears spontaneously."""
    from fabulist.state.bts import apply_bts_cast_proposals
    from fabulist.state.cast import roster

    sid = "uaa-off"
    apply_bts_cast_proposals(
        log,
        sid,
        {"state": {"Marcus": {"emotional": {"mood": "wary"}}}, "references": []},
    )
    assert [m for m in roster(log, sid) if m.provenance == "user"] == []


# --- the write-action loop (W9.29 §6) ----------------------------------------
#
# A function-calling connection drives the BTS pass as a TOOL-ACTION LOOP: the
# model may read the registry, the roster and the last turns for free, records
# each change as ONE validated call (the engine appends it as it lands, D9), and
# closes the turn with a completion that calls no tool. The retired notation path
# writes nothing on such a connection (W9.29 §11.2).

SESSION = "bts-loop"


class LoopCaller:
    """ModelCaller scripted as (tool_name | None, payload) steps.

    A named tool call carries ``meta.name`` exactly as ``RegistryModelCaller``
    surfaces it (the loop attributes multi-tool calls by that name). When the
    scripted steps run out the caller answers with a plain content completion,
    which is the loop's natural stop — the fixture can never spin.
    """

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls: list[list[dict]] = []
        self.profiles: list[dict | None] = []

    async def __call__(self, messages, stream=True, request_profile=None):
        self.calls.append([dict(message) for message in messages])
        self.profiles.append(request_profile)
        name, payload = self.replies.pop(0) if self.replies else (None, "recorded.")
        if name is None:
            yield {"type": "content", "delta": payload}
            return
        yield {
            "type": "tool_call",
            "delta": json.dumps(payload),
            "meta": {"name": name},
        }


def _use_session(session_id=SESSION):
    from fabulist.agents.debug import CURRENT_SESSION

    CURRENT_SESSION.set(session_id)


def _loop_rows(log, session_id=SESSION):
    return [
        event.payload
        for event in log.get_events(session_id)
        if event.payload.get("event") == "agent_loop"
    ]


def _tool_results(log, session_id=SESSION):
    return [row for row in _loop_rows(log, session_id) if row["kind"] == "tool_result"]


def _entity_payloads(log, session_id=SESSION, kind="entity_establish"):
    return [
        event.payload
        for event in log.get_events(session_id)
        if event.payload.get("event") == kind
    ]


def _profiled():
    return {"tools": [{"type": "function", "function": {"name": "get_roster"}}]}


def test_the_loop_reads_the_roster_then_acts_then_closes_on_a_plain_completion(log):
    """get_roster -> establish_location -> summary: the natural stop IS the close-out."""
    _use_session()
    from fabulist.state.cast import admit_member

    admit_member(log, SESSION, "Elara", provenance="card", admitted_by="user")
    caller = LoopCaller(
        (ROSTER_TOOL, {}),
        (ESTABLISH_LOCATION_TOOL, {"name": "bathroom"}),
        (None, "Recorded the bathroom."),
    )
    result = run(BtsRunner(caller, log=log, profile_fn=_profiled)("u", "prose"))

    assert result.actions == 1 and result.rejected == 0
    # Three completions: the read, the write, and the model's own close-out.
    assert len(caller.calls) == 3
    assert [row["kind"] for row in _loop_rows(log)] == [
        "completion",
        "tool_call",
        "tool_result",
        "completion",
        "tool_call",
        "tool_result",
        "completion",
        "final",
    ]
    results = _tool_results(log)
    assert results[0]["tool"] == ROSTER_TOOL
    assert [member["name"] for member in results[0]["result"]["members"]] == ["Elara"]
    assert results[1]["result"]["ok"] is True
    assert results[1]["result"]["id"].startswith("loc_")
    assert _entity_payloads(log)[0]["name"] == "bathroom"
    # The loop asks with auto, never a forced choice (a forced call traps it).
    assert caller.profiles[0]["tool_choice"] == "auto"
    assert result.raw == "Recorded the bathroom."


def test_the_served_tool_set_is_the_three_reads_plus_the_ten_write_actions(log):
    _use_session()

    tools = bts_loop_tools(log, SESSION)
    names = [tool.name for tool in tools]

    assert names[:3] == [FOLDED_STATE_TOOL, ROSTER_TOOL, RECENT_DELTAS_TOOL]
    assert names[3:] == list(WRITE_TOOL_NAMES)
    # ``report_world_state`` is NOT served any more (W9.29 §6): the loop's stop is
    # the close-out, not a terminal report call.
    assert BTS_TOOL_NAME not in names


def test_the_reads_return_the_registry_render_and_the_last_turns_actions(log):
    """The reads are thin wrappers over views the engine already folds."""
    _use_session()
    from fabulist.state.entities import establish_location, move_being

    settle(log, SESSION)
    caller = LoopCaller(
        (FOLDED_STATE_TOOL, {}),
        (RECENT_DELTAS_TOOL, {"count": 2}),
        (None, "ok"),
    )
    run(BtsRunner(caller, log=log, profile_fn=_profiled)("u", "prose"))

    results = _tool_results(log)
    assert results[0]["result"]["registry"].startswith(ENTITY_PALETTE_DIRECTIVE)
    assert "bathroom" in results[0]["result"]["registry"]
    # Recent turns: the actions that landed, grouped by their summary rows.
    assert results[1]["result"]["count"] == 1
    turn = results[1]["result"]["turns"][0]
    assert [action["event"] for action in turn["actions"]] == [
        "entity_establish",
        "entity_move",
    ]


def settle(log, session_id):
    """One recorded turn: a location, a move and the turn's summary row."""
    from fabulist.state.entities import (
        append_entity_summary,
        establish_location,
        move_being,
    )

    bathroom = establish_location(log, session_id, "bathroom")
    move_being(log, session_id, "player", bathroom)
    append_entity_summary(log, session_id, actions=2, seq=1)
    return bathroom


def test_a_rejected_action_is_answered_in_loop_and_the_model_corrects(log):
    """The plan §6 error text rides back as the tool result; nothing is appended."""
    _use_session()
    from fabulist.state.cast import admit_member

    admit_member(log, SESSION, "Elara", provenance="card", admitted_by="user")
    caller = LoopCaller(
        (SET_ATTR_TOOL, {"host": "Elara", "subject": "player", "key": "hostility", "value": 142}),
        (SET_ATTR_TOOL, {"host": "Elara", "subject": "player", "key": "hostility", "value": 42}),
        (None, "corrected"),
    )
    result = run(BtsRunner(caller, log=log, profile_fn=_profiled)("u", "prose"))

    results = _tool_results(log)
    assert results[0]["result"] == {
        "ok": False,
        "error": "hostility expects an integer in 0–100, got 142",
    }
    assert results[1]["result"]["ok"] is True
    assert results[1]["result"]["id"].startswith("mt_")
    assert result.actions == 1 and result.rejected == 1
    assert _entity_payloads(log, kind="entity_set")[0]["value"] == 42


def test_a_spent_budget_refuses_further_writes_with_the_plan_text(log):
    _use_session()
    caller = LoopCaller(
        *[(ESTABLISH_THREAD_TOOL, {"name": f"thread {index}"}) for index in range(12)],
        (None, "budget spent"),
    )
    result = run(BtsRunner(caller, log=log, profile_fn=_profiled)("u", "prose"))

    results = _tool_results(log)
    assert [row["result"]["ok"] for row in results[:10]] == [True] * 10
    assert results[10]["result"] == {"ok": False, "error": ENTITY_BUDGET_ERROR}
    assert results[11]["result"] == {"ok": False, "error": ENTITY_BUDGET_ERROR}
    assert result.actions == 10
    assert len(_entity_payloads(log)) == 10


def test_a_truncated_completion_before_any_action_fails_the_pass(log):
    """D86: a truncated completion is never trusted, and re-running it cannot help."""
    _use_session()

    class Truncated:
        def __init__(self):
            self.calls = 0

        async def __call__(self, messages, stream=True, request_profile=None):
            self.calls += 1
            if self.calls == 1:
                yield {
                    "type": "tool_call",
                    "delta": '{"name": "bath',
                    "meta": {"name": ESTABLISH_LOCATION_TOOL},
                }
                yield {
                    "type": "error",
                    "delta": "generation truncated",
                    "meta": {"finish_reason": "length"},
                }
                return
            yield {"type": "content", "delta": ""}

    caller = Truncated()
    with pytest.raises(BtsExtractionError) as excinfo:
        run(BtsRunner(caller, log=log, profile_fn=_profiled)("u", "prose"))
    assert "truncated" in str(excinfo.value)
    assert excinfo.value.stage == "registry"
    # No repair re-run: the same budget would truncate the retry too.
    assert caller.calls == 2


def test_actions_that_landed_before_a_truncation_stand(log):
    """§6: each action was validated on its own; append-only means they stay."""
    _use_session()

    class TruncatedAfterOne:
        def __init__(self):
            self.calls = 0

        async def __call__(self, messages, stream=True, request_profile=None):
            self.calls += 1
            if self.calls == 1:
                yield {
                    "type": "tool_call",
                    "delta": json.dumps({"name": "bathroom"}),
                    "meta": {"name": ESTABLISH_LOCATION_TOOL},
                }
                return
            if self.calls == 2:
                yield {
                    "type": "tool_call",
                    "delta": '{"name": "kit',
                    "meta": {"name": ESTABLISH_LOCATION_TOOL},
                }
                yield {
                    "type": "error",
                    "delta": "generation truncated",
                    "meta": {"finish_reason": "length"},
                }
                return
            # A terminal completion, so the fixture can never spin (the loop has
            # no iteration cap by design — agents/loop.py).
            yield {"type": "content", "delta": ""}

    result = run(BtsRunner(TruncatedAfterOne(), log=log, profile_fn=_profiled)("u", "prose"))

    assert result.actions == 1
    assert [payload["name"] for payload in _entity_payloads(log)] == ["bathroom"]


def test_a_connection_without_function_calling_writes_no_state(log):
    """§11.2: the notation write path is retired — Tier D writes nothing, ever."""
    _use_session()
    caller = LoopCaller((None, DELTA_BLOCK))
    runner = BtsRunner(caller, log=log, profile_fn=lambda: None)

    with pytest.raises(BtsExtractionError) as excinfo:
        run(runner("u", "prose"))

    assert excinfo.value.stage == "tier_d"
    assert "function calling" in str(excinfo.value)
    # One call, no loop, no tools — and the notation block it wrote is NOT parsed.
    assert len(caller.calls) == 0
    assert caller.profiles == []
    assert _loop_rows(log) == []
    assert _entity_payloads(log) == []


def test_the_tail_carries_the_action_protocol_and_the_palette(log):
    """Issue #67: the tail is an ASSISTANT message before the final user message.

    The old shape appended a second ``system`` message after the payload, which a
    stock Jinja chat template (the Qwen family) refuses outright — a 500 on a
    local GGUF. The tail now rides the narrator's own carrier
    (``prompts.envelope``), so the shape is ``[system, assistant, user]``.
    """
    _use_session()

    caller = LoopCaller((None, "nothing to record"))
    run(BtsRunner(caller, log=log, profile_fn=_profiled)("u", "prose"))

    messages = caller.calls[0]
    assert [message["role"] for message in messages] == ["system", "assistant", "user"]
    tail = messages[-2]
    assert tail["role"] == "assistant"
    assert tail["content"].startswith(BTS_ACTION_PROTOCOL)
    assert ENTITY_PALETTE_DIRECTIVE in tail["content"]
    assert "loc_off_screen" in tail["content"]
    # The protocol states the budget and the close-out explicitly.
    assert "writes cost one of fifteen" in tail["content"]
    assert "close" in tail["content"]
    # The payload (the user message) is untouched by the engine tail, and only
    # index 0 is a system message.
    assert BTS_ACTION_PROTOCOL not in messages[-1]["content"]
    assert [i for i, m in enumerate(messages) if m["role"] == "system"] == [0]


def test_the_protocol_names_the_w9_32_verbs_and_the_player_rule():
    """W9.32 §3.3/§3.5: four new write verbs, each with the rule that makes it safe."""
    assert "ten write" not in BTS_ACTION_PROTOCOL
    # Each verb, with the one constraint the engine enforces behind it.
    assert "set_condition" in BTS_ACTION_PROTOCOL
    assert "the CLOCK's job" in BTS_ACTION_PROTOCOL
    assert "set_body" in BTS_ACTION_PROTOCOL
    assert "adjust_economy" in BTS_ACTION_PROTOCOL
    assert "never the denomination" in BTS_ACTION_PROTOCOL
    assert "propose_time" in BTS_ACTION_PROTOCOL
    assert "the engine refuses a move backwards" in BTS_ACTION_PROTOCOL
    assert "one of fifteen" in BTS_ACTION_PROTOCOL
    # The player is the USER's persona: never a new person, never in the roster.
    assert 'RULE: who:"player" is the USER\'s own persona' in BTS_ACTION_PROTOCOL
    assert "get_roster (it lists cast members only" in BTS_ACTION_PROTOCOL


# --- the reconciliation cadence (W9.29 §12) ---------------------------------


def _committed_turns(log, session_id=SESSION, count=1):
    """Commit ``count`` narratives: the cadence's left side (BTS runs post-turn)."""
    for index in range(count):
        log.append_event(
            session_id, "narrative", "narrator", {"prose": f"turn {index + 1}"}
        )


def test_the_reconcile_tool_and_directive_appear_only_on_a_cadence_turn(log):
    """§12: the pass is a cadence, not a per-turn surface."""
    from fabulist.state.bts import RECONCILE_CADENCE, RECONCILE_DIRECTIVE
    from fabulist.state.entity_tools import RECONCILE_TOOL

    assert RECONCILE_CADENCE == 10
    _use_session()
    _committed_turns(log, count=RECONCILE_CADENCE - 1)

    ordinary = LoopCaller((None, "nothing to record"))
    ordinary_runner = BtsRunner(ordinary, log=log, profile_fn=_profiled)
    run(ordinary_runner("u", "prose"))
    assert RECONCILE_DIRECTIVE not in ordinary.calls[0][-2]["content"]
    assert ordinary_runner.last_reconcile is False
    assert ordinary_runner.last_turn.reconcile is False

    # The tenth committed turn is the reconcile turn.
    _committed_turns(log, count=1)
    caller = LoopCaller((RECONCILE_TOOL, {"beliefs": []}), (None, "recorded"))
    runner = BtsRunner(caller, log=log, profile_fn=_profiled)
    run(runner("u", "prose"))

    assert runner.last_reconcile is True
    assert runner.last_turn.reconcile is True
    messages = caller.calls[0]
    # Issue #67: the tail is the assistant message immediately before the final
    # user message — never a second system message.
    assert [message["role"] for message in messages] == ["system", "assistant", "user"]
    tail = messages[-2]["content"]
    assert RECONCILE_DIRECTIVE in tail
    # LAST in the tail: the instruction that changes per turn is the most recent
    # content (AGENTS 6 — the seeded prefix stays byte-stable).
    assert tail.rstrip().endswith(RECONCILE_DIRECTIVE)
    # The tool was OFFERED: the loop resolved the call by name and answered the
    # empty belief list with the readable refusal (an unknown name is answered as
    # an unattributable call instead, which this is not).
    result = _tool_results(log)[0]
    assert result["tool"] == RECONCILE_TOOL
    assert result["result"]["ok"] is False
    assert "beliefs" in result["result"]["error"]


def test_a_completed_pass_resets_the_cadence_and_a_missed_one_retries(log):
    """D86 for the reconciliation job: a turn that never ran the pass retries.

    The clock is the ``agent_loop`` trace (no new state): a reconcile CALL advances
    it by its own committed-turn index, so the next pass is due ten turns later; a
    turn where the model never called ``reconcile`` advances nothing.
    """
    from fabulist.state.bts import RECONCILE_CADENCE, last_reconcile_turn
    from fabulist.state.entity_tools import RECONCILE_TOOL

    _use_session()
    _committed_turns(log, count=RECONCILE_CADENCE)
    caller = LoopCaller(
        (RECONCILE_TOOL, {"beliefs": [{"kind": "location", "who": "player", "at": "off_screen"}]}),
        (None, "recorded"),
    )
    run(BtsRunner(caller, log=log, profile_fn=_profiled)("u", "prose"))

    assert last_reconcile_turn(log, SESSION) == RECONCILE_CADENCE
    # Nine more turns: not due yet.
    _committed_turns(log, count=RECONCILE_CADENCE - 1)
    quiet = LoopCaller((None, "nothing to record"))
    runner = BtsRunner(quiet, log=log, profile_fn=_profiled)
    run(runner("u", "prose"))
    assert runner.last_reconcile is False

    # The tenth turn IS due; a run that never calls the tool leaves the clock put,
    # so the turn after it is due again (the missed window is retried).
    _committed_turns(log, count=1)
    missed = LoopCaller((None, "nothing to record"))
    runner = BtsRunner(missed, log=log, profile_fn=_profiled)
    run(runner("u", "prose"))
    assert runner.last_reconcile is True
    assert last_reconcile_turn(log, SESSION) == RECONCILE_CADENCE

    _committed_turns(log, count=1)
    retry = BtsRunner(LoopCaller((None, "nothing to record")), log=log, profile_fn=_profiled)
    run(retry("u", "prose"))
    assert retry.last_reconcile is True


def test_loop_rows_carry_decreasing_headroom_when_the_slot_declares_a_window(log):
    """W25.5: the BTS slot's declared window becomes passive row headroom."""
    _use_session()
    caller = LoopCaller(
        (ROSTER_TOOL, {}),
        (None, "recorded"),
    )
    result = run(
        BtsRunner(
            caller, log=log, profile_fn=_profiled, window_fn=lambda: 4096
        )("u", "prose")
    )

    assert result.actions == 0
    rows = _loop_rows(log)
    by_iteration: dict[int, int] = {}
    for row in rows:
        assert "headroom" in row, row
        by_iteration.setdefault(row["iteration"], row["headroom"])
    headrooms = [by_iteration[iteration] for iteration in sorted(by_iteration)]
    assert len(headrooms) == 2
    # The read's result grows the tail, so iteration 2 has less headroom — and
    # the loop still ran to the model's own close-out.
    assert headrooms[0] > headrooms[1]
    assert [row["kind"] for row in rows][-1] == "final"


def test_loop_rows_carry_no_headroom_without_a_declared_window(log):
    """Undeclared Context Size keeps the pre-W9.25 row shape."""
    _use_session()
    caller = LoopCaller((ROSTER_TOOL, {}), (None, "recorded"))
    run(BtsRunner(caller, log=log, profile_fn=_profiled)("u", "prose"))

    rows = _loop_rows(log)
    assert rows
    assert all("headroom" not in row for row in rows)


def test_a_transport_timeout_ends_the_pass_without_a_retry(log):
    """Hang protection is per connection: the loop records it and BTS stops there.

    A second attempt would buy an identical wait, so the pass raises (the node
    then logs ``world_state_failed``) and the loop's own ``timeout`` row carries
    the bound the call rode as provenance.
    """
    _use_session()

    class Timeout:
        def __init__(self):
            self.calls = 0

        async def __call__(self, messages, stream=True, request_profile=None):
            self.calls += 1
            raise TimeoutError("read timed out")
            yield  # pragma: no cover - the raise is the whole reply

    caller = Timeout()
    with pytest.raises(BtsExtractionError) as excinfo:
        run(
            BtsRunner(
                caller, log=log, profile_fn=_profiled, timeout_fn=lambda: 900.0
            )("u", "prose")
        )
    assert "timed out" in str(excinfo.value)
    assert caller.calls == 1
    rows = _loop_rows(log)
    assert [row["kind"] for row in rows] == ["timeout", "final"]
    assert rows[0]["timeout_s"] == 900.0
    assert rows[1]["timed_out"] is True


def test_an_empty_run_with_no_summary_fails_the_pass(log):
    """Req. 10: nothing recorded and nothing said is a total output failure."""
    _use_session()
    caller = LoopCaller((None, ""))

    with pytest.raises(BtsExtractionError) as excinfo:
        run(BtsRunner(caller, log=log, profile_fn=_profiled)("u", "prose"))

    assert "no action was recorded" in str(excinfo.value)


# --- the node: fold the registry, commit the compat render (W9.29 §6) --------


def test_the_node_commits_the_summary_and_the_flat_delta_only(log):
    """One loop, then: entity_summary + the flat state-delta. NO compat render.

    W9.29 §6 retires the compat ``world_state`` render in W4: the registry IS the
    record and every reader is registry-first, so a fresh registry-era turn writes
    no payload row at all. The flat keys the HUD/facts/planner read still land
    (``entities.flat_render`` + the clock).
    """
    from fabulist.graph.nodes import _run_world_state

    _use_session("entity-node")
    from fabulist.state.cast import admit_member

    admit_member(log, "entity-node", "Seraphine", provenance="card", admitted_by="user")
    narrative = log.append_event(
        "entity-node", "narrative", "narrator", {"prose": "Seraphine runs the bath."}
    )
    from fabulist.state.cast import roster

    seraphine = roster(log, "entity-node")[0].member_id
    caller = LoopCaller(
        (ESTABLISH_LOCATION_TOOL, {"name": "bathroom"}),
        (ESTABLISH_THREAD_TOOL, {"name": "the slow courtship", "detail": "she tests her"}),
        (SET_ATTR_TOOL, {"host": "scene", "key": "tension", "value": 6}),
        (SET_ATTR_TOOL, {"host": "scene", "key": "weather", "value": "rain"}),
        (SET_ATTR_TOOL, {"host": "scene", "key": "tone", "value": "intimate"}),
        (SET_ATTR_TOOL, {"host": seraphine, "subject": "player", "key": "hostility", "value": 42}),
        (None, "recorded"),
    )
    runner = BtsRunner(caller, log=log, profile_fn=_profiled)

    deltas, bts_result, committed = run(
        _run_world_state(
            runner,
            log,
            "entity-node",
            "u",
            "Seraphine runs the bath.",
            "dialogue",
            {"time": "09:15"},
            narrative_seq=narrative.seq,
        )
    )

    summaries = _entity_payloads(log, "entity-node", "entity_summary")
    assert len(summaries) == 1
    assert summaries[0] == {
        "event": "entity_summary",
        "actions": 6,
        "rejected": 0,
        "flags": 0,
        "seq": narrative.seq,
    }
    # FLIPPED (W9.29 §6/W4): the compat ``world_state`` render is RETIRED, so a
    # registry-era turn appends no payload row — the registry's own ``entity_*``
    # rows are the record now.
    assert _entity_payloads(log, "entity-node", "world_state") == []
    assert committed is None
    # What the retired render used to prove lives in the registry itself.
    from fabulist.state.entities import registry

    fold = registry(log, "entity-node")
    assert [thread.name for thread in fold.threads.values()] == ["the slow courtship"]
    assert [meter.value for meter in fold.meters.values() if meter.key == "tension"] == [6]
    # W9.32 §6 (W11): the flat SHADOW keys are no longer written — the scene
    # traits above ARE the answer, and every reader derives ``weather``/``tone``
    # from them (``entities.rendered_state``). The delta carries the clock alone.
    assert set(deltas) == {"time"}
    assert deltas["time"] == "09:15"
    assert bts_result["actions"] == 6


def test_the_compat_world_state_render_is_retired(log):
    """W9.29 §6/W4: the renderer AND the row it wrote are gone from the node.

    The placement it used to publish (same room -> venue, elsewhere -> absence) is
    the registry's own ``assignments`` now — presence reads it directly
    (``tests/test_presence.py``), so a shape nobody writes must not be reachable
    from ``graph.nodes`` at all.
    """
    import fabulist.graph.nodes as graph_nodes
    from fabulist.state.cast import admit_member, member_by_name, roster
    from fabulist.state.entities import (
        OFF_SCREEN,
        establish_location,
        move_being,
        registry,
    )

    assert not hasattr(graph_nodes, "_compat_world_state")
    assert not hasattr(graph_nodes, "_being_label")

    sid = "compat"
    admit_member(log, sid, "Seraphine", provenance="card", admitted_by="user")
    admit_member(log, sid, "Yara", provenance="card", admitted_by="user")
    seraphine = member_by_name(roster(log, sid), "Seraphine").member_id
    yara = member_by_name(roster(log, sid), "Yara").member_id
    hall = establish_location(log, sid, "upstairs hall")
    bath = establish_location(log, sid, "bathroom", parent_id=hall)
    move_being(log, sid, "player", bath)
    move_being(log, sid, seraphine, bath)
    move_being(log, sid, yara, "off_screen", at=hall)

    fold = registry(log, sid)
    assert fold.assignments[seraphine] == bath
    assert fold.assignments[yara] == OFF_SCREEN
    assert fold.off_screen_ref == hall
    assert _entity_payloads(log, sid, "world_state") == []


def test_the_cast_door_is_fed_from_the_turn_mention_set(log, monkeypatch):
    """§6 review R1: the door's input is the MENTION SET, not the retired render.

    The compat render used to contribute the names it placed; with it retired, a
    turn whose write actions dealt with a person must still hand that person to
    ``apply_bts_cast_proposals`` (resolve -> alias -> D79 proposal), or a rename or
    a first sighting would stop reaching the cast door entirely.
    """
    from fabulist.graph.nodes import _run_world_state
    from fabulist.state import bts as bts_module
    from fabulist.state.cast import admit_member, member_by_name, roster

    seen: list[dict] = []

    def _capture(log_, session_id_, result_):
        seen.append(dict(result_))
        return []

    monkeypatch.setattr(bts_module, "apply_bts_cast_proposals", _capture, raising=False)
    _use_session("cast-door")
    admit_member(log, "cast-door", "Seraphine", provenance="card", admitted_by="user")
    admit_member(log, "cast-door", "Yara", provenance="card", admitted_by="user")
    seraphine = member_by_name(roster(log, "cast-door"), "Seraphine").member_id
    narrative = log.append_event(
        "cast-door", "narrative", "narrator", {"prose": "Yara steps out."}
    )
    caller = LoopCaller(
        (ESTABLISH_LOCATION_TOOL, {"name": "bathroom"}),
        (MOVE_BEING_TOOL, {"who": "Yara", "to": "bathroom"}),
        (SET_ATTR_TOOL, {"host": seraphine, "subject": "player", "key": "hostility", "value": 42}),
        (None, "recorded"),
    )
    runner = BtsRunner(caller, log=log, profile_fn=_profiled)

    run(
        _run_world_state(
            runner,
            log,
            "cast-door",
            "u",
            "Yara steps out.",
            "dialogue",
            {"time": "09:15"},
            narrative_seq=narrative.seq,
        )
    )

    assert len(seen) == 1
    fed = seen[0]
    names = [row["name"] for row in fed.get("references") or []]
    assert names == ["Yara", "Seraphine"]
    assert set(fed.get("state") or {}) == {"Yara", "Seraphine"}


def test_world_state_failed_names_the_registry_stage(log):
    from fabulist.graph.nodes import _run_world_state

    _use_session("node-fail")

    class Empty:
        async def __call__(self, user_msg, prose, prior_state=None, *, checkpoint=False):
            raise BtsExtractionError("nothing recorded", raw="")

    deltas, bts_result, committed = run(
        _run_world_state(Empty(), log, "node-fail", "u", "p", "", {"time": "09:15"})
    )

    failed = _entity_payloads(log, "node-fail", "world_state_failed")
    assert failed[0]["stage"] == "registry"
    assert committed is None
    assert bts_result is None
    # The clock still ticks (D3): the turn keeps time even when BTS failed.
    assert deltas["time"] == "09:15"


# --- W9.32 §3.5: the accepted clock proposal lands in the state-delta --------
#
# The tracker may PROPOSE a time with prose evidence (``propose_time``, validated
# by the engine against this turn's clock); an accepted one appends a
# ``system``/``clock_proposal`` audit row. The node reads THAT row back — rows
# appended after this turn's mark, newest wins — and stamps the turn's one
# ``state-delta`` with it instead of the rules clock's fold. The audit row is
# provenance, never a second storage path: the next ``pre_turn`` advances FROM the
# stamped fold, which is what keeps D3's "one home for time" true after the
# partial reversal.

MONDAY = "Monday, June 02, 2025"


class _Proposes:
    """``world_state`` stand-in that records one accepted proposal, then returns.

    ``time=None`` records nothing (the silent turn the table still moves), and
    ``raise_after=True`` dies AFTER the accept — the case where the engine
    validated a proposal that must never reach the fold (review P2-13).
    """

    def __init__(
        self,
        log,
        session_id,
        *,
        time="21:30",
        date=MONDAY,
        day=1,
        raise_after=False,
    ):
        self.log = log
        self.session_id = session_id
        self.time = time
        self.date = date
        self.day = day
        self.raise_after = raise_after

    async def __call__(
        self, user_msg, prose, prior_state=None, *, checkpoint=False, clock=None
    ):
        if self.time:
            self.log.append_event(
                self.session_id,
                "system",
                "bts",
                {
                    "event": "clock_proposal",
                    "time": self.time,
                    "date": self.date,
                    "day": self.day,
                    "evidence": "the shutters glow with first light",
                    "advanced_minutes": 90,
                },
            )
        if self.raise_after:
            raise BtsExtractionError("the loop died after the accept", raw="")
        return {"actions": 1, "rejected": 0, "flags": 0}


def test_an_accepted_proposal_stamps_the_turns_state_delta(log):
    from fabulist.graph.nodes import _run_world_state

    _use_session("proposal-land")

    deltas, _, _ = run(
        _run_world_state(
            _Proposes(log, "proposal-land"),
            log,
            "proposal-land",
            "I wait out the night.",
            "Grey light fills the room.",
            "dialogue",
            {"time": "20:00", "date": MONDAY, "day": "1"},
        )
    )

    # The proposal's whole triple wins over the clock fold (D3 partial reversal).
    assert (deltas["time"], deltas["date"], deltas["day"]) == ("21:30", MONDAY, "1")
    # … and the ONE storage path carries it: the turn's state-delta row.
    deltas_rows = [
        event.payload
        for event in log.get_events("proposal-land")
        if event.type == "state-delta"
    ]
    assert deltas_rows[-1]["time"] == "21:30"
    assert deltas_rows[-1]["date"] == MONDAY


def test_the_next_pre_turn_advances_from_the_proposed_time(log):
    from fabulist.graph.clock import parse_facts
    from fabulist.graph.nodes import _run_world_state, make_pre_turn_node

    _use_session("proposal-next")
    run(
        _run_world_state(
            _Proposes(log, "proposal-next", time="23:40"),
            log,
            "proposal-next",
            "I wait for the bell.",
            "The bell rings.",
            "dialogue",
            {"time": "20:00", "date": MONDAY, "day": "1"},
        )
    )

    pre_turn = make_pre_turn_node(log=log)
    update = run(
        pre_turn({"session_id": "proposal-next", "user_content": "I walk to the tower"})
    )

    facts = parse_facts(update["facts"])

    assert facts["time"] == "00:05"  # 23:40 + travel (25), across midnight
    assert facts["date"] == "Tuesday, June 03, 2025"


def test_a_failed_loop_keeps_the_clock_fold_even_after_an_accept(log):
    """Review P2-13: accepted but not applied is NOT applied.

    The proposal row stands (actions before a failure stand; D9 append-only), but
    the turn failed, so the stamp stays the rules clock's — nothing downstream may
    read an instant the fold ignored as the turn's time.
    """
    from fabulist.graph.nodes import _run_world_state

    _use_session("proposal-fail")

    deltas, bts_result, _ = run(
        _run_world_state(
            _Proposes(log, "proposal-fail", raise_after=True),
            log,
            "proposal-fail",
            "u",
            "p",
            "dialogue",
            {"time": "20:00", "date": MONDAY, "day": "1"},
        )
    )

    assert _entity_payloads(log, "proposal-fail", "world_state_failed")
    assert bts_result is None
    assert deltas["time"] == "20:00"
    assert deltas["date"] == MONDAY


def test_a_proposal_from_an_earlier_turn_is_not_re_applied(log):
    """The mark is what makes it THIS turn's proposal: yesterday's row is provenance."""
    from fabulist.graph.nodes import _run_world_state

    _use_session("proposal-old")
    log.append_event(
        "proposal-old",
        "system",
        "bts",
        {
            "event": "clock_proposal",
            "time": "06:00",
            "date": MONDAY,
            "day": 1,
            "evidence": "we slept until morning",
        },
    )

    deltas, _, _ = run(
        _run_world_state(
            _Proposes(log, "proposal-old", time=None),
            log,
            "proposal-old",
            "u",
            "p",
            "dialogue",
            {"time": "20:00", "date": MONDAY, "day": "1"},
        )
    )

    assert deltas["time"] == "20:00"


def test_a_silent_turn_still_stamps_the_clock_fold(log):
    """No proposal this turn = the fixed table stays the mover (D3, unchanged)."""
    from fabulist.graph.nodes import _run_world_state

    _use_session("proposal-silent")

    deltas, _, _ = run(
        _run_world_state(
            _Proposes(log, "proposal-silent", time=None),
            log,
            "proposal-silent",
            "u",
            "p",
            "travel",
            {"time": "20:00", "date": MONDAY, "day": "1"},
        )
    )

    assert (deltas["time"], deltas["date"], deltas["day"]) == ("20:00", MONDAY, "1")


# --- W9.32 §3.5: the turn's clock rides the LEDGER into the write tools ------
#
# ``set_condition``'s expiry and ``propose_time``'s move are judged against THIS
# turn's instant. The tool can only read the PROJECTION, which mid-loop still holds
# the previous turn's time, so the node hands the ledger the clock the clock node
# computed (``(time, date)``, time first) and the ledger judges against that.
#
# The seam's ``clock`` keyword has a default, so a turn whose facts carry no
# readable instant passes nothing at all and the ledger falls back to the fold
# (``entity_tools._turn_clock``) — the non-live callers' behaviour, unchanged.


class _CapturesClock:
    """``world_state`` stand-in that records every keyword it was handed."""

    def __init__(self):
        self.calls: list[dict] = []

    async def __call__(
        self, user_msg, prose, prior_state=None, *, checkpoint=False, **kwargs
    ):
        self.calls.append(dict(kwargs))
        return {"actions": 0, "rejected": 0, "flags": 0}


def test_the_node_hands_the_turns_clock_to_the_ledger(log):
    from fabulist.graph.nodes import _run_world_state

    _use_session("clock-threaded")
    capture = _CapturesClock()

    run(
        _run_world_state(
            capture,
            log,
            "clock-threaded",
            "u",
            "p",
            "dialogue",
            {"time": "20:00", "date": MONDAY, "day": "1"},
        )
    )

    assert capture.calls == [{"clock": ("20:00", MONDAY)}]


def test_an_unreadable_clock_passes_nothing_so_the_ledger_folds_its_own(log):
    from fabulist.graph.nodes import _run_world_state

    _use_session("clock-missing")
    capture = _CapturesClock()

    run(
        _run_world_state(capture, log, "clock-missing", "u", "p", "dialogue", {"time": "20:00"})
    )

    # No date = no usable instant: the seam is called without the keyword at all.
    assert capture.calls == [{}]


def test_the_runners_ledger_carries_the_clock_it_was_handed(log):
    _use_session()
    caller = LoopCaller((None, "nothing to record"))
    runner = BtsRunner(caller, log=log, profile_fn=_profiled)

    run(runner("u", "prose", clock=("21:30", MONDAY)))

    assert runner.last_turn.clock == ("21:30", MONDAY)


def test_a_ledger_built_without_a_clock_still_runs(log):
    _use_session()
    caller = LoopCaller((None, "nothing to record"))
    runner = BtsRunner(caller, log=log, profile_fn=_profiled)

    result = run(runner("u", "prose"))

    assert result.actions == 0
    assert runner.last_turn.clock is None


# --- W9.32 R8: the derived genre gets its home in the scene branch ----------


class _GenreDecision:
    """Decision caller stand-in: one genre, counted."""

    def __init__(self, genre):
        self.genre = genre
        self.calls = 0

    async def classify_genre(self, state):
        self.calls += 1
        return self.genre


def _scene_genre(log, session_id):
    from fabulist.state import entities

    return entities.scene_traits(entities.registry(log, session_id)).get("genre", "")


def test_the_derived_genre_is_written_as_the_scene_trait_once(log):
    """The trait is the fact's ONLY home (R8); no flat ``genre`` key is written."""
    from fabulist.graph.nodes import _run_world_state
    from fabulist.state import entities

    _use_session("genre-home")
    decision = _GenreDecision("urban fantasy")

    def _turn():
        return run(
            _run_world_state(
                _Proposes(log, "genre-home", time=None),
                log,
                "genre-home",
                "u",
                "The rain smells of hot tar and neon.",
                "dialogue",
                {"time": "20:00", "date": MONDAY, "day": "1"},
                decision=decision,
                prior_narratives=[],
            )
        )

    deltas, _, _ = _turn()
    _turn()  # a second turn derives again — the trait is already home, so no write

    assert _scene_genre(log, "genre-home") == "urban fantasy"
    rows = [
        event
        for event in log.get_events("genre-home")
        if event.payload.get("event") == "entity_set"
    ]
    assert [(row.actor, row.payload["host"], row.payload["key"]) for row in rows] == [
        ("decision", entities.SCENE, "genre")
    ]
    # W9.32 §6 (W11): the flat ``genre`` key is retired — the scene trait IS the
    # fact, and ``entities.rendered_state`` derives the flat key from it.
    assert "genre" not in deltas


def test_a_trackers_genre_is_not_clobbered_by_the_derivation(log):
    """One owner per cell (§5): the tracker set it, so the decision never rewrites it."""
    from fabulist.graph.nodes import _run_world_state
    from fabulist.state import entities

    _use_session("genre-tracker")
    entities.set_attribute(
        log, "genre-tracker", entities.SCENE, key="genre", value="cosmic horror"
    )
    decision = _GenreDecision("urban fantasy")

    run(
        _run_world_state(
            _Proposes(log, "genre-tracker", time=None),
            log,
            "genre-tracker",
            "u",
            "The rain smells of hot tar and neon.",
            "dialogue",
            {"time": "20:00", "date": MONDAY, "day": "1"},
            decision=decision,
            prior_narratives=[],
        )
    )

    assert _scene_genre(log, "genre-tracker") == "cosmic horror"
    rows = [
        event
        for event in log.get_events("genre-tracker")
        if event.payload.get("event") == "entity_set"
        and event.payload.get("key") == "genre"
    ]
    assert len(rows) == 1  # the tracker's own write; the derivation added none


def test_a_genre_delta_from_the_tracker_skips_the_derivation(log):
    """The pre-existing rule survives: another owner's genre means d1 is not asked."""
    from fabulist.graph.nodes import _run_world_state

    _use_session("genre-extractor")

    class _WithGenre(_Proposes):
        async def __call__(
            self, user_msg, prose, prior_state=None, *, checkpoint=False, clock=None
        ):
            return {"actions": 0, "rejected": 0, "flags": 0, "deltas": {"genre": "noir"}}

    decision = _GenreDecision("space opera")
    deltas, _, _ = run(
        _run_world_state(
            _WithGenre(log, "genre-extractor", time=None),
            log,
            "genre-extractor",
            "u",
            "p",
            "dialogue",
            {"time": "20:00", "date": MONDAY, "day": "1"},
            decision=decision,
            prior_narratives=[],
        )
    )

    assert deltas["genre"] == "noir"
    assert decision.calls == 0
    assert _scene_genre(log, "genre-extractor") == ""
