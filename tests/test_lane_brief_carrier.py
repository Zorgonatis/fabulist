"""Issue #73 — a LANE brief rides the assistant carrier, never ``system``.

The live defect (owner's preview): ``SystemMessageOrderError`` from glm-5.3 —
"system message at index 21 of 23 (slot completion.narration)" — so every NPC
lane was lost on the turn. The cause is structural, not a wording bug:

* a lane flow (``flows.npc`` / ``flows.proposals``) is compiled STANDALONE and
  its messages are APPENDED after the narrator's history
  (``graph.nodes.NpcLaneFanout._request``, ``AssetProposalFanout._messages``);
* the flow's ``system`` row compiled to a real ``system`` message, so it landed
  at index 21 of the final request — where the central guard
  (:func:`fabulist.connections.client.build_request`) refuses it, and a stock
  Jinja chat template answers a hard 500;
* the ``payload`` row already compiled to the ``user`` ask, correctly.

The ruling under test (the same one as issues #44/#67/#68): the narrator's
carrier is the ONE tail shape. A lane brief is ENGINE INSTRUCTION, so it rides
``prompts.envelope.ENVELOPE_ROLE`` — an ``assistant`` message at its own
position, which in every lane flow is immediately before the final user ask —
and the ask stays ``user``. The narrator's own bytes never move (ADR-005).

Both lane paths are pinned here: the compiled preset flow (``compile_flow`` on
``LANE_INTERACTIONS``) and the two code defaults
(``graph.nodes.default_npc_lane_messages`` /
``prompts.proposals.default_lane_messages``), which share
``prompts.modules.carry_lane_brief`` so they cannot disagree.

``stock_qwen_render`` is the Qwen3 template rule in miniature: it is what makes
"the shape" mean "a stock local GGUF would accept this payload" instead of a
role-list convention.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from fabulist.connections import Registry, build_request
from fabulist.connections.client import SystemMessageOrderError
from fabulist.graph.nodes import make_asset_proposals_node, make_npc_lanes_node
from fabulist.prompts.envelope import ENVELOPE_ROLE
from fabulist.prompts.modules import (
    DEFAULT_FLOWS,
    LANE_INTERACTIONS,
    carry_lane_brief,
    compile_flow,
)
from fabulist.prompts.preset_store import compile_preset_flow
from fabulist.prompts.proposals import default_lane_messages

SID = "s-lane-carrier"

#: The lane brief + ask the wiring resolves for a flow compile.
NPC_PARTS = {
    "system": "LANE BRIEF + PRIVATE BLOCK",
    "payload": "JSON ASK",
    "variables": {"name": "Marcus"},
}
PROPOSALS_PARTS = {"system": "PROPOSAL LANE BRIEF", "payload": "TURN + TYPE PAYLOAD"}

#: One lane member context rich enough for the private block to render.
MEMBER = {
    "member_id": "m1",
    "name": "Marcus",
    "aliases": ["Marc"],
    "categories": {"mood": {"temper": "short"}},
    "witnessed": [3, 4],
    "beat": "someone pounds on the door",
    "last_intent": "bar the door",
}


def stock_qwen_render(messages: list[dict]) -> str:
    """The Qwen3 chat template's rule, verbatim in miniature."""
    rendered: list[str] = []
    for index, message in enumerate(messages):
        if message.get("role") == "system" and index != 0:
            raise ValueError("System message must be at the beginning.")
        rendered.append(f"<|im_start|>{message['role']}\n{message['content']}<|im_end|>")
    return "\n".join(rendered)


def _registry(*slots: str) -> Registry:
    """A registry binding every slot under test to one dummy connection."""
    return Registry.from_dict(
        {
            "connections": {"c": {"base_url": "http://host/v1", "model": "m"}},
            "agents": {slot: "c" for slot in slots},
        }
    )


def _narrator_shaped_history(turns: int = 20) -> list[dict]:
    """A LONG narrator history: the frozen prefix, then history, ending on user.

    ``turns`` is deliberately well past any index a short fixture would reach —
    the live failure was index 21 of 23.
    """
    messages: list[dict] = [{"role": "system", "content": "the frozen prefix"}]
    for index in range(turns):
        messages.append({"role": "user", "content": f"player turn {index}"})
        messages.append({"role": "assistant", "content": f"prose {index}"})
    messages.append(
        {"role": ENVELOPE_ROLE, "content": "<preset_runtime>engine tail</preset_runtime>"}
    )
    messages.append({"role": "user", "content": "the player's latest ask"})
    return messages


def _requested(agent: str, lane_messages: list[dict]) -> list[dict]:
    """The final request a lane actually sends: the shared prefix + its tail."""
    shared = _narrator_shaped_history()
    messages = [*shared, *lane_messages]
    # The chokepoint every consumer rides: this is what raised on the preview.
    build_request(agent, messages, registry=_registry(agent))
    return messages


# -- the compiled preset path ------------------------------------------------


@pytest.mark.parametrize(
    ("interaction", "parts"),
    [("npc", NPC_PARTS), ("proposals", PROPOSALS_PARTS)],
)
def test_a_compiled_lane_flow_passes_the_guard_and_carries_its_brief(interaction, parts):
    """The defect, at the exact seam: a lane flow appended after the history."""
    lane = compile_preset_flow(None, interaction, dict(parts))

    assert [message["role"] for message in lane] == [ENVELOPE_ROLE, "user"]
    assert lane[0]["content"] == parts["system"]  # the brief, unchanged
    assert lane[1]["content"] == parts["payload"]  # the ask stays the ask

    messages = _requested(interaction, lane)
    # The brief sits IMMEDIATELY before the ask, and the ask closes the request.
    assert messages[-2] is not None and messages[-2]["role"] == ENVELOPE_ROLE
    assert messages[-1]["role"] == "user"
    assert messages[-2]["content"] == parts["system"]
    assert [i for i, m in enumerate(messages) if m["role"] == "system"] == [0]
    # A stock template renders it instead of raising.
    stock_qwen_render(messages)


@pytest.mark.parametrize(
    ("interaction", "parts"),
    [("npc", NPC_PARTS), ("proposals", PROPOSALS_PARTS)],
)
def test_the_preset_row_role_is_the_carrier_seam_not_the_flow_data(interaction, parts):
    """A preset's authored rows carry the CONTENT and the ORDER — not the role.

    The default flow still declares a ``system`` row (that is the row TYPE the
    step editor shows); what it ships is decided once, at compile time, by
    :data:`LANE_INTERACTIONS` — so an unedited preset, a re-seeded one and the
    code default can never disagree.
    """
    assert interaction in LANE_INTERACTIONS
    assert "narrate" not in LANE_INTERACTIONS  # the prefix is not a lane
    assert [module.type for module in DEFAULT_FLOWS[interaction]] == [
        "system",
        "payload",
    ]
    compiled = compile_flow(interaction, DEFAULT_FLOWS[interaction], dict(parts))
    assert [message["role"] for message in compiled] == [ENVELOPE_ROLE, "user"]


# -- the code-default path (no preset) ---------------------------------------


def test_the_npc_default_tail_agrees_with_the_compiled_flow(log):
    """``default_npc_lane_messages`` is the same shape as the preset path."""
    from fabulist.graph.nodes import default_npc_lane_messages

    default = default_npc_lane_messages(MEMBER)
    compiled = compile_flow("npc", DEFAULT_FLOWS["npc"], dict(NPC_PARTS))

    assert [message["role"] for message in default] == [
        message["role"] for message in compiled
    ]
    assert default[0]["role"] == ENVELOPE_ROLE and default[-1]["role"] == "user"
    _requested("npc", default)


def test_the_proposals_default_tail_agrees_with_the_compiled_flow():
    default = default_lane_messages(
        str(PROPOSALS_PARTS["system"]), str(PROPOSALS_PARTS["payload"])
    )
    compiled = compile_flow("proposals", DEFAULT_FLOWS["proposals"], dict(PROPOSALS_PARTS))

    assert [message["role"] for message in default] == [
        message["role"] for message in compiled
    ]
    assert default == compiled
    _requested("proposals", default)


# -- the live fan-out seams --------------------------------------------------


class _RecordingLaneCaller:
    """Records the messages served to the lane; answers one JSON object."""

    def __init__(self):
        self.calls: list[list[dict]] = []

    async def __call__(self, messages, stream=True, **kwargs):
        self.calls.append(messages)
        yield {
            "type": "content",
            "delta": json.dumps(
                {"intent": "shut it", "narrative": "he shuts it", "lines": []}
            ),
        }


def test_the_npc_fanout_serves_a_guard_legal_tail(log):
    """The exact path that failed live: shared prefix + the lane's own tail."""
    fanout = make_npc_lanes_node(
        log=log,
        build_messages=lambda state: [],
        caller=_RecordingLaneCaller(),
    )
    update = asyncio.run(
        fanout.lane(
            {
                "session_id": SID,
                "member": dict(MEMBER),
                "shared": _narrator_shaped_history(),
                "max_lanes": 1,
            }
        )
    )
    assert update["lane_meta"]["ran"] == 1
    messages = fanout.caller.calls[0]
    build_request("npc", messages, registry=_registry("npc"))
    assert messages[-2]["role"] == ENVELOPE_ROLE
    assert messages[-1]["role"] == "user"
    stock_qwen_render(messages)


def test_the_proposal_fanout_serves_a_guard_legal_tail(log):
    """``AssetProposalFanout``'s own code-default lane flow, same rule."""
    fanout = make_asset_proposals_node(log=log)
    lane = fanout._messages(dict(PROPOSALS_PARTS))

    assert [message["role"] for message in lane] == [ENVELOPE_ROLE, "user"]
    _requested("proposals", lane)


# -- the regression pins -----------------------------------------------------


@pytest.mark.parametrize(
    ("interaction", "parts"),
    [("npc", NPC_PARTS), ("proposals", PROPOSALS_PARTS)],
)
def test_a_lane_brief_role_cannot_go_back_to_system(interaction, parts):
    """The old shape is refused at the chokepoint — a regression cannot ship."""
    lane = compile_preset_flow(None, interaction, dict(parts))
    assert all(message["role"] != "system" for message in lane)

    legacy = [{"role": "system", "content": parts["system"]}]
    legacy.append({"role": "user", "content": parts["payload"]})
    shared = _narrator_shaped_history()
    with pytest.raises(SystemMessageOrderError) as excinfo:
        build_request(interaction, [*shared, *legacy], registry=_registry(interaction))
    assert excinfo.value.index == len(shared)


@pytest.mark.parametrize(
    "messages",
    [
        # the canonical lane shape, uncompiled
        [{"role": "system", "content": "brief"}, {"role": "user", "content": "ask"}],
        # an authored flow that adds a second engine row
        [
            {"role": "system", "content": "brief"},
            {"role": "system", "content": "note"},
            {"role": "user", "content": "ask"},
        ],
        # a text row a preset author authored as ``system``
        [
            {"role": "system", "content": "brief"},
            {"role": "assistant", "content": "prose"},
            {"role": "system", "content": "authored note"},
            {"role": "user", "content": "ask"},
        ],
    ],
)
def test_carry_lane_brief_moves_every_system_row_and_nothing_else(messages):
    carried = carry_lane_brief(messages)

    assert [message["role"] for message in carried] == [
        ENVELOPE_ROLE if message["role"] == "system" else message["role"]
        for message in messages
    ]
    assert [message["content"] for message in carried] == [
        message["content"] for message in messages
    ]
    # A NEW list; the compiled rows are never mutated in place.
    assert all(carried[i] is not messages[i] for i, m in enumerate(messages) if m["role"] == "system")
    assert [message["role"] for message in messages][0] == "system"
