// Pure topology editing (W9.5, R19/R21/R23) — the graph view's model layer.
//
// Every helper takes a `Topology` (the engine's executable wire shape, see
// `types.ts`) and returns a NEW one; the input is never mutated, so a pane can
// hold a draft next to the last-saved snapshot and diff the two. There are no
// imports beyond the type mirror, no Svelte, no api client: the whole file is
// readable and testable by inspection.
//
// A REJECTED edit returns the input reference unchanged (`next === topology`).
// That is the one signal callers need: "nothing to emit" versus "re-wire this".
// Rejections are the local mirrors of the engine's own refusals — an unknown
// node id, a self-edge, a duplicate edge identity.

import type {
	FlowNodeKind,
	Topology,
	TopologyEdge,
	TopologyNode,
	TopologyViolation
} from './types';

/** `Node.kind` vocabulary the topology accepts. */
export const TOPOLOGY_NODE_KINDS: readonly FlowNodeKind[] = [
	'stage',
	'gate',
	'branch',
	'trigger',
	'terminal'
];

/**
 * The editor's stable edge identity. The wire shape carries no edge id (the
 * engine keys edges by their endpoints), so identity is derived from content —
 * stable across reloads, and changed exactly when the wiring changes.
 */
export function edgeId(edge: TopologyEdge): string {
	const condition = edge.condition ? `:${edge.condition}` : '';
	return `${edge.from}->${edge.to}#${edge.kind}${condition}`;
}

/** A deep copy — the draft must not alias the snapshot it is compared against. */
export function cloneTopology(topology: Topology): Topology {
	return {
		nodes: topology.nodes.map((node) => ({ ...node })),
		edges: topology.edges.map((edge) => ({ ...edge }))
	};
}

export function hasNode(topology: Topology, id: string): boolean {
	return topology.nodes.some((node) => node.id === id);
}

/** Add one node (R19). An empty or already-declared id is refused.
 *
 * With `canonicalOrder` (the ids in canonical order) a canonical id is inserted
 * at its canonical position instead of appended: the engine reads the FIRST
 * declared node as the entry and the LAST as the terminal, so list order is
 * part of the wiring — an appended `pre_turn` would land after `done`.
 */
export function addNode(
	topology: Topology,
	node: TopologyNode,
	canonicalOrder?: readonly string[]
): Topology {
	const id = node.id.trim();
	if (!id || hasNode(topology, id)) return topology;
	const added: TopologyNode = { id, kind: node.kind };
	// W9.10: a template reference (and the node data it seeds) is part of the
	// row — dropping it here would turn a runnable template node into an unknown
	// id the engine refuses.
	if (node.template) added.template = node.template;
	if (node.data) added.data = { ...node.data };
	const rank = canonicalOrder ? canonicalOrder.indexOf(id) : -1;
	if (!canonicalOrder || rank < 0) {
		return { nodes: [...topology.nodes, added], edges: topology.edges };
	}
	const nodes = [...topology.nodes];
	const at = nodes.findIndex((existing) => canonicalOrder.indexOf(existing.id) > rank);
	nodes.splice(at < 0 ? nodes.length : at, 0, added);
	return { nodes, edges: topology.edges };
}

/** Remove a node and every edge that touched it. */
export function removeNode(topology: Topology, id: string): Topology {
	if (!hasNode(topology, id)) return topology;
	return {
		nodes: topology.nodes.filter((node) => node.id !== id),
		edges: topology.edges.filter((edge) => edge.from !== id && edge.to !== id)
	};
}

/** Add one edge. Unknown endpoints, self-edges and duplicate edges are refused. */
export function addEdge(topology: Topology, edge: TopologyEdge): Topology {
	if (!hasNode(topology, edge.from) || !hasNode(topology, edge.to)) return topology;
	if (edge.from === edge.to) return topology;
	if (topology.edges.some((existing) => edgeId(existing) === edgeId(edge))) return topology;
	return { nodes: topology.nodes, edges: [...topology.edges, edge] };
}

/** Remove the edge with this derived id. */
export function removeEdge(topology: Topology, id: string): Topology {
	const edges = topology.edges.filter((edge) => edgeId(edge) !== id);
	if (edges.length === topology.edges.length) return topology;
	return { nodes: topology.nodes, edges };
}

/**
 * Re-attach one edge's endpoints. `ends` is the complete new wiring, so a drag
 * that moves only the source still carries the target it keeps. The edge keeps
 * its kind/condition/cap (only the wiring changes).
 */
export function moveEdge(
	topology: Topology,
	id: string,
	ends: { from: string; to: string }
): Topology {
	const index = topology.edges.findIndex((edge) => edgeId(edge) === id);
	if (index < 0) return topology;
	const edge = topology.edges[index];
	if (!hasNode(topology, ends.from) || !hasNode(topology, ends.to)) return topology;
	if (ends.from === ends.to) return topology;
	if (ends.from === edge.from && ends.to === edge.to) return topology;
	const moved: TopologyEdge = { ...edge, from: ends.from, to: ends.to };
	// Re-wiring must not alias another edge: identity is (from, to, kind, condition).
	const collides = topology.edges.some(
		(existing, at) => at !== index && edgeId(existing) === edgeId(moved)
	);
	if (collides) return topology;
	const edges = [...topology.edges];
	edges[index] = moved;
	return { nodes: topology.nodes, edges };
}

/**
 * The cheap client mirror of the engine's validator (`graph/contracts.py`
 * `validate_topology`), so the UI can disable Save before a round trip.
 *
 * Mirrored codes (the engine's own strings):
 *
 *  · `duplicate_node`           — the same id declared twice
 *  · `unknown_node`             — a node id the engine has no stage contract for
 *                                 (only checkable when `known` ids are given)
 *  · `orphan_edge`              — an edge endpoint that is not a declared node
 *  · `cap_violation`            — a branch `cap` below 1 (it can never run)
 *  · `missing_entry`            — the first declared node is fed by an edge
 *  · `missing_terminal`         — the last declared node is not `terminal`
 *  · `causal_survivor_broken`   — `bts` is not downstream of `critic` (C2/R23)
 *  · `unreachable_node`         — a declared node the entry cannot reach
 *
 * NOT mirrored: `unsatisfied_requirement` — the data-dependency invariant is
 * checked against the engine's `NodeContract` artifact table, which the wire
 * shape does not carry. The server stays authoritative: its violations replace
 * these after a refused save, and it is the only judge of a legal graph.
 *
 * `known` is the canonical node set (the `canonical` topology's ids). A node
 * carrying a `template` (W9.10) is legal as long as that template resolves to
 * one of them: `behaviours` maps `{template id: implements}` (the palette
 * library), so the editor can add a template node with an owner-chosen id and
 * still know the engine will build it. An unknown id AND no template is what
 * the engine refuses.
 */
export function validateLocal(
	topology: Topology,
	known?: readonly string[],
	behaviours?: Record<string, string>
): TopologyViolation[] {
	const violations: TopologyViolation[] = [];

	// 1. Node identity (declared, unique, and — when known — contracted).
	const declared: string[] = [];
	for (const node of topology.nodes) {
		if (!node.id) {
			violations.push({ code: 'unknown_node', message: 'a node row has no id' });
			continue;
		}
		if (declared.includes(node.id)) {
			violations.push({
				code: 'duplicate_node',
				node: node.id,
				message: `node "${node.id}" is declared twice`
			});
			continue;
		}
		if (known && !known.includes(node.id)) {
			// A template node is legal when its template resolves to a known
			// behaviour (W9.10); without a template it is an unknown stage.
			const implementsId = node.template ? behaviours?.[node.template] : undefined;
			if (!implementsId || !known.includes(implementsId)) {
				violations.push({
					code: 'unknown_node',
					node: node.id,
					message: node.template
						? `node "${node.id}" runs template "${node.template}", which no node template in the library resolves to a runnable behaviour`
						: `node "${node.id}" has no engine contract and no template; the graph can run ${known.join(
								', '
							)}`
				});
				continue;
			}
		}
		declared.push(node.id);
	}

	if (declared.length === 0) {
		violations.push({
			code: 'missing_entry',
			message: 'the topology declares no node to start the turn from'
		});
		violations.push({
			code: 'missing_terminal',
			message: 'the topology declares no node to end the turn at'
		});
		return violations;
	}

	// 2. Edges: endpoints, and a cap that could never run.
	const successors = new Map<string, string[]>(declared.map((id) => [id, []]));
	const predecessors = new Map<string, string[]>(declared.map((id) => [id, []]));
	for (const edge of topology.edges) {
		if (edge.cap != null && (!Number.isInteger(edge.cap) || edge.cap < 1)) {
			violations.push({
				code: 'cap_violation',
				edge: edgeId(edge),
				message: `edge ${edge.from} → ${edge.to} carries cap ${edge.cap}; a branch may not have cap < 1`
			});
		}
		if (!declared.includes(edge.from) || !declared.includes(edge.to)) {
			violations.push({
				code: 'orphan_edge',
				edge: edgeId(edge),
				message: `edge ${edge.from} → ${edge.to} names a node the topology does not declare`
			});
			continue;
		}
		successors.get(edge.from)?.push(edge.to);
		predecessors.get(edge.to)?.push(edge.from);
	}

	// 3. The engine starts at the first declared node and ends at the last.
	const entry = declared[0];
	const fed = predecessors.get(entry) ?? [];
	if (fed.length > 0) {
		violations.push({
			code: 'missing_entry',
			node: entry,
			message: `entry node "${entry}" is fed by "${fed[0]}"; the turn must start at a node nothing routes into`
		});
	}
	const last = declared[declared.length - 1];
	const lastKind = topology.nodes.find((node) => node.id === last)?.kind;
	if (lastKind !== 'terminal') {
		violations.push({
			code: 'missing_terminal',
			node: last,
			message: `the graph ends at "${last}" (${lastKind ?? 'unknown'}); the last declared node must be kind "terminal"`
		});
	}

	// 4. Causal-survivor invariant (C2/R23): bts folds the SURVIVING prose.
	if (declared.includes('bts') && !ancestors('bts', predecessors).includes('critic')) {
		violations.push({
			code: 'causal_survivor_broken',
			node: 'bts',
			message:
				'bts must run downstream of critic: it folds the SURVIVING prose, so the verdict has to precede it'
		});
	}

	// 5. Every declared node must be executable from the entry.
	const reached = new Set<string>([entry]);
	const stack = [entry];
	while (stack.length) {
		const id = stack.pop() as string;
		for (const next of successors.get(id) ?? []) {
			if (reached.has(next)) continue;
			reached.add(next);
			stack.push(next);
		}
	}
	for (const id of declared) {
		if (!reached.has(id)) {
			violations.push({
				code: 'unreachable_node',
				node: id,
				message: `"${id}" cannot be reached from the entry node "${entry}"`
			});
		}
	}

	return violations;
}

/** Every node that can reach `id` (transitively; cycle-safe). */
function ancestors(id: string, predecessors: Map<string, string[]>): string[] {
	const seen = new Set<string>();
	const stack = [...(predecessors.get(id) ?? [])];
	while (stack.length) {
		const current = stack.pop() as string;
		if (seen.has(current)) continue;
		seen.add(current);
		stack.push(...(predecessors.get(current) ?? []));
	}
	return [...seen];
}
