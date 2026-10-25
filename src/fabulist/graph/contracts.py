"""Node contracts + topology invariants (W9.5, R23) — the graph's type system.

The turn topology is data (:data:`fabulist.graph.graph.CANONICAL_TOPOLOGY`) and
a preset may rewire it, so the editor needs a REJECTION rule, not a lock
(R23: "the editor enforces invariants like a type system: a wiring that breaks
one is REJECTED, not silently prevented"). This module is that rule.

Each canonical node declares three things in :data:`NODE_CONTRACTS`:

* ``requires`` — the ARTIFACTS the node reads (its data dependencies),
* ``provides`` — the artifacts it writes for the nodes downstream of it,
* ``klass`` — which primitive it is: ``model_pass`` (one model call),
  ``gate`` (advisory inspection emitting flags, never prose), ``deterministic``
  (code only) or ``terminal`` (writes nothing; the turn ends after it).

The table is derived from what the nodes in :mod:`fabulist.graph.nodes`
actually read and write on ``TurnState`` / the log — kept deliberately small
(granularity is per conceptual artifact, not per dict key). ``user`` is a ROOT
artifact (:data:`ROOT_ARTIFACTS`): the turn input every graph starts from, not
any node's output. ``surviving_prose`` is the critic's output — the prose AFTER
the verdict — which is what makes the causal-survivor invariant (bts must fold
the surviving turn, C2/R23) a data dependency as well as an explicit check.

:func:`validate_topology` returns violations as ``{code, message}`` dicts, plus
``node`` or ``edge`` naming what the violation is about. The codes
(:data:`VIOLATION_CODES`) are stable API: the editor renders them, so they are
added, never renamed.

Template nodes (W9.10)
----------------------
A topology node may reference a node template instead of naming a canonical
node: ``{"id": "my_speak", "template": "tpl_narrate"}``. Such a node has no
contract of its own — it IS the behaviour its template implements — so
``validate_topology`` judges it by that behaviour's ``requires``/``provides``
(and its klass when it is the terminal node). The template lookup is passed in
as ``templates`` (``{template id: implements}``, from
:func:`fabulist.graph.node_templates.template_behaviours`): a contract table is
not allowed to reach the event log, and the caller already folds it. A template
id the mapping does not carry is ``unknown_node`` naming BOTH the node and the
template — the builder would refuse it, so the door must too.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

__all__ = [
    "NODE_CONTRACTS",
    "ROOT_ARTIFACTS",
    "VIOLATION_CODES",
    "NodeContract",
    "NodeKlass",
    "implements_of",
    "validate_topology",
]

#: What a node IS, at the granularity the editor draws it (R15/R16: deterministic
#: code first, AI second; a model call may be a pass or an agent loop later).
NodeKlass = Literal["model_pass", "gate", "deterministic", "terminal"]


@dataclass(frozen=True)
class NodeContract:
    """One node's data contract + primitive class.

    ``requires`` / ``provides`` are artifact names (see the module docstring);
    they are what :func:`validate_topology` checks for the data-dependency and
    causal-survivor invariants.
    """

    klass: NodeKlass
    requires: tuple[str, ...] = ()
    provides: tuple[str, ...] = ()


#: Artifacts the turn already carries when the graph starts (the user message),
#: so a node requiring one is satisfied without an upstream producer.
ROOT_ARTIFACTS: frozenset[str] = frozenset({"user"})

#: The canonical node contracts (W9.5). Derived from the node implementations:
#: ``pre_turn`` classifies + advances the clock (facts/action_class) AND renders
#: the folded plan (the planner junction's source), ``planner`` runs the
#: steering pass pre-narration (W9.20), ``npc_lanes`` fans out one decision lane
#: per active cast member (W9.13), ``narrate`` composes prose + segments,
#: ``commit`` writes the turn row (narrative_event), ``validate`` turns segments
#: into ``validator_flags``, ``decision_gate`` turns prose into advisory
#: ``gate_flags``, ``critic`` returns a verdict and the ``surviving_prose``,
#: ``bts`` folds the surviving turn into ``bts_state``, ``env_hud`` renders the
#: HUD from that state, ``attribution`` colours the speakers deterministically
#: (no model call), ``done`` writes nothing. The W9.22 proposal stage's two rows
#: are GONE with the stage (W9.23 §3.6): those lanes run off-turn in
#: :mod:`fabulist.state.historian`, so they provide nothing the turn needs.
NODE_CONTRACTS: dict[str, NodeContract] = {
    "pre_turn": NodeContract(
        "deterministic", ("user",), ("facts", "action_class", "plan")
    ),
    # W9.20: the steering plane. ONE model pass run at the ``pre_turn`` junction
    # when the preset's cadence says it is due — it reads the turn input + the
    # facts pre_turn compiled (like the lanes) and PROVIDES ``plan``, which
    # ``pre_turn`` also provides: that dual provider is deliberate (pre_turn
    # renders the FOLDED plan every turn, §5.5). Nothing READS ``plan`` here —
    # the narrator's plan dependency is a prompt seam, not a state artifact.
    "planner": NodeContract("model_pass", ("user", "facts"), ("plan",)),
    # W9.13: the NPC fan-out. One model pass per ACTIVE cast member (bounded
    # concurrency, not a capped set). It PROVIDES ``npc_lines`` for the narrator
    # but the narrator does not REQUIRE them: a topology without the fan-out is
    # legal and produces today's single-narrator turn byte-for-byte (parity is
    # the whole point of the empty-block rule).
    "npc_lanes": NodeContract("model_pass", ("user", "facts"), ("npc_lines",)),
    # W9.17: the fan-out's per-member node. One model pass over the dispatcher's
    # member row + the shared prefix; it provides that member's lane and nothing
    # the rest of the graph REQUIRES (the narrator's bottom block is optional by
    # construction, exactly like ``npc_lanes``).
    "npc_lane": NodeContract("model_pass", ("user", "facts"), ("npc_lane",)),
    "narrate": NodeContract("model_pass", ("user", "facts"), ("prose", "segments")),
    "commit": NodeContract("deterministic", ("prose", "segments"), ("narrative_event",)),
    "validate": NodeContract("gate", ("prose", "segments"), ("validator_flags",)),
    "decision_gate": NodeContract("gate", ("prose",), ("gate_flags",)),
    "critic": NodeContract(
        "model_pass",
        ("prose", "validator_flags"),
        ("critic_verdict", "surviving_prose"),
    ),
    "bts": NodeContract("model_pass", ("surviving_prose", "user"), ("bts_state",)),
    "env_hud": NodeContract("deterministic", ("bts_state",), ("env_hud",)),
    # The attribution pass reads the narrator's prose against the session cast
    # and provides the speaker rows the colour bookkeeping consumes. It is
    # DETERMINISTIC: no model call (owner decision — the extractor-class call
    # per turn was pure cost; see :mod:`fabulist.state.attribution`), so its
    # contract class is ``deterministic`` like ``env_hud``.
    "attribution": NodeContract(
        "deterministic", ("surviving_prose",), ("attribution_rows",)
    ),
    "done": NodeContract("terminal"),
}

#: The stable violation codes, each documented where it is emitted:
#:
#: * ``unknown_node`` — a node id the engine has no stage for (or a node row
#:   without an id). The builder refuses it, so the graph would not build.
#: * ``duplicate_node`` — one id declared twice (the builder refuses it).
#: * ``duplicate_condition`` — two edges out of one node branch on the same
#:   condition (the builder refuses it: one target per branch value).
#: * ``missing_entry`` — the node the turn starts from is fed by an edge, or the
#:   topology declares no node at all.
#: * ``missing_terminal`` — the node the turn ends at is not ``terminal``.
#: * ``unsatisfied_requirement`` — a node's ``requires`` artifact is provided by
#:   no upstream node (the data-dependency invariant).
#: * ``causal_survivor_broken`` — ``bts`` is not downstream of ``critic``: it
#:   must fold the SURVIVING prose (C2/R23).
#: * ``unreachable_node`` — a declared node the entry node cannot reach.
#: * ``orphan_edge`` — an edge naming a node the topology does not declare.
#: * ``cap_violation`` — an edge carries a ``cap`` below 1 (a branch that can
#:   never run).
VIOLATION_CODES: tuple[str, ...] = (
    "unknown_node",
    "duplicate_node",
    "duplicate_condition",
    "missing_entry",
    "missing_terminal",
    "unsatisfied_requirement",
    "causal_survivor_broken",
    "unreachable_node",
    "orphan_edge",
    "cap_violation",
)


def implements_of(template: Any) -> str | None:
    """The canonical node id a node template EXECUTES, or ``None``.

    Accepts every shape a template reaches the graph layer in: the plain
    behaviour id (``"narrate"`` — how ``templates`` mappings are passed), a
    stored template mapping, or a
    :class:`~fabulist.graph.node_templates.NodeTemplate`. One spelling of the
    rule for both the validator and the builder.
    """
    if isinstance(template, str):
        return template.strip() or None
    if isinstance(template, Mapping):
        value = template.get("implements")
    else:
        value = getattr(template, "implements", None)
    return value.strip() if isinstance(value, str) and value.strip() else None


def _violation(
    code: str,
    message: str,
    *,
    node: str | None = None,
    edge: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One violation row: ``{code, message}`` + ``node`` or ``edge``."""
    violation: dict[str, Any] = {"code": code, "message": message}
    if node is not None:
        violation["node"] = node
    if edge is not None:
        violation["edge"] = edge
    return violation


def _parts(topology: Any) -> tuple[list[Any], list[Any]]:
    """Read the ``nodes`` / ``edges`` rows out of a topology, unfiltered.

    A topology is preset data and may be hand-edited, so nothing here raises:
    a missing/non-list ``nodes``/``edges`` reads as empty (which the checks then
    reject) and a malformed ROW is reported by the check that owns it rather
    than silently dropped — a dropped row would quietly build a different graph.
    """
    if not isinstance(topology, Mapping):
        return [], []
    nodes = topology.get("nodes")
    edges = topology.get("edges")
    return (
        list(nodes) if isinstance(nodes, (list, tuple)) else [],
        list(edges) if isinstance(edges, (list, tuple)) else [],
    )


def _ancestors(
    node_id: str, predecessors: Mapping[str, list[str]]
) -> set[str]:
    """Every node that can reach ``node_id`` (transitively; cycle-safe)."""
    seen: set[str] = set()
    stack = list(predecessors.get(node_id, ()))
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        stack.extend(predecessors.get(current, ()))
    return seen


def _reachable(entry: str, successors: Mapping[str, list[str]]) -> set[str]:
    """Every node reachable from ``entry`` (transitively; cycle-safe)."""
    seen = {entry}
    stack = [entry]
    while stack:
        for target in successors.get(stack.pop(), ()):
            if target not in seen:
                seen.add(target)
                stack.append(target)
    return seen


def validate_topology(
    topology: Any, templates: Mapping[str, Any] | None = None
) -> list[dict[str, Any]]:
    """Every invariant violation in ``topology``; ``[]`` means it is legal.

    The checks are the ones the graph builder would otherwise crash on
    (unknown/duplicate nodes, a graph with no entry or no terminal) plus the
    W9.5 invariant classes (R23): each node's ``requires`` must be provided by
    an upstream node, and ``bts`` must be downstream of ``critic``. Anything
    reported here is REJECTED by the write door
    (:mod:`fabulist.api.topology_api`); the engine itself falls back to the
    canonical topology instead of building a broken graph
    (:func:`fabulist.prompts.preset_store.preset_topology`).

    ``templates`` (``{template id: implements}``, or a mapping of template
    mappings) is the node-template library; a node row naming one is judged by
    that behaviour's contract (see the module docstring).
    """
    nodes, edges = _parts(topology)
    library: Mapping[str, Any] = templates or {}
    violations: list[dict[str, Any]] = []

    # 1. Node identity: only a declared, unique, contracted behaviour is
    #    executable — either a canonical id, or a template's ``implements``.
    node_ids: list[str] = []
    #: ``{node id: the canonical node id whose contract it runs}``.
    behaviour: dict[str, str] = {}
    for node in nodes:
        node_id = node.get("id") if isinstance(node, Mapping) else None
        if not isinstance(node_id, str) or not node_id:
            violations.append(
                _violation("unknown_node", f"node without an id: {node!r}")
            )
            continue
        if node_id in node_ids:
            violations.append(
                _violation(
                    "duplicate_node", f"node {node_id!r} is declared twice", node=node_id
                )
            )
            continue
        if node_id in NODE_CONTRACTS:
            behaviour[node_id] = node_id
            node_ids.append(node_id)
            continue
        template_id = node.get("template") if isinstance(node, Mapping) else None
        if isinstance(template_id, str) and template_id:
            implements = implements_of(library.get(template_id))
            if implements is None:
                violations.append(
                    _violation(
                        "unknown_node",
                        f"node {node_id!r} runs node template {template_id!r}, which "
                        "the node-template library does not define",
                        node=node_id,
                    )
                )
                continue
            if implements not in NODE_CONTRACTS:
                violations.append(
                    _violation(
                        "unknown_node",
                        f"node {node_id!r} runs node template {template_id!r}, which "
                        f"implements {implements!r} — no such node contract "
                        f"(the graph can run {', '.join(NODE_CONTRACTS)})",
                        node=node_id,
                    )
                )
                continue
            behaviour[node_id] = implements
            node_ids.append(node_id)
            continue
        violations.append(
            _violation(
                "unknown_node",
                f"node {node_id!r} has no contract and no node template; the graph "
                f"can run {', '.join(NODE_CONTRACTS)}",
                node=node_id,
            )
        )

    if not node_ids:
        violations.append(
            _violation("missing_entry", "topology declares no node to start the turn from")
        )
        violations.append(
            _violation("missing_terminal", "topology declares no node to end the turn at")
        )
        return violations

    # 2. Edges: every endpoint is a declared node (else nothing could run it),
    #    and a branch cap below 1 is a branch that can never run.
    successors: dict[str, list[str]] = {node_id: [] for node_id in node_ids}
    predecessors: dict[str, list[str]] = {node_id: [] for node_id in node_ids}
    conditions: dict[tuple[Any, Any], int] = {}
    for row in edges:
        if not isinstance(row, Mapping):
            violations.append(
                _violation(
                    "orphan_edge",
                    f"edge row is not an object: {row!r}",
                )
            )
            continue
        edge = dict(row)
        cap = edge.get("cap")
        if cap is not None and (not isinstance(cap, int) or isinstance(cap, bool) or cap < 1):
            violations.append(
                _violation(
                    "cap_violation",
                    f"edge {edge.get('from')!r} -> {edge.get('to')!r} carries cap "
                    f"{cap!r}; a branch that may run must have cap >= 1",
                    edge=edge,
                )
            )
        # A branch value routes to ONE target, and a fan-out edge (W9.17) is not a
        # branch value: it repeats as many times as there are members.
        condition = edge.get("condition")
        if condition:
            seen = conditions.setdefault((edge.get("from"), condition), 0) + 1
            conditions[(edge.get("from"), condition)] = seen
            if seen > 1:
                violations.append(
                    _violation(
                        "duplicate_condition",
                        f"node {edge.get('from')!r} has more than one edge for "
                        f"condition {condition!r}; one branch value, one target",
                        edge=edge,
                    )
                )
        source = edge.get("from")
        target = edge.get("to")
        if source not in node_ids or target not in node_ids:
            violations.append(
                _violation(
                    "orphan_edge",
                    f"edge {source!r} -> {target!r} names a node the topology does "
                    "not declare",
                    edge=edge,
                )
            )
            continue
        successors[source].append(target)
        predecessors[target].append(source)

    # 3. The builder starts at the first declared node and ends at the last
    #    (``graph.build_graph``): those two positions carry the shape.
    entry = node_ids[0]
    if predecessors[entry]:
        violations.append(
            _violation(
                "missing_entry",
                f"entry node {entry!r} is fed by {predecessors[entry][0]!r}; the turn "
                "must start at a node nothing routes into",
                node=entry,
            )
        )
    final = node_ids[-1]
    final_klass = NODE_CONTRACTS[behaviour[final]].klass
    if final_klass != "terminal":
        violations.append(
            _violation(
                "missing_terminal",
                f"the graph ends at {final!r} ({final_klass}); the turn's END must "
                "follow a terminal node",
                node=final,
            )
        )

    # 4. Causal-survivor invariant (R23/C2): bts folds the SURVIVING prose, so a
    #    critic verdict must precede it. Expressed separately from the data
    #    dependency below because it is a deliberate ordering constraint, not an
    #    artifact lookup.
    ancestors = {node_id: _ancestors(node_id, predecessors) for node_id in node_ids}
    for node_id in node_ids:
        if behaviour[node_id] != "bts":
            continue
        upstream = {behaviour[upstream_id] for upstream_id in ancestors[node_id]}
        if "critic" in upstream:
            continue
        violations.append(
            _violation(
                "causal_survivor_broken",
                "bts must run downstream of critic: it folds the SURVIVING prose, so "
                "the verdict has to precede it (causal-survivor invariant)",
                node=node_id,
            )
        )

    # 5. Data-dependency invariant: every artifact a node reads is provided by
    #    an upstream node (or is a root artifact / the node's own output).
    for node_id in node_ids:
        contract = NODE_CONTRACTS[behaviour[node_id]]
        available = set(ROOT_ARTIFACTS)
        for upstream in ancestors[node_id] | {node_id}:
            available.update(NODE_CONTRACTS[behaviour[upstream]].provides)
        for artifact in contract.requires:
            if artifact not in available:
                violations.append(
                    _violation(
                        "unsatisfied_requirement",
                        f"{node_id!r} requires {artifact!r}, which no upstream node "
                        "provides (data-dependency invariant)",
                        node=node_id,
                    )
                )

    # 6. Every declared node must be executable from the entry.
    reached = _reachable(entry, successors)
    for node_id in node_ids:
        if node_id not in reached:
            violations.append(
                _violation(
                    "unreachable_node",
                    f"{node_id!r} cannot be reached from the entry node {entry!r}",
                    node=node_id,
                )
            )

    return violations
