"""Flow manifest (W9.1) — the schema, the authored R5 node set, and the builder.

This module is the single topology seam (plan §2): :func:`build_manifest`
assembles a recursive, deterministic manifest from an authored **topology
source** and the code's own registries (stage names, step catalog, bindings,
prompt flows). The committed artifact (``prompts/generated/flow-manifest.json``)
and ``GET /api/flow/manifest`` both come from the builder, so they cannot drift.

Design rules (plan §2.1):

* **Recursive from day one (R13d).** A :class:`Node` MAY carry a
  ``composition`` sub-graph of the *same* node shape, so nesting is unbounded.
* **Regions make the cache contract visible (R13b/C4).** A composition declares
  ``regions[]``; every composition node names its region. Wave 1 only projects
  :data:`fabulist.prompts.modules.DEFAULT_FLOWS` read-only — it never changes
  prompt assembly.
* **Branches are first-class.** Edges carry ``kind`` / ``condition`` / ``cap``;
  the critic's ok/diffs fan-out is modelled, not implied. The ``fail -> narrate``
  regeneration edge of D49 is GONE (W9.24 §3.3.7): the narrator repairs in its own
  node before the commit, and a post-commit fail keeps the turn.
* **The door is a link, not a copy.** Each node's ``prompt_surface`` points at
  the W6 step editor (``step_id`` = interaction id) or names an engine-owned
  reason; no step content is duplicated here.
* **Preset-independent artifact.** ``agent.default_agent`` comes from
  :data:`DEFAULT_BINDINGS`; the live endpoint overlays ``agent.bound_agent``
  from :func:`~fabulist.prompts.agent_sets.bindings_for` (the same
  ``default_agent``/``bound_agent`` split ``GET /api/prompts/steps`` uses).

``build_manifest`` raises on drift: every registry ``model_pass`` interaction
must exist, every ``step_editor`` ``step_id`` must be a catalog step, every
emitted stage name must appear in ``activation_map``, and every ``activation_map``
value must be a real turn-node id. A rename fails loudly instead of silently
missing.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

__all__ = [
    "ACTIVATION_MAP",
    "CodeTopology",
    "COMPOSITION_NODE_ID",
    "GENERATED_FROM",
    "OFF_TURN_FLOWS",
    "REGION_OF_MODULE_TYPE",
    "TURN_EDGES",
    "TURN_NODES",
    "build_composition",
    "build_manifest",
    "check",
    "code_topology",
    "render",
    "stage_names",
    "write",
]

#: Provenance citations, mirroring the agent-map generator: each dotted source
#: is the thing the manifest is derived from (never hand-copied content).
GENERATED_FROM: dict[str, str] = {
    "node_set": "fabulist.flow.manifest.TURN_NODES",
    "off_turn_flows": "fabulist.flow.manifest.OFF_TURN_FLOWS",
    "stage_names": "fabulist.graph.nodes.STAGE_NAMES",
    "step_catalog": "fabulist.prompts.catalog.INTERACTION_CATALOG",
    "bindings": "fabulist.prompts.agent_sets.DEFAULT_BINDINGS",
    "flows": "fabulist.prompts.modules.DEFAULT_FLOWS",
}

#: The turn node whose ``composition`` carries the R13 recursion proof.
COMPOSITION_NODE_ID = "narrate"

#: Which graph region each narrate-flow module type belongs to (authored). A
#: module type absent here raises in :func:`build_composition` — a new narrate
#: row cannot silently drop out of the composition.
REGION_OF_MODULE_TYPE: dict[str, str] = {
    "narrator_system": "stable_prefix",
    "library": "stable_prefix",
    "history": "neutral",
    "user": "neutral",
    "facts": "dynamic_tail",
    "recalled": "dynamic_tail",
}

#: Region display metadata; ``order`` fixes the rendered top-to-bottom layout.
_REGION_META: dict[str, dict[str, Any]] = {
    "stable_prefix": {"title": "Fixed start (the same every turn)", "order": 0},
    "neutral": {"title": "Conversation", "order": 1},
    "dynamic_tail": {"title": "Changes every turn", "order": 2},
}

#: Authored per-module-type description + prompt-surface link for the narrate
#: composition (the door is a link, never a content copy).
_COMPOSITION_MODULE_META: dict[str, dict[str, str]] = {
    "narrator_system": {
        "description": "The narrator's standing instructions — sent identically every turn.",
        "door": "step_editor",
        "step_id": "narrate",
    },
    "library": {
        "description": "What the narrator is told about the visual panels it may draw.",
        "door": "step_editor",
        "step_id": "narrate",
    },
    "history": {
        "description": "Earlier turns, cleaned up.",
        "door": "fixed",
        "reason": "engine-supplied treated history",
    },
    "user": {
        "description": "Your latest message.",
        "door": "fixed",
        "reason": "engine-supplied user message",
    },
    "facts": {
        "description": "What the world currently records.",
        "door": "fixed",
        "reason": "engine-injected facts",
    },
    "recalled": {
        "description": "Memories and lore pulled in for this turn.",
        "door": "fixed",
        "reason": "engine recall seam",
    },
}


def _node(
    id: str,
    title: str,
    kind: str,
    *,
    runs: Mapping[str, Any],
    agent: Mapping[str, Any] | None = None,
    prompt_surface: Mapping[str, Any],
    inspector: Mapping[str, Any],
    activation: list[str] | None = None,
    composition: Mapping[str, Any] | None = None,
    fanout: Mapping[str, Any] | None = None,
    description: str = "",
    notes: list[str] | None = None,
) -> dict[str, Any]:
    """Build one ``Node`` row (the schema shape reused by turn + off-turn graphs)."""
    return {
        "id": id,
        "title": title,
        "kind": kind,
        "runs": dict(runs),
        "agent": dict(agent) if agent is not None else None,
        "prompt_surface": dict(prompt_surface),
        "inspector": dict(inspector),
        "activation": list(activation or []),
        "composition": dict(composition) if composition is not None else None,
        "fanout": dict(fanout) if fanout is not None else None,
        "meta": {"description": description, "notes": list(notes or [])},
    }


def _edge(
    id: str,
    frm: str,
    to: str,
    kind: str = "flow",
    *,
    condition: str | None = None,
    label: str | None = None,
    cap: int | None = None,
) -> dict[str, Any]:
    """Build one ``Edge`` row."""
    return {
        "id": id,
        "from": frm,
        "to": to,
        "kind": kind,
        "condition": condition,
        "label": label,
        "cap": cap,
    }


def _runs(
    kind: str,
    *,
    interaction: str | None = None,
    call_site: str | None = None,
    description: str,
) -> dict[str, Any]:
    return {
        "kind": kind,
        "interaction": interaction,
        "call_site": call_site,
        "description": description,
    }


def _agent(interaction: str | None, default_agent: str | None, source: str) -> dict[str, Any]:
    return {
        "interaction": interaction,
        "default_agent": default_agent,
        "bound_agent": None,
        "source": source,
    }


def _inspector(kind: str, *, agent_filter: str | None = None, events: list[str] | None = None) -> dict[str, Any]:
    # ``agent_filter`` is the CAPTURE interaction label (agents/debug.py labels:
    # narrate / narrate_repair / critic / bts / attribution / imagegen) — NOT the
    # bound model agent, which returns zero rows.
    return {"kind": kind, "agent_filter": agent_filter, "events": list(events or [])}


#: The authored R5 turn node set (plan §2.3), in flow order. The ``narrate``
#: node's ``composition`` is injected by :func:`build_manifest` from
#: :func:`build_composition` (derived from ``DEFAULT_FLOWS``) — it is not
#: hand-authored. ``bound_agent`` is authored ``None`` and overlaid by the API.
TURN_NODES: tuple[dict[str, Any], ...] = (
    _node(
        "pre_turn",
        "Pre-turn",
        "stage",
        runs=_runs(
            "fixed",
            call_site="fabulist.graph.nodes.make_pre_turn_node",
            description="Reads what you just did, moves the clock on, and gathers the facts the narrator needs.",
        ),
        prompt_surface={"door": "fixed", "reason": "engine-owned rules clock/classifier"},
        inspector=_inspector("fixed_stage"),
        description="Engine step — no model call, nothing to edit here.",
    ),
    _node(
        "planner",
        "Planner",
        "stage",
        runs=_runs(
            "model_pass",
            interaction="planner",
            description=(
                "First checks the world's records — building them on the first turn, "
                "tidying them afterwards — then plans the scene: the story paths, the "
                "date and time, the weather, and the facts the plan keeps established."
            ),
        ),
        agent=_agent("planner", "narrator", "agent_bindings"),
        prompt_surface={"door": "step_editor", "step_id": "planner"},
        inspector=_inspector(
            "model_pass",
            agent_filter="planner",
            # W9.31 §3.5 (S1): the seed phase's writes are the SAME ``entity_*``
            # rows the tracker appends (the registry is producer-agnostic), so the
            # planner node declares the same ``entity_*`` list the bts node does —
            # ``validation_notice`` included, because a repair may file a
            # contradiction flag. ``world_state`` / ``world_state_failed`` stay
            # bts-only: the planner never runs those stages. A declared name that
            # this node does not emit costs nothing; a withheld one would hide a
            # real row.
            events=[
                "plan",
                "plan_refresh_failed",
                "entity_establish",
                "entity_set",
                "entity_update",
                "entity_move",
                "entity_retire",
                "entity_summary",
                "validation_notice",
            ],
        ),
        activation=["plan"],
        description=(
            "Runs before the lanes and the narration, and only when a plan is due "
            "(always on turn 1) — never every turn."
        ),
    ),
    _node(
        "npc_lanes",
        "NPC lanes",
        "stage",
        runs=_runs(
            "model_pass",
            interaction="npc",
            description="Every cast member in the scene works out what they want and acts on it — four at a time.",
        ),
        agent=_agent("npc", "completion.narration", "agent_bindings"),
        prompt_surface={"door": "step_editor", "step_id": "npc"},
        inspector=_inspector("model_pass", agent_filter="npc"),
        # R30/R22: one arm per ACTIVE member, merged by the narrator; the arms
        # run concurrently in a pool of max_parallel. Rendered as a fan so the
        # only real parallelism in the graph is not hidden inside one box.
        fanout={
            "kind": "per_member",
            "source": "cast_presence",
            "merge": "narrate",
            "max_parallel": 4,
            "nodes": ["npc_lane"],
        },
        description=(
            "One lane per cast member in the scene, merged back into the narration. "
            "With no cast there are no lanes and the turn runs as usual."
        ),
    ),
    _node(
        "npc_lane",
        "NPC lane",
        "fanout",
        runs=_runs(
            "model_pass",
            interaction="npc",
            description="One cast member's intent, narration and lines.",
        ),
        agent=_agent("npc", "completion.narration", "agent_bindings"),
        prompt_surface={"door": "step_editor", "step_id": "npc"},
        inspector=_inspector("model_pass", agent_filter="npc", events=["npc_lane"]),
        description=(
            "One lane. Lanes are written down in cast order, not in the order they "
            "happen to finish."
        ),
    ),
    _node(
        "narrate",
        "Narrator turn",
        "stage",
        runs=_runs(
            "model_pass",
            interaction="narrate",
            description="Streams the prose and the panel markup.",
        ),
        agent=_agent("narrate", "narrator", "agent_bindings"),
        prompt_surface={"door": "step_editor", "step_id": "narrate"},
        inspector=_inspector("model_pass", agent_filter="narrate"),
        activation=["narrate"],
        description=(
            "The in-character pass. One draft streams; if the checks flag it, the same "
            "step repairs it in place before anything is kept."
        ),
    ),
    _node(
        "commit",
        "Commit",
        "engine",
        runs=_runs(
            "fixed",
            call_site="fabulist.graph.nodes._commit_narrative",
            description="Saves your message and the narrator's reply. A cancelled turn never gets here.",
        ),
        prompt_surface={"door": "fixed", "reason": "event-log commit, no prompt"},
        inspector=_inspector("fixed_stage", events=["narrative"]),
        description="The line between what streamed and what was kept.",
    ),
    _node(
        "validate",
        "Validate",
        "gate",
        runs=_runs(
            "fixed",
            call_site="fabulist.graph.nodes.validate_dsl",
            description="Checks the panel markup and rebuilds it if it is malformed.",
        ),
        prompt_surface={"door": "fixed", "reason": "deterministic gate + resynthesizer"},
        inspector=_inspector("gate", events=["validator_flags"]),
        activation=["dsl"],
        description="A rule-based check — no model call.",
    ),
    _node(
        "decision_gate",
        "Decision gate",
        "gate",
        runs=_runs(
            "model_pass",
            call_site="fabulist.graph.nodes._run_turn_gate",
            description="Asks the yes/no checks — the anti-slop list and the movement question — 30 at a time.",
        ),
        agent=_agent(None, None, "engine"),
        prompt_surface={"door": "fixed", "reason": "decision client, not a W6 step"},
        inspector=_inspector("gate", agent_filter="narrate_repair"),
        description="Flags only: it never changes the reply.",
    ),
    _node(
        "critic",
        "Critic",
        "gate",
        runs=_runs(
            "model_pass",
            interaction="critic",
            description="Reviews the reply: either fine, or the issues it found. The turn is kept either way.",
        ),
        agent=_agent("critic", "extractor", "agent_bindings"),
        prompt_surface={"door": "step_editor", "step_id": "critic"},
        inspector=_inspector("gate", agent_filter="critic", events=["critic_verdict"]),
        activation=["critic"],
        description=(
            "A failing verdict still keeps the turn — repairs happen inside the narrator "
            "step, and the verdict is recorded so you can see what it objected to."
        ),
    ),
    _node(
        "bts",
        "World tracker (BTS)",
        "stage",
        runs=_runs(
            "model_pass",
            interaction="bts",
            description="Records what changed in the world.",
        ),
        agent=_agent("bts", "extractor", "agent_bindings"),
        prompt_surface={"door": "step_editor", "step_id": "bts"},
        inspector=_inspector(
            "model_pass",
            agent_filter="bts",
            # W9.29 §6: the tracker writes ``entity_*`` rows through its tool loop
            # (one per validated action, plus the per-turn summary); without them
            # here the per-node inspector would hide every recorded change.
            events=[
                "world_state",
                "world_state_failed",
                "entity_establish",
                "entity_set",
                "entity_update",
                "entity_move",
                "entity_retire",
                "entity_summary",
                "validation_notice",
            ],
        ),
        activation=["world_state"],
        description="The tracker: it writes the state you see in the side panel.",
    ),
    _node(
        "env_hud",
        "Env HUD",
        "stage",
        runs=_runs(
            "engine",
            call_site="fabulist.state.env_hud.env_hud_statement",
            description="Writes the scene's status strip (weather, time, place).",
        ),
        prompt_surface={"door": "fixed", "reason": "engine-emitted HUD, never narrator"},
        inspector=_inspector("fixed_stage", events=["env_hud", "env_hud_failed"]),
        activation=["env_hud"],
    ),
    _node(
        "attribution",
        "Attributing speakers",
        "stage",
        runs=_runs(
            "engine",
            call_site="fabulist.state.attribution.parse_attribution",
            description=(
                "Works out who says each quoted line from the colour the narrator wrapped "
                "it in — no model call."
            ),
        ),
        prompt_surface={
            "door": "none",
            "reason": "parses the narrator's colour markup; no prompt of its own",
        },
        inspector=_inspector(
            "fixed_stage", events=["attribution", "attribution_failed"]
        ),
        activation=["attribution"],
        description=(
            "Tags every quoted line with its speaker, and remembers the first colour each "
            "character appears in."
        ),
    ),
    _node(
        "done",
        "Done",
        "terminal",
        runs=_runs("engine", description="The turn is finished; clean-up runs after this."),
        prompt_surface={"door": "none", "reason": "terminal"},
        inspector=_inspector("terminal"),
    ),
)

#: The turn graph's edges (plan §2.3): the linear spine plus the critic's
#: ok/diffs fan-out. The ``fail -> narrate`` regeneration edge is RETIRED
#: (W9.24 §3.3.7) — the narrator repairs in its own node, pre-commit.
TURN_EDGES: tuple[dict[str, Any], ...] = (
    # W9.20: the plan junction on pre_turn (the same conditional primitive the
    # critic and the Send fan-out use). ``plan`` -> the planner step, ``skip`` ->
    # straight to the lanes; the planner rejoins the spine at the lanes.
    _edge("e1", "pre_turn", "planner", "branch", condition="plan due", label="due"),
    _edge("e1a", "pre_turn", "npc_lanes", "branch", condition="no plan due", label="skip"),
    _edge("e1e", "planner", "npc_lanes"),
    # W9.17: the fan-out itself (one Send per ACTIVE member) and its zero-member
    # route straight to the narrator; the per-member node merges into narrate.
    _edge("e1b", "npc_lanes", "npc_lane", "fanout", label="per member"),
    _edge("e1c", "npc_lanes", "narrate", label="no cast"),
    _edge("e1d", "npc_lane", "narrate"),
    _edge("e2", "narrate", "commit"),
    _edge("e2b", "commit", "validate"),
    _edge("e3", "validate", "decision_gate"),
    # W9.19 (B): the critic is CONDITIONAL — a clean turn (no validator flags,
    # no gate flags) routes straight to the planner and logs critic_skipped.
    _edge("e4", "decision_gate", "critic", "branch", condition="needs critic", label="flags"),
    # W9.20: the clean route and the critic's keep verdicts land on the world
    # tracker — the planner is no longer post-commit.
    _edge("e4b", "decision_gate", "bts", "branch", condition="clean", label="clean"),
    _edge("e5", "critic", "bts", "branch", condition="verdict == ok", label="ok"),
    _edge("e6", "critic", "bts", "branch", condition="verdict == diffs", label="diffs"),
    # e7 (critic -> narrate, condition "verdict == fail", cap 2) is RETIRED: no
    # verdict re-enters the narrator any more (W9.24 §3.3.7).
    _edge("e10", "bts", "env_hud"),
    # W9.23 §3.6: the tail is the attribution sweep into the terminal — the
    # proposal lanes left the turn (they are the historian's, below).
    _edge("e11", "env_hud", "attribution"),
    _edge("e12", "attribution", "done"),
)

#: Off-turn graphs (plan §2.3), each anchored (``attached_to``) to the turn node
#: it touches (R5): recall feeds pre_turn, imagegen hangs off the narrator reply,
#: compaction runs after the world tracker, and assistant hooks fire after done.
OFF_TURN_FLOWS: tuple[dict[str, Any], ...] = (
    {
        "id": "recall",
        "kind": "off_turn",
        "title": "Pre-turn recall",
        "attached_to": "pre_turn",
        "entry": "recall_embed",
        "nodes": [
            _node(
                "recall_embed",
                "Recall embed",
                "trigger",
                runs=_runs(
                    "embedder",
                    call_site="fabulist.memory.embedder.embed_texts",
                    description="Turns your message into a search and pulls the closest earlier turns back in.",
                ),
                agent=_agent(None, None, "engine"),
                prompt_surface={"door": "fixed", "reason": "memory seam, not a W6 step"},
                inspector=_inspector("fixed_stage", events=["recall_queries"]),
                description="Runs alongside the turn, before it, not inside it.",
            )
        ],
        "edges": [
            _edge("oa1", "recall_embed", "pre_turn", "attach", label="recall block"),
        ],
    },
    {
        "id": "imagegen",
        "kind": "off_turn",
        "title": "Image generation",
        "attached_to": "narrate",
        "entry": "dsl_statement",
        "nodes": [
            _node(
                "dsl_statement",
                "Narrative DSL",
                "trigger",
                runs=_runs(
                    "fixed",
                    description="A panel in the narrator's reply that asks for an image.",
                ),
                prompt_surface={"door": "none", "reason": "narrator output, not an authored step"},
                inspector=_inspector("fixed_stage"),
            ),
            _node(
                "imagegen_call",
                "Image generation",
                "stage",
                runs=_runs(
                    "hook",
                    call_site="fabulist.api.imagegen_api",
                    description="Draws the image the reply asked for.",
                ),
                agent=_agent(None, "imagegen", "agent_bindings"),
                prompt_surface={"door": "fixed", "reason": "engine hook, no W6 step"},
                inspector=_inspector("fixed_stage", agent_filter="imagegen"),
            ),
        ],
        "edges": [
            _edge("ob1", "dsl_statement", "imagegen_call"),
        ],
    },
    {
        "id": "compaction",
        "kind": "off_turn",
        "title": "Compaction",
        "attached_to": "bts",
        "entry": "budget_check",
        "nodes": [
            _node(
                "budget_check",
                "Budget check",
                "trigger",
                runs=_runs(
                    "engine",
                    call_site="fabulist.graph.compaction.CompactService.compact",
                    description=(
                        "Runs after the turn — automatically when the chat gets close to "
                        "the window, or when you press Compact — and rolls the history "
                        "into a summary."
                    ),
                ),
                prompt_surface={"door": "fixed", "reason": "post-turn hook"},
                inspector=_inspector("fixed_stage"),
                description="Runs after the turn finishes, never during it.",
            ),
            _node(
                "summarize",
                "Summarize",
                "stage",
                runs=_runs(
                    "model_pass",
                    interaction="summarize",
                    description="Writes one summary of the turns so far.",
                ),
                agent=_agent("summarize", "summarizer", "agent_bindings"),
                prompt_surface={"door": "step_editor", "step_id": "summarize"},
                inspector=_inspector("model_pass", agent_filter="summarizer"),
            ),
        ],
        "edges": [
            _edge("oc1", "budget_check", "summarize", condition="over_budget"),
        ],
    },
    {
        "id": "assistant_hooks",
        "kind": "off_turn",
        "title": "Assistant hooks",
        "attached_to": "done",
        "entry": "event",
        "nodes": [
            _node(
                "event",
                "Event",
                "trigger",
                runs=_runs(
                    "engine",
                    description="Something that woke an assistant: a per-turn hook, or you asking it.",
                ),
                prompt_surface={"door": "fixed", "reason": "event trigger"},
                inspector=_inspector("fixed_stage"),
            ),
            _node(
                "assistant_consult",
                "Assistant consult",
                "stage",
                runs=_runs(
                    "model_pass",
                    interaction="assistant",
                    description="What the assistant answered.",
                ),
                agent=_agent("assistant", "narrator", "agent_bindings"),
                prompt_surface={"door": "step_editor", "step_id": "assistant"},
                inspector=_inspector("model_pass", agent_filter="narrator"),
            ),
        ],
        "edges": [
            _edge("od1", "event", "assistant_consult"),
        ],
    },
    {
        "id": "historian",
        "kind": "off_turn",
        "title": "Historian",
        "attached_to": "done",
        "entry": "due_check",
        "nodes": [
            _node(
                "due_check",
                "Due check",
                "trigger",
                runs=_runs(
                    "engine",
                    call_site="fabulist.state.historian.HistorianRunner.is_due",
                    description=(
                        "Post-turn hook: is the preset's cadence up (or did the "
                        "owner press run-now)?"
                    ),
                ),
                prompt_surface={"door": "none", "reason": "post-turn hook"},
                inspector=_inspector("fixed_stage"),
                description=(
                    "Runs AFTER 'done' in its own task, never on the turn "
                    "stream: two cheap chain reads when not due."
                ),
            ),
            _node(
                "proposal_lanes",
                "Proposal lanes",
                "stage",
                runs=_runs(
                    "model_pass",
                    interaction="proposals",
                    description=(
                        "Touched-entity evidence over the run window + the "
                        "should-we-propose verdict, then one extractor pass per "
                        "data type (world / actors / books)."
                    ),
                ),
                agent=_agent("proposals", "extractor", "agent_bindings"),
                prompt_surface={"door": "step_editor", "step_id": "proposals"},
                inspector=_inspector(
                    "model_pass",
                    agent_filter="proposals",
                    events=["historian_run", "asset_proposal"],
                ),
                description=(
                    "W9.23 §3.6: the same lane system the turn used to run, "
                    "driven by the historian over the window since the last "
                    "run. It files `asset_proposal` rows for the owner's review "
                    "home — no lane writes an asset, and no cast row is ever "
                    "filed here (cast proposals are the turn's)."
                ),
            ),
        ],
        "edges": [
            # The attach edge points at the turn node the flow hangs off (the
            # recall row's direction): the renderer draws an external TARGET as
            # a dashed stub, and `attached_to` names the anchor.
            _edge("oh1", "due_check", "done", "attach", label="after done"),
            _edge("oh2", "due_check", "proposal_lanes", condition="due or run-now"),
        ],
    },
)

#: Stage name (the ``turn_stages`` emitter's key) -> turn node id. DATA, never
#: component code (requirement 5): the visualizer reads this table to light a
#: node from a stage frame, so the ``narrate``/``narrator`` naming gap is carried
#: as data instead of hardcoded.
ACTIVATION_MAP: dict[str, str] = {
    # W9.20: the planner's own stage frame (it is the first step of a plan turn).
    "plan": "planner",
    "lanes": "npc_lanes",
    "narrate": "narrate",
    "dsl": "validate",
    "critic": "critic",
    # ``regen`` RETIRED (W9.24 §3.3.7): repair iterations re-emit the ``narrate``
    # slot's label, never a stage of their own.
    "world_state": "bts",
    "env_hud": "env_hud",
    "attribution": "attribution",
}


@dataclass(frozen=True)
class CodeTopology:
    """The authored, code-side topology source for :func:`build_manifest`.

    Wave 1 passes this (the code-authored R5 node set); wave W9.6 will pass the
    compiled-LangGraph source (``graph.get_graph()``) through the same seam and
    the same output schema.
    """

    turn_nodes: tuple[Mapping[str, Any], ...]
    turn_edges: tuple[Mapping[str, Any], ...]
    off_turn_flows: tuple[Mapping[str, Any], ...]
    activation_map: Mapping[str, str]


#: The wave-1 topology source: the authored R5 node set above.
code_topology = CodeTopology(
    turn_nodes=TURN_NODES,
    turn_edges=TURN_EDGES,
    off_turn_flows=OFF_TURN_FLOWS,
    activation_map=ACTIVATION_MAP,
)


def stage_names() -> tuple[str, ...]:
    """The emitted stage names, read from the graph emitter (never authored).

    Prefers the public ``fabulist.graph.nodes.STAGE_NAMES`` alias; falls back to
    the private ``_STAGE_SPECS`` keys until that alias lands, so the activation
    map cannot drift from what :func:`~fabulist.graph.nodes._emit_stage` writes.
    """
    from fabulist.graph import nodes

    names = getattr(nodes, "STAGE_NAMES", None)
    if names is None:
        names = tuple(nodes._STAGE_SPECS)
    return tuple(names)


def build_composition(interaction: str = COMPOSITION_NODE_ID) -> dict[str, Any]:
    """Derive the prompt-assembly ``SubGraph`` for ``interaction`` (R13 proof).

    Projects :data:`~fabulist.prompts.modules.DEFAULT_FLOWS` read-only: one
    ``trigger`` node per flow row (region from :data:`REGION_OF_MODULE_TYPE`),
    all feeding the assembled-prompt terminal. Raising on an unmapped module
    type keeps the composition honest when the flow grows.
    """
    from fabulist.prompts.modules import DEFAULT_FLOWS

    modules = DEFAULT_FLOWS.get(interaction)
    if not modules:
        raise RuntimeError(
            f"no DEFAULT_FLOWS entry for interaction {interaction!r}; the "
            "narrate composition cannot be derived"
        )

    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    used_regions: list[str] = []
    for module in modules:
        module_type = module.type
        region = REGION_OF_MODULE_TYPE.get(module_type)
        if region is None:
            raise RuntimeError(
                f"module type {module_type!r} in the {interaction!r} flow has no "
                "REGION_OF_MODULE_TYPE entry; author its region in "
                "src/fabulist/flow/manifest.py"
            )
        if region not in used_regions:
            used_regions.append(region)
        meta = _COMPOSITION_MODULE_META.get(module_type, {})
        surface: dict[str, Any]
        if meta.get("door") == "step_editor":
            surface = {"door": "step_editor", "step_id": meta["step_id"]}
        else:
            surface = {"door": "fixed", "reason": meta.get("reason", "engine-supplied module")}
        nodes.append(
            _node(
                module.id,
                module.name,
                "trigger",
                runs=_runs("fixed", description=meta.get("description", f"{module.name} block.")),
                prompt_surface=surface,
                inspector=_inspector("fixed_stage"),
                description=f"Region: {region}",
            )
        )
        edges.append(_edge(f"e_{module.id}_sink", module.id, "assembled_prompt"))

    nodes.append(
        _node(
            "assembled_prompt",
            "Assembled prompt",
            "terminal",
            runs=_runs(
                "fixed",
                description="Ordered message list the narrator caller receives.",
            ),
            prompt_surface={"door": "none", "reason": "composition output"},
            inspector=_inspector("terminal"),
            description="Linear projection of the W6 flow (one source, two editors).",
        )
    )

    regions = [
        {
            "id": region,
            "kind": region,
            "title": _REGION_META[region]["title"],
            "order": _REGION_META[region]["order"],
        }
        for region in sorted(used_regions, key=lambda r: _REGION_META[r]["order"])
    ]

    return {
        "kind": "composition",
        "title": "Narrator prompt assembly",
        "regions": regions,
        "entry": modules[0].id,
        "sink": "assembled_prompt",
        "nodes": nodes,
        "edges": edges,
    }


def _resolve_default_agents(node: dict[str, Any], bindings: Mapping[str, Any]) -> None:
    """Set ``agent.default_agent`` from ``bindings``; reset ``bound_agent`` to null.

    The artifact is preset-independent: only the D19 default is baked in here.
    The endpoint overlays the active preset's ``bound_agent`` afterwards.
    """
    agent = node.get("agent")
    if not agent:
        return
    interaction = agent.get("interaction")
    if interaction and interaction in bindings:
        bound = bindings[interaction]
        if isinstance(bound, Mapping) and bound.get("agent"):
            agent["default_agent"] = bound["agent"]
    agent["bound_agent"] = None


def _turn_graph(
    topology_source: CodeTopology, bindings: Mapping[str, Any]
) -> dict[str, Any]:
    nodes = [copy.deepcopy(node) for node in topology_source.turn_nodes]
    for node in nodes:
        _resolve_default_agents(node, bindings)
        if node["id"] == COMPOSITION_NODE_ID:
            node["composition"] = build_composition(COMPOSITION_NODE_ID)
    return {
        "id": "turn_flow",
        "kind": "turn",
        "title": "Turn flow",
        "attached_to": None,
        "entry": nodes[0]["id"],
        "nodes": nodes,
        "edges": [copy.deepcopy(edge) for edge in topology_source.turn_edges],
    }


def _validate(manifest: Mapping[str, Any]) -> None:
    """Fail loudly on any registry / catalog / activation drift."""
    from fabulist.prompts.agent_sets import INTERACTIONS
    from fabulist.prompts.catalog import INTERACTION_CATALOG

    turn_graph = manifest["graphs"][0]
    turn_ids = {node["id"] for node in turn_graph["nodes"]}

    for graph in manifest["graphs"]:
        ids = [node["id"] for node in graph["nodes"]]
        if len(ids) != len(set(ids)):
            raise RuntimeError(f"graph {graph['id']!r} has duplicate node ids")
        if graph["entry"] not in ids:
            raise RuntimeError(f"graph {graph['id']!r} entry {graph['entry']!r} is not a node")

    for graph in manifest["graphs"]:
        for node in _walk_nodes(graph["nodes"]):
            runs = node["runs"]
            if runs["kind"] == "model_pass" and runs["interaction"] is not None:
                if runs["interaction"] not in INTERACTIONS:
                    raise RuntimeError(
                        f"node {node['id']!r} runs interaction {runs['interaction']!r} "
                        "which is not in INTERACTIONS"
                    )
            agent = node.get("agent")
            if agent and agent.get("interaction") and agent["interaction"] not in INTERACTIONS:
                raise RuntimeError(
                    f"node {node['id']!r} agent interaction {agent['interaction']!r} "
                    "is not in INTERACTIONS"
                )
            surface = node["prompt_surface"]
            if surface["door"] == "step_editor" and surface["step_id"] not in INTERACTION_CATALOG:
                raise RuntimeError(
                    f"node {node['id']!r} step_editor step_id {surface['step_id']!r} "
                    "is not in INTERACTION_CATALOG"
                )

    names = stage_names()
    activation = manifest["activation_map"]
    missing = [name for name in names if name not in activation]
    if missing:
        raise RuntimeError(
            "activation_map is missing emitted stage name(s): " + ", ".join(missing)
        )
    extra = [name for name in activation if name not in names]
    if extra:
        raise RuntimeError(
            "activation_map has stage name(s) the emitter does not emit: " + ", ".join(extra)
        )
    for stage, node_id in activation.items():
        if node_id not in turn_ids:
            raise RuntimeError(
                f"activation_map[{stage!r}] = {node_id!r} is not a turn node id"
            )
    for node in turn_graph["nodes"]:
        for stage in node["activation"]:
            if activation.get(stage) != node["id"]:
                raise RuntimeError(
                    f"node {node['id']!r} activation {stage!r} disagrees with activation_map"
                )


def _walk_nodes(nodes: list[Mapping[str, Any]]):
    """Yield every node, descending into ``composition`` sub-graphs (recursion)."""
    for node in nodes:
        yield node
        composition = node.get("composition")
        if composition:
            yield from _walk_nodes(composition["nodes"])


def build_manifest(
    topology_source: CodeTopology | None = None,
    bindings: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble the deterministic flow manifest. Raises on drift.

    ``topology_source`` is the single seam (defaults to :data:`code_topology`);
    ``bindings`` supplies ``agent.default_agent`` (defaults to
    :data:`~fabulist.prompts.agent_sets.DEFAULT_BINDINGS`).
    """
    from fabulist.prompts.agent_sets import DEFAULT_BINDINGS

    topo = topology_source if topology_source is not None else code_topology
    binds: Mapping[str, Any] = DEFAULT_BINDINGS if bindings is None else bindings

    graphs: list[dict[str, Any]] = [_turn_graph(topo, binds)]
    for flow in topo.off_turn_flows:
        graph = copy.deepcopy(flow)
        for node in graph["nodes"]:
            _resolve_default_agents(node, binds)
        graphs.append(graph)

    manifest: dict[str, Any] = {
        "schema_version": 1,
        "generated_from": dict(GENERATED_FROM),
        "activation_map": dict(topo.activation_map),
        "graphs": graphs,
    }
    _validate(manifest)
    return manifest


def render(manifest: dict[str, Any]) -> str:
    """Render the manifest as deterministic JSON (sorted keys, trailing newline)."""
    return json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def check(path: Path) -> bool:
    """Return ``True`` when the file at ``path`` is up to date with the code."""
    try:
        existing = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return False
    return existing == render(build_manifest())


def write(path: Path) -> Path:
    """Write the generated manifest to ``path``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(build_manifest()), encoding="utf-8")
    return path
