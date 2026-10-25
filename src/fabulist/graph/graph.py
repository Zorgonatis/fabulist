"""LangGraph assembly: the turn topology (W9.6).

The graph is authored as DATA (``CANONICAL_TOPOLOGY``) and built from it: nodes
are the per-stage factories in :mod:`fabulist.graph.nodes`, edges are the
linear spine, the W9.20 plan junction on ``pre_turn``, the critic's verdict
fan-out and the W9.17 NPC-lane ``Send`` fan-out. A preset can carry its own
topology (``preset_store.preset_topology``); ``build_graph(topology=...)``
builds that instead, so a custom pipeline never needs a code change.

The W9.22 asset-proposal stage is GONE from the turn (W9.23 §3.6): the world /
actor / book lane system is driven off-turn by
:mod:`fabulist.state.historian`, so the tail is ``attribution -> done`` again.
A stored topology saved while the stage existed is STRIPPED at compile
(:func:`_strip_legacy_proposals`), never booted with a node the graph has no
factory for.

Fan-out stages are a TABLE (:data:`FANOUT_STAGES`), not special cases: every
``Send`` fan-out names its dispatcher node, the node its router sends to, and
how its object is built, so a new fan-out is a row plus its node (W9.22 §3).

The critic's verdict fan-out is ``ok`` / ``diffs`` only (W9.24 §3.3.7): the
``fail -> narrate`` regeneration edge and its ``regen`` stage are RETIRED. A
``fail`` keeps the delivered turn with its verdict row, and quality repair now
happens INSIDE the narrator node (draft → validate → targeted patch) before the
commit. A stored topology that still carries the dead ``fail`` edge compiles and
boots — :func:`fabulist.graph.nodes.critic_route` never returns ``fail``, so the
edge is unreachable and no node id is orphaned; it is tolerated, never stripped.

The planner is a STEP, not a tail (W9.20 §2): ``pre_turn`` is a junction whose
router (:func:`fabulist.graph.nodes.make_plan_due_route`) sends a due turn
through ``planner`` before the lanes and a skip turn straight to them.

Checkpointer is cache, not truth (D11)
--------------------------------------
The compiled graph is checkpointed with a SQLite saver and ``thread_id`` set to
the session id, so a second turn on the same thread resumes accumulated graph
state (prior facts, last action classification). That checkpoint is *derived*:
every value it holds is reconstructible from the event log —

* the turn narrative is written as ``narrative`` events,
* the state it carries is written as ``state-delta`` events,
* ``pre_turn`` rebuilds the prior facts from the log's folded state and only
  falls back to the checkpoint for a cold thread (see ``nodes.facts_from_log``).

On divergence the log wins; a checkpoint may be dropped and rebuilt by replaying
the log. Nothing here may hold state the log cannot reconstruct.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph

from .clock import ActionClassifier, classify_action
from .nodes import (
    critic_route,
    needs_critic_route,
    DEFAULT_PLAN_CADENCE,
    default_narrator_messages,
    make_attribution_node,
    make_bts_node,
    make_critic_node,
    make_decision_gate_node,
    make_done_node,
    make_env_hud_node,
    make_commit_node,
    make_narrate_node,
    make_npc_lanes_node,
    make_plan_due_route,
    make_planner_node,
    make_pre_turn_node,
    make_validate_node,
)
from .contracts import implements_of
from .types import ExtractorFn, LogPort, ModelCaller, TurnState

__all__ = [
    "CANONICAL_TOPOLOGY",
    "NODE_NAMES",
    "build_graph",
    "open_sqlite_checkpointer",
    "strip_legacy_topology",
]

#: The canonical turn topology (plan §2.3 / W9.6): the linear spine, the
#: W9.20 plan junction on ``pre_turn``, the critic's verdict fan-out and the
#: W9.17 NPC-lane ``Send`` fan-out.
#: ``kind`` is informational today (it names the shape of the step — the same
#: vocabulary the flow manifest uses); ``condition`` on an edge names the branch
#: value its source node routes on, and ``fanout`` marks a ``Send`` edge
#: (see :func:`build_graph`).
CANONICAL_TOPOLOGY: dict[str, list[dict]] = {
    "nodes": [
        {"id": "pre_turn", "kind": "stage"},
        # W9.20 §2: the planner is the junction's ``plan`` arm — a steering plane
        # that runs BEFORE the lanes and the narration, never a per-turn step.
        {"id": "planner", "kind": "stage"},
        # W9.13/W9.17: the NPC fan-out. ``npc_lanes`` is the DISPATCHER (resolve
        # the ACTIVE cast, build the shared prefix once); the fan-out edge below
        # turns it into one ``Send("npc_lane", ...)`` per member. ``max_lanes`` is
        # node data and bounds CONCURRENCY, not the lane set.
        {"id": "npc_lanes", "kind": "stage", "data": {"max_lanes": 4}},
        {"id": "npc_lane", "kind": "stage"},
        {"id": "narrate", "kind": "stage"},
        {"id": "commit", "kind": "stage"},
        {"id": "validate", "kind": "gate"},
        {"id": "decision_gate", "kind": "gate"},
        {"id": "critic", "kind": "gate"},
        {"id": "bts", "kind": "stage"},
        {"id": "env_hud", "kind": "stage"},
        {"id": "attribution", "kind": "stage"},
        # W9.23 §3.6: the tail is ``attribution -> done`` again — the proposal
        # lanes moved OFF the turn (the historian runs them between turns).
        {"id": "done", "kind": "terminal"},
    ],
    "edges": [
        # W9.20 §2: the plan junction. The router's return values ARE these
        # conditions (``plan_due_route`` -> "plan" | "skip").
        {"from": "pre_turn", "to": "planner", "kind": "branch", "condition": "plan"},
        {"from": "pre_turn", "to": "npc_lanes", "kind": "branch", "condition": "skip"},
        {"from": "planner", "to": "npc_lanes"},
        # W9.17: the fan-out itself, plus its ZERO-MEMBER route to the narrator
        # (the plain edge the builder turns into the fan-out's empty branch — an
        # empty Send list is never routed).
        {"from": "npc_lanes", "to": "npc_lane", "fanout": True},
        {"from": "npc_lanes", "to": "narrate"},
        {"from": "npc_lane", "to": "narrate"},
        {"from": "narrate", "to": "commit"},
        {"from": "commit", "to": "validate"},
        {"from": "validate", "to": "decision_gate"},
        # W9.19 (B): the critic is conditional — a clean turn (no validator
        # flags, no gate flags) routes straight past it. W9.20: that route and
        # the critic's keep verdicts all land on ``bts`` (the planner is no
        # longer post-commit); ``needs_critic_route`` still RETURNS the literal
        # ``"planner"`` as its clean condition value — this row maps it.
        {"from": "decision_gate", "to": "critic", "kind": "branch", "condition": "critic"},
        {"from": "decision_gate", "to": "bts", "kind": "branch", "condition": "planner"},
        {"from": "critic", "to": "bts", "kind": "branch", "condition": "ok"},
        {"from": "critic", "to": "bts", "kind": "branch", "condition": "diffs"},
        # W9.24 §3.3.7: the ``critic -> narrate [fail]`` regeneration edge is
        # GONE. ``critic_route`` returns only ok/diffs (a legacy ``fail`` parse
        # keeps the turn), so a stored topology carrying the old edge still
        # compiles — the branch value simply never comes back.
        {"from": "bts", "to": "env_hud"},
        {"from": "env_hud", "to": "attribution"},
        # W9.23 §3.6: the proposal lane system is OFF-turn now, so nothing
        # closes the tail but the terminal itself.
        {"from": "attribution", "to": "done"},
    ],
}

#: Node ids in canonical execution order (the topology's declared node order).
NODE_NAMES = tuple(node["id"] for node in CANONICAL_TOPOLOGY["nodes"])


@dataclass(frozen=True)
class FanoutStage:
    """One ``Send`` fan-out the builder can wire: its node ids + code default.

    ``dispatcher`` is the fan-out's node id — it owns both routing halves, the
    ``route`` (one ``Send`` per lane, or ``[]``) and the ``join`` (the fan-in
    flush) — and ``lane`` is the node id the router sends to, one invocation per
    lane. The builders are keyed by this PAIR because the two halves share one
    closure (lane caller, lane flow, pool width), which is why they cannot be
    independent factories.

    ``build`` instantiates the pair from the builder's own seams (keyword args:
    ``log``, ``build_messages``) when the wiring supplied no object for the
    stage. ``None`` means the stage has no code default: the builder then wires
    an :class:`_UnwiredFanout`, which dispatches no lanes (see its docstring).
    """

    dispatcher: str
    lane: str
    build: Callable[..., Any] | None = None


def _default_npc_fanout(
    *, log: LogPort, build_messages: Callable[[TurnState], list[dict]]
) -> Callable:
    """The NPC fan-out's code default: the dispatcher with NO lane caller.

    Without a caller the dispatcher plans no lanes and the router returns no
    ``Send``s, so a graph that never wired the fan-out keeps today's turn
    (direct node calls / tests).
    """
    return make_npc_lanes_node(log=log, build_messages=build_messages)


#: EVERY ``Send`` fan-out the canonical topology carries, one row each (W9.22
#: §3). Adding a fan-out stage is a ROW here plus its node — never another
#: special case in :func:`_node_factories`, which is what made the single
#: hard-coded W9.17 NPC fan-out un-extendable.
FANOUT_STAGES: tuple[FanoutStage, ...] = (
    # W9.13/W9.17: one lane per ACTIVE cast member. The wiring supplies this
    # object (it owns the pool width and the per-lane ``npc`` binding); the code
    # default runs with no caller, i.e. zero lanes.
    FanoutStage("npc_lanes", "npc_lane", build=_default_npc_fanout),
    # W9.23 §3.6: the W9.22 proposal stage's row is GONE with the stage. Its
    # fan-out object (``nodes.AssetProposalFanout``) still exists and is still
    # the lane system — ``state.historian`` drives it off-turn, outside this
    # table, because the table describes the TURN.
)


class _UnwiredFanout:
    """A declared fan-out stage that has neither a supplied object nor a code default.

    Every stage in :data:`FANOUT_STAGES` today has a code default, so this is the
    table's LAST-RESORT branch: it exists so a future stage row can declare
    itself with ``build=None`` and still compile, and so the "supplied dispatcher
    without a lane half" case has an inert lane to fall back on.

    It dispatches NOTHING — :meth:`route` returns no ``Send`` and the builder
    takes the stage's zero-lane edge. Zero lanes is not a placeholder answer, it
    is the stage's own decision where the stage's gate cannot be evaluated (the
    W9.22 §3 double gate needs touched-entity evidence AND a should-we-propose
    verdict, and an unwired stage has neither). What it must never do is
    half-run — plan lanes that nothing routes — which is the W9.17 failure this
    class rules out.

    Deliberately private and deliberately dumb: behaviour arrives as a real
    fan-out object from the wiring (the ``npc_lanes`` pattern), so this class
    can never drift into a second implementation of a gate.
    """

    def __init__(self, lane: str) -> None:
        #: The node id this fan-out routes ``Send`` tasks to — ``build_graph``
        #: checks it against the topology's fan-out edge (the same contract as
        #: :attr:`fabulist.graph.nodes.NpcLaneFanout.fanout_node`).
        self.fanout_node = lane

    async def __call__(self, state: TurnState) -> dict:  # noqa: ARG002 - dispatcher
        return {}

    async def route(self, state: TurnState) -> list:  # noqa: ARG002 - no lanes
        return []

    async def lane(self, payload: dict) -> dict:  # noqa: ARG002 - no lanes
        return {}


#: The condition a conditional edge branches on, per source node. A topology
#: that conditions an edge out of any other node has no router and is rejected
#: (node templates are W9.10). Two node kinds carry a router of their own
#: instead: a fan-out source (:attr:`fabulist.graph.nodes.NpcLaneFanout.route`)
#: and a junction bound per build — the W9.20 plan junction rides the
#: ``pre_turn`` node (it closes over the log + the live cadence reader, so it
#: cannot be a free function here) — see :func:`_router_for`.
_ROUTERS: dict[str, Callable[[TurnState], Any]] = {
    "critic": critic_route,
    # W9.19 (B): the critic runs only when a flag demands it.
    "decision_gate": needs_critic_route,
}


def _router_for(behaviour: str, node: Callable | None) -> Callable | None:
    """The router that wires a node's conditional edges, or ``None``.

    A stage node's router is named by :data:`_ROUTERS` through its BEHAVIOUR (a
    template node placed in the critic's slot routes on the critic's verdict); a
    fan-out node carries its own on the node object (the fan-out closure owns
    the lane flow, so the router cannot be a free function), and so does the
    ``pre_turn`` junction (it owns the log + cadence closure). ``behaviour`` is
    the canonical node id the topology row executes — the row's own id for a
    plain canonical node, the template's ``implements`` for a template node
    (W9.10).
    """
    route = getattr(node, "route", None)
    return route if callable(route) else _ROUTERS.get(behaviour)


def _joined(node: Callable, join: Callable) -> Callable:
    """Wrap ``node`` so a fan-out's :meth:`join` flush runs at the fan-in.

    A ``Send`` fan-out returns no control flow: nothing runs between "every lane
    finished" and the next node, and the per-lane log rows must follow the
    DISPATCHER's member order (not completion order) with the ``npc_lanes``
    summary appended after them. So the fan-out hands its flush to the builder,
    which attaches it to the first node downstream of the fan-out target. The
    flush is sync, deterministic and idempotent per turn (``lane_meta.emitted``),
    and its state update rides along with the wrapped node's own.
    """

    async def joined(state: TurnState) -> dict:
        # The flush's update is NOT merged into the node's input: a shallow
        # ``{**state, **update}`` would replace a whole reduced channel with the
        # flush's partial key (``lane_meta`` -> ``{"emitted": True}``). Only the
        # node's own RETURN carries it, through the graph's reducers.
        update = join(state) or {}
        result = await node(state)
        return {**update, **(result or {})}

    joined.__name__ = getattr(node, "__name__", "joined")
    return joined


def _supplied_fanouts(
    *, npc_lanes: Any = None, fanouts: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """The pre-built fan-out objects, keyed by dispatcher node id (W9.22 §3).

    ``fanouts`` is the general seam — ``{dispatcher node id: fan-out object}`` —
    and ``npc_lanes`` is the W9.17 spelling the existing wiring passes, folded
    in here so ONE code path builds every fan-out. When both name the same
    stage an explicit ``fanouts`` row wins (the general seam is the specific
    one); a ``None`` row means "nothing supplied", not "no fan-out".
    """
    supplied = {
        str(node_id): node
        for node_id, node in (fanouts or {}).items()
        if node is not None
    }
    if npc_lanes is not None:
        supplied.setdefault("npc_lanes", npc_lanes)
    return supplied


def _fanout_objects(
    supplied: Mapping[str, Any], seams: Mapping[str, Any]
) -> dict[str, Callable]:
    """``{node id: factory}`` for every stage in :data:`FANOUT_STAGES`.

    TWO node ids per stage — the dispatcher and its lane node — because one
    closure builds both (see :class:`FanoutStage`), which is what makes the
    table the single place a fan-out is declared. Resolution order per stage:

    1. the object the wiring supplied (it owns the lane caller, the flow and the
       pool width), used as-is;
    2. the stage's code default, built from THIS graph's seams;
    3. :class:`_UnwiredFanout` — declared, zero lanes, no model call.

    A caller that supplied only the DISPATCHER half (an object with no callable
    ``lane``) gets the stage's own lane node from (2)/(3): it then runs with no
    caller => zero lanes, exactly like an engine that never wired the fan-out.
    """
    out: dict[str, Callable] = {}
    for stage in FANOUT_STAGES:
        default = stage.build(**seams) if stage.build is not None else None
        node = supplied.get(stage.dispatcher)
        if node is None:
            node = (
                default
                if default is not None
                else _UnwiredFanout(stage.lane)
            )
        lane_node = getattr(node, "lane", None)
        if not callable(lane_node):
            lane_node = (
                default.lane
                if default is not None
                else _UnwiredFanout(stage.lane).lane
            )
        out[stage.dispatcher] = node
        out[stage.lane] = lane_node
    return out


def _node_factories(
    *,
    model: ModelCaller,
    extractor: ExtractorFn,
    log: LogPort,
    classifier: ActionClassifier,
    config_snapshot: dict | None,
    build_messages: Callable[[TurnState], list[dict]],
    world_state,
    attribution,
    critic,
    decision,
    narrate_loop_capabilities=None,
    narrate_loop_timeout=None,
    narrate_loop_window=None,
    narrate_loop_return_thinking=None,
    narrate_provider_label=None,
    npc_lanes=None,
    planner=None,
    plan_cadence=DEFAULT_PLAN_CADENCE,
    fanouts: Mapping[str, Any] | None = None,
) -> dict[str, Callable]:
    """Every node the builder can instantiate, keyed by topology node id."""
    # W9.13/W9.17/W9.22 §3: the fan-out stages come from ONE table
    # (:data:`FANOUT_STAGES`). ``npc_lanes`` still arrives PRE-BUILT from the
    # wiring, which owns the preset's topology data (lane cap) and the per-lane
    # ``npc`` binding; the general ``fanouts`` mapping is how a newer stage
    # arrives. Either way the object is the fan-out's DISPATCHER and carries its
    # other halves — ``route`` (the conditional edge) and ``lane`` (the
    # per-lane node) — because they share one closure.
    fanout_nodes = _fanout_objects(
        _supplied_fanouts(npc_lanes=npc_lanes, fanouts=fanouts),
        {"log": log, "build_messages": build_messages},
    )
    # W9.20 §2: the plan junction's router. It is BOUND here (not a ``_ROUTERS``
    # free function) because it closes over the log and the live cadence reader
    # — the same reason the fan-out's router rides its node object.
    pre_turn = make_pre_turn_node(log=log, classifier=classifier)
    pre_turn.route = make_plan_due_route(log=log, cadence=plan_cadence)
    return {
        **fanout_nodes,
        "pre_turn": pre_turn,
        # W9.24 §3.3: the narrate node owns draft → validate → repair. The
        # capability map, the transport bound, the NARRATE slot's available
        # prompt-token budget and the connection's reasoning-echo setting are all
        # passed as the wiring's LIVE readers, so a re-bound preset agent or a
        # hot-reloaded connection applies on the next turn without a graph
        # rebuild.
        "narrate": make_narrate_node(
            model=model,
            log=log,
            build_messages=build_messages,
            loop_capabilities=narrate_loop_capabilities,
            loop_timeout_s=narrate_loop_timeout,
            loop_window_fn=narrate_loop_window,
            loop_return_thinking=narrate_loop_return_thinking,
            provider_label_fn=narrate_provider_label,
        ),
        "commit": make_commit_node(log=log, config_snapshot=config_snapshot),
        "validate": make_validate_node(log=log),
        "decision_gate": make_decision_gate_node(log=log, decision=decision),
        "critic": make_critic_node(log=log, critic=critic),
        # W9.20: ``planner`` arrives PRE-BUILT from the wiring, which owns its
        # seams (the narrator-class caller, the directive, the live preset
        # config, the request profile). Without it the node is inert and
        # ``pre_turn``'s junction still routes — a plan turn simply writes no
        # plan (direct node calls / tests).
        "planner": planner if planner is not None else make_planner_node(log=log),
        "bts": make_bts_node(
            extractor=extractor, log=log, world_state=world_state, decision=decision
        ),
        "env_hud": make_env_hud_node(log=log),
        "attribution": make_attribution_node(log=log, attribution=attribution),
        "done": make_done_node(),
    }


def _strip_legacy_proposals(
    node_ids: list[str], edges: list[dict]
) -> tuple[list[str], list[dict]]:
    """REMOVE the retired W9.22 proposal stage from a stored topology (§3.6.4).

    Every topology the owner saved while the in-turn proposal stage existed
    carries ``asset_proposals`` + ``asset_proposal_lane``. The stage is gone from
    the turn (W9.23 §3.6: :mod:`fabulist.state.historian` drives those lanes
    between turns), so compiling such a save verbatim would die on "unknown node
    id" and the engine would fall back to the WHOLESALE canonical topology,
    discarding the owner's OTHER custom edits. The strip therefore rewrites the
    COMPILE only (the stored record and the editor's read are untouched — D71's
    recorded precedent) and is exactly the inverse of the W9.22 inserter:

    * the two node ids disappear and ``P -> asset_proposals`` becomes
      ``P -> done``, where ``P`` fed the dispatcher and ``done`` is the terminal
      the stage fed. Being the inserter's inverse, it PRESERVES every other
      edit — a custom ``P`` is restored, not canonicalised;
    * the shape must match that inverse EXACTLY — the two ids adjacent and
      immediately before the terminal, exactly four edges touching them (the
      dispatcher's incoming plain edge, its fan-out edge to the lane, its
      zero-lane route to the terminal, and the lane's edge to the terminal), and
      no other plain arm into the terminal. Anything else is REFUSED
      (:class:`ValueError`) rather than guessed at: a legal-but-non-canonical
      save — both ids plus the fan-out edge, hand-placed mid-graph — is a shape
      the inserter could not have produced, so the strip will not touch it;
    * a HALF-declared stage (one id, or the dispatcher without its fan-out edge)
      is REFUSED the same way. A refusal reaches the engine's ``_compile``,
      which falls back to the canonical topology (D74's precedent): a wrong
      graph is never booted in place of a broken one.

    The literal ids ``asset_proposals`` / ``asset_proposal_lane`` / ``done`` are
    the same convention as :func:`_upgrade_legacy_fanout` (N5).
    """
    dispatcher, lane, terminal = "asset_proposals", "asset_proposal_lane", "done"
    if dispatcher not in node_ids and lane not in node_ids:
        return node_ids, edges
    if dispatcher not in node_ids or lane not in node_ids:
        raise ValueError(
            "the retired W9.22 proposal stage is half-declared: it needs both "
            f"{dispatcher!r} and {lane!r}; compile refused so the engine "
            "falls back to the canonical topology"
        )
    index = node_ids.index(dispatcher)
    touching = [
        edge
        for edge in edges
        if dispatcher in (edge.get("from"), edge.get("to"))
        or lane in (edge.get("from"), edge.get("to"))
    ]
    plain = [
        edge
        for edge in touching
        if not edge.get("condition") and not edge.get("fanout")
    ]
    fanouts = [edge for edge in touching if edge.get("fanout")]
    # Identity, never equality: two distinct edges can carry identical data and
    # only the ones that TOUCH the stage may be dropped.
    touching_ids = {id(edge) for edge in touching}
    if not fanouts:
        raise ValueError(
            f"{dispatcher!r} is declared without its fan-out edge to {lane!r}: "
            "compile refused so the engine falls back to the canonical topology"
        )
    # The shape check: EXACTLY the four edges the W9.22 inserter wrote, with the
    # two ids adjacent immediately before the terminal. Anything else is a
    # hand-edited (or half-declared) shape the strip will not guess at.
    incoming = [edge for edge in plain if edge.get("to") == dispatcher]
    shape_ok = (
        node_ids[index + 1 : index + 3] == [lane, terminal]
        and len(touching) == 4
        and len(plain) == 3
        and len(fanouts) == 1
        and fanouts[0].get("from") == dispatcher
        and fanouts[0].get("to") == lane
        and len(incoming) == 1
        and [edge.get("to") for edge in plain if edge.get("from") == dispatcher]
        == [terminal]
        and [edge.get("to") for edge in plain if edge.get("from") == lane]
        == [terminal]
        and not [
            edge
            for edge in edges
            if edge.get("to") == terminal
            and not edge.get("condition")
            and not edge.get("fanout")
            and id(edge) not in touching_ids
        ]
    )
    if not shape_ok:
        raise ValueError(
            f"cannot strip the retired W9.22 proposal stage: {dispatcher!r} and "
            f"{lane!r} are not in the shape the W9.22 inserter produced (both "
            f"ids immediately before {terminal!r}, one plain edge in, the "
            f"{dispatcher}->{lane} fan-out, the zero-lane route and the lane's "
            f"edge to {terminal!r}, and no other plain arm into it); the strip "
            "never guesses at custom edits, so compile refused and the engine "
            "falls back to the canonical topology"
        )
    restored = incoming[0]
    stripped_edges: list[dict] = []
    for edge in edges:
        if edge is restored:
            stripped_edges.append({**edge, "to": terminal})
            continue
        if id(edge) in touching_ids:
            continue
        stripped_edges.append(edge)
    return (
        [node_id for node_id in node_ids if node_id not in (dispatcher, lane)],
        stripped_edges,
    )


def strip_legacy_topology(topology: dict) -> dict:
    """``topology`` with the retired W9.22 proposal stage removed (§3.6.4).

    The ``{nodes, edges}`` door over :func:`_strip_legacy_proposals`, for the
    engine's stored-topology read (:func:`fabulist.prompts.preset_store.
    preset_topology`): that read VALIDATES the stored shape, and an
    ``asset_proposals`` node is now an unknown id, so the strip has to happen
    BEFORE validation or the owner's other custom edits are lost to the
    wholesale canonical fallback. Node rows keep their data/template keys; a
    topology without the stage is returned unchanged (the same dict object).
    """
    node_ids, edges, _rows = _topology_parts(topology)
    stripped_ids, stripped_edges = _strip_legacy_proposals(
        list(node_ids), [dict(edge) for edge in edges]
    )
    if stripped_ids == node_ids and stripped_edges == edges:
        return topology
    keep = set(stripped_ids)
    return {
        "nodes": [dict(node) for node in topology["nodes"] if node.get("id") in keep],
        "edges": stripped_edges,
    }


def _upgrade_legacy_critic_gate(edges: list[dict]) -> list[dict]:
    """Convert a pre-W9.19 ``decision_gate -> critic`` plain edge to the branch.

    Every topology saved before the conditional critic has the unconditional
    edge, which would now run the critic on EVERY turn regardless of flags —
    the opposite of the change. Like the lane upgrade, this rewrites the
    COMPILE only (the stored record is untouched) and stays narrow: it fires
    only when the gate has the old plain edge and no conditional edges at all.
    """
    plain = [
        edge
        for edge in edges
        if edge.get("from") == "decision_gate"
        and edge.get("to") == "critic"
        and not edge.get("condition")
    ]
    if len(plain) != 1:
        return edges
    if any(
        edge.get("from") == "decision_gate" and edge.get("condition")
        for edge in edges
    ):
        return edges
    out: list[dict] = []
    for edge in edges:
        if edge is plain[0]:
            out.append(
                {"from": "decision_gate", "to": "critic", "kind": "branch", "condition": "critic"}
            )
            out.append(
                {"from": "decision_gate", "to": "planner", "kind": "branch", "condition": "planner"}
            )
            continue
        out.append(edge)
    return out


def _planner_behaviour_ids(
    node_ids: list[str],
    rows: Mapping[str, dict],
    templates: Mapping[str, Any] | None,
) -> list[str]:
    """The node ids whose BEHAVIOUR is the planner (W9.20 §10).

    A canonical ``planner`` row executes the planner; so does a template node
    whose template ``implements`` it (W9.10) — the upgrade must see that case to
    REFUSE it rather than boot a per-turn post-commit planner.
    """
    library: Mapping[str, Any] = templates or {}
    found: list[str] = []
    for node_id in node_ids:
        template_id = (rows.get(node_id) or {}).get("template")
        if isinstance(template_id, str) and template_id:
            if implements_of(library.get(template_id)) == "planner":
                found.append(node_id)
        elif node_id == "planner":
            found.append(node_id)
    return found


def _upgrade_legacy_planner(
    node_ids: list[str],
    edges: list[dict],
    rows: Mapping[str, dict],
    templates: Mapping[str, Any] | None,
) -> list[dict]:
    """REPOSITION a post-commit planner onto the W9.20 plan junction (§10).

    Every topology saved before W9.20 places the planner AFTER the commit
    (``decision_gate``/``critic`` -> ``planner`` -> ``bts``). Compiling that
    shape with the new node would run the steering model pass on EVERY turn at
    the tail — the opposite of the owner's design ([OWNER] #1) — so the upgrade
    MOVES it: the old incoming edges are retargeted to the planner's plain
    successor ``T``, ``planner -> T`` is dropped, and the junction is added
    (``pre_turn -> planner [plan]``, ``pre_turn -> npc_lanes [skip]``,
    ``planner -> npc_lanes``). Like the other upgrades it rewrites the COMPILE
    only; the stored record is untouched.

    Fires only on the canonical-old wiring: incoming ``decision_gate ->
    planner [planner]`` (original, or inserted by
    :func:`_upgrade_legacy_critic_gate`) and/or ``critic -> planner [ok|diffs]``,
    and exactly one plain outgoing edge. The already-upgraded junction is a
    no-op (idempotent). ANYTHING ELSE — a template node implementing the planner
    behaviour under another id, a planner wired somewhere hand-made, no
    ``pre_turn``/``npc_lanes`` to join — RAISES, so the engine's compile falls
    back to the canonical topology instead of running a post-commit planner
    (W9.20 §10/N3). The junction targets the literal ids ``pre_turn`` /
    ``npc_lanes``, the same convention as :func:`_upgrade_legacy_fanout` (N5):
    a topology that renamed them is refused and falls back.
    """
    planner_ids = _planner_behaviour_ids(node_ids, rows, templates)
    if not planner_ids:
        return edges
    if planner_ids != ["planner"]:
        raise ValueError(
            "planner behaviour is not on the canonical 'planner' node (found on "
            f"{', '.join(repr(node_id) for node_id in planner_ids)}); the W9.20 "
            "plan junction cannot be repositioned, compile refused"
        )

    incoming = [edge for edge in edges if edge.get("to") == "planner"]
    if not incoming:
        # Not WIRED at all: a planner row nothing routes to is inert (an
        # unreachable declaration, which validation reports). Nothing can run it
        # per turn, so there is nothing to reposition.
        return edges
    plain_out = [
        edge
        for edge in edges
        if edge.get("from") == "planner"
        and not edge.get("condition")
        and not edge.get("fanout")
    ]
    if (
        len(incoming) == 1
        and incoming[0].get("from") == "pre_turn"
        and incoming[0].get("condition") == "plan"
        and len(plain_out) == 1
        and plain_out[0].get("to") == "npc_lanes"
    ):
        return edges  # already the junction: idempotent
    canonical_old = bool(incoming) and len(plain_out) == 1 and all(
        (
            edge.get("from") == "decision_gate"
            and edge.get("condition") == "planner"
        )
        or (
            edge.get("from") == "critic"
            and edge.get("condition") in ("ok", "diffs")
        )
        for edge in incoming
    )
    if not canonical_old:
        raise ValueError(
            "planner is neither on the W9.20 plan junction nor on the pre-W9.20 "
            "post-commit wiring (decision_gate[planner] / critic[ok|diffs] -> "
            "planner -> one plain successor); compile refused so the engine "
            "falls back to the canonical topology"
        )

    for node_id in ("pre_turn", "npc_lanes"):
        if node_id not in node_ids:
            raise ValueError(
                f"the plan junction needs the canonical node {node_id!r}; the "
                "stored topology does not declare it, compile refused"
            )

    target = plain_out[0].get("to")
    out: list[dict] = []
    for edge in edges:
        if edge.get("to") == "planner":
            out.append({**edge, "to": target})
            continue
        if edge is plain_out[0] or (
            edge.get("from") == "planner" and not edge.get("condition")
        ):
            continue
        # The plain spine edge pre_turn -> npc_lanes becomes the junction's
        # ``skip`` arm (a node cannot be both a plain and a conditional target).
        if (
            edge.get("from") == "pre_turn"
            and edge.get("to") == "npc_lanes"
            and not edge.get("condition")
        ):
            continue
        out.append(edge)
    out.insert(
        0,
        {"from": "pre_turn", "to": "planner", "kind": "branch", "condition": "plan"},
    )
    out.insert(
        1,
        {"from": "pre_turn", "to": "npc_lanes", "kind": "branch", "condition": "skip"},
    )
    out.append({"from": "planner", "to": "npc_lanes"})
    return out


def _upgrade_legacy_fanout(node_ids: list[str], edges: list[dict]) -> tuple[list[str], list[dict]]:
    """Insert the W9.17 ``npc_lane`` node into a pre-W9.17 stored topology.

    The preset store holds the topology the owner last saved, and every saved
    topology from before W9.17 says ``npc_lanes -> narrate`` with a plain edge —
    the old single-node pool. Compiling that shape verbatim would SILENTLY run
    zero lanes: the dispatcher writes ``lane_plan``, no fan-out edge routes it,
    and the narrator loses its cast block. The upgrade keeps the stored
    record untouched (the editor/GET still shows what is stored) and only
    rewrites the COMPILE:

    * ``npc_lanes -> T`` (one plain edge) becomes
      ``npc_lanes -> npc_lane`` (fanout) + ``npc_lanes -> T`` (the zero-member
      route, unchanged) + ``npc_lane -> T``;
    * a topology that already carries a fan-out edge is returned unchanged;
    * anything else (no ``npc_lanes``, several plain edges out of it) is also
      unchanged — validation reports it, this never guesses.
    """
    if any(edge.get("fanout") for edge in edges):
        return node_ids, edges
    if "npc_lanes" not in node_ids:
        return node_ids, edges
    out = [edge for edge in edges if edge.get("from") == "npc_lanes" and not edge.get("condition")]
    if len(out) != 1:
        return node_ids, edges
    target = out[0].get("to")
    if not isinstance(target, str) or not target or target == "npc_lane":
        return node_ids, edges
    upgraded_nodes = list(node_ids)
    if "npc_lane" not in upgraded_nodes:
        # Before the old fan-out's successor keeps a readable node order.
        at = upgraded_nodes.index(target) if target in upgraded_nodes else len(upgraded_nodes)
        upgraded_nodes.insert(at, "npc_lane")
    upgraded_edges: list[dict] = []
    for edge in edges:
        if edge is out[0] or (edge.get("from") == "npc_lanes" and edge.get("to") == target and not edge.get("condition")):
            upgraded_edges.append({"from": "npc_lanes", "to": "npc_lane", "fanout": True})
            upgraded_edges.append({"from": "npc_lanes", "to": target})
            upgraded_edges.append({"from": "npc_lane", "to": target})
            continue
        upgraded_edges.append(edge)
    return upgraded_nodes, upgraded_edges


def _topology_parts(topology: dict) -> tuple[list[str], list[dict], dict[str, dict]]:
    """Read ``(node_ids, edges, node rows by id)`` from a topology, rejecting malformed data."""
    nodes = topology.get("nodes")
    edges = topology.get("edges")
    if not isinstance(nodes, (list, tuple)) or not nodes:
        raise ValueError("topology must carry a non-empty 'nodes' list")
    if not isinstance(edges, (list, tuple)):
        raise ValueError("topology 'edges' must be a list")
    node_ids: list[str] = []
    rows: dict[str, dict] = {}
    for node in nodes:
        node_id = node.get("id") if isinstance(node, dict) else None
        if not isinstance(node_id, str) or not node_id:
            raise ValueError(f"topology node without an id: {node!r}")
        if node_id in node_ids:
            raise ValueError(f"duplicate topology node id: {node_id!r}")
        node_ids.append(node_id)
        rows[node_id] = dict(node)
    return node_ids, [dict(edge) for edge in edges], rows


def _resolve_nodes(
    node_ids: list[str],
    rows: dict[str, dict],
    factories: dict[str, Callable],
    templates: Mapping[str, Any] | None,
) -> tuple[dict[str, Callable], dict[str, str]]:
    """``({node id: factory}, {node id: behaviour})`` for the topology's nodes.

    A plain canonical node executes its own factory. A TEMPLATE node (W9.10 —
    ``{"id": "my_speak", "template": "tpl_narrate"}``) executes the factory of
    the canonical id its template ``implements``: the node id is a LABEL the
    owner chose, the behaviour is what runs. Both failure modes name the node
    AND the template (and, for a bad ``implements``, the behaviour), because the
    owner's next move is to fix one of them.
    """
    library: Mapping[str, Any] = templates or {}
    resolved: dict[str, Callable] = {}
    behaviour: dict[str, str] = {}
    for node_id in node_ids:
        # ``_upgrade_legacy_fanout`` may INSERT a node id the stored topology
        # never declared (the pre-W9.17 lane upgrade), so a missing row is a
        # plain canonical node, not an error.
        template_id = (rows.get(node_id) or {}).get("template")
        if isinstance(template_id, str) and template_id:
            template = library.get(template_id)
            if template is None:
                raise ValueError(
                    f"unknown node template: node {node_id!r} references template "
                    f"{template_id!r}, which the node-template library does not define"
                )
            implements = implements_of(template)
            if not implements or implements not in factories:
                raise ValueError(
                    f"node template {template_id!r} (node {node_id!r}) implements "
                    f"{implements!r}, which has no node factory; known behaviours: "
                    f"{', '.join(sorted(factories))}"
                )
            resolved[node_id] = factories[implements]
            behaviour[node_id] = implements
            continue
        if node_id not in factories:
            raise ValueError(f"unknown node id: {node_id!r}")
        resolved[node_id] = factories[node_id]
        behaviour[node_id] = node_id
    return resolved, behaviour


def build_graph(
    *,
    model: ModelCaller,
    extractor: ExtractorFn,
    log: LogPort,
    classifier: ActionClassifier = classify_action,
    config_snapshot: dict | None = None,
    build_messages: Callable[[TurnState], list[dict]] = default_narrator_messages,
    checkpointer=None,
    world_state=None,
    attribution=None,
    critic=None,
    narrate_loop_capabilities=None,
    narrate_loop_timeout=None,
    narrate_loop_window=None,
    narrate_loop_return_thinking=None,
    narrate_provider_label=None,
    decision=None,
    npc_lanes=None,
    planner=None,
    plan_cadence=DEFAULT_PLAN_CADENCE,
    topology: dict | None = None,
    templates: Mapping[str, Any] | None = None,
    fanouts: Mapping[str, Any] | None = None,
):
    """Assemble and compile the turn graph from ``topology``.

    ``topology=None`` builds :data:`CANONICAL_TOPOLOGY` (byte-identical to the
    pre-W9.6 graph). A supplied topology must reference known node ids — the
    per-stage factories above — and every conditional edge's source must have a
    router; anything else raises :class:`ValueError` rather than silently
    building a different graph. A node row carrying ``template`` executes its
    template's ``implements`` behaviour (W9.10): ``templates`` is the library
    (``{template id: implements}``, or a mapping of template rows — see
    :func:`fabulist.graph.node_templates.template_behaviours`), and an unknown
    template or an ``implements`` with no factory raises naming both.

    ``plan_cadence`` is the W9.20 planner cadence the ``pre_turn`` junction
    routes on — an int or a zero-arg callable reading the ACTIVE preset's
    config, so a Studio cadence edit applies without rebuilding the graph.

    ``narrate_loop_capabilities`` / ``narrate_loop_timeout`` are the W9.24 §3.3
    narrator repair loop's seams: the narrate connection's capability map and the
    transport bound its calls ride (both as zero-arg readers when live). Without
    them the node drafts exactly as before — a flagged draft is kept, never
    re-drafted. ``narrate_loop_window`` is the third seam (W9.25 §3.5.3): the
    NARRATE slot's available prompt-token budget as a zero-arg reader, which puts
    passive ``headroom`` on the loop's rows (``None`` = no declared Context Size,
    today's row shape). ``narrate_loop_return_thinking`` is the fourth: the
    narrate connection's "Return thinking" setting as a zero-arg reader (``None``
    = AUTO), so the repair loop echoes a thinking model's ``reasoning_content``
    back when the provider returned some. ``narrate_provider_label`` is the fifth:
    the same zero-arg reader for the model a pre-commit provider abort must name,
    read at the failure so a live rebind is named correctly (ADR-023).

    ``fanouts`` is the W9.22 §3 seam for the ``Send`` fan-out stages:
    ``{dispatcher node id: fan-out object}``, where the object carries both
    halves (``route`` + ``lane``) of the fan-out. The W9.17 ``npc_lanes``
    keyword is the same seam under its historical name; whatever is not
    supplied falls back to :data:`FANOUT_STAGES`. Today the table carries the
    NPC lanes only — the W9.22 proposal stage left the turn (W9.23 §3.6) and its
    fan-out object is driven by :mod:`fabulist.state.historian` instead.

    ``checkpointer`` is any LangGraph checkpointer (e.g. the saver yielded by
    :func:`open_sqlite_checkpointer`); pass ``None`` for a cache-free run.
    """
    node_ids, edges, rows = _topology_parts(
        topology if topology is not None else CANONICAL_TOPOLOGY
    )
    # W9.23 §3.6: a topology saved while the W9.22 proposal stage existed is
    # STRIPPED at compile (the stored record is never rewritten), or refused.
    # It runs FIRST, before the other upgrades, because they read the tail: the
    # npc-lane upgrade skips any topology carrying a fan-out edge, so a save
    # that also predates W9.17 would otherwise keep a plain ``npc_lanes`` edge
    # after the strip and silently run zero lanes.
    node_ids, edges = _strip_legacy_proposals(node_ids, edges)
    # W9.17: a topology saved before the Send fan-out still means "run the cast
    # lanes"; upgrade the COMPILE (the stored record is never rewritten).
    node_ids, edges = _upgrade_legacy_fanout(node_ids, edges)
    edges = _upgrade_legacy_critic_gate(edges)
    # W9.20 §10: a pre-W9.20 planner sits post-commit — MOVE it onto the plan
    # junction (after the two upgrades above, whose output it recognises).
    edges = _upgrade_legacy_planner(node_ids, edges, rows, templates)
    factories = _node_factories(
        model=model,
        extractor=extractor,
        log=log,
        classifier=classifier,
        config_snapshot=config_snapshot,
        build_messages=build_messages,
        world_state=world_state,
        attribution=attribution,
        critic=critic,
        narrate_loop_capabilities=narrate_loop_capabilities,
        narrate_loop_timeout=narrate_loop_timeout,
        narrate_loop_window=narrate_loop_window,
        narrate_loop_return_thinking=narrate_loop_return_thinking,
        narrate_provider_label=narrate_provider_label,
        decision=decision,
        npc_lanes=npc_lanes,
        planner=planner,
        plan_cadence=plan_cadence,
        fanouts=fanouts,
    )
    # W9.10: resolve every node to the factory that executes it — a canonical
    # node its own, a template node its template's ``implements`` behaviour
    # (raising for an unknown id / template).
    resolved, behaviour = _resolve_nodes(node_ids, rows, factories, templates)

    # W9.17/W9.22: a fan-out edge (``{"from": S, "to": T, "fanout": True}``)
    # makes S a CONDITIONAL source whose router returns ``list[Send]`` or ``[]``;
    # S's ONE plain edge is the zero-lane route (``narrate`` / ``done`` in the
    # canonical topology). The fan-out's ``join`` flush is attached to the first
    # node downstream of T (see :func:`_joined`). Local name ``fanout_edges``:
    # ``fanouts`` is the caller's seam ({dispatcher: object}).
    fanout_edges: dict[str, str] = {}
    for edge in edges:
        if not edge.get("fanout"):
            continue
        source = edge.get("from")
        if source in fanout_edges:
            raise ValueError(f"duplicate fan-out edge out of {source!r}")
        fanout_edges[source] = edge.get("to")
    if fanout_edges:
        for edge in edges:
            source = edge.get("from")
            target = edge.get("to")
            if source not in fanout_edges or edge.get("fanout"):
                continue
            join = getattr(resolved.get(source), "join", None)
            if callable(join) and target in resolved:
                resolved[target] = _joined(resolved[target], join)

    builder = StateGraph(TurnState)
    for node_id in node_ids:
        builder.add_node(node_id, resolved[node_id])

    #: ``{source: {condition: target}}`` for the topology's conditional edges.
    conditional: dict[str, dict[str, str]] = {}
    #: ``{source: target}`` — a fan-out source's zero-member route.
    empty_routes: dict[str, str] = {}
    for edge in edges:
        source = edge.get("from")
        target = edge.get("to")
        if source not in resolved:
            raise ValueError(f"unknown node id: {source!r}")
        if target not in resolved:
            raise ValueError(f"unknown node id: {target!r}")
        if source in fanout_edges:
            # The fan-out owns this node's routing: its one plain edge is the
            # empty route, and a second one would be silently unreachable.
            if edge.get("fanout"):
                continue
            if source in empty_routes:
                raise ValueError(
                    f"fan-out source {source!r} has more than one plain edge; the "
                    "fan-out owns its routing (one empty route, then Sends)"
                )
            empty_routes[source] = target
            continue
        condition = edge.get("condition")
        if condition:
            routes = conditional.setdefault(source, {})
            if condition in routes:
                raise ValueError(
                    f"duplicate condition {condition!r} on node {source!r}"
                )
            routes[condition] = target
        else:
            builder.add_edge(source, target)

    for source, routes in conditional.items():
        router = _router_for(behaviour[source], resolved.get(source))
        if router is None:
            raise ValueError(f"no router for conditional edges out of {source!r}")
        builder.add_conditional_edges(source, router, routes)

    for source, target in fanout_edges.items():
        router = _router_for(behaviour[source], resolved.get(source))
        if router is None:
            raise ValueError(f"no router for the fan-out edge out of {source!r}")
        expected = getattr(resolved[source], "fanout_node", None)
        if expected is not None and expected != target:
            raise ValueError(
                f"fan-out edge {source!r} -> {target!r} disagrees with the "
                f"node's own fan-out target {expected!r}"
            )
        empty = empty_routes.get(source)
        if empty is None:
            raise ValueError(
                f"fan-out source {source!r} needs a plain edge for its zero-lane route"
            )
        builder.add_conditional_edges(
            source, _fanout_path(router, empty), [target, empty]
        )

    builder.add_edge(START, node_ids[0])
    builder.add_edge(node_ids[-1], END)
    return builder.compile(checkpointer=checkpointer)


def _fanout_path(router: Callable, empty_target: str) -> Callable:
    """Adapt a fan-out router to a conditional-edge path function.

    The router speaks ``list[Send]`` (one per member) or an empty list; the
    builder owns the topology, so it supplies the empty case's target. Both are
    awaited: a fan-out router builds the shared prompt and must stay on the
    loop thread (a sync path function runs in a worker thread).
    """

    async def path(state: TurnState):
        sends = await router(state)
        return sends if sends else empty_target

    return path


@asynccontextmanager
async def open_sqlite_checkpointer(path: str | Path) -> AsyncIterator[AsyncSqliteSaver]:
    """Async context manager yielding a SQLite checkpointer backed by ``path``.

    Construct inside a running event loop (``AsyncSqliteSaver`` binds to the
    loop). The connection is closed when the context exits.
    """
    async with AsyncSqliteSaver.from_conn_string(str(path)) as saver:
        yield saver
