"""Issue #104: ONE request assembly per interaction — the parity guard.

The studio preview exists to show what the engine sends. Before #104 it kept a
private copy of the parts assembly (``prompts_api._flow_parts``) covering six of
the eleven flows: ``planner``/``npc``/``proposals`` returned ``KeyError:
'system'`` (a 500 in the drawer) and ``critic`` returned a list the engine never
sends. The fix is one named assembly per interaction
(:mod:`fabulist.prompts.request_parts`, plus the planner node's composer), called
by BOTH the live call site and the preview — so this file is the regression guard:

* one test per interaction comparing the PREVIEW list with the list the live
  call site builds (composed here from the same primitives the engine uses, not
  by calling the preview's own function), and
* one test proving the wiring's live call site still delegates to that assembly
  (a re-copied parts dict in ``wiring.py`` would fail the AST pin), and
* the completeness pin: every entry of ``INTERACTIONS`` has a preview builder,
  so a new interaction cannot ship with a 500 in the drawer.

ADR-005 rides along: the lists are compared BYTE FOR BYTE, so any preview-only
text inside a message fails here.
"""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path

import httpx
import pytest

from fabulist.api import create_app
from fabulist.api.fake import FakeTurnEngine
from fabulist.api.prompts_api import _PREVIEW_BUILDERS, router as prompts_router
from fabulist.graph.types import initial_turn_state
from fabulist.prompts.agent_sets import INTERACTIONS
from fabulist.prompts.preset_store import (
    active_preset,
    append_preset,
    compile_preset_flow,
    planner_config,
    set_active,
)

SID = "s1"
USER_TEXT = "answer me"


def run(coro):
    return asyncio.run(coro)


# -- fixtures ----------------------------------------------------------------


def _seed_session(log, session_id: str = SID) -> None:
    """A card, one committed turn and one user message — a realistic session."""
    log.append_event(session_id, "system", "system", {"card": {"name": "Kesh"}})
    log.append_event(session_id, "user", "user", {"text": "hello"})
    log.append_event(
        session_id,
        "narrative",
        "narrator",
        {
            "prose": "The rain kept up all evening.",
            "dsl": "",
            "segments": [{"kind": "prose", "text": "The rain kept up all evening."}],
        },
    )


@pytest.fixture
def app_and_log(tmp_path: Path):
    log_path = tmp_path / "events.db"
    app = create_app(FakeTurnEngine(), log_path)
    app.include_router(prompts_router)
    log = app.state.log
    from fabulist.wiring import make_narrator_message_builder

    # The SAME builder `build_app` mounts on a live service.
    app.state.message_builder = make_narrator_message_builder(log)
    _seed_session(log)
    try:
        yield app, log
    finally:
        log.close()


def _preview(app, interaction: str, user_text: str = USER_TEXT, **extra) -> dict:
    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/prompts/preview",
                json={
                    "session_id": SID,
                    "user_text": user_text,
                    "interaction": interaction,
                    **extra,
                },
            )
        assert resp.status_code == 200, resp.text
        return resp.json()

    return run(scenario())


# -- completeness ------------------------------------------------------------


def test_every_interaction_has_a_preview_builder() -> None:
    """A dropdown entry with no builder is the #104 500 — pin the table."""
    assert set(_PREVIEW_BUILDERS) == set(INTERACTIONS) - {"narrate"}


# -- narrate -----------------------------------------------------------------


def test_preview_narrate_matches_the_live_builder(app_and_log) -> None:
    app, _log = app_and_log
    body = _preview(app, "narrate", "go")
    assert body["preview_mode"] == "builder"
    live = app.state.message_builder(initial_turn_state(SID, "go", "narrate"))
    assert body["messages"] == live


def test_preview_narrate_renders_the_previewed_preset(app_and_log) -> None:
    """Issue #104: ``preset_id`` reaches the builder branch (it used to be ignored)."""
    app, log = app_and_log
    active_before = _preview(app, "narrate", "go")["messages"]
    pid = append_preset(
        log,
        {
            "name": "Marker",
            "modules": [
                {
                    "id": "m",
                    "name": "M",
                    "type": "text",
                    "role": "system",
                    "content": "PREVIEW-MARKER",
                    "enabled": True,
                },
                {
                    "id": "u",
                    "name": "U",
                    "type": "user",
                    "role": "user",
                    "content": "",
                    "enabled": True,
                },
            ],
        },
    )
    body = _preview(app, "narrate", "go", preset_id=pid)
    assert body["preview_mode"] == "builder"
    assert body["preset_id"] == pid
    # The authored row renders inside the system prefix (the compile folds the
    # leading system run into ONE message — issue #44), so match on content.
    assert any("PREVIEW-MARKER" in str(m.get("content", "")) for m in body["messages"])
    # …and the active preset's own list is untouched (the override is per call).
    assert _preview(app, "narrate", "go")["messages"] == active_before


# -- ooc ---------------------------------------------------------------------


def test_preview_ooc_matches_the_live_ooc_builder(app_and_log) -> None:
    """The live OOC builder: no facts row, no history copy, the ``[OOC]`` flag."""
    app, log = app_and_log
    from fabulist.wiring import make_ooc_message_builder

    body = _preview(app, "ooc")
    live = make_ooc_message_builder(log)(SID, USER_TEXT)
    assert body["preview_mode"] == "flow"
    assert body["messages"] == live
    assert body["messages"][-1] == {"role": "user", "content": f"[OOC] {USER_TEXT}"}


# -- extract / summarize / bts ----------------------------------------------


def test_preview_extract_matches_the_live_call(app_and_log) -> None:
    app, log = app_and_log
    from fabulist.state.extractor import EXTRACTOR_SYSTEM_PROMPT, _turn_block

    body = _preview(app, "extract")
    narrative = "The rain kept up all evening."
    live = compile_preset_flow(
        active_preset(log),
        "extract",
        {
            "system": EXTRACTOR_SYSTEM_PROMPT,
            "turn_payload": _turn_block(USER_TEXT, narrative),
            "variables": {"message": USER_TEXT, "last_narrative": narrative},
        },
    )
    assert body["messages"] == live


def test_preview_summarize_matches_the_live_call(app_and_log) -> None:
    app, log = app_and_log
    from fabulist.graph.compaction import build_summarizer_messages

    body = _preview(app, "summarize")
    base = build_summarizer_messages("The rain kept up all evening.")
    live = compile_preset_flow(
        active_preset(log),
        "summarize",
        {
            "system": str(base[0]["content"]),
            "payload": str(base[1]["content"]),
            "variables": {},
        },
    )
    assert body["messages"] == live


def test_preview_bts_matches_the_live_call(app_and_log) -> None:
    app, log = app_and_log
    from fabulist.state.bts import BTS_SYSTEM_PROMPT, build_bts_messages

    body = _preview(app, "bts")
    base = build_bts_messages(
        USER_TEXT, "The rain kept up all evening.", None, checkpoint=False
    )
    live = compile_preset_flow(
        active_preset(log),
        "bts",
        {
            "system": str(base[0].get("content", BTS_SYSTEM_PROMPT)),
            "payload": str(base[1].get("content", "")) if len(base) > 1 else "",
            "variables": {},
        },
    )
    assert body["messages"] == live


# -- caption / assistant -----------------------------------------------------


def test_preview_caption_matches_the_live_call(app_and_log) -> None:
    """The payload shape is the captioner's; the missing bytes are a response note."""
    app, log = app_and_log
    from fabulist.media.images import build_caption_messages

    body = _preview(app, "caption")
    base = build_caption_messages({"payload": {}})
    live = compile_preset_flow(
        active_preset(log),
        "caption",
        {
            "system": str(base[0].get("content", "")),
            "payload": base[1].get("content", ""),
            "variables": {},
        },
    )
    assert body["messages"] == live
    # The substitution is stated beside the list, never inside it.
    assert body["notes"]
    assert all("no bytes" not in str(m.get("content", "")) for m in body["messages"])


def test_preview_assistant_matches_the_live_call(app_and_log) -> None:
    app, log = app_and_log
    from fabulist.api.assistants_api import assistant_definitions
    from fabulist.assistants.manager import AssistantContext, build_assistant_messages
    from fabulist.assistants.panel import scene_facts_line, scene_recent_prose

    body = _preview(app, "assistant")
    definition = assistant_definitions(log)[0]
    context = AssistantContext(
        facts=scene_facts_line(log, SID),
        recent_prose=scene_recent_prose(log, SID),
        user_msg=USER_TEXT,
    )
    base = build_assistant_messages(definition, context)
    live = compile_preset_flow(
        active_preset(log),
        "assistant",
        {
            "system": str(base[0].get("content", "")),
            "history": [],
            "user_content": str(base[1].get("content", "")),
            "variables": {},
        },
    )
    assert body["messages"] == live
    assert body["assistant_id"] == definition.id


# -- critic ------------------------------------------------------------------


def test_preview_critic_matches_the_live_call(app_and_log) -> None:
    """System + payload: the list the engine sends (the old preview had no user row)."""
    app, log = app_and_log
    from fabulist.prompts.critic import CRITIC_SYSTEM_PROMPT
    from fabulist.prompts.request_parts import render_critic_payload

    body = _preview(app, "critic")
    evidence = {
        "user_message": USER_TEXT,
        "prose": "",
        "flags": [],
        "prior_prose": ["The rain kept up all evening."],
        "state_line": "",
    }
    live = compile_preset_flow(
        active_preset(log),
        "critic",
        {
            "system": CRITIC_SYSTEM_PROMPT,
            "payload": render_critic_payload(evidence),
            "variables": {},
        },
    )
    assert body["messages"] == live
    assert [m["role"] for m in body["messages"]] == ["system", "user"]


# -- planner -----------------------------------------------------------------


def test_preview_planner_matches_the_node_composer(app_and_log) -> None:
    """Directive + the node's context block + the ask — the list the planner sends."""
    app, log = app_and_log
    from fabulist.graph.nodes import (
        DEFAULT_PLANNER_DIRECTIVE,
        _planner_context,
        _planner_messages,
        committed_turn_count,
    )
    from fabulist.graph.plans import load_last_plan

    body = _preview(app, "planner")
    state = initial_turn_state(SID, USER_TEXT, "planner")
    config = planner_config(active_preset(log))
    live = _planner_messages(
        directive=DEFAULT_PLANNER_DIRECTIVE,
        context=_planner_context(log, SID, state),
        prior=load_last_plan(log, SID),
        steer=str(config.get("steer") or ""),
        fields=config.get("fields") or [],
        profile=None,
        turn_seq=committed_turn_count(log, SID),
    )
    assert body["messages"] == live
    system = body["messages"][0]["content"]
    assert DEFAULT_PLANNER_DIRECTIVE in system
    assert "CAST AND WORLD:" in system  # the node's context block is in the list
    assert body["messages"][-1]["role"] == "user"  # …and so is the ask


# -- npc ---------------------------------------------------------------------


def test_preview_npc_matches_prefix_plus_lane_tail(app_and_log) -> None:
    """Narrator prefix + ONE lane tail, wrapped by the same runtime bound."""
    app, log = app_and_log
    from fabulist.prompts.npc import build_lane_ask, build_lane_system
    from fabulist.prompts.wrapper import wrap_runtime
    from fabulist.graph.nodes import render_private_block
    from fabulist.prompts.request_parts import npc_lane_context
    from fabulist.state.cast import ensure_roster, member_by_id, roster
    from fabulist.state.presence import active_members
    from fabulist.state.settings import ANTI_INJECTION_DEFAULT, anti_injection_enabled

    body = _preview(app, "npc")
    assert body["preview_mode"] == "flow"
    assert body["lane_member"] == "Kesh"

    ensure_roster(log, SID)
    member_ids, _source = active_members(log, SID)
    member = member_by_id(roster(log, SID), member_ids[0])
    assert member is not None
    context = npc_lane_context(log, SID, member)
    tail = compile_preset_flow(
        active_preset(log),
        "npc",
        {
            "system": build_lane_system(
                member.display_name, render_private_block(context)
            ),
            "payload": build_lane_ask(member.display_name),
            "variables": {"name": member.display_name},
        },
    )
    try:
        anti_injection = bool(anti_injection_enabled(log))
    except Exception:  # pragma: no cover - the shipped default
        anti_injection = ANTI_INJECTION_DEFAULT
    if anti_injection:
        tail = [{**m, "content": wrap_runtime(m.get("content"))} for m in tail]
    shared = app.state.message_builder(
        initial_turn_state(SID, USER_TEXT, "narrate")
    )
    assert body["messages"] == [*shared, *tail]
    # The list ENDS with the lane tail (the ask is last), never with the prefix.
    assert body["messages"][-2:] == tail[-2:]


# -- proposals ---------------------------------------------------------------


def test_preview_proposals_matches_the_lane_request(app_and_log) -> None:
    """The proposal lane's OWN request (it is sent standalone, not after a prefix)."""
    app, log = app_and_log
    from fabulist.prompts.request_parts import proposal_lane_blocks
    from fabulist.state.proposal_lanes import PROPOSAL_LANES

    body = _preview(app, "proposals")
    assert body["preview_mode"] == "flow"
    assert body["lane"] == PROPOSAL_LANES[0].lane

    blocks = proposal_lane_blocks(
        log,
        PROPOSAL_LANES[0],
        touched=(),
        prose="The rain kept up all evening.",
    )
    live = compile_preset_flow(
        active_preset(log),
        "proposals",
        {
            "system": blocks["system"],
            "payload": blocks["payload"],
            "variables": {},
        },
    )
    assert body["messages"] == live
    assert body["messages"][-1]["role"] == "user"


# -- the wiring delegation pin ----------------------------------------------


_WIRING_PATH = (
    Path(__file__).resolve().parents[1] / "src" / "fabulist" / "wiring.py"
)

#: live wiring closure -> the ONE assembly function it must call (#104).
_DELEGATIONS = {
    "_extract_flow_messages": "extract_request",
    "_bts_flow_messages": "bts_request",
    "_planner_flow_messages": "planner_directive_messages",
    "_critic_flow_messages": "critic_request",
    "_proposals_flow_messages": "proposals_request",
    "_npc_flow_messages": "npc_request",
    "_summarize_flow_messages": "summarize_request",
    "_assistant_flow_messages": "assistant_request",
    "_caption_flow_messages": "caption_request",
    "make_ooc_message_builder": "ooc_request",
}


def _called_names(fn: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            func = node.func
            name = (
                func.attr
                if isinstance(func, ast.Attribute)
                else getattr(func, "id", "")
            )
            if name:
                names.add(name)
    return names


def test_the_live_call_sites_delegate_to_the_one_assembly() -> None:
    """A re-copied parts dict in wiring.py would fail here (the #104 regression)."""
    tree = ast.parse(_WIRING_PATH.read_text(encoding="utf-8"))
    found = {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
    }
    for closure, assembler in _DELEGATIONS.items():
        assert closure in found, f"wiring.{closure} is gone (renamed?)"
        assert assembler in _called_names(found[closure]), (
            f"wiring.{closure} no longer calls {assembler} — the assembly was "
            "copied again (issue #104)"
        )


def test_the_preview_route_keeps_no_second_assembly() -> None:
    """The old private parts assembly must not come back."""
    source = Path(
        Path(__file__).resolve().parents[1] / "src" / "fabulist" / "api" / "prompts_api.py"
    ).read_text(encoding="utf-8")
    assert "def _flow_parts(" not in source
    assert "def _degraded_parts(" in source  # the test-only narrate fallback stays
