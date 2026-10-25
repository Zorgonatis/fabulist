<script lang="ts">
	// The reusable flow renderer (W9.2) — plus the EDIT MODE the Studio's graph
	// home drives (W9.5, R19/R21/R23).
	//
	// Read-only (`editing = false`) it is exactly what it always was: a
	// `FlowManifest` plus a caller-derived `ActivationMap`, drawn on a
	// deterministic layered layout. Zero chat coupling — the Studio home (R11)
	// and the chat panel (W9.3) mount the same component.
	//
	// Edit mode (`editing = true`) draws the EXECUTABLE topology instead: the
	// caller passes `topology` and receives every wiring change back through
	// `onchange` (the caller owns save/revert). The component only knows how to
	// re-attach an edge end (click an edge → drag one of its two handles onto a
	// node) and how to remove a node or an edge; node CREATION lives in the
	// caller's palette (R19). All model edits go through `lib/flow/editor.ts`.
	//
	// Layout is component-owned and deterministic (orchestrator decision 3):
	// layers are computed from the edge topology (longest-path over flow/branch
	// edges) at fixed node dimensions, so there are no hand-authored coordinates
	// to drift. `loop_back` edges are excluded from layering (they are back
	// edges) and drawn in a gutter on the flow's UPSTREAM side (the left in
	// vertical, the top in horizontal) so the regenerate cycle reads as a loop.
	//
	// ORIENTATION is automatic (owner decision, lane B): the turn reads left→right,
	// but the same board also lives in panes that are taller than they are wide (a
	// docked Studio pane, a narrow viewport). So the component measures the pane it
	// is laid out IN with a `ResizeObserver` and draws horizontal while that pane is
	// at least as wide as it is tall, vertical otherwise — no manual switch, no prop
	// for the hosts to set. Horizontal is the pre-measurement default (SSR and the
	// first paint; nothing reads `window` at render time). The measured box is the
	// nearest scrolling ANCESTOR, not this component: its own height is
	// content-driven, so measuring it would feed the chosen layout back into the
	// choice.

	import FlowNode from './FlowNode.svelte';
	import type { FlowLane } from '#lib/api/flow';
	import { edgeId as topologyEdgeId, moveEdge, removeEdge, removeNode } from './editor';
	import type {
		ActivationMap,
		ActivationState,
		FlowEdge,
		FlowGraph,
		FlowManifest,
		Topology,
		TopologyEdge,
		TopologyNode
	} from './types';

	let {
		manifest = null,
		activation = {},
		debug = false,
		selectedId = null,
		onselect,
		onopenstep,
		editing = false,
		topology = null,
		onchange,
		lanes = [],
		laneWidth = 4,
		lanesEnabled = true,
		inFlight = false
	}: {
		/** The descriptive flow (read-only mode). Not needed while editing. */
		manifest?: FlowManifest | null;
		/** The last turn's ACTUAL lane set (W9.8): one arm per member, no stubs. */
		lanes?: FlowLane[];
		/** Pool width from the fanout descriptor (label only). */
		laneWidth?: number;
		/** Issue #56.3: false when the topology switches the stage off — the
		 *  narrator runs one forward pass and no lane arm will ever land. */
		lanesEnabled?: boolean;
		/** Issue #56.1: the turn's lanes are planned and still in flight. */
		inFlight?: boolean;
		/** Per-node trail, keyed by node id (derive with `activationFromSteps`). */
		activation?: ActivationMap;
		/** Session debug toggle — surfaces capture availability on nodes. */
		debug?: boolean;
		selectedId?: string | null;
		onselect?: (id: string) => void;
		/** Prompt-door callback: carries the W6 step id, opens nothing. */
		onopenstep?: (stepId: string) => void;
		/** Edit mode: draw `topology` instead of `manifest`. */
		editing?: boolean;
		/** The executable topology under edit (ignored when `editing = false`). */
		topology?: Topology | null;
		/** Emitted with the WHOLE next topology after any wiring edit. */
		onchange?: (topology: Topology) => void;
	} = $props();

	// --- deterministic layered layout (decision 3) --------------------------
	//
	// ONE layout, TWO orientations. Geometry is computed on the FLOW axis (the
	// layer axis — the direction edges travel) and the CROSS axis (in-layer
	// stacking), then mapped onto the plane by `ax`/`ay` in `layoutGraph`.
	// Vertical is exactly the pre-lane-B shape (flow = y, cross = x), so the old
	// board did not move; horizontal swaps the two axes (flow = x, cross = y).
	const NODE_W = 208;
	const NODE_H = 88;
	/** Gap ALONG the flow axis, between layers — the edge labels live in it. It is
	 *  the SAME gap in both orientations (not swapped with `CROSS_GAP`), so a layer
	 *  gap is always wide enough for a label whichever way the flow reads. */
	const FLOW_GAP = 54;
	/** Gap ACROSS the flow axis, between the nodes stacked inside one layer. */
	const CROSS_GAP = 34;
	/** Cross-axis lead-in: the loop-back gutter rides this (top / left) side. */
	const CROSS_PAD_IN = 58;
	/** Flow-axis padding at the entry and the exit of the board. */
	const FLOW_PAD_IN = 16;
	const FLOW_PAD_OUT = 10;
	const CROSS_PAD_OUT = 16;
	const LOOP_GUTTER = 30;
	const EXTERNAL_STUB = 18;

	/** How the flow reads: `horizontal` = left→right, `vertical` = top→bottom. */
	type Orientation = 'horizontal' | 'vertical';

	interface Position {
		x: number;
		y: number;
	}
	interface EdgeView {
		id: string;
		from: string;
		to: string;
		d: string;
		tone: string;
		dashed: boolean;
		label: string | null;
		labelX: number;
		labelY: number;
	}
	interface Board {
		graph: FlowGraph;
		edges: EdgeView[];
		positions: Map<string, Position>;
		width: number;
		height: number;
	}
	/** The geometry of one graph, independent of how nodes are drawn. */
	interface Layout {
		edges: EdgeView[];
		positions: Map<string, Position>;
		width: number;
		height: number;
	}
	/** The edit-mode board: node ids + kinds and the executable wiring. */
	interface EditBoard extends Layout {
		nodes: TopologyNode[];
	}

	/** Colour a tone as a theme variable consumed by inline stroke/fill. */
	function toneColor(tone: string): string {
		switch (tone) {
			case 'ok':
				return 'var(--ok)';
			case 'diffs':
				return 'var(--warn)';
			case 'fail':
				return 'var(--danger-text)';
			case 'loop':
				return 'var(--accent-strong)';
			case 'attach':
				return 'var(--text-dim)';
			default:
				return 'var(--text-muted)';
		}
	}

	function branchTone(label: string | null): string {
		if (label === 'ok' || label === 'diffs' || label === 'fail') return label;
		return 'flow';
	}

	function edgeLabel(edge: FlowEdge): string | null {
		const cap = edge.cap ? ` ×${edge.cap}` : '';
		if (edge.label) return `${edge.label}${cap}`;
		if (edge.cap) return `cap ${edge.cap}`;
		return null;
	}

	/**
	 * Assign every node a layer (longest path over forward edges) and a slot inside
	 * that layer, then draw every edge on those coordinates — in `orientation`.
	 *
	 * The layering itself is orientation-free; only the mapping from (flow, cross)
	 * to the plane changes. Everything that used to assume "downstream = down" is
	 * now expressed on the flow axis: an edge leaves the source's DOWNSTREAM face
	 * and enters the target's UPSTREAM face (right / left in horizontal), the
	 * parallel-branch spread moves across the flow, external attach stubs point
	 * downstream, and the `loop_back` arc curls into the cross-axis upstream gutter
	 * (top in horizontal, left in vertical).
	 */
	function layoutGraph(
		nodeIds: string[],
		graphEdges: FlowEdge[],
		orientation: Orientation
	): Layout {
		const horizontal = orientation === 'horizontal';
		/** A node's extent along the flow axis / across it. */
		const nodeFlow = horizontal ? NODE_W : NODE_H;
		const nodeCross = horizontal ? NODE_H : NODE_W;
		/** The plane x / y coordinate of a (flow, cross) point. */
		const ax = (flow: number, cross: number): number => (horizontal ? flow : cross);
		const ay = (flow: number, cross: number): number => (horizontal ? cross : flow);
		/** The same point as an SVG `d` fragment. */
		const pt = (flow: number, cross: number): string => `${ax(flow, cross)} ${ay(flow, cross)}`;

		const ids = new Set(nodeIds);
		const originalIndex = new Map(nodeIds.map((id, i) => [id, i]));
		const forward = graphEdges.filter(
			(e) => (e.kind === 'flow' || e.kind === 'branch') && ids.has(e.from) && ids.has(e.to)
		);

		const outAdj = new Map<string, string[]>();
		const indeg = new Map<string, number>();
		for (const id of nodeIds) {
			outAdj.set(id, []);
			indeg.set(id, 0);
		}
		for (const e of forward) {
			outAdj.get(e.from)?.push(e.to);
			indeg.set(e.to, (indeg.get(e.to) ?? 0) + 1);
		}

		const layer = new Map<string, number>();
		for (const id of nodeIds) layer.set(id, 0);
		const pending = new Map(indeg);
		const queue = nodeIds.filter((id) => indeg.get(id) === 0);
		const seen = new Set<string>();
		while (queue.length) {
			const id = queue.shift();
			if (id === undefined || seen.has(id)) continue;
			seen.add(id);
			for (const to of outAdj.get(id) ?? []) {
				layer.set(to, Math.max(layer.get(to) ?? 0, (layer.get(id) ?? 0) + 1));
				pending.set(to, (pending.get(to) ?? 0) - 1);
				if (pending.get(to) === 0) queue.push(to);
			}
		}
		// Cycle leftovers (none in wave 1): stack one layer past the deepest.
		let deepest = 0;
		for (const id of seen) deepest = Math.max(deepest, layer.get(id) ?? 0);
		for (const id of nodeIds) {
			if (!seen.has(id)) {
				deepest += 1;
				layer.set(id, deepest);
			}
		}

		const byLayer = new Map<number, string[]>();
		for (const id of nodeIds) {
			const l = layer.get(id) ?? 0;
			const bucket = byLayer.get(l) ?? [];
			bucket.push(id);
			byLayer.set(l, bucket);
		}

		// Place on the (flow, cross) axes first; the plane mapping happens last.
		const placed = new Map<string, { flow: number; cross: number }>();
		let crossCount = 1;
		for (const [l, bucket] of byLayer) {
			bucket.sort((a, b) => (originalIndex.get(a) ?? 0) - (originalIndex.get(b) ?? 0));
			crossCount = Math.max(crossCount, bucket.length);
			bucket.forEach((id, slot) => {
				placed.set(id, {
					flow: FLOW_PAD_IN + l * (nodeFlow + FLOW_GAP),
					cross: CROSS_PAD_IN + slot * (nodeCross + CROSS_GAP)
				});
			});
		}

		const positions = new Map<string, Position>();
		for (const [id, at] of placed) {
			positions.set(id, { x: ax(at.flow, at.cross), y: ay(at.flow, at.cross) });
		}

		const layerCount = deepest + 1;
		const flowExtent =
			FLOW_PAD_IN + layerCount * nodeFlow + (layerCount - 1) * FLOW_GAP + FLOW_PAD_OUT;
		const crossExtent =
			CROSS_PAD_IN + crossCount * nodeCross + (crossCount - 1) * CROSS_GAP + CROSS_PAD_OUT;
		// The board's width and height swap with the orientation.
		const width = horizontal ? flowExtent : crossExtent;
		const height = horizontal ? crossExtent : flowExtent;

		const edges: EdgeView[] = [];

		// Fan edges that share a source AND target (critic's ok/diffs branches)
		// so both labels stay readable instead of stacking on one another.
		const parallel = new Map<string, string[]>();
		for (const e of graphEdges) {
			if (e.kind !== 'flow' && e.kind !== 'branch') continue;
			if (!placed.get(e.from) || !placed.get(e.to)) continue;
			const key = `${e.from}->${e.to}`;
			parallel.set(key, [...(parallel.get(key) ?? []), e.id]);
		}

		for (const e of graphEdges) {
			const from = placed.get(e.from);
			if (!from) continue;

			// Attach edge to a node in another graph: a dashed stub off the
			// source's DOWNSTREAM face (right in horizontal, bottom in vertical).
			const to = placed.get(e.to);
			if (!to) {
				const face = from.flow + nodeFlow;
				const mid = from.cross + nodeCross / 2;
				edges.push({
					id: e.id,
					from: e.from,
					to: e.to,
					d: `M ${pt(face, mid)} L ${pt(face + EXTERNAL_STUB, mid)}`,
					tone: 'attach',
					dashed: true,
					label: e.label ? `${e.label} → ${e.to}` : `→ ${e.to}`,
					labelX: ax(face + EXTERNAL_STUB + 10, mid),
					labelY: ay(face + EXTERNAL_STUB + 10, mid)
				});
				continue;
			}

			if (e.kind === 'loop_back') {
				// Back edge: a closed arc into the cross-axis gutter above/left of the
				// board, so the regenerate cycle reads as a loop instead of a step that
				// points backwards.
				const sFlow = from.flow + nodeFlow / 2;
				const tFlow = to.flow + nodeFlow / 2;
				const gutter = to.cross - LOOP_GUTTER;
				edges.push({
					id: e.id,
					from: e.from,
					to: e.to,
					d: `M ${pt(sFlow, from.cross)} C ${pt(sFlow, from.cross - LOOP_GUTTER)}, ${pt(tFlow, gutter)}, ${pt(tFlow, to.cross)}`,
					tone: 'loop',
					dashed: true,
					label: edgeLabel(e),
					labelX: ax((sFlow + tFlow) / 2, gutter),
					labelY: ay((sFlow + tFlow) / 2, gutter)
				});
				continue;
			}

			// Downstream face -> upstream face, curving along the flow axis.
			const face = from.flow + nodeFlow;
			const entry = to.flow;
			const start = from.cross + nodeCross / 2;
			const end = to.cross + nodeCross / 2;
			const siblings = parallel.get(`${e.from}->${e.to}`) ?? [];
			const idx = siblings.indexOf(e.id);
			const spread = (idx - (siblings.length - 1) / 2) * 28;
			const ctrl = Math.max(16, Math.min(40, (entry - face) / 2));
			const fs = start + spread;
			const ts = end + spread;
			const midFlow = (face + entry) / 2;
			edges.push({
				id: e.id,
				from: e.from,
				to: e.to,
				d: `M ${pt(face, fs)} C ${pt(face + ctrl, fs)}, ${pt(entry - ctrl, ts)}, ${pt(entry, ts)}`,
				tone: e.kind === 'branch' ? branchTone(e.label) : 'flow',
				dashed: false,
				label: edgeLabel(e),
				labelX: ax(midFlow, (fs + ts) / 2),
				labelY: ay(midFlow, (fs + ts) / 2)
			});
		}

		return { edges, positions, width, height };
	}

	// --- orientation: horizontal by default, vertical in a tall pane ---------
	//
	// `root` is this component's own element. The box that DECIDES is the nearest
	// scrolling ancestor around it (the host pane the owner sees), because this
	// component's height is content-driven — measuring it would feed the layout it
	// just picked back into the choice. Nothing here runs during SSR: the observer
	// only ever lives inside an effect, so the pre-measurement render is the plain
	// horizontal layout.
	let root = $state<HTMLElement | null>(null);
	/** The measured host box; 0 × 0 until the first client-side measurement. */
	let hostWidth = $state(0);
	let hostHeight = $state(0);

	/** A pane at least as wide as it is tall reads as a left→right pipeline. */
	const orientation: Orientation = $derived(hostHeight > hostWidth ? 'vertical' : 'horizontal');

	/** The box the board is laid out IN: the nearest scroll pane, else the parent. */
	function hostBox(element: HTMLElement): HTMLElement | null {
		let node: HTMLElement | null = element.parentElement;
		while (node) {
			const style = getComputedStyle(node);
			if (style.overflowX !== 'visible' || style.overflowY !== 'visible') return node;
			node = node.parentElement;
		}
		return element.parentElement;
	}

	$effect(() => {
		const element = root;
		if (!element) return;
		const host = hostBox(element);
		if (!host) return;
		const measure = (): void => {
			hostWidth = host.clientWidth;
			hostHeight = host.clientHeight;
		};
		measure();
		const observer = new ResizeObserver(measure);
		observer.observe(host);
		return () => observer.disconnect();
	});

	/**
	 * W9.8: inject one arm per ACTUAL lane member into the graph that declares a
	 * `fanout`, so the only real parallelism is drawn as a fan (owner: "unless
	 * you can draw the actual members don't bother"). No lanes -> no arms: the
	 * Studio (no live turn) shows the plain node.
	 */
	function withLaneArms(graph: FlowGraph): FlowGraph {
		const fan = (graph.nodes ?? []).find((node) => node.fanout);
		if (!fan || !fan.fanout) return graph;
		// W9.17: the manifest now carries the real per-member node (`npc_lane` —
		// the `Send` target). It is a TEMPLATE for N invocations, not a station,
		// so the read view must never draw it as a stub beside the actual arms
		// (owner: "unless you can draw the actual members don't bother"). Edit
		// mode keeps it: that board is where the real topology is authored.
		const stubs = new Set<string>((fan.fanout.nodes ?? []).map((id) => String(id)));
		const strip = (source: FlowGraph): FlowGraph => ({
			...source,
			nodes: (source.nodes ?? []).filter((node) => !stubs.has(node.id)),
			edges: (source.edges ?? []).filter(
				(edge) => !stubs.has(edge.from) && !stubs.has(edge.to)
			)
		});
		const base = strip(graph);
		if (lanes.length === 0) return base;
		const merge = String(fan.fanout.merge ?? '');
		const arms = lanes.map((lane, index) => {
			const key = String(lane.member_id || lane.name || index);
			const intent = (lane.intent ?? '').trim();
			// Issue #56.1: a PLANNED member has no answer yet — say so, instead of
			// drawing an empty intent as if the lane had come back silent. A plan
			// left behind by a turn that ended early (abort) reads as abandoned
			// rather than forever "running".
			const detail = lane.pending
				? inFlight
					? 'in flight — no answer yet'
					: 'planned — the turn ended before this lane answered'
				: lane.ok
					? intent
					: `lane failed: ${intent || 'no output'}`;
			return {
				id: `lane_arm_${key}`,
				title: lane.name ?? key,
				kind: 'trigger' as const,
				runs: { kind: 'model_pass' as const, interaction: 'npc', call_site: null,
					description: detail },
				agent: null,
				prompt_surface: { door: 'none' as const, reason: 'NPC lane (per member)' },
				inspector: { kind: 'model_pass' as const, agent_filter: 'npc', events: [] },
				activation: [],
				composition: null,
				fanout: null,
				meta: {
					description: `lane ${index + 1} of ${lanes.length} · pool ${laneWidth}${
						lane.pending && inFlight ? ' · running' : ''
					}`,
					notes: []
				},
				arm: true
			};
		});
		return {
			...base,
			nodes: [...(base.nodes ?? []), ...arms],
			edges: [
				...(base.edges ?? []).filter((edge) => !(edge.from === fan.id && edge.to === merge)),
				...arms.flatMap((arm): FlowEdge[] => [
					{ id: `${fan.id}->${arm.id}`, from: fan.id, to: arm.id, kind: 'flow', condition: null, label: null, cap: null },
					{ id: `${arm.id}->${merge}`, from: arm.id, to: merge, kind: 'flow', condition: null, label: null, cap: null }
				])
			]
		};
	}

	function placeGraph(graph: FlowGraph): Board {
		return {
			graph,
			...layoutGraph((graph.nodes ?? []).map((n) => n.id), graph.edges ?? [], orientation)
		};
	}

	const boards = $derived(
		editing ? [] : (manifest?.graphs ?? []).map((graph) => placeGraph(withLaneArms(graph)))
	);
	const TONES = ['flow', 'ok', 'diffs', 'fail', 'loop', 'attach'];

	function stateOf(id: string): ActivationState {
		return activation[id] ?? 'pending';
	}

	// --- edit mode (W9.5, R19/R21/R23) --------------------------------------
	//
	// Selection is an EDGE (not a node): the two editable things in a rewiring
	// UI are an edge's ends. A selected edge shows one handle per end; dragging a
	// handle onto a node re-attaches that end through `editor.moveEdge`.

	let selectedEdgeId = $state<string | null>(null);
	let dragEnd = $state<'from' | 'to' | null>(null);
	let dropTarget = $state<string | null>(null);

	/** The topology edge under edit, resolved through its derived id. */
	const selectedEdge = $derived(
		editing && topology && selectedEdgeId
			? (topology.edges.find((edge) => topologyEdgeId(edge) === selectedEdgeId) ?? null)
			: null
	);

	/** A topology edge as the layout/render vocabulary spells it (condition = label). */
	function toFlowEdge(edge: TopologyEdge): FlowEdge {
		return {
			id: topologyEdgeId(edge),
			from: edge.from,
			to: edge.to,
			kind: edge.kind,
			condition: edge.condition,
			label: edge.condition,
			cap: edge.cap
		};
	}

	/**
	 * The edit board. Duplicate node ids / duplicate edge identities are dropped
	 * here: they are validator findings (`editor.validateLocal`) and a keyed
	 * `{#each}` would otherwise throw instead of reporting them.
	 */
	const editBoard = $derived.by((): EditBoard | null => {
		if (!editing || !topology) return null;
		const nodes: TopologyNode[] = [];
		const seenNodes = new Set<string>();
		for (const node of topology.nodes) {
			if (seenNodes.has(node.id)) continue;
			seenNodes.add(node.id);
			nodes.push(node);
		}
		const edges: FlowEdge[] = [];
		const seenEdges = new Set<string>();
		for (const edge of topology.edges) {
			const view = toFlowEdge(edge);
			if (seenEdges.has(view.id)) continue;
			seenEdges.add(view.id);
			edges.push(view);
		}
		return { nodes, ...layoutGraph(nodes.map((node) => node.id), edges, orientation) };
	});

	function reattach(id: string, end: 'from' | 'to', nodeId: string): void {
		if (!topology) return;
		const edge = topology.edges.find((candidate) => topologyEdgeId(candidate) === id);
		if (!edge) return;
		const ends =
			end === 'from' ? { from: nodeId, to: edge.to } : { from: edge.from, to: nodeId };
		const next = moveEdge(topology, id, ends);
		if (next === topology) return; // self-edge, unknown node, or an existing edge's identity
		selectedEdgeId = topologyEdgeId({ ...edge, from: ends.from, to: ends.to });
		onchange?.(next);
	}

	function removeSelectedEdge(): void {
		if (!topology || !selectedEdgeId) return;
		const next = removeEdge(topology, selectedEdgeId);
		if (next === topology) return;
		selectedEdgeId = null;
		onchange?.(next);
	}

	function removeNodeById(id: string): void {
		if (!topology) return;
		const next = removeNode(topology, id);
		if (next === topology) return;
		// Removing a node takes its edges with it — a selection on one is gone.
		if (!next.edges.some((edge) => topologyEdgeId(edge) === selectedEdgeId)) selectedEdgeId = null;
		onchange?.(next);
	}

	function onHandleDragStart(event: DragEvent, end: 'from' | 'to'): void {
		if (!selectedEdgeId) return;
		dragEnd = end;
		if (event.dataTransfer) {
			event.dataTransfer.effectAllowed = 'move';
			event.dataTransfer.setData('text/plain', selectedEdgeId);
		}
	}

	function onHandleDragEnd(): void {
		dragEnd = null;
		dropTarget = null;
	}

	function onCellDragOver(event: DragEvent, id: string): void {
		if (!dragEnd) return;
		event.preventDefault(); // a preventDefault'ed dragover is what makes a cell a drop target
		if (event.dataTransfer) event.dataTransfer.dropEffect = 'move';
		dropTarget = id;
	}

	function onCellDragLeave(id: string): void {
		if (dropTarget === id) dropTarget = null;
	}

	function onCellDrop(event: DragEvent, id: string): void {
		const end = dragEnd;
		const id_ = selectedEdgeId;
		dragEnd = null;
		dropTarget = null;
		if (!end || !id_) return;
		event.preventDefault();
		reattach(id_, end, id);
	}
</script>

<!-- `data-flow-orientation` is the axis contract a host may read (the chat
     panel centres its auto-scroll on the axis the board actually extends along). -->
<div
	class="flow"
	class:horizontal={orientation === 'horizontal'}
	data-flow-orientation={orientation}
	bind:this={root}
	style="--node-w:{NODE_W}px; --node-h:{NODE_H}px;"
>
	{#if editing}
		<section class="graph" aria-label="Turn order">
			<header class="graph-head">
				<h3 class="graph-title">Turn order</h3>
				<span class="graph-kind">executed</span>
				<span class="graph-attach">the steps the engine runs, in order</span>
			</header>

			{#if editBoard}
				<div class="edit-bar">
					{#if selectedEdge}
						<span class="edit-edge">
							{selectedEdge.from} → {selectedEdge.to}{selectedEdge.condition
								? ` · ${selectedEdge.condition}`
								: ''}{selectedEdge.cap ? ` ×${selectedEdge.cap}` : ''}
						</span>
						<button type="button" class="edit-action" onclick={removeSelectedEdge}>
							Remove edge
						</button>
					{:else}
						<span class="edit-hint">
							Click an edge to select it, then drag its <em>from</em> / <em>to</em> handle onto another
							node to re-attach that end.
						</span>
					{/if}
				</div>

				<div class="board" style="width:{editBoard.width}px; height:{editBoard.height}px;">
					<svg class="edges" width={editBoard.width} height={editBoard.height} aria-hidden="true">
						<defs>
							{#each TONES as tone (tone)}
								<marker
									id={`topology-arrow-${tone}`}
									markerWidth="9"
									markerHeight="9"
									refX="7.5"
									refY="4.5"
									orient="auto"
								>
									<path class="arrow" style="fill:{toneColor(tone)}" d="M0,0 L9,4.5 L0,9 z" />
								</marker>
							{/each}
						</defs>
						{#each editBoard.edges as edge (edge.id)}
							<path
								class="edge"
								class:dashed={edge.dashed}
								class:selected={selectedEdgeId === edge.id}
								style="stroke:{toneColor(edge.tone)}"
								d={edge.d}
								marker-end={edge.tone === 'attach'
									? undefined
									: `url(#topology-arrow-${edge.tone})`}
							/>
						{/each}
					</svg>

					{#each editBoard.edges as edge (edge.id)}
						<button
							type="button"
							class="edge-pick"
							class:active={selectedEdgeId === edge.id}
							style="left:{edge.labelX}px; top:{edge.labelY}px; color:{toneColor(
								edge.tone
							)}; border-color:{toneColor(edge.tone)};"
							title={`Select edge ${edge.from} → ${edge.to}`}
							onclick={() => (selectedEdgeId = edge.id)}
						>
							{edge.label ?? `${edge.from} → ${edge.to}`}
						</button>
					{/each}

					{#each editBoard.nodes as node (node.id)}
						<div
							class="cell edit-cell"
							class:drop-target={dropTarget === node.id}
							role="group"
							aria-label={`node ${node.id}`}
							style="left:{editBoard.positions.get(node.id)?.x ?? 0}px; top:{editBoard.positions.get(
								node.id
							)?.y ?? 0}px;"
							ondragover={(event) => onCellDragOver(event, node.id)}
							ondragleave={() => onCellDragLeave(node.id)}
							ondrop={(event) => onCellDrop(event, node.id)}
						>
							<div
								class="edit-node"
								class:edge-end={selectedEdge?.from === node.id || selectedEdge?.to === node.id}
							>
								<span class="edit-node-id">{node.id}</span>
								<span class="edit-kind" data-kind={node.kind}>{node.kind}</span>
							</div>
							<button
								type="button"
								class="node-remove"
								title={`Remove node ${node.id}`}
								aria-label={`Remove node ${node.id}`}
								onclick={() => removeNodeById(node.id)}
							>
								×
							</button>
							{#if selectedEdge?.from === node.id}
								<button
									type="button"
									class="handle handle-from"
									draggable="true"
									title="Drag onto a node to re-attach this edge's source"
									ondragstart={(event) => onHandleDragStart(event, 'from')}
									ondragend={onHandleDragEnd}
								>
									from ⤳
								</button>
							{/if}
							{#if selectedEdge?.to === node.id}
								<button
									type="button"
									class="handle handle-to"
									draggable="true"
									title="Drag onto a node to re-attach this edge's target"
									ondragstart={(event) => onHandleDragStart(event, 'to')}
									ondragend={onHandleDragEnd}
								>
									⤳ to
								</button>
							{/if}
						</div>
					{/each}
				</div>
			{:else}
				<p class="empty">Nothing to show yet.</p>
			{/if}
		</section>
	{/if}

	{#each boards as board (board.graph.id)}
		<section class="graph" aria-label={board.graph.title}>
			<header class="graph-head">
				<h3 class="graph-title">{board.graph.title}</h3>
				<span class="graph-kind">{board.graph.kind === 'turn' ? 'turn' : 'off-turn'}</span>
				{#if board.graph.attached_to}
					<span class="graph-attach">attaches to {board.graph.attached_to}</span>
				{/if}
			</header>

			<div class="board" style="width:{board.width}px; height:{board.height}px;">
				<svg class="edges" width={board.width} height={board.height} aria-hidden="true">
					<defs>
						{#each TONES as tone (tone)}
							<marker
								id={`${board.graph.id}-arrow-${tone}`}
								markerWidth="9"
								markerHeight="9"
								refX="7.5"
								refY="4.5"
								orient="auto"
							>
								<path class="arrow" style="fill:{toneColor(tone)}" d="M0,0 L9,4.5 L0,9 z" />
							</marker>
						{/each}
					</defs>
					{#each board.edges as edge (edge.id)}
						<path
							class="edge"
							class:dashed={edge.dashed}
							style="stroke:{toneColor(edge.tone)}"
							d={edge.d}
							marker-end={edge.tone === 'attach'
								? undefined
								: `url(#${board.graph.id}-arrow-${edge.tone})`}
						/>
					{/each}
				</svg>

				{#each board.edges as edge (edge.id)}
					{#if edge.label}
						<span
							class="edge-label"
							style="left:{edge.labelX}px; top:{edge.labelY}px; color:{toneColor(edge.tone)}; border-color:{toneColor(edge.tone)};"
						>
							{edge.label}
						</span>
					{/if}
				{/each}

				{#each board.graph.nodes as node (node.id)}
					<div
						class="cell"
						style="left:{board.positions.get(node.id)?.x ?? 0}px; top:{board.positions.get(node.id)?.y ?? 0}px;"
					>
						<FlowNode
							{node}
							state={stateOf(node.id)}
							selected={selectedId === node.id}
							{debug}
							{onselect}
							{onopenstep}
							arm={Boolean((node as { arm?: boolean }).arm)}
							laneNote={node.fanout && !(node as { arm?: boolean }).arm
								? !lanesEnabled
									? 'lanes off — the narrator writes this scene alone'
									: lanes.length > 0
										? `${lanes.length} lane${lanes.length === 1 ? '' : 's'} · ${laneWidth} at once${
												inFlight ? ' · in flight' : ''
											}`
										: 'no lanes yet — they appear on the next turn'
								: ''}
						/>
					</div>
				{/each}
			</div>
		</section>
	{:else}
		{#if !editing}
			<p class="empty">No flows to show.</p>
		{/if}
	{/each}
</div>

<style>
	.flow {
		display: flex;
		flex-direction: column;
		gap: 1rem;
	}
	.graph {
		display: flex;
		flex-direction: column;
		gap: 0.4rem;
	}
	.graph-head {
		display: flex;
		align-items: baseline;
		gap: 0.5rem;
	}
	.graph-title {
		margin: 0;
		font-size: var(--font-ui);
		font-weight: 600;
	}
	.graph-kind {
		padding: 0.05rem 0.35rem;
		border-radius: 4px;
		font-size: var(--font-micro);
		text-transform: uppercase;
		letter-spacing: 0.03em;
		background: var(--bg-inset);
		color: var(--text-muted);
	}
	.graph-attach {
		font-size: var(--font-micro);
		color: var(--text-dim);
	}
	.board {
		position: relative;
		border: 1px solid var(--line);
		border-radius: 8px;
		background: var(--bg-inset);
	}
	.edges {
		position: absolute;
		inset: 0;
		pointer-events: none;
	}
	.edge {
		fill: none;
		stroke-width: 1.6;
	}
	.edge.dashed {
		stroke-dasharray: 5 4;
	}
	.edge.selected {
		stroke-width: 3.4;
	}
	.arrow {
		stroke: none;
	}
	.edge-label {
		position: absolute;
		transform: translate(-50%, -50%);
		padding: 0.02rem 0.32rem;
		border: 1px solid;
		border-radius: 999px;
		background: var(--bg-inset);
		font-size: var(--font-micro);
		line-height: 1.4;
		white-space: nowrap;
	}
	.edge-pick {
		position: absolute;
		transform: translate(-50%, -50%);
		max-width: 208px;
		overflow: hidden;
		text-overflow: ellipsis;
		padding: 0.02rem 0.34rem;
		border: 1px solid;
		border-radius: 999px;
		background: var(--bg-raised);
		font: inherit;
		font-size: var(--font-micro);
		line-height: 1.4;
		white-space: nowrap;
		cursor: pointer;
	}
	.edge-pick:hover {
		background: var(--bg-hover);
	}
	.edge-pick.active {
		box-shadow: 0 0 0 2px var(--accent-soft);
		background: var(--bg-hover);
	}
	.edit-bar {
		display: flex;
		align-items: center;
		gap: 0.4rem;
		min-height: 1.4rem;
	}
	.edit-edge {
		padding: 0.05rem 0.4rem;
		border: 1px solid var(--accent-line);
		border-radius: 5px;
		font-size: var(--font-micro);
		color: var(--accent-strong);
		white-space: nowrap;
	}
	.edit-action {
		padding: 0.1rem 0.45rem;
		border: 1px solid var(--line);
		border-radius: 5px;
		background: transparent;
		color: var(--text-muted);
		font: inherit;
		font-size: var(--font-micro);
		cursor: pointer;
	}
	.edit-action:hover {
		border-color: var(--danger-text);
		color: var(--danger-text);
	}
	.edit-hint {
		font-size: var(--font-micro);
		color: var(--text-dim);
	}
	.edit-cell {
		border-radius: 7px;
	}
	.edit-cell.drop-target {
		outline: 2px dashed var(--accent-strong);
		outline-offset: 3px;
	}
	.edit-node {
		display: flex;
		align-items: baseline;
		justify-content: space-between;
		gap: 0.4rem;
		height: 100%;
		padding: 0.4rem 0.5rem;
		box-sizing: border-box;
		border: 1px solid var(--line);
		border-radius: 7px;
		background: var(--bg-raised);
	}
	.edit-node.edge-end {
		border-color: var(--accent);
	}
	.edit-node-id {
		font-weight: 600;
		font-size: var(--font-small);
		line-height: 1.15;
		overflow: hidden;
		text-overflow: ellipsis;
	}
	.edit-kind {
		flex: 0 0 auto;
		padding: 0.05rem 0.35rem;
		border-radius: 4px;
		font-size: var(--font-micro);
		text-transform: uppercase;
		letter-spacing: 0.03em;
		background: var(--bg-inset);
		color: var(--text-muted);
	}
	.edit-kind[data-kind='gate'] {
		background: var(--accent-soft);
		color: var(--accent-strong);
	}
	.edit-kind[data-kind='terminal'] {
		border: 1px solid var(--line);
	}
	.node-remove {
		position: absolute;
		top: -8px;
		right: -8px;
		width: 18px;
		height: 18px;
		padding: 0;
		border: 1px solid var(--line);
		border-radius: 50%;
		background: var(--bg-raised);
		color: var(--text-muted);
		font: inherit;
		font-size: var(--font-micro);
		line-height: 1;
		cursor: pointer;
	}
	.node-remove:hover {
		border-color: var(--danger-text);
		color: var(--danger-text);
	}
	.handle {
		position: absolute;
		left: 50%;
		transform: translateX(-50%);
		padding: 0.02rem 0.4rem;
		border: 1px solid var(--accent-strong);
		border-radius: 999px;
		background: var(--accent-soft);
		color: var(--accent-strong);
		font: inherit;
		font-size: var(--font-micro);
		line-height: 1.5;
		cursor: grab;
		white-space: nowrap;
		z-index: 2;
	}
	.handle-from {
		bottom: -10px;
	}
	.handle-to {
		top: -10px;
	}
	/* Horizontal: an edge leaves its source's RIGHT face and enters its target's
	   LEFT face, so each re-attach handle rides the face it will redraw. */
	.flow.horizontal .handle-from {
		top: 50%;
		bottom: auto;
		left: 100%;
		transform: translate(-50%, -50%);
	}
	.flow.horizontal .handle-to {
		top: 50%;
		left: 0;
		transform: translate(-50%, -50%);
	}
	.handle:active {
		cursor: grabbing;
	}
	.cell {
		position: absolute;
		width: var(--node-w);
		height: var(--node-h);
	}
	.empty {
		margin: 0;
		font-size: var(--font-small);
		color: var(--text-dim);
	}
</style>
